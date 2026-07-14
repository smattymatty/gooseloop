"""git-recap v2 (the journal engine): watermarks, ISO-week arithmetic,
skip_when seatbelts, amend semantics, and the post-write-only watermark
advance.

Real git repos in tmp_path (cheap and honest — the watermark logic IS git
plumbing); no goose calls anywhere.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from engines.git_recap import GitRecapEngine, GitRecapEnvironment
from engines.git_recap.engine import _closed_week, _iso_week_id, _today, _week_dates
from gooseloop.phase import Context


# ---- helpers -----------------------------------------------------


def _ctx(env: GitRecapEnvironment | None) -> Context:
    return Context(model="m", session_dir=None, base_env={}, environment=env)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for k, v in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
    return path


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", f"add {name}"], check=True)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _env(tmp_path: Path, repos: list[Path]) -> GitRecapEnvironment:
    return GitRecapEnvironment(
        repos=repos,
        author="test@example.com",
        journal_dir=tmp_path / "journal",
        state_path=tmp_path / "git-recap.state.json",
        first_run_days=7,
    )


# ---- date arithmetic ------------------------------------------------


def test_iso_week_id_shape():
    assert _iso_week_id(date(2026, 7, 13)) == "2026-W29"


def test_closed_week_is_previous_iso_week():
    # Mon Jul 13 2026 is in W29; the closed week is W28.
    assert _closed_week(date(2026, 7, 13)) == "2026-W28"
    # Sunday still belongs to W29 — closed stays W28 all week long.
    assert _closed_week(date(2026, 7, 19)) == "2026-W28"


def test_week_dates_monday_to_sunday():
    days = _week_dates("2026-W28")
    assert days[0] == "2026-07-06"
    assert days[-1] == "2026-07-12"
    assert len(days) == 7


# ---- watermarks ------------------------------------------------------


def test_first_run_falls_back_to_window(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    sha = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    assert env.fresh_commit_shas(repo) == [sha]


def test_watermark_bounds_fresh_commits(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    old = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    env.seen_heads = {str(repo): old}
    env.advance_watermarks()

    assert env.fresh_commit_shas(repo) == []
    new = _commit(repo, "b.txt")
    assert env.fresh_commit_shas(repo) == [new]


def test_gap_days_are_still_covered(tmp_path):
    """Skip three days: everything after the watermark is fresh, no
    matter how old — the whole point of watermark over window."""
    repo = _make_repo(tmp_path / "r1")
    mark = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    env.seen_heads = {str(repo): mark}
    env.advance_watermarks()
    shas = [_commit(repo, f"f{i}.txt") for i in range(3)]
    assert set(env.fresh_commit_shas(repo)) == set(shas)


def test_stale_watermark_sha_falls_back_to_window(tmp_path):
    """A rewritten history (rebase) can orphan the watermark sha; the env
    must fall back to the first-run window, never crash or return zero."""
    repo = _make_repo(tmp_path / "r1")
    sha = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    env.seen_heads = {str(repo): "0" * 40}
    env.advance_watermarks()
    assert env.fresh_commit_shas(repo) == [sha]


def test_fresh_commits_captures_seen_heads(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    head = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    env.fresh_commits()
    assert env.seen_heads == {str(repo): head}


# ---- seatbelts --------------------------------------------------------


def test_daily_skips_wrong_date(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    _commit(repo, "a.txt")
    engine = GitRecapEngine(env=_env(tmp_path, [repo]))
    reason = engine._skip_daily({"date": "1999-01-01"})
    assert isinstance(reason, str) and "1999-01-01" in reason


def test_daily_skips_when_no_fresh_commits(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    mark = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    env.seen_heads = {str(repo): mark}
    env.advance_watermarks()
    engine = GitRecapEngine(env=env)
    reason = engine._skip_daily({"date": _today()})
    assert isinstance(reason, str) and "nothing to journal" in reason


def test_daily_runs_for_today_with_fresh_commits(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    _commit(repo, "a.txt")
    engine = GitRecapEngine(env=_env(tmp_path, [repo]))
    assert engine._skip_daily({"date": _today()}) is False


def test_daily_amend_is_not_skipped(tmp_path):
    """Today's entry existing does NOT skip the daily when fresh commits
    exist — that's the amend run (grill decision)."""
    repo = _make_repo(tmp_path / "r1")
    _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_today()).write_text("# existing entry\n")
    engine = GitRecapEngine(env=env)
    assert engine._skip_daily({"date": _today()}) is False


