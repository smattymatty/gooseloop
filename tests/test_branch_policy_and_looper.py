"""Smoke tests for the looper that don't require the goose binary.

The looper invokes goose via run_goose_with_retry; these tests monkey-patch
that to feed canned outputs into the looper and verify the framework
correctly:

  - parses the review's deliverable JSON
  - validates the schema
  - seeds the operator_actions ledger from the review
  - builds body Phases from routing[] via engine.branch_policies
  - runs summary last with the final ledger visible
  - enforces Pipeline as the return type of engine.pipeline()
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gooseloop import (
    BranchPolicy,
    Context,
    Engine,
    Environment,
    GooseLooper,
    LooperConfig,
    Phase,
    Pipeline,
)


# ---- fakes -------------------------------------------------------

class _SilentEnv(Environment):
    def env_vars(self) -> dict[str, str]:
        return {"FOO": "bar"}


REVIEW_OUTPUT = (
    "<<<DELIVERABLE_JSON>>>\n"
    + json.dumps({
        "protocol_version": "1.0",
        "status": "done",
        "summary": "two greetings due",
        "insights": ["two names in scope"],
        "routing": [
            {"recipe": "greet", "params": {"name": "alice"}, "reason": "first"},
            {"recipe": "greet", "params": {"name": "bob"}, "reason": "second"},
        ],
        "operator_actions": [
            {"action": "double-check greetings landed", "why": "smoke test"},
        ],
    })
    + "\n<<<END_DELIVERABLE>>>\n"
)

GREET_OUTPUT = "wrote a greeting\n"
SUMMARY_OUTPUT = "## Summary\n- alice ok\n- bob ok\n"


class _RecordingEngine(Engine):
    branch_policies = {
        "greet": BranchPolicy(
            output_path=lambda p: Path(f"/tmp/gooseloop-test/{p.get('name')}.txt"),
        ),
    }

    def __init__(self) -> None:
        self.precheck_ran = False
        self.body_post_called_with: list[str] = []

    @property
    def name(self) -> str:
        return "test"

    def precheck(self, ctx: Context) -> None:
        self.precheck_ran = True

    def pipeline(self, ctx: Context) -> Pipeline:
        return Pipeline(
            review=Phase(name="review", recipe_path="review.yaml"),
            body=[],
            summary=Phase(name="summary", recipe_path="summary.yaml"),
        )


class _BadEngine(Engine):
    @property
    def name(self) -> str:
        return "bad"

    def pipeline(self, ctx: Context) -> Any:
        return [Phase(name="x", recipe_path="x.yaml")]  # wrong type


# ---- looper monkey-patch helper ----------------------------------

class _CannedGoose:
    """Return canned outputs keyed by recipe name."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, recipe_path: str, model: str, extra_env=None, *,
                 max_retries=6, base_delay=5, success_predicate=None,
                 label=None, environment=None,
                 local_path=None, overlay_paths=None) -> str:
        self.calls.append(recipe_path)
        for stem, output in self.mapping.items():
            if stem in recipe_path:
                return output
        return ""


@pytest.fixture
def patched_looper(monkeypatch):
    canned = _CannedGoose({
        "review.yaml": REVIEW_OUTPUT,
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    })
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", canned)
    return canned


def _make_config(tmp_path: Path) -> LooperConfig:
    return LooperConfig.load(anchor=tmp_path, warn_on_missing=False)


# ---- tests -------------------------------------------------------

def test_pipeline_type_enforced(tmp_path, patched_looper):
    looper = GooseLooper(
        engine=_BadEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    with pytest.raises(TypeError, match="Pipeline"):
        looper.begin_loop()


def test_routing_phase_gets_output_path_env_var(tmp_path):
    """Regression 2026-06-04: the body recipe wrote ${SHA}.md while the
    BranchPolicy predicate looked for <slug>-<sha8>.md. Filename mismatch
    caused fake transient-error retries on every successful write. The
    framework now injects OUTPUT_PATH so recipe + predicate read the same
    source of truth."""
    from gooseloop.branch_policy import BranchPolicy
    from gooseloop.phase import Context, Phase
    from pathlib import Path as _P

    engine = _RecordingEngine()
    engine.branch_policies = {
        "greet": BranchPolicy(
            output_path=lambda p: _P(f"/tmp/x/{p['name']}.txt"),
        ),
    }
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    phase = looper._phase_from_routing(
        "greet", {"name": "alice"}, engine.branch_policies["greet"],
    )
    env = phase.build_env(Context(model="m", session_dir=None, base_env={}))
    assert env["OUTPUT_PATH"] == "/tmp/x/alice.txt"
    assert env["NAME"] == "alice"


def test_routing_phase_no_output_path_skips_env_injection(tmp_path):
    """If the policy has no output_path, no OUTPUT_PATH gets injected
    (recipes that don't write files don't need it)."""
    from gooseloop.branch_policy import BranchPolicy
    from gooseloop.phase import Context

    engine = _RecordingEngine()
    engine.branch_policies = {"x": BranchPolicy()}
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    phase = looper._phase_from_routing("x", {"k": "v"}, engine.branch_policies["x"])
    env = phase.build_env(Context(model="m", session_dir=None, base_env={}))
    assert "OUTPUT_PATH" not in env


def test_shipped_recipes_use_output_path_convention():
    """Pin the recipe-side contract: any body recipe that writes a file
    must use ${OUTPUT_PATH} so it agrees with the framework-injected
    path. Earlier mismatch cost token budget on every recap."""
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1]
    for rel in [
        "engines/hello_world/recipes/greet.yaml",
        "engines/git_recap/recipes/summarize-commit.yaml",
    ]:
        text = (root / rel).read_text()
        assert "${OUTPUT_PATH}" in text, f"{rel} must reference ${{OUTPUT_PATH}}"


