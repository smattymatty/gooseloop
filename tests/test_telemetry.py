"""Phase telemetry (ADR 0012, PROTOCOL §14): one wide event per phase in
phases.jsonl, full transcripts beside them, review/body/summary uniformly,
failures keeping their last attempt's output.

Uses the same patched seams as test_branch_policy_and_looper (preparation
+ goose invocation) — no goose binary, real looper."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from gooseloop import (
    BranchPolicy,
    Engine,
    Environment,
    GooseLooper,
    LooperConfig,
    Phase,
    Pipeline,
    telemetry,
)

REVIEW_OUTPUT = (
    "<<<DELIVERABLE_JSON>>>\n"
    + json.dumps({
        "protocol_version": "1.0",
        "status": "done",
        "summary": "one greeting due",
        "insights": ["one name in scope"],
        "routing": [
            {"recipe": "greet", "params": {"name": "alice"}, "reason": "first"},
        ],
        "operator_actions": [],
    })
    + "\n<<<END_DELIVERABLE>>>\n"
)
GREET_OUTPUT = "wrote a greeting for alice\n"
SUMMARY_OUTPUT = "## Summary\n- alice ok\n"


class _Env(Environment):
    def env_vars(self) -> dict[str, str]:
        return {"BASE_MARKER": "constant-per-session"}


class _TelemetryEngine(Engine):
    branch_policies = {"greet": BranchPolicy()}

    @property
    def name(self) -> str:
        return "telemetry-test"

    def pipeline(self, ctx) -> Pipeline:
        return Pipeline(
            review=Phase(name="review", recipe_path="review.yaml"),
            summary=Phase(name="summary", recipe_path="summary.yaml"),
        )


@contextlib.contextmanager
def _unprepared(recipe_path, extra_env=None, **kwargs):
    yield str(recipe_path)


def _canned(mapping):
    def run(recipe_path, model, extra_env=None, *, stats=None, **kwargs):
        if stats is not None:
            stats["attempts"] = 1
        for stem, output in mapping.items():
            if stem in recipe_path:
                return output
        return ""
    return run


def _patch(monkeypatch, run) -> None:
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _unprepared)
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", run)


def _run_pass(tmp_path, monkeypatch, mapping=None) -> Path:
    _patch(monkeypatch, _canned(mapping or {
        "review.yaml": REVIEW_OUTPUT,
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_TelemetryEngine(),
        environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = result["session_dir"]
    assert session_dir is not None
    return Path(session_dir)


def test_full_pass_emits_the_whole_sandwich(tmp_path, monkeypatch):
    session_dir = _run_pass(tmp_path, monkeypatch)
    events = telemetry.read_phase_events(session_dir)
    kinds = [(e["kind"], e["status"]) for e in events]
    assert kinds == [("review", "ok"), ("body", "ok"), ("summary", "ok")]
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_events_carry_the_wide_dimensions(tmp_path, monkeypatch):
    session_dir = _run_pass(tmp_path, monkeypatch)
    review, greet, summary = telemetry.read_phase_events(session_dir)
    assert review["phase"] == "review"
    assert review["attempts"] == 1
    assert greet["env"].get("NAME") == "alice"  # routing param, uppercased
    assert isinstance(greet["duration_s"], (int, float))
    assert summary["kind"] == "summary"


def test_transcripts_persist_full_and_are_referenced(tmp_path, monkeypatch):
    session_dir = _run_pass(tmp_path, monkeypatch)
    for event in telemetry.read_phase_events(session_dir):
        ref = event["transcript"]
        assert ref, f"{event['phase']} has no transcript ref"
        text = (session_dir / ref).read_text()
        assert event["transcript_chars"] == len(text)
    review_event = telemetry.read_phase_events(session_dir)[0]
    assert REVIEW_OUTPUT == (session_dir / review_event["transcript"]).read_text()


def test_base_env_recorded_once_in_meta_not_per_event(tmp_path, monkeypatch):
    session_dir = _run_pass(tmp_path, monkeypatch)
    meta = json.loads((session_dir / "session.meta.json").read_text())
    assert meta["base_env"]["BASE_MARKER"] == "constant-per-session"
    for event in telemetry.read_phase_events(session_dir):
        assert "BASE_MARKER" not in event["env"]


def test_failed_review_keeps_its_last_attempt_transcript(tmp_path, monkeypatch):
    """The headline debugging win: a review that emits garbage finally
    leaves its evidence behind instead of evaporating with the raise."""
    garbage = "I am a model that forgot the sentinels entirely."

    def failing_run(recipe_path, model, extra_env=None, *, stats=None, **kwargs):
        if stats is not None:
            stats["attempts"] = 3
            stats["last_output"] = garbage
        raise RuntimeError("goose failed after 2 retries: review")

    _patch(monkeypatch, failing_run)
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = Path(result["session_dir"])
    events = telemetry.read_phase_events(session_dir)
    assert len(events) == 1
    event = events[0]
    assert (event["kind"], event["status"]) == ("review", "failed")
    assert event["attempts"] == 3
    assert "goose failed" in event["error"]
    assert (session_dir / event["transcript"]).read_text() == garbage


def test_unparseable_review_records_failed_with_transcript(tmp_path, monkeypatch):
    session_dir = _run_pass(tmp_path, monkeypatch, mapping={
        "review.yaml": "no sentinels here at all",
    })
    events = telemetry.read_phase_events(session_dir)
    assert events[0]["status"] == "failed"
    assert "wrapped JSON" in events[0]["error"]
    assert (session_dir / events[0]["transcript"]).read_text() == "no sentinels here at all"


def test_skipped_body_phase_records_reason(tmp_path, monkeypatch):
    class _SkippingEngine(_TelemetryEngine):
        def pipeline(self, ctx) -> Pipeline:
            return Pipeline(
                review=Phase(name="review", recipe_path="review.yaml"),
                body=[Phase(name="extra", recipe_path="greet.yaml",
                            skip_if=lambda c: "already greeted today")],
                summary=Phase(name="summary", recipe_path="summary.yaml"),
            )

    _patch(monkeypatch, _canned({
        "review.yaml": REVIEW_OUTPUT,
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_SkippingEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    result = looper.begin_loop()
    events = telemetry.read_phase_events(Path(result["session_dir"]))
    skipped = [e for e in events if e["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["phase"] == "extra"
    assert skipped[0]["skip_reason"] == "already greeted today"
    assert skipped[0]["transcript"] is None


def test_reader_tolerates_a_torn_final_line(tmp_path):
    (tmp_path / "phases.jsonl").write_text(
        json.dumps({"seq": 1, "phase": "review"}) + "\n"
        + '{"seq": 2, "phase": "half-writ'
    )
    events = telemetry.read_phase_events(tmp_path)
    assert len(events) == 1
    assert events[0]["seq"] == 1


def test_no_save_pass_emits_nothing_and_does_not_crash(tmp_path, monkeypatch):
    _patch(monkeypatch, _canned({
        "review.yaml": REVIEW_OUTPUT, "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=False,
    )
    result = looper.begin_loop()
    assert result["session_dir"] is None


def test_body_event_carries_the_actions_it_raised(tmp_path, monkeypatch):
    """Per-phase action deltas ride the wide event (additive §14 key), so
    a decision is durable the moment its phase settles — mid-run seal
    queues and crashed passes both depend on it."""
    class _RaisingEngine(_TelemetryEngine):
        branch_policies = {
            "greet": BranchPolicy(),
        }

        def pipeline(self, ctx) -> Pipeline:
            def post(_o, c):
                c.add_operator_action("seal the thing", why="it landed")
            return Pipeline(
                review=Phase(name="review", recipe_path="review.yaml"),
                body=[Phase(name="worker", recipe_path="greet.yaml", post_process=post)],
                summary=Phase(name="summary", recipe_path="summary.yaml"),
            )

    _patch(monkeypatch, _canned({
        "review.yaml": REVIEW_OUTPUT.replace(
            '"routing": [\n        {"recipe": "greet", "params": {"name": "alice"}, "reason": "first"},\n    ]',
            '"routing": []'),
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_RaisingEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    result = looper.begin_loop()
    events = telemetry.read_phase_events(Path(result["session_dir"]))
    worker = next(e for e in events if e["phase"] == "worker")
    assert worker["actions"] == [{"action": "seal the thing", "why": "it landed"}]
    summary = next(e for e in events if e["kind"] == "summary")
    assert summary["actions"] == []  # only the delta, never the whole ledger


# ---- the input half: prompts persist (what the model SAW) ---------------


def _rendered(tmp_path):
    """A prepared_recipe fake that yields a REAL rendered file, the way
    context_prepend does — so the looper's capture reads actual bytes."""
    @contextlib.contextmanager
    def prepared(recipe_path, extra_env=None, **kwargs):
        f = tmp_path / f"rendered-{Path(str(recipe_path)).name}"
        f.write_text(f"prompt: rendered-for {recipe_path}\n")
        yield str(f)
    return prepared