def test_weekly_skips_wrong_week_and_existing_and_empty(tmp_path):
    env = _env(tmp_path, [])
    engine = GitRecapEngine(env=env)
    closed = _closed_week()

    wrong = engine._skip_weekly({"week": "1999-W01"})
    assert isinstance(wrong, str) and "1999-W01" in wrong

    # No dailies in the closed week -> nothing to review.
    empty = engine._skip_weekly({"week": closed})
    assert isinstance(empty, str) and "no dailies" in empty

    # Dailies exist but the weekly is already written -> written once.
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_week_dates(closed)[0]).write_text("# a day\n")
    env.weekly_dir.mkdir(parents=True)
    env.weekly_path(closed).write_text("# done\n")
    exists = engine._skip_weekly({"week": closed})
    assert isinstance(exists, str) and "already reviewed" in exists


def test_weekly_due_when_closed_week_has_dailies(tmp_path):
    env = _env(tmp_path, [])
    engine = GitRecapEngine(env=env)
    closed = _closed_week()
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_week_dates(closed)[2]).write_text("# a day\n")
    assert engine._skip_weekly({"week": closed}) is False


# ---- watermark advance is gated on the write --------------------------


def _run_summary_post(engine: GitRecapEngine, env: GitRecapEnvironment) -> Context:
    ctx = _ctx(env)
    pipeline = engine.pipeline(ctx)
    assert pipeline.summary is not None and pipeline.summary.post_process is not None
    pipeline.summary.post_process("summary text", ctx)
    return ctx


def test_watermark_advances_only_after_daily_exists(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    head = _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    engine = GitRecapEngine(env=env)
    env.fresh_commits()  # captures seen_heads, as a real pass would

    # Daily never wrote: no advance, and an operator action is raised.
    ctx = _run_summary_post(engine, env)
    assert env.watermarks() == {}
    actions = ctx.artifacts.get("operator_actions", [])
    assert any("watermarks were NOT advanced" in a.get("why", "") for a in actions)

    # Daily exists and is non-empty: advance to the seen head.
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_today()).write_text("# 2026 entry\n")
    _run_summary_post(engine, env)
    assert env.watermarks() == {str(repo): head}


def test_empty_daily_file_does_not_advance(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    engine = GitRecapEngine(env=env)
    env.fresh_commits()
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_today()).write_text("")  # zero bytes = not a write
    _run_summary_post(engine, env)
    assert env.watermarks() == {}


# ---- env methods -------------------------------------------------------


def test_recent_dailies_excludes_today_and_orders_oldest_first(tmp_path):
    env = _env(tmp_path, [])
    env.daily_dir.mkdir(parents=True)
    for d in ["2026-07-01", "2026-07-02", "2026-07-03"]:
        env.daily_path(d).write_text(f"entry {d}")
    env.daily_path(_today()).write_text("today, must not appear")
    out = env.recent_dailies()
    assert "2026-07-01" in out and "2026-07-03" in out
    assert "must not appear" not in out
    assert out.index("2026-07-01") < out.index("2026-07-03")


def test_todays_entry_amend_context(tmp_path):
    env = _env(tmp_path, [])
    assert "first entry" in env.todays_entry()
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_today()).write_text("# so far today")
    assert "so far today" in env.todays_entry()


def test_closed_week_dailies_only_that_week(tmp_path):
    env = _env(tmp_path, [])
    closed = _closed_week()
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_week_dates(closed)[0]).write_text("in-week entry")
    env.daily_path("2020-01-01").write_text("ancient entry")
    out = env.closed_week_dailies()
    assert "in-week entry" in out
    assert "ancient entry" not in out


