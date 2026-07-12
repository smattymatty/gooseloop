"""GooseLooper — drive an Engine's Pipeline for one pass.

Order per ADR 0006:

    1. Build Context (model, session dir, base_env, environment).
    2. engine.precheck(ctx). Raise aborts the pass.
    3. pipeline = engine.pipeline(ctx). Must be a Pipeline dataclass.
    4. Run review FIRST. The framework wraps the engine's review post_process
       to also parse the deliverable JSON, validate the schema, build
       child phases from routing[] via engine.branch_policies, and seed
       the operator_actions ledger from the review's payload.
    5. Drain the body queue. Review-spawned children sit at HEAD; engine
       cadence phases follow. Body phase post_process may enqueue more.
    6. Run summary LAST, with the full ledger and outputs available via ctx.
    7. Print session footer.

The looper knows nothing about prospects, scoring, or any engine concept.
"""

import dataclasses
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from .branch_policy import BranchPolicy
from .config import LooperConfig
from .context_prepend import prepared_recipe
from .engine import Engine
from .environment import Environment
from .footer import print_session_footer, recipe_label
from .goose import run_goose_with_retry
from .phase import Context, Phase, Pipeline
from .predicates import file_nonempty
from .protocol import (
    PROTOCOL_VERSION,
    OperatorAction,
    ProtocolVersionError,
    ReviewOutput,
    RoutingEntry,
    validate_review,
)
from .session import log_step, new_session
from .extract import extract_json_with_provenance
from .text import Color, banner, colored