def test_prompt_persists_what_the_model_saw(tmp_path, monkeypatch):
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _rendered(tmp_path))
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", _canned({
        "review.yaml": REVIEW_OUTPUT, "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    session_dir = Path(looper.begin_loop()["session_dir"])
    events = telemetry.read_phase_events(session_dir)
    assert len(events) == 3
    for e in events:
        assert e["prompt"], f"phase {e['phase']} lost its prompt"
        assert e["prompt"].endswith(".prompt.yaml")
        content = (session_dir / e["prompt"]).read_text()
        assert "rendered-for" in content
        assert e["prompt_chars"] == len(content)


def test_failed_phase_still_keeps_its_prompt(tmp_path, monkeypatch):
    """Failure investigations need the input MOST — the prompt is captured
    before goose runs, so it survives whatever happens after."""
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _rendered(tmp_path))
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", _canned({
        "review.yaml": "no sentinels here, parsing will fail",
    }))
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    session_dir = Path(looper.begin_loop()["session_dir"])
    review = telemetry.read_phase_events(session_dir)[0]
    assert review["status"] == "failed"
    assert review["prompt"] is not None
    assert (session_dir / review["prompt"]).exists()


def test_prompt_with_secret_is_redacted_flagged_and_raised(tmp_path, monkeypatch):
    """A secret pasted INTO the input is the same incident as one printed
    out: redact the artifact, flag the event, raise the rotate card."""
    @contextlib.contextmanager
    def poisoned(recipe_path, extra_env=None, **kwargs):
        f = tmp_path / f"rendered-{Path(str(recipe_path)).name}"
        f.write_text("prompt: uses MY_API_TOKEN=supersecretvalue1234 inline\n")
        yield str(f)

    monkeypatch.setattr("gooseloop.looper.prepared_recipe", poisoned)
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", _canned({
        "review.yaml": REVIEW_OUTPUT, "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    session_dir = Path(looper.begin_loop()["session_dir"])
    review = telemetry.read_phase_events(session_dir)[0]
    persisted = (session_dir / review["prompt"]).read_text()
    assert "supersecretvalue" not in persisted
    assert "[REDACTED" in persisted
    assert any("in prompt" in f for f in review["flags"])
    import json as _json
    ledger = _json.loads((session_dir / "ledger.json").read_text())
    assert any("prompt" in a["action"] for a in ledger["operator_actions"])