def test_phase_banners_include_step_progress(tmp_path, patched_looper, capsys):
    """Every phase banner reads `[N/M]` so the operator knows position.

    Regression for the 2026-06-04 UX request: long-running engines like
    git-recap (15+ body phases) need progress indication so the operator
    knows whether they're at step 3 of 17 or step 16 of 17.
    """
    from gooseloop.text import strip_ansi

    looper = GooseLooper(
        engine=_RecordingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    looper.begin_loop()
    out = strip_ansi(capsys.readouterr().out)
    # Review shows [1/?] because routing hasn't run yet — the total is
    # structurally unknowable until review completes. After review spawns
    # 2 children, planned = 4 and subsequent banners show real numbers.
    assert "review · [1/?]" in out
    assert "branch:greet · [2/4]" in out
    assert "branch:greet · [3/4]" in out
    assert "summary · [4/4]" in out


def test_review_seeds_operator_actions_and_spawns_body(tmp_path, patched_looper):
    engine = _RecordingEngine()
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    result = looper.begin_loop()
    assert engine.precheck_ran is True
    # review + 2 greet + summary = 4 goose calls
    assert result["goose_calls"] == 4
    # And actions_planned must include the review-spawned children.
    # Regression 2026-06-04: this used to read "2 planned · 5 ran" because
    # review-spawned phases weren't being added to actions_planned.
    assert result["actions_planned"] == 4
    assert result["actions_ran"] == 4
    assert result["actions_skipped"] == 0
    # both greet routings ran
    greet_calls = [c for c in patched_looper.calls if "greet" in c]
    assert len(greet_calls) == 2
    # operator action from review present
    actions = result["operator_actions"]
    assert any(a["action"] == "double-check greetings landed" for a in actions)
    assert result["review_status"] == "done"


def test_review_only_skips_body_and_summary(tmp_path, patched_looper):
    looper = GooseLooper(
        engine=_RecordingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
        review_only=True,
    )
    result = looper.begin_loop()
    assert result["goose_calls"] == 1  # review only
    assert all("greet" not in c and "summary" not in c
               for c in patched_looper.calls)


def test_review_error_status_skips_body_and_summary(tmp_path, monkeypatch):
    bad_review = (
        "<<<DELIVERABLE_JSON>>>\n"
        + json.dumps({
            "protocol_version": "1.0",
            "status": "error",
            "summary": "couldn't review",
            "insights": [],
            "routing": [],
            "operator_actions": [{"action": "investigate", "why": "review failed"}],
        })
        + "\n<<<END_DELIVERABLE>>>\n"
    )
    canned = _CannedGoose({
        "review.yaml": bad_review,
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    })
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", canned)
    looper = GooseLooper(
        engine=_RecordingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    result = looper.begin_loop()
    assert result["review_status"] == "error"
    assert result["goose_calls"] == 1
    assert any("investigate" in a["action"] for a in result["operator_actions"])


def test_review_default_predicate_rejects_truncated_output(tmp_path, monkeypatch):
    """Regression 2026-06-04: stream truncation mid-emit produced output
    without a closing sentinel; framework accepted it (no transient
    pattern matched) and failed downstream. The looper now wraps the
    review's success_predicate with extract_json_with_provenance, so a
    truncated stream becomes a retry, not a silent acceptance."""
    truncated = (
        "<<<DELIVERABLE_JSON>>>\n"
        '{"protocol_version":"1.0","status":"done","summary":"oh no the strea'
    )  # cuts mid-string mid-emit; no closing markers, unbalanced braces.

    calls = []
    def fake_run(recipe_path, model, extra_env=None, *,
                 max_retries=6, base_delay=5, success_predicate=None,
                 label=None, environment=None,
                 local_path=None, overlay_paths=None):
        calls.append(recipe_path)
        # Simulate retry behaviour of the real run_goose_with_retry:
        # the predicate fires per attempt and gates retry.
        if success_predicate is not None and not success_predicate(truncated):
            raise RuntimeError("simulated max-retries exhausted")
        return truncated

    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", fake_run)

    looper = GooseLooper(
        engine=_RecordingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    result = looper.begin_loop()
    # Predicate caught the bad output (no balanced JSON); the looper raised
    # and we landed in review's exception branch.
    assert result["review_status"] == "error"


def test_review_missing_sentinels_marks_error(tmp_path, monkeypatch):
    canned = _CannedGoose({
        "review.yaml": "no sentinels here\n",
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    })
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", canned)
    looper = GooseLooper(
        engine=_RecordingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    result = looper.begin_loop()
    assert result["review_status"] == "error"


def test_review_partial_runs_body_but_skips_summary(tmp_path, monkeypatch):
    partial_review = (
        "<<<DELIVERABLE_JSON>>>\n"
        + json.dumps({
            "protocol_version": "1.0",
            "status": "partial",
            "summary": "only partial info available",
            "insights": [],
            "routing": [],
            "operator_actions": [{"action": "fix missing input", "why": "partial"}],
        })
        + "\n<<<END_DELIVERABLE>>>\n"
    )
    canned = _CannedGoose({
        "review.yaml": partial_review,
        "summary.yaml": SUMMARY_OUTPUT,
    })
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", canned)
    looper = GooseLooper(
        engine=_RecordingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    result = looper.begin_loop()
    # Review only; summary skipped because status=partial.
    assert "summary.yaml" not in [Path(c).name for c in patched_calls(canned)]
    assert result["review_status"] == "partial"


def patched_calls(canned: _CannedGoose) -> list[str]:
    return canned.calls
