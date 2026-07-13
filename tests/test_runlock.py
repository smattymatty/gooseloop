"""run.lock behaviour: one run at a time per loop root (ADR 0010,
PROTOCOL section 13).

Unit tests exercise RunLock directly; integration tests drive
begin_loop with the same patched seams as test_branch_policy_and_looper
(preparation + goose invocation) so no goose binary is needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gooseloop import (
    RUN_LOCK_FILENAME,
    Engine,
    Environment,
    GooseLooper,
    LooperConfig,
    Phase,
    Pipeline,
    RunLockHeldError,
)
from gooseloop.runlock import RunLock


# ---- unit: RunLock ---------------------------------------------------


def test_acquire_writes_the_contract_fields(tmp_path):
    lock = RunLock(tmp_path)
    lock.acquire(engine="engines.doc_drift", session_id=None)
    data = json.loads((tmp_path / RUN_LOCK_FILENAME).read_text())
    assert data == {
        "pid": os.getpid(),
        "started": data["started"],  # presence + shape checked below
        "engine": "engines.doc_drift",
        "session_id": None,
    }
    assert data["started"].endswith("+00:00")  # ISO 8601, UTC


def test_release_removes_the_lock(tmp_path):
    lock = RunLock(tmp_path)
    lock.acquire(engine="e", session_id=None)
    lock.release()
    assert not (tmp_path / RUN_LOCK_FILENAME).exists()


def test_release_is_quiet_if_the_file_is_already_gone(tmp_path):
    lock = RunLock(tmp_path)
    lock.acquire(engine="e", session_id=None)
    (tmp_path / RUN_LOCK_FILENAME).unlink()
    lock.release()  # must not raise


def test_live_holder_refuses_second_acquire(tmp_path):
    """Our own pid is definitionally alive, so a second acquire must
    refuse — and the error message must name engine, pid, and start."""
    first = RunLock(tmp_path)
    first.acquire(engine="engines.doc_drift", session_id="2026-07-13T14-02-11")
    second = RunLock(tmp_path)
    with pytest.raises(RunLockHeldError, match="engines.doc_drift") as exc:
        second.acquire(engine="other", session_id=None)
    assert str(os.getpid()) in str(exc.value)
    # The loser must not have damaged the winner's lock.
    assert json.loads((tmp_path / RUN_LOCK_FILENAME).read_text())["pid"] == os.getpid()


def _spawn_and_reap() -> int:
    """A pid that provably belonged to a real process and is now dead."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_dead_pid_is_reclaimed_with_a_warning(tmp_path, capsys):
    (tmp_path / RUN_LOCK_FILENAME).write_text(json.dumps({
        "pid": _spawn_and_reap(),
        "started": "2026-07-13T00:00:00+00:00",
        "engine": "engines.crashed",
        "session_id": "2026-07-13T00-00-00",
    }))
    lock = RunLock(tmp_path)
    lock.acquire(engine="engines.next", session_id=None)
    err = capsys.readouterr().err
    assert "stale" in err and "engines.crashed" in err
    assert json.loads((tmp_path / RUN_LOCK_FILENAME).read_text())["engine"] == "engines.next"


def test_corrupt_lock_is_reclaimed_not_fatal(tmp_path, capsys):
    """A crash mid-write leaves garbage: no pid to probe, but also no
    evidence of a live run. Reclaim loudly rather than refuse forever."""
    (tmp_path / RUN_LOCK_FILENAME).write_text("{not json")
    lock = RunLock(tmp_path)
    lock.acquire(engine="engines.next", session_id=None)
    assert "corrupt" in capsys.readouterr().err
    assert json.loads((tmp_path / RUN_LOCK_FILENAME).read_text())["engine"] == "engines.next"


def test_lock_with_non_int_pid_counts_as_stale(tmp_path):
    (tmp_path / RUN_LOCK_FILENAME).write_text(json.dumps({"pid": "what"}))
    lock = RunLock(tmp_path)
    lock.acquire(engine="engines.next", session_id=None)  # must not raise


def test_annotate_records_the_session_id_in_place(tmp_path):
    lock = RunLock(tmp_path)
    lock.acquire(engine="e", session_id=None)
    lock.annotate(session_id="2026-07-13T14-02-11")
    data = json.loads((tmp_path / RUN_LOCK_FILENAME).read_text())
    assert data["session_id"] == "2026-07-13T14-02-11"
    assert data["pid"] == os.getpid()