def test_journal_status_names_the_decisions(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    status = env.journal_status()
    assert "TOTAL fresh: 1" in status
    assert f"today: {_today()}" in status
    assert f"closed week: {_closed_week()}" in status
    assert "weekly due: False" in status


# ---- precheck ------------------------------------------------------------


def test_precheck_requires_repos(tmp_path):
    env = _env(tmp_path, [])
    engine = GitRecapEngine(env=env)
    with pytest.raises(RuntimeError, match="no repos configured"):
        engine.precheck(_ctx(env))


def test_precheck_rejects_non_git_paths(tmp_path):
    not_git = tmp_path / "plain"
    not_git.mkdir()
    env = _env(tmp_path, [not_git])
    engine = GitRecapEngine(env=env)
    with pytest.raises(RuntimeError, match="not git repositories"):
        engine.precheck(_ctx(env))


def test_precheck_creates_journal_dirs(tmp_path):
    repo = _make_repo(tmp_path / "r1")
    env = _env(tmp_path, [repo])
    GitRecapEngine(env=env).precheck(_ctx(env))
    assert env.daily_dir.is_dir()
    assert env.weekly_dir.is_dir()


# ---- policies wire output paths -----------------------------------------


def test_policies_compute_the_journal_paths(tmp_path):
    env = _env(tmp_path, [])
    engine = GitRecapEngine(env=env)
    daily = engine.branch_policies["daily"]
    weekly = engine.branch_policies["weekly"]
    assert daily.output_path is not None and weekly.output_path is not None
    assert daily.output_path({"date": "2026-07-13"}) == env.daily_path("2026-07-13")
    assert weekly.output_path({"week": "2026-W28"}) == env.weekly_path("2026-W28")


def test_state_file_round_trips(tmp_path):
    env = _env(tmp_path, [])
    env.seen_heads = {"/a/repo": "abc123"}
    env.advance_watermarks()
    data = json.loads((tmp_path / "git-recap.state.json").read_text())
    assert data["watermarks"]["/a/repo"] == "abc123"


def test_watermark_advance_uses_the_runs_env_not_the_engines(tmp_path):
    """Regression, caught live 2026-07-13: the CLI builds the engine's env
    and the looper's env as SEPARATE instances. seen_heads is captured on
    the looper's instance when the daily renders fresh_commits; the
    post_process must act on ctx.environment, or it advances 0 repos and
    the next run re-covers everything."""
    repo = _make_repo(tmp_path / "r1")
    head = _commit(repo, "a.txt")

    engine_env = _env(tmp_path, [repo])   # instance A (engine's)
    run_env = _env(tmp_path, [repo])      # instance B (the looper's)
    engine = GitRecapEngine(env=engine_env)
    run_env.fresh_commits()               # capture happens on B, as live
    assert engine_env.seen_heads == {}    # A never saw anything

    run_env.daily_dir.mkdir(parents=True)
    run_env.daily_path(_today()).write_text("# entry\n")
    ctx = _ctx(run_env)
    pipeline = engine.pipeline(ctx)
    assert pipeline.summary is not None and pipeline.summary.post_process is not None
    pipeline.summary.post_process("out", ctx)
    assert run_env.watermarks() == {str(repo): head}


def test_empty_capture_raises_operator_action_not_silence(tmp_path):
    """A daily that wrote while NO heads were captured is an anomaly the
    operator must see — never a quiet 'advanced 0 repo(s)'."""
    repo = _make_repo(tmp_path / "r1")
    _commit(repo, "a.txt")
    env = _env(tmp_path, [repo])
    engine = GitRecapEngine(env=env)
    env.daily_dir.mkdir(parents=True)
    env.daily_path(_today()).write_text("# entry\n")
    ctx = _ctx(env)
    pipeline = engine.pipeline(ctx)
    pipeline.summary.post_process("out", ctx)
    actions = ctx.artifacts.get("operator_actions", [])
    assert any("watermark" in a.get("action", "") for a in actions)
    assert env.watermarks() == {}
