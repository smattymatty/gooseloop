"""Smoke tests for the looper that don't require the goose binary.

The looper prepares recipes via prepared_recipe and invokes goose via
run_goose_with_retry; these tests monkey-patch both seams (preparation
would otherwise read recipe files that don't exist) to feed canned
outputs into the looper and verify the framework correctly:

  - parses the review's deliverable JSON
  - validates the schema
  - seeds the operator_actions ledger from the review
  - builds body Phases from routing[] via engine.branch_policies
  - runs summary last with the final ledger visible
  - enforces Pipeline as the return type of engine.pipeline()
"""

from __future__ import annotations

import contextlib
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
                 label=None, stats=None, sandbox=None) -> str:
        self.calls.append(recipe_path)
        for stem, output in self.mapping.items():
            if stem in recipe_path:
                return output
        return ""


@contextlib.contextmanager
def _unprepared(recipe_path, extra_env=None, **kwargs):
    """Bypass overlay merge + context render; these tests' recipe paths
    are fake and preparation would try to read them from disk."""
    yield str(recipe_path)


def _patch_goose(monkeypatch, run) -> None:
    """Patch the looper's two invocation seams: preparation and goose."""
    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _unprepared)
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", run)


@pytest.fixture
def patched_looper(monkeypatch):
    canned = _CannedGoose({
        "review.yaml": REVIEW_OUTPUT,
        "greet": GREET_OUTPUT,
        "summary.yaml": SUMMARY_OUTPUT,
    })
    _patch_goose(monkeypatch, canned)
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


def test_summary_output_written_to_session_dir(tmp_path, patched_looper):
    """Regression 2026-07-13: every body phase leaves a file (a draft, a
    recap) but the summary phase — whose entire job is the human-facing
    report — left nothing behind once the terminal scrollback was gone.
    The looper now writes the summary phase's raw stdout to
    <session_dir>/summary.md, alongside session.log and session.meta.json."""
    engine = _RecordingEngine()
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = result["session_dir"]
    assert session_dir is not None
    summary_path = session_dir / "summary.md"
    assert summary_path.exists()
    assert summary_path.read_text() == SUMMARY_OUTPUT


def test_planned_bound_shows_ceiling_on_review(tmp_path, patched_looper):
    """A model-routed engine that declares a planned_bound turns the review
    banner's [1/?] into [1/<=N] (1 review + 0 static body + 1 summary = 2,
    plus a bound of 2 = <=4)."""
    class _BoundedEngine(_RecordingEngine):
        def planned_bound(self, ctx: Context):
            return 2

    looper = GooseLooper(
        engine=_BoundedEngine(), environment=_SilentEnv(),
        config=_make_config(tmp_path), save=False,
    )
    looper.begin_loop()
    assert looper._review_total == "<=4"


def test_no_planned_bound_shows_question_mark(tmp_path, patched_looper):
    looper = GooseLooper(
        engine=_RecordingEngine(), environment=_SilentEnv(),
        config=_make_config(tmp_path), save=False,
    )
    looper.begin_loop()
    assert looper._review_total == "?"


def test_planned_bound_zero_shows_exact_total(tmp_path, patched_looper):
    """bound 0 means nothing will route: show the exact static total, not <=."""
    class _ZeroBound(_RecordingEngine):
        def planned_bound(self, ctx: Context):
            return 0

    looper = GooseLooper(
        engine=_ZeroBound(), environment=_SilentEnv(),
        config=_make_config(tmp_path), save=False,
    )
    looper.begin_loop()
    assert looper._review_total == "2"