def test_concurrent_acquires_exactly_one_wins(tmp_path):
    """All contenders share this process's (live) pid, so every loser
    must refuse — the O_EXCL window can never admit two winners."""
    def attempt(i: int) -> bool:
        lock = RunLock(tmp_path)
        try:
            lock.acquire(engine=f"engines.contender{i}", session_id=None)
            return True
        except RunLockHeldError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results.count(True) == 1


# ---- integration: begin_loop -----------------------------------------


REVIEW_OUTPUT = (
    "<<<DELIVERABLE_JSON>>>\n"
    + json.dumps({
        "protocol_version": "1.0",
        "status": "done",
        "summary": "nothing to do",
        "insights": [],
        "routing": [],
        "operator_actions": [],
    })
    + "\n<<<END_DELIVERABLE>>>\n"
)


class _TinyEnv(Environment):
    def env_vars(self) -> dict[str, str]:
        return {}


class _LockPeekingEngine(Engine):
    """Records what run.lock said while the pass was in flight —
    precheck runs after lock acquisition and session creation, so it
    sees the annotated lock."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.peeked: dict | None = None

    @property
    def name(self) -> str:
        return "tiny"

    def precheck(self, ctx) -> None:
        if self.lock_path.exists():
            self.peeked = json.loads(self.lock_path.read_text())

    def pipeline(self, ctx) -> Pipeline:
        return Pipeline(review=Phase(name="review", recipe_path="review.yaml"))


@contextlib.contextmanager
def _unprepared(recipe_path, extra_env=None, **kwargs):
    yield str(recipe_path)


@pytest.fixture
def patched_goose(monkeypatch):
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _unprepared)
    monkeypatch.setattr(
        "gooseloop.looper.run_goose_with_retry",
        lambda *a, **k: REVIEW_OUTPUT,
    )


def _looper(tmp_path: Path, engine: Engine, **kwargs) -> GooseLooper:
    config = LooperConfig.load(anchor=tmp_path, warn_on_missing=False)
    return GooseLooper(
        engine=engine, environment=_TinyEnv(), config=config,
        review_only=True, **kwargs,
    )


def test_begin_loop_holds_lock_during_pass_and_releases_after(tmp_path, patched_goose):
    lock_path = tmp_path / RUN_LOCK_FILENAME
    engine = _LockPeekingEngine(lock_path)
    _looper(tmp_path, engine, save=False).begin_loop()
    assert engine.peeked is not None, "lock was not held while the pass ran"
    assert engine.peeked["pid"] == os.getpid()
    assert engine.peeked["engine"] == type(engine).__module__
    assert not lock_path.exists(), "lock survived the pass"


def test_begin_loop_annotates_the_session_id(tmp_path, patched_goose):
    engine = _LockPeekingEngine(tmp_path / RUN_LOCK_FILENAME)
    result = _looper(tmp_path, engine, save=True).begin_loop()
    assert engine.peeked["session_id"] == result["session_dir"].name


def test_no_save_run_still_locks_with_null_session_id(tmp_path, patched_goose):
    engine = _LockPeekingEngine(tmp_path / RUN_LOCK_FILENAME)
    _looper(tmp_path, engine, save=False).begin_loop()
    assert engine.peeked["session_id"] is None


def test_begin_loop_releases_the_lock_when_the_pass_crashes(tmp_path, patched_goose):
    class _CrashingEngine(_LockPeekingEngine):
        def precheck(self, ctx) -> None:
            raise RuntimeError("boom")

    engine = _CrashingEngine(tmp_path / RUN_LOCK_FILENAME)
    with pytest.raises(RuntimeError, match="boom"):
        _looper(tmp_path, engine, save=False).begin_loop()
    assert not (tmp_path / RUN_LOCK_FILENAME).exists()


def test_begin_loop_refuses_while_a_live_run_holds_the_lock(tmp_path, patched_goose):
    RunLock(tmp_path).acquire(engine="engines.other", session_id=None)
    engine = _LockPeekingEngine(tmp_path / RUN_LOCK_FILENAME)
    with pytest.raises(RunLockHeldError, match="engines.other"):
        _looper(tmp_path, engine, save=True).begin_loop()
    # Refusal happens before any work: no session folder was created.
    assert not (tmp_path / "reviews").exists()


def test_session_meta_records_the_engine_module(tmp_path, patched_goose):
    engine = _LockPeekingEngine(tmp_path / RUN_LOCK_FILENAME)
    result = _looper(tmp_path, engine, save=True).begin_loop()
    meta = json.loads((result["session_dir"] / "session.meta.json").read_text())
    assert meta["engine"] == "tiny"
    assert meta["engine_module"] == type(engine).__module__