class GooseLooper:
    """Execution shell for an Engine's Pipeline.

    Usage:
        GooseLooper(engine=MyEngine(), environment=MyEnv()).begin_loop()

    Parameters:
        engine: the Engine instance driving this pass.
        environment: the Environment instance (None = engine recipes that
            never reference env_method are still runnable).
        config: LooperConfig instance. None = LooperConfig.load() from cwd.
        model: override the model. Falls back to engine.default_model(),
            then to config.default_model.
        save: write a session folder (default True).
        validate: run engine.precheck() before the pipeline (default True).
        review_only: stop after the review phase (default False).
        review_overlays: list of CLI overlay paths for the review recipe.
        summary_overlays: list of CLI overlay paths for the summary recipe.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        environment: Optional[Environment] = None,
        config: Optional[LooperConfig] = None,
        model: Optional[str] = None,
        save: bool = True,
        validate: bool = True,
        review_only: bool = False,
        review_overlays: Optional[list[Path]] = None,
        summary_overlays: Optional[list[Path]] = None,
    ):
        self.engine = engine
        self.environment = environment
        self.config = config or LooperConfig.load()
        self.model = model or engine.default_model() or self.config.default_model
        self.save = save
        self.validate = validate
        self.review_only = review_only
        self.review_overlays = review_overlays or []
        self.summary_overlays = summary_overlays or []

    # ------------------------------------------------------------------
    # public entry

    def begin_loop(self) -> dict[str, Any]:
        """Run one pipeline pass. Returns accounting summary."""
        runner_start = time.perf_counter()
        goose_calls = 0
        actions_ran = 0
        actions_skipped = 0

        session_dir = (new_session(self.config.sessions_dir, self.model, self.engine.name)
                       if self.save else None)

        env_paths = self.environment.env_vars() if self.environment else {}
        ctx = Context(
            model=self.model,
            session_dir=session_dir,
            base_env={**env_paths, **self.engine.base_env()},
            environment=self.environment,
        )

        if self.validate:
            banner(f"{self.engine.name}: precheck", Color.CYAN)
            try:
                self.engine.precheck(ctx)
            except Exception as e:
                print(colored(f"\nPrecheck failed: {e}", Color.RED), file=sys.stderr)
                raise

        pipeline = self.engine.pipeline(ctx)
        if not isinstance(pipeline, Pipeline):
            raise TypeError(
                f"Engine.pipeline() must return Pipeline, got {type(pipeline).__name__}"
            )

        # Step + planned counters. Methods bump them; banners read them
        # to render `[step/planned]` progress in the phase header.
        self._step = 0
        self._planned = 1 + len(pipeline.body) + (1 if pipeline.summary else 0)

        body_queue: deque[Phase] = deque()

        # --- 1. review ------------------------------------------------
        review_status = "done"
        review_output: ReviewOutput | None = None

        try:
            review_calls, review_ok, review_status, review_output = self._run_review(
                pipeline.review, ctx, body_queue,
            )
            goose_calls += review_calls
            # Children the review spawned via routing[] need to count as
            # planned work. Read straight from the queue before any cadence
            # phases get appended.
            self._planned += len(body_queue)
            if review_ok:
                actions_ran += 1
            else:
                actions_skipped += 1
        except Exception as e:
            print(colored(
                f"\nReview failed: {type(e).__name__}: {e}",
                Color.RED,
            ), file=sys.stderr)
            if os.environ.get("GOOSELOOP_DEBUG"):
                import traceback
                traceback.print_exc(file=sys.stderr)
            actions_skipped += 1
            review_status = "error"

        # --- 2. body --------------------------------------------------
        if self.review_only:
            if session_dir:
                log_step(session_dir, "review-only: skipping body and summary")
        elif review_status == "error":
            if session_dir:
                log_step(session_dir, "review status=error: skipping body and summary")
            actions_skipped += len(pipeline.body) + (1 if pipeline.summary else 0)
        else:
            # Engine-declared cadence phases follow review-spawned children.
            body_queue.extend(pipeline.body)
            body_calls, body_ran, body_skipped = self._drain_body(body_queue, ctx)
            goose_calls += body_calls
            actions_ran += body_ran
            actions_skipped += body_skipped

            # --- 3. summary -------------------------------------------
            if pipeline.summary is not None and review_status != "partial":
                try:
                    summary_calls, summary_ok = self._run_phase(
                        pipeline.summary, ctx,
                        overlays=self.summary_overlays,
                        is_summary=True,
                    )
                    goose_calls += summary_calls
                    if summary_ok:
                        actions_ran += 1
                    else:
                        actions_skipped += 1
                except RuntimeError as e:
                    print(colored(f"\nSummary failed: {e}", Color.RED), file=sys.stderr)
                    actions_skipped += 1
            elif pipeline.summary is not None and review_status == "partial":
                if session_dir:
                    log_step(session_dir, "review status=partial: skipping summary")
                actions_skipped += 1

        print_session_footer(
            elapsed=time.perf_counter() - runner_start,
            goose_calls=goose_calls,
            actions_planned=self._planned,
            actions_ran=actions_ran,
            actions_skipped=actions_skipped,
            outputs=list(ctx.artifacts.get("outputs_written", [])),
            operator_actions=list(ctx.artifacts.get("operator_actions", [])),
            session_dir=session_dir,
        )

        return {
            "goose_calls": goose_calls,
            "actions_planned": self._planned,
            "actions_ran": actions_ran,
            "actions_skipped": actions_skipped,
            "outputs": list(ctx.artifacts.get("outputs_written", [])),
            "operator_actions": list(ctx.artifacts.get("operator_actions", [])),
            "review_status": review_status,
            "review_output": review_output,
            "session_dir": session_dir,
        }

    # ------------------------------------------------------------------
    # progress

    def _announce(self, phase_name: str, *,
                  total: str | None = None,
                  color: str = Color.MAGENTA) -> None:
        """Bump the step counter and print the phase banner with `[N/M]`.

        N is monotonically increasing across the whole pass (review +
        body + summary). M is the current planned total, which grows
        when review-spawned or body-spawned children land in the queue.
        Mid-run growth of M is expected: a `[3/5]` followed by `[4/7]`
        means the previous phase spawned two more.

        `total` overrides the M display when the caller knows the
        planned count is structurally unknowable at this point — the
        review phase, specifically, runs *before* its routing[] spawns
        body children, so the framework can't truthfully claim a total
        for it. Review calls with total="?".
        """
        self._step += 1
        total_display = total if total is not None else str(self._planned)
        # · separator survives banner()'s word-rewrap (it splits on
        # whitespace and rejoins with single spaces); double-space would
        # collapse, leaving "review [1/2]" instead of the intended layout.
        banner(
            f"{self.engine.name}: {phase_name} · [{self._step}/{total_display}]",
            color,
        )

    # ------------------------------------------------------------------
    # review

    def _run_review(self, review: Phase, ctx: Context,
                    body_queue: deque[Phase]) -> tuple[int, bool, str, ReviewOutput | None]:
        """Run review; parse output; seed ledger; spawn body children.

        Returns (goose_calls, succeeded, status, parsed_output).
        """
        # Review's planned total is structurally unknown: routing[] hasn't
        # run yet. Display "?" instead of a misleading partial count.
        self._announce(review.name, total="?")
        # Wrap the review's success_predicate so the retry loop fails any
        # attempt that didn't emit parseable wrapped JSON. Without this,
        # a mid-stream truncation (e.g. provider stream decode error) gets
        # accepted as "success" by the default transient-error check, the
        # downstream parse fails, and retries are off the table by then.
        # Engines that explicitly set their own predicate keep it.
        review_with_guard = (
            review if review.success_predicate is not None
            else dataclasses.replace(review, success_predicate=_review_output_parseable)
        )
        output = self._invoke_recipe(review_with_guard, ctx, overlays=self.review_overlays)
        if output is None:
            return 0, False, "error", None

        extracted = extract_json_with_provenance(output)
        if extracted is None:
            print(colored(
                "Review did not emit recognisable wrapped JSON; cannot parse.",
                Color.RED,
            ), file=sys.stderr)
            if ctx.session_dir:
                log_step(ctx.session_dir, "review: no wrapped JSON found by any recognizer")
            return 1, False, "error", None

        if not extracted.is_canonical:
            msg = (
                f"review parsed via {extracted.recognizer} (non-canonical wrapper). "
                f"Tighten the review recipe to use <<<DELIVERABLE_JSON>>> / "
                f"<<<END_DELIVERABLE>>> for stable runs."
            )
            print(colored(msg, Color.YELLOW), file=sys.stderr)
            if ctx.session_dir:
                log_step(ctx.session_dir, f"review: {msg}")
            ctx.add_operator_action(
                action="tighten review recipe to canonical sentinels",
                why=f"review parsed via {extracted.recognizer}; "
                    f"weaker models will drift further if the recipe stays ambiguous",
                recognizer=extracted.recognizer,
            )

        try:
            review_output = validate_review(extracted.payload)
        except ProtocolVersionError as e:
            print(colored(f"Review protocol mismatch: {e}", Color.RED), file=sys.stderr)
            if ctx.session_dir:
                log_step(ctx.session_dir, f"review: protocol mismatch ({e})")
            return 1, False, "error", None
        except ValueError as e:
            print(colored(f"Review schema invalid: {e}", Color.RED), file=sys.stderr)
            if ctx.session_dir:
                log_step(ctx.session_dir, f"review: schema invalid ({e})")
            return 1, False, "error", None

        # Stash the full payload for engine extensions to read.
        ctx.artifacts["review_output"] = dict(review_output)
        ctx.artifacts["review_routing"] = list(review_output.get("routing", []))

        # Seed the ledger from the review's operator_actions. validate_review
        # already normalised entries to {action: str, why: str, ...extras}, so
        # the loop trusts that shape and just guards against an empty action.
        for entry in review_output.get("operator_actions", []):
            action = entry.get("action", "")
            why = entry.get("why", "")
            if not action:
                continue
            extras = {k: v for k, v in entry.items() if k not in ("action", "why")}
            ctx.add_operator_action(action=action, why=why, **extras)

        # Persist the review JSON to the session for downstream phases.
        if ctx.session_dir:
            actions_dir = ctx.session_dir / "actions"
            actions_dir.mkdir(parents=True, exist_ok=True)
            review_path = actions_dir / "review.json"
            import json
            review_path.write_text(json.dumps(review_output, indent=2))
            ctx.base_env["REVIEW_JSON_PATH"] = str(review_path)
            log_step(ctx.session_dir, f"review: wrote {review_path}")

        # Spawn body children from routing[].
        status = str(review_output.get("status", "done"))
        if status == "done":
            children = self._build_body_phases(review_output.get("routing", []))
            body_queue.extend(children)
        elif status == "partial":
            if ctx.session_dir:
                log_step(ctx.session_dir, "review status=partial: skipping routing")

        # Engine post_process for the review (if any) runs after parsing.
        if review.post_process is not None:
            extra_children = review.post_process(output, ctx)
            if extra_children:
                body_queue.extend(extra_children)

        return 1, True, status, review_output

    def _build_body_phases(self, routing: list[RoutingEntry]) -> list[Phase]:
        """Build body Phases from review routing entries via BranchPolicy."""
        out: list[Phase] = []
        for entry in routing:
            recipe = entry.get("recipe")
            if not isinstance(recipe, str):
                continue
            params: dict[str, Any] = entry.get("params") or {}
            policy = self.engine.branch_policies.get(recipe, BranchPolicy())
            phase = self._phase_from_routing(recipe, params, policy)
            out.append(phase)
        return out

    def _phase_from_routing(self, recipe: str, params: dict[str, Any],
                            policy: BranchPolicy) -> Phase:
        recipe_path = self._resolve_recipe_path(recipe)
        param_env = _params_to_env(params)

        # If the policy can compute an output path, inject it as OUTPUT_PATH
        # so the recipe writes to exactly the file the predicate later checks.
        # Without this the recipe and the predicate could (and did) disagree
        # on filenames — recipe wrote ${SHA}.md, predicate looked for
        # <slug>-<sha8>.md, every successful write triggered a fake "transient
        # error" retry until max_retries.
        out_path: Path | None = None
        if policy.output_path is not None:
            out_path = policy.output_path(params)
            if out_path is not None:
                param_env["OUTPUT_PATH"] = str(out_path)

        if policy.predicate is not None:
            predicate: Optional[Callable[[str], bool]] = policy.predicate
        elif out_path is not None:
            predicate = file_nonempty(out_path)
        else:
            predicate = None

        skip = None
        if policy.skip_when is not None:
            captured_params = dict(params)
            skip = lambda _ctx, _p=captured_params: policy.skip_when(_p)  # noqa: E731

        post = None
        if policy.output_path is not None:
            captured_params = dict(params)
            def post(_out: str, ctx: Context, _p: dict[str, Any] = captured_params) -> None:
                op = policy.output_path(_p) if policy.output_path else None
                if op is not None:
                    ctx.record_output(op)
                return None

        label = f"{recipe}[{params.get('slug') or 'branch'}]" \
            if params.get("slug") else None

        def build_env(_ctx: Context, _e: dict[str, str] = param_env) -> dict[str, str]:
            return dict(_e)

        return Phase(
            name=f"branch:{recipe}",
            recipe_path=str(recipe_path),
            build_env=build_env,
            success_predicate=predicate,
            post_process=post,
            skip_if=skip,
            label=label,
        )

    # ------------------------------------------------------------------
    # body

    def _drain_body(self, queue: deque[Phase], ctx: Context) -> tuple[int, int, int]:
        """Drain the body queue. Children spawned via post_process land at HEAD.

        Bumps self._planned when post_process spawns new phases so the
        `[step/planned]` banner stays accurate mid-run.
        """
        calls = 0
        ran = 0
        skipped = 0
        executed = 0

        while queue:
            if executed >= self.config.max_queue_depth:
                print(colored(
                    f"\nQueue depth cap ({self.config.max_queue_depth}) hit; "
                    f"aborting remaining phases.",
                    Color.RED,
                ), file=sys.stderr)
                break

            phase = queue.popleft()
            executed += 1
            phase_calls, ok, children = self._run_phase_with_children(phase, ctx)
            calls += phase_calls
            if ok:
                ran += 1
            else:
                skipped += 1
            if children:
                self._planned += len(children)
                queue.extendleft(reversed(children))

        return calls, ran, skipped

    def _run_phase_with_children(self, phase: Phase, ctx: Context) -> tuple[int, bool, list[Phase]]:
        self._announce(phase.name)
        # skip_if check (after the banner so the operator sees which phase
        # is being skipped and why).
        skip_result = phase.skip_if(ctx) if phase.skip_if is not None else None
        if skip_result:
            msg = (f"Skipped phase {phase.name}: {skip_result}"
                   if isinstance(skip_result, str)
                   else f"Skipped phase {phase.name} (skip_if returned True)")
            print(colored(msg, Color.YELLOW))
            if ctx.session_dir:
                log_step(ctx.session_dir, msg)
            return 0, True, []  # skip counts as ok (handled by caller as skipped)

        output = self._invoke_recipe(phase, ctx)
        if output is None:
            return 0, False, []

        children: list[Phase] = []
        if phase.post_process is not None:
            try:
                returned = phase.post_process(output, ctx)
                if returned:
                    children = list(returned)
            except Exception as e:
                print(colored(f"\nPhase {phase.name} post_process raised: {e}", Color.RED),
                      file=sys.stderr)
                if ctx.session_dir:
                    log_step(ctx.session_dir, f"Phase {phase.name} post_process raised: {e}")

        if ctx.session_dir:
            log_step(ctx.session_dir, f"Phase {phase.name} completed.")
        return 1, True, children

    # ------------------------------------------------------------------
    # generic phase + recipe invocation

    def _run_phase(self, phase: Phase, ctx: Context, *,
                   overlays: list[Path] | None = None,
                   is_summary: bool = False) -> tuple[int, bool]:
        self._announce(phase.name)
        output = self._invoke_recipe(phase, ctx, overlays=overlays)
        if output is None:
            return 0, False
        if phase.post_process is not None:
            try:
                phase.post_process(output, ctx)
            except Exception as e:
                print(colored(f"\nPhase {phase.name} post_process raised: {e}", Color.RED),
                      file=sys.stderr)
        if ctx.session_dir:
            log_step(ctx.session_dir, f"Phase {phase.name} completed.")
        return 1, True

    def _invoke_recipe(self, phase: Phase, ctx: Context, *,
                       overlays: list[Path] | None = None) -> str | None:
        phase_env = phase.build_env(ctx)
        extra_env = {**ctx.base_env, **phase_env}
        try:
            with prepared_recipe(
                Path(phase.recipe_path),
                extra_env,
                environment=self.environment,
                local_path=_local_overlay_for(Path(phase.recipe_path)),
                overlay_paths=overlays or [],
            ) as effective_path:
                return run_goose_with_retry(
                    effective_path,
                    self.model,
                    extra_env=extra_env,
                    max_retries=self.config.retry.max_retries,
                    base_delay=self.config.retry.base_delay,
                    success_predicate=phase.success_predicate,
                    label=phase.label or recipe_label(phase.recipe_path),
                )
        except RuntimeError as e:
            print(colored(f"\nPhase {phase.name} failed: {e}", Color.RED), file=sys.stderr)
            if ctx.session_dir:
                log_step(ctx.session_dir, f"Phase {phase.name} FAILED: {e}")
            return None

    # ------------------------------------------------------------------

    def _resolve_recipe_path(self, recipe: str) -> str:
        """Look up a body recipe by name in the engine's recipes directory.

        Accepts a bare name ("to-outreach") or a full path
        ("recipes/to-outreach.yaml"). Bare names resolve against
        engine.recipes_dir() with .yaml suffix.
        """
        if recipe.endswith(".yaml") or recipe.endswith(".yml") or "/" in recipe:
            return recipe
        return f"{self.engine.recipes_dir()}/{recipe}.yaml"


def _review_output_parseable(output: str) -> bool:
    """Default review success_predicate: at least extract_json must succeed.

    Catches mid-stream truncation (provider decode error after partial
    output) and other "looks fine to goose but doesn't parse" failures.
    Validation of required keys + status enum + protocol version happens
    in validate_review after extraction; this predicate's only job is
    "is there enough output to try."
    """
    return extract_json_with_provenance(output) is not None


def _params_to_env(params: dict[str, Any]) -> dict[str, str]:
    """Convert routing params to env vars. Keys uppercased; values stringified."""
    out: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        out[str(k).upper()] = str(v)
    return out


def _local_overlay_for(recipe_path: Path) -> Path | None:
    """Compute the conventional .local.yaml sibling for an overlay base.

    Built with with_name, not with_suffix: with_suffix on "review.local"
    treats ".local" as the suffix and REPLACES it, collapsing the
    candidate back to the base path (the bug that silently disabled
    local overlays until 2026-07-12).
    """
    if recipe_path.suffix not in (".yaml", ".yml"):
        return None
    candidate = recipe_path.with_name(
        recipe_path.stem + ".local" + recipe_path.suffix
    )
    return candidate if candidate.exists() else None