def test_no_summary_md_when_summary_phase_absent(tmp_path, patched_looper):
    """A Pipeline with summary=None (permitted per ADR 0006) writes no
    summary.md — there is nothing to capture."""
    class _NoSummaryEngine(_RecordingEngine):
        def pipeline(self, ctx: Context) -> Pipeline:
            return Pipeline(review=Phase(name="review", recipe_path="review.yaml"))

    looper = GooseLooper(
        engine=_NoSummaryEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = result["session_dir"]
    assert session_dir is not None
    assert not (session_dir / "summary.md").exists()


def test_ledger_json_written_with_final_operator_actions(tmp_path, patched_looper):
    """Regression 2026-07-13: the same gap as summary.md, one layer deeper.
    review.json freezes the review's SEED ledger; body-appended actions
    (e.g. a body phase's own add_operator_action calls) only ever reached
    the terminal footer. ledger.json now persists the FINAL, complete
    operator_actions + outputs_written after the whole pass completes."""
    class _AppendingEngine(_RecordingEngine):
        def pipeline(self, ctx: Context) -> Pipeline:
            def _append(_output: str, c: Context) -> None:
                c.add_operator_action("seal the thing", why="body said so")
                c.record_output("/tmp/gooseloop-test/out.txt")
            return Pipeline(
                review=Phase(name="review", recipe_path="review.yaml"),
                body=[],
                summary=Phase(name="summary", recipe_path="summary.yaml",
                              post_process=_append),
            )

    looper = GooseLooper(
        engine=_AppendingEngine(),
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = result["session_dir"]
    ledger_path = session_dir / "ledger.json"
    assert ledger_path.exists()
    ledger = json.loads(ledger_path.read_text())
    actions = [a["action"] for a in ledger["operator_actions"]]
    assert "double-check greetings landed" in actions  # from the review seed
    assert "seal the thing" in actions                  # body-appended
    assert "/tmp/gooseloop-test/out.txt" in ledger["outputs_written"]


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


def test_shipped_recipes_reference_their_policy_output_env():
    """Pin the recipe-side contract (ADR 0011): a body recipe that writes a
    file must reference ${<output_env>} so it agrees with the framework-
    injected path. Earlier mismatch cost token budget on every recap.
    hello_world uses a custom name (GREETING_FILE) to teach the wire;
    git_recap keeps the OUTPUT_PATH default — the pair pins both modes."""
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1]
    for rel, var in [
        ("engines/hello_world/recipes/greet.yaml", "GREETING_FILE"),
        ("engines/git_recap/recipes/daily.yaml", "OUTPUT_PATH"),
        ("engines/git_recap/recipes/weekly.yaml", "OUTPUT_PATH"),
    ]:
        text = (root / rel).read_text()
        assert f"${{{var}}}" in text, f"{rel} must reference ${{{var}}}"


def test_shipped_engines_pass_output_env_verification(tmp_path):
    """The looper's ADR 0011 pre-run check accepts every shipped engine's
    policy/recipe pairing as it exists on disk."""
    # importlib, not attribute-style import: each engine package's
    # __init__.py exposes an `engine` ATTRIBUTE (the class) that shadows
    # the `engine` submodule on `import pkg.engine as x`.
    import importlib
    hw = importlib.import_module("engines.hello_world.engine")
    gr = importlib.import_module("engines.git_recap.engine")

    recap_env = gr.GitRecapEnvironment(
        repos=[], author="auto", journal_dir=tmp_path / "journal",
        state_path=tmp_path / "git-recap.state.json",
    )
    for engine in [
        hw.HelloEngine(),
        gr.GitRecapEngine(env=recap_env),
    ]:
        looper = GooseLooper(
            engine=engine,
            config=_make_config(tmp_path),
            save=False,
        )
        looper._verify_output_env_contracts()  # must not raise


def test_hello_world_greet_skips_when_greeting_exists(tmp_path, monkeypatch):
    """Re-runs are idempotent: a name whose greeting is already on disk is
    skipped with a reason before any model call; a missing or empty file
    lets the phase run."""
    import importlib
    hw = importlib.import_module("engines.hello_world.engine")

    monkeypatch.setenv("GREETINGS_DIR", str(tmp_path))
    policy = hw.HelloEngine().branch_policies["greet"]

    assert policy.skip_when({"name": "alice"}) is None  # nothing on disk yet

    (tmp_path / "alice.txt").write_text("Hello, alice!\n")
    reason = policy.skip_when({"name": "alice"})
    assert reason == "greeting already on disk: alice.txt"

    (tmp_path / "bob.txt").write_text("")  # empty file = not produced
    assert policy.skip_when({"name": "bob"}) is None
    assert policy.skip_when({}) is None  # no name param: not the skip's call


# ---- ADR 0011: output_env injection + contract verification -------

def _engine_with_recipe(tmp_path: Path, prompt: str, policy: BranchPolicy):
    """A minimal engine whose recipes dir holds one real greet.yaml."""
    recipes = tmp_path / "recipes"
    recipes.mkdir(exist_ok=True)
    (recipes / "greet.yaml").write_text(
        'version: "1.0.0"\ntitle: "greet"\ndescription: "d"\n'
        f"prompt: |\n  {prompt}\n"
    )

    class _E(_RecordingEngine):
        def recipes_dir(self) -> str:
            return str(recipes)

    engine = _E()
    engine.branch_policies = {"greet": policy}
    return engine


def test_routing_phase_injects_under_custom_output_env(tmp_path):
    """ADR 0011: output_env names the env var the computed path lands in."""
    from pathlib import Path as _P

    engine = _RecordingEngine()
    engine.branch_policies = {
        "greet": BranchPolicy(
            output_path=lambda p: _P(f"/tmp/x/{p['name']}.txt"),
            output_env="GREETING_FILE",
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
    assert env["GREETING_FILE"] == "/tmp/x/alice.txt"
    assert "OUTPUT_PATH" not in env


def test_output_env_mismatch_refuses_pass_before_any_model_call(
        tmp_path, patched_looper):
    """The recipe writes to ${OUTPUT_PATH} but the policy injects under
    GREETING_FILE: the pass must refuse before goose is ever invoked."""
    engine = _engine_with_recipe(
        tmp_path,
        "Write to ${OUTPUT_PATH}.",
        BranchPolicy(output_path=lambda p: Path("/tmp/x/a.txt"),
                     output_env="GREETING_FILE"),
    )
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    with pytest.raises(RuntimeError, match=r"GREETING_FILE"):
        looper.begin_loop()
    assert patched_looper.calls == []


def test_output_env_match_passes_verification(tmp_path, patched_looper):
    """${GREETING_FILE} in the prompt satisfies the check; the pass runs."""
    engine = _engine_with_recipe(
        tmp_path,
        "Write to ${GREETING_FILE}.",
        BranchPolicy(output_path=lambda p: Path("/tmp/x/a.txt"),
                     output_env="GREETING_FILE"),
    )
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    result = looper.begin_loop()
    assert result["review_status"] == "done"


def test_output_env_bare_dollar_form_accepted(tmp_path):
    """substitute_env resolves both ${VAR} and $VAR, so both satisfy the
    contract check — but $VARSUFFIX must not count as a $VAR reference."""
    from gooseloop.looper import _prompt_references_var
    assert _prompt_references_var("write to ${GREETING_FILE}", "GREETING_FILE")
    assert _prompt_references_var("write to $GREETING_FILE now", "GREETING_FILE")
    assert not _prompt_references_var("write to $GREETING_FILES", "GREETING_FILE")
    assert not _prompt_references_var("no reference here", "GREETING_FILE")


def test_output_env_missing_recipe_file_is_skipped(tmp_path, patched_looper):
    """A registered policy whose recipe file doesn't exist on disk is not
    checked: routing to it fails loud on its own, and test doubles register
    policies for recipes never read from disk."""
    engine = _RecordingEngine()  # greet.yaml resolves to a nonexistent path
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    looper._verify_output_env_contracts()  # must not raise


def test_output_env_invalid_name_refused(tmp_path):
    """output_env must be a valid env var name; ${GREETING-FILE} would
    never substitute, so the policy is refused up front."""
    engine = _engine_with_recipe(
        tmp_path,
        "Write to ${GREETING-FILE}.",
        BranchPolicy(output_path=lambda p: Path("/tmp/x/a.txt"),
                     output_env="GREETING-FILE"),
    )
    looper = GooseLooper(
        engine=engine,
        environment=_SilentEnv(),
        config=_make_config(tmp_path),
        save=False,
    )
    with pytest.raises(RuntimeError, match="valid env var name"):
        looper._verify_output_env_contracts()


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
    _patch_goose(monkeypatch, canned)
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
    def fake_run(recipe_path, model, extra_env=None, *, stats=None,
                 max_retries=6, base_delay=5, success_predicate=None,
                 label=None, sandbox=None):
        calls.append(recipe_path)
        # Simulate retry behaviour of the real run_goose_with_retry:
        # the predicate fires per attempt and gates retry.
        if success_predicate is not None and not success_predicate(truncated):
            raise RuntimeError("simulated max-retries exhausted")
        return truncated

    _patch_goose(monkeypatch, fake_run)

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
    _patch_goose(monkeypatch, canned)
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
    _patch_goose(monkeypatch, canned)
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


def test_local_overlay_for_finds_the_dot_local_sibling(tmp_path):
    """Regression 2026-07-12: the candidate was built with with_suffix,
    which treats ".local" as a suffix and REPLACES it — collapsing
    review.local.yaml back to review.yaml. The base file always exists,
    so the "local overlay" was the base merged with itself and the
    .local.yaml convention (ADR 0008 layer 2) silently never applied."""
    from gooseloop.looper import _local_overlay_for

    base = tmp_path / "review.yaml"
    base.write_text("prompt: base\n")
    local = tmp_path / "review.local.yaml"
    local.write_text("prompt: local\n")

    assert _local_overlay_for(base) == local


def test_local_overlay_for_none_when_absent(tmp_path):
    from gooseloop.looper import _local_overlay_for

    base = tmp_path / "review.yaml"
    base.write_text("prompt: base\n")
    assert _local_overlay_for(base) is None
    assert _local_overlay_for(tmp_path / "review.json") is None


# ---- routing[] as plan of record (ADR 0013) --------------------------

def test_engine_body_recorded_in_routing_with_provenance(tmp_path, monkeypatch):
    """Engine-built body phases appear in the review's routing[] with
    routed_by="engine", AFTER the model's entries — the persisted review
    is the whole pass's plan, not just the model's slice."""
    canned = _CannedGoose({
        "review.yaml": REVIEW_OUTPUT,
        "greet": GREET_OUTPUT,
        "cadence": "did the cadence thing\n",
        "summary.yaml": SUMMARY_OUTPUT,
    })
    _patch_goose(monkeypatch, canned)

    engine = _RecordingEngine()
    cadence = Phase(name="cadence", recipe_path="recipes/cadence.yaml",
                    label="cadence[weekly]")
    original_pipeline = engine.pipeline
    engine.pipeline = lambda ctx: Pipeline(  # type: ignore[method-assign]
        review=original_pipeline(ctx).review,
        body=[cadence],
        summary=original_pipeline(ctx).summary,
    )
    looper = GooseLooper(
        engine=engine, environment=_SilentEnv(),
        config=_make_config(tmp_path), save=False,
    )
    result = looper.begin_loop()
    routing = result["review_output"]["routing"]

    assert [e["routed_by"] for e in routing] == ["model", "model", "engine"]
    engine_entry = routing[-1]
    assert engine_entry["recipe"] == "cadence"
    assert engine_entry["reason"] == "cadence[weekly]"
    # Record, never instruction: the cadence phase ran exactly once
    # (from pipeline.body), not twice.
    assert sum("cadence" in c for c in canned.calls) == 1


def test_engine_routing_entries_not_rebuilt_as_phases(tmp_path):
    """_build_body_phases must skip routed_by='engine' — those phases
    already exist in pipeline.body."""
    looper = GooseLooper(
        engine=_RecordingEngine(), environment=_SilentEnv(),
        config=_make_config(tmp_path), save=False,
    )
    phases = looper._build_body_phases([
        {"recipe": "greet", "params": {"name": "x"}, "reason": "", "routed_by": "model"},
        {"recipe": "greet", "params": {}, "reason": "engine-built", "routed_by": "engine"},
    ])
    assert len(phases) == 1


def test_partial_review_gets_no_engine_injection(tmp_path, monkeypatch):
    """A skipped body must not be claimed by the plan of record."""
    import json as _json
    partial = (
        "<<<DELIVERABLE_JSON>>>\n"
        + _json.dumps({
            "protocol_version": "1.0", "status": "partial",
            "summary": "s", "insights": [], "routing": [], "operator_actions": [],
        })
        + "\n<<<END_DELIVERABLE>>>\n"
    )
    canned = _CannedGoose({"review.yaml": partial, "summary.yaml": SUMMARY_OUTPUT})
    _patch_goose(monkeypatch, canned)

    engine = _RecordingEngine()
    original_pipeline = engine.pipeline
    engine.pipeline = lambda ctx: Pipeline(  # type: ignore[method-assign]
        review=original_pipeline(ctx).review,
        body=[Phase(name="cadence", recipe_path="recipes/cadence.yaml")],
        summary=None,
    )
    looper = GooseLooper(
        engine=engine, environment=_SilentEnv(),
        config=_make_config(tmp_path), save=False,
    )
    result = looper.begin_loop()
    assert result["review_output"]["routing"] == []