# ---- retry attempts persist (§14 attempt_log) ----------------------------


def _flaky(mapping, fail_first_n=1, failure_text="server error: hiccup"):
    """A run_goose fake that mimics the real retry loop's stats contract:
    fail_first_n invocations 'fail' (logged with their output), then the
    canned success — everything in one call, like the real function."""
    def run(recipe_path, model, extra_env=None, *, stats=None, **kwargs):
        final = next((o for s, o in mapping.items() if s in recipe_path), "")
        if stats is not None:
            log = [{"attempt": i + 1, "returncode": 0, "duration_s": 1.0,
                    "outcome": "transient-error", "retry_delay_s": 5,
                    "output": failure_text} for i in range(fail_first_n)]
            log.append({"attempt": fail_first_n + 1, "returncode": 0,
                        "duration_s": 2.0, "outcome": "ok", "output": None})
            stats["attempts"] = fail_first_n + 1
            stats["attempt_log"] = log
        return final
    return run


def test_retry_attempts_persist_their_transcripts(tmp_path, monkeypatch):
    _patch(monkeypatch, _flaky({
        "review.yaml": REVIEW_OUTPUT, "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }))
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    session_dir = Path(looper.begin_loop()["session_dir"])
    review = telemetry.read_phase_events(session_dir)[0]
    log = review["attempt_log"]
    assert [e["outcome"] for e in log] == ["transient-error", "ok"]
    # The failed attempt keeps its own transcript file; the final entry
    # points at the phase transcript — the record is complete, no gaps.
    assert log[0]["transcript"].endswith(".attempt-1.txt")
    assert "server error" in (session_dir / log[0]["transcript"]).read_text()
    assert log[1]["transcript"] == review["transcript"]
    assert "output" not in log[0]  # inline text never lands in the event


def test_secret_in_a_retry_attempt_is_redacted_flagged_and_raised(tmp_path, monkeypatch):
    """The same secret handling: attempt 2 succeeded clean, but attempt 1
    printed a credential — that still reached the provider."""
    _patch(monkeypatch, _flaky({
        "review.yaml": REVIEW_OUTPUT, "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    }, failure_text="failed but leaked STRIPE_SECRET_KEY=sk_live_oops12345678"))
    looper = GooseLooper(
        engine=_TelemetryEngine(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    session_dir = Path(looper.begin_loop()["session_dir"])
    review = telemetry.read_phase_events(session_dir)[0]
    persisted = (session_dir / review["attempt_log"][0]["transcript"]).read_text()
    assert "sk_live_oops" not in persisted
    assert "[REDACTED" in persisted
    assert any("retry attempts" in f for f in review["flags"])
    import json as _json
    ledger = _json.loads((session_dir / "ledger.json").read_text())
    assert any("retry" in a["action"] for a in ledger["operator_actions"])
