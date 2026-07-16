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
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .boundary import bwrap_prefix, persist_masks, resolve_sandbox
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
    REVIEW_OUTPUT_CONTRACT,
    SUMMARY_OUTPUT_CONTRACT,
    OperatorAction,
    ProtocolVersionError,
    ReviewOutput,
    RoutingEntry,
    review_repair_prompt,
    validate_review,
)
from .recipe_merge import load_layered_recipe
from . import telemetry
from .guardrails import scan_and_redact
from .runlock import RunLock
from .session import log_step, new_session, record_base_env
from .extract import Extracted, extract_json_with_provenance, extract_summary_markdown
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
        engine_module: Optional[str] = None,
    ):
        self.engine = engine
        # The RESOLVED module the engine was loaded from (the CLI passes
        # it). The class's own __module__ is only a fallback — engines
        # routinely define their class in a submodule (engine.py) and
        # expose it at the package __init__, and run.lock/session.meta
        # should name the package the operator ran, not the file the
        # class happens to live in.
        self.engine_module = engine_module or type(engine).__module__
        self.environment = environment
        self.config = config or LooperConfig.load()
        self.model = model or engine.default_model() or self.config.default_model
        self.save = save
        self.validate = validate
        self.review_only = review_only
        self.review_overlays = review_overlays or []
        self.summary_overlays = summary_overlays or []
        # Telemetry side-channel, refreshed by every _invoke_recipe call
        # (ADR 0012): the env injected for the current phase, goose retry
        # stats, and the failure message when the invocation raised.
        self._event_seq = 0
        self._invoke_env: dict[str, str] = {}
        self._invoke_stats: dict[str, Any] = {}
        self._invoke_error: Optional[str] = None
        # The rendered recipe the CURRENT invocation handed to goose —
        # context blocks filled, env substituted. Captured before the
        # temp file evaporates, so every event keeps what the model SAW,
        # not just what it said (the input half of PROTOCOL section 14).
        self._invoke_prompt: Optional[str] = None
        # THE BOUNDARY (PROTOCOL section 15): bwrap prefix every goose
        # spawn runs under, resolved once per pass. None = unsandboxed.
        self._sandbox: Optional[list[str]] = None

    # ------------------------------------------------------------------
    # public entry

    def begin_loop(self) -> dict[str, Any]:
        """Run one pipeline pass. Returns accounting summary.

        Holds the loop root's run.lock for the whole pass (ADR 0010,
        PROTOCOL section 13): raises RunLockHeldError before any phase
        runs if another run is in flight. Library-level on purpose, so
        embedders get the same protection as the CLI.
        """
        lock = RunLock(self.config.anchor)
        lock.acquire(engine=self._engine_module(), session_id=None)
        try:
            return self._run_pass(lock)
        finally:
            lock.release()

    def _engine_module(self) -> str:
        return self.engine_module

    def _run_pass(self, lock: RunLock) -> dict[str, Any]:
        runner_start = time.perf_counter()
        goose_calls = 0
        actions_ran = 0
        actions_skipped = 0

        # THE BOUNDARY resolves before any session artifact exists: a
        # refusal (BoundaryUnavailableError) must leave nothing behind.
        boundary = resolve_sandbox(self.config.anchor)
        self._sandbox = boundary.prefix if boundary else None

        session_dir = (new_session(self.config.sessions_dir, self.model, self.engine.name,
                                   engine_module=self._engine_module())
                       if self.save else None)
        if session_dir:
            lock.annotate(session_id=session_dir.name)
        self._event_seq = 0

        if boundary:
            print(colored(
                f"  boundary: {len(boundary.masks)} paths masked (bwrap)",
                Color.CYAN,
            ))
        if session_dir:
            log_step(session_dir,
                     f"boundary: {len(boundary.masks)} paths masked (bwrap)"
                     if boundary else
                     "boundary: none (bubblewrap unavailable)")
            # The session keeps the mask MAP (patterns + exact paths), so
            # boundary anomalies diff across runs — and the map itself is
            # masked: a list of where secrets live is denied to the goose
            # the same as what it maps (PROTOCOL section 15).
            artifact = persist_masks(session_dir, boundary)
            if boundary and artifact is not None:
                self._sandbox = bwrap_prefix(boundary.masks + [artifact])

        env_paths = self.environment.env_vars() if self.environment else {}
        ctx = Context(
            model=self.model,
            session_dir=session_dir,
            base_env={**env_paths, **self.engine.base_env()},
            environment=self.environment,
        )
        if session_dir:
            # The session-constant half of the telemetry dimensionality
            # lives here once; per-phase events record only what varies
            # (ADR 0012).
            record_base_env(session_dir, ctx.base_env)

        if self.validate:
            banner(f"{self.engine.name}: precheck", Color.CYAN)
            try:
                self.engine.precheck(ctx)
            except Exception as e:
                print(colored(f"\nPrecheck failed: {e}", Color.RED), file=sys.stderr)
                raise

        self._verify_output_env_contracts()

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
                pipeline.review, ctx, body_queue, engine_body=pipeline.body,
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

        if session_dir:
            self._write_ledger(session_dir, ctx)

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

    def _write_ledger(self, session_dir: Path, ctx: Context) -> None:
        """Persist the FINAL operator_actions ledger and outputs list.

        review.json (if the engine writes one) is the review's ledger seed,
        frozen before any body phase runs. Body phases append to
        ctx.artifacts via add_operator_action/record_output for the rest of
        the pass, and until now that final, complete ledger existed only in
        the terminal footer — gone the moment the scrollback was. Same gap
        as summary.md, one layer deeper: a reader of the session folder
        alone (a dashboard, a future self) could not see what the pass
        actually decided needed sealing.
        """
        import json
        ledger_path = session_dir / "ledger.json"
        ledger_path.write_text(json.dumps({
            "operator_actions": list(ctx.artifacts.get("operator_actions", [])),
            "outputs_written": list(ctx.artifacts.get("outputs_written", [])),
        }, indent=2))
        log_step(session_dir, f"ledger: wrote {ledger_path}")

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
                    body_queue: deque[Phase], *,
                    engine_body: list[Phase] | None = None,
                    ) -> tuple[int, bool, str, ReviewOutput | None]:
        """Run review; parse output; seed ledger; spawn body children.

        `engine_body` is the pipeline's engine-built body — recorded into
        routing[] with routed_by="engine" (ADR 0013) so the persisted
        review is the whole pass's plan, not just the model's slice.

        Returns (goose_calls, succeeded, status, parsed_output).
        """
        # Review's planned total is structurally unknown: routing[] hasn't
        # run yet. Display "?" instead of a misleading partial count.
        self._announce(review.name, total="?")
        # Who owns "is this review valid?" — the feedback loop below, or the
        # goose-level guard predicate. They must not both, because the guard
        # (_review_output_valid) blind-retries the SAME prompt and then RAISES
        # on exhaustion, so _invoke_recipe returns None before the feedback loop
        # ever sees the bad output. So: with repair enabled, the loop owns it —
        # it re-prompts with the exact reason, which a weak model acts on. With
        # repair OFF, keep the guard: fail an unparseable/invalid attempt so
        # goose retries it (the pre-repair behavior, e.g. against a stream
        # truncation). Engines that set their own predicate always keep it.
        repair_enabled = self.config.retry.review_repair_attempts > 0
        review_with_guard = (
            review
            if review.success_predicate is not None or repair_enabled
            else dataclasses.replace(review, success_predicate=_review_output_valid)
        )
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        # Validate-and-repair loop. A review that fails to parse or fails the
        # schema is re-prompted with the EXACT rejection reason appended to the
        # output contract, not silently aborted. Weak models routinely emit the
        # wrong sentinels or an invented schema on the first shot and correct
        # once told precisely what was wrong. Total tries = 1 + repair attempts.
        attempts = 1 + max(0, self.config.retry.review_repair_attempts)
        suffix = REVIEW_OUTPUT_CONTRACT
        review_output: ReviewOutput | None = None
        extracted: Extracted | None = None
        output: str | None = None
        calls = 0
        last_error = "review produced no valid output"
        for attempt in range(attempts):
            output = self._invoke_recipe(
                review_with_guard, ctx,
                overlays=self.review_overlays, prompt_suffix=suffix,
            )
            calls += 1
            if output is None:
                # The goose call itself failed (exhausted transient retries);
                # re-prompting a dead invocation buys nothing.
                self._emit_phase_event(
                    ctx, review, kind="review", status="failed", started=started, t0=t0,
                    error=self._invoke_error,
                    transcript=self._invoke_stats.get("last_output"),
                )
                return calls, False, "error", None

            review_output, last_error, extracted = self._parse_review(output)
            if review_output is not None:
                break

            if ctx.session_dir:
                log_step(ctx.session_dir, f"review: rejected ({last_error})")
            if attempt + 1 < attempts:
                if ctx.session_dir:
                    log_step(ctx.session_dir,
                             f"review: repairing with feedback (attempt {attempt + 2}/{attempts})")
                suffix = f"{REVIEW_OUTPUT_CONTRACT}\n\n{review_repair_prompt(last_error)}"

        if review_output is None:
            print(colored(
                f"Review invalid after {attempts} attempt(s): {last_error}", Color.RED,
            ), file=sys.stderr)
            self._emit_phase_event(
                ctx, review, kind="review", status="failed", started=started, t0=t0,
                error=last_error, transcript=output,
            )
            return calls, False, "error", None

        if extracted is not None and not extracted.is_canonical:
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

        # ADR 0013: routing[] is the plan of record for the WHOLE pass.
        # Engine-built body phases are appended by the framework with
        # routed_by="engine" — deterministic facts recorded by the party
        # that owns them, never round-tripped through the model. Model
        # entries stay first: children run before cadence phases (ADR
        # 0006), so the list reads in execution order. Injected only when
        # the body will actually run.
        status = str(review_output.get("status", "done"))
        if status == "done" and engine_body and not self.review_only:
            review_output["routing"] = list(review_output.get("routing", [])) + [
                _engine_routing_entry(p) for p in engine_body
            ]

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

        self._emit_phase_event(
            ctx, review, kind="review", status="ok", started=started, t0=t0,
            transcript=output,
            actions=list(ctx.artifacts.get("operator_actions", [])),
        )
        return calls, True, status, review_output

    def _parse_review(self, output: str) -> tuple[ReviewOutput | None, str, Extracted | None]:
        """Extract + validate one review output. Pure (no logging/emit): returns
        (review_output or None, rejection_reason, extracted). rejection_reason is
        the exact text the repair loop feeds back to the model; extracted carries
        the wrapper provenance for the non-canonical warning on the winning try."""
        extracted = extract_json_with_provenance(output)
        if extracted is None:
            return None, ("no JSON found between <<<DELIVERABLE_JSON>>> and "
                          "<<<END_DELIVERABLE>>> (or any recognised wrapper)"), None
        try:
            return validate_review(extracted.payload), "", extracted
        except ProtocolVersionError as e:
            return None, f"protocol version mismatch: {e}", extracted
        except ValueError as e:
            return None, f"schema invalid: {e}", extracted

    def _build_body_phases(self, routing: list[RoutingEntry]) -> list[Phase]:
        """Build body Phases from review routing entries via BranchPolicy.

        Only routed_by="model" entries construct phases: "engine" entries
        record phases the engine already built into pipeline.body (ADR
        0013) — building them again would run the body twice.
        """
        out: list[Phase] = []
        for entry in routing:
            if entry.get("routed_by") == "engine":
                continue
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

        # If the policy can compute an output path, inject it under the
        # policy's output_env name (default OUTPUT_PATH) so the recipe
        # writes to exactly the file the predicate later checks. Without
        # this the recipe and the predicate could (and did) disagree on
        # filenames — recipe wrote ${SHA}.md, predicate looked for
        # <slug>-<sha8>.md, every successful write triggered a fake
        # "transient error" retry until max_retries. The recipe's reference
        # to ${<output_env>} is verified before the pass runs (ADR 0011,
        # _verify_output_env_contracts).
        out_path: Path | None = None
        if policy.output_path is not None:
            out_path = policy.output_path(params)
            if out_path is not None:
                param_env[policy.output_env] = str(out_path)

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
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()
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
            self._invoke_env, self._invoke_stats = {}, {}
            self._invoke_prompt = None
            self._emit_phase_event(
                ctx, phase, kind="body", status="skipped", started=started, t0=t0,
                skip_reason=(skip_result if isinstance(skip_result, str)
                             else "skip_if returned True"),
            )
            return 0, True, []  # skip counts as ok (handled by caller as skipped)

        outputs_before = len(ctx.artifacts.get("outputs_written", []))
        actions_before = len(ctx.artifacts.get("operator_actions", []))
        output = self._invoke_recipe(phase, ctx)
        if output is None:
            self._emit_phase_event(
                ctx, phase, kind="body", status="failed", started=started, t0=t0,
                error=self._invoke_error,
                transcript=self._invoke_stats.get("last_output"),
            )
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

        self._emit_phase_event(
            ctx, phase, kind="body", status="ok", started=started, t0=t0,
            transcript=output,
            outputs=list(ctx.artifacts.get("outputs_written", []))[outputs_before:],
            actions=list(ctx.artifacts.get("operator_actions", []))[actions_before:],
        )
        if ctx.session_dir:
            log_step(ctx.session_dir, f"Phase {phase.name} completed.")
        return 1, True, children

    # ------------------------------------------------------------------
    # phase telemetry (ADR 0012, PROTOCOL §14)

    def _emit_phase_event(self, ctx: Context, phase: Phase, *, kind: str,
                          status: str, started: str, t0: float,
                          transcript: Optional[str] = None,
                          outputs: Optional[list[Any]] = None,
                          actions: Optional[list[Any]] = None,
                          error: Optional[str] = None,
                          skip_reason: Optional[str] = None) -> None:
        """One wide event per phase, appended as it settles. Best-effort:
        telemetry never fails a pass the work itself did not fail."""
        if not ctx.session_dir:
            return
        # Egress tripwire (ADR 0014): secret-shaped values never persist,
        # and a hit turns into a flag + an operator action — the seal
        # queue goes red instead of the leak being a later discovery.
        flags: list[str] = []
        if transcript is not None:
            transcript, findings = scan_and_redact(transcript)
            if findings:
                kinds = ", ".join(f"{f.kind} ×{f.count}" for f in findings)
                flags.append(f"secret-like content redacted ({kinds})")
                action = {
                    "action": f"ROTATE CREDENTIALS: phase {phase.name} output "
                              f"contained secret-shaped content",
                    "why": f"{kinds} — values were redacted in the persisted "
                           f"transcript, but the phase's output already went "
                           f"to the model provider; treat them as compromised. "
                           f"Likely prompt injection or an over-permissive "
                           f"phase reading files it should not.",
                }
                ctx.add_operator_action(**action)
                actions = list(actions or []) + [action]
                log_step(ctx.session_dir, f"GUARDRAIL: {flags[0]} in phase {phase.name}")
        # Retry attempts get the same secret handling as the transcript
        # they almost became: a secret in attempt 2's output reached the
        # provider even if attempt 3 succeeded clean.
        attempt_log = list(getattr(self, "_invoke_stats", {}).get("attempt_log") or [])
        retry_kinds: list[str] = []
        for entry in attempt_log:
            if entry.get("output") is not None:
                redacted, r_findings = scan_and_redact(entry["output"])
                entry["output"] = redacted
                retry_kinds += [f"{f.kind} ×{f.count}" for f in r_findings]
        if retry_kinds:
            kinds = ", ".join(retry_kinds)
            flag = f"secret-like content redacted in retry attempts ({kinds})"
            flags.append(flag)
            action = {
                "action": f"ROTATE CREDENTIALS: phase {phase.name} retry "
                          f"attempts contained secret-shaped content",
                "why": f"{kinds} — a non-final attempt's output carried it, "
                       f"so it reached the model provider even though the "
                       f"phase eventually settled clean. Treat it as "
                       f"compromised.",
            }
            ctx.add_operator_action(**action)
            actions = list(actions or []) + [action]
            log_step(ctx.session_dir, f"GUARDRAIL: {flag} in phase {phase.name}")
        prompt = getattr(self, "_invoke_prompt", None)
        if prompt is not None:
            prompt, p_findings = scan_and_redact(prompt)
            if p_findings:
                kinds = ", ".join(f"{f.kind} ×{f.count}" for f in p_findings)
                flag = f"secret-like content redacted in prompt ({kinds})"
                flags.append(flag)
                action = {
                    "action": f"ROTATE CREDENTIALS: phase {phase.name} prompt "
                              f"contained secret-shaped content",
                    "why": f"{kinds} — a secret was pasted INTO the model's "
                           f"input (an env value, an env_method, or a context "
                           f"file carried it), so it reached the provider. "
                           f"Rotate it, then fix the source that pasted it.",
                }
                ctx.add_operator_action(**action)
                actions = list(actions or []) + [action]
                log_step(ctx.session_dir, f"GUARDRAIL: {flag} in phase {phase.name}")
        self._event_seq += 1
        telemetry.record_phase(
            ctx.session_dir,
            seq=self._event_seq,
            name=phase.name,
            kind=kind,
            recipe=str(phase.recipe_path),
            label=phase.label,
            status=status,
            started=started,
            duration_s=time.perf_counter() - t0,
            env=getattr(self, "_invoke_env", None),
            outputs=outputs,
            transcript_text=transcript,
            prompt_text=prompt,
            error=error,
            skip_reason=skip_reason,
            attempts=getattr(self, "_invoke_stats", {}).get("attempts"),
            attempt_log=attempt_log,
            actions=actions,
            flags=flags,
        )

    # ------------------------------------------------------------------
    # generic phase + recipe invocation

    def _run_phase(self, phase: Phase, ctx: Context, *,
                   overlays: list[Path] | None = None,
                   is_summary: bool = False) -> tuple[int, bool]:
        self._announce(phase.name)
        kind = "summary" if is_summary else "body"
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()
        outputs_before = len(ctx.artifacts.get("outputs_written", []))
        actions_before = len(ctx.artifacts.get("operator_actions", []))
        # The framework appends the summary output contract to every summary
        # prompt (ADR 0018), the summary-side analogue of the review contract:
        # the marker envelope can't depend on each private recipe copying it.
        suffix = SUMMARY_OUTPUT_CONTRACT if is_summary else ""
        output = self._invoke_recipe(phase, ctx, overlays=overlays, prompt_suffix=suffix)
        if output is None:
            self._emit_phase_event(
                ctx, phase, kind=kind, status="failed", started=started, t0=t0,
                error=self._invoke_error,
                transcript=self._invoke_stats.get("last_output"),
            )
            return 0, False
        if is_summary and ctx.session_dir:
            summary_path = ctx.session_dir / "summary.md"
            # summary.md is the operator-facing report, not the raw transcript
            # (ADR 0018): keep only what the recipe wrapped in
            # <<<SUMMARY_MD>>>…<<<END_SUMMARY>>>. The full verbatim output still
            # persists under transcripts/ (ADR 0012), so nothing is lost. No
            # marker → fall back to the whole output so a legacy or misbehaving
            # recipe still yields a durable summary (fail toward keeping content).
            report = extract_summary_markdown(output)
            if report is None:
                report = output
                log_step(ctx.session_dir,
                         "summary: no <<<SUMMARY_MD>>> marker; wrote full "
                         "transcript to summary.md (tighten the summary recipe)")
            redacted_summary, summary_findings = scan_and_redact(report)
            summary_path.write_text(redacted_summary)
            if summary_findings:
                log_step(ctx.session_dir,
                         "GUARDRAIL: secret-like content redacted in summary.md")
            log_step(ctx.session_dir, f"summary: wrote {summary_path}")
        if phase.post_process is not None:
            try:
                phase.post_process(output, ctx)
            except Exception as e:
                print(colored(f"\nPhase {phase.name} post_process raised: {e}", Color.RED),
                      file=sys.stderr)
        self._emit_phase_event(
            ctx, phase, kind=kind, status="ok", started=started, t0=t0,
            transcript=output,
            outputs=list(ctx.artifacts.get("outputs_written", []))[outputs_before:],
            actions=list(ctx.artifacts.get("operator_actions", []))[actions_before:],
        )
        if ctx.session_dir:
            log_step(ctx.session_dir, f"Phase {phase.name} completed.")
        return 1, True

    def _invoke_recipe(self, phase: Phase, ctx: Context, *,
                       overlays: list[Path] | None = None,
                       prompt_suffix: str = "") -> str | None:
        phase_env = phase.build_env(ctx)
        extra_env = {**ctx.base_env, **phase_env}
        # Telemetry side-channel (ADR 0012): what THIS invocation injected
        # and how goose behaved, readable by _emit_phase_event afterwards.
        self._invoke_env = phase_env
        self._invoke_stats = {}
        self._invoke_error = None
        self._invoke_prompt = None
        try:
            with prepared_recipe(
                Path(phase.recipe_path),
                extra_env,
                environment=self.environment,
                local_path=_local_overlay_for(Path(phase.recipe_path)),
                overlay_paths=overlays or [],
                prompt_suffix=prompt_suffix,
            ) as effective_path:
                try:
                    self._invoke_prompt = Path(effective_path).read_text()
                except OSError:
                    self._invoke_prompt = None
                return run_goose_with_retry(
                    effective_path,
                    self.model,
                    extra_env=extra_env,
                    max_retries=self.config.retry.max_retries,
                    base_delay=self.config.retry.base_delay,
                    success_predicate=phase.success_predicate,
                    label=phase.label or recipe_label(phase.recipe_path),
                    stats=self._invoke_stats,
                    sandbox=self._sandbox,
                )
        except RuntimeError as e:
            print(colored(f"\nPhase {phase.name} failed: {e}", Color.RED), file=sys.stderr)
            self._invoke_error = str(e)
            if ctx.session_dir:
                log_step(ctx.session_dir, f"Phase {phase.name} FAILED: {e}")
            return None

    # ------------------------------------------------------------------
    # ADR 0011: output_env contract verification

    def _verify_output_env_contracts(self) -> None:
        """Refuse the pass if a policy's output_env is never referenced by
        its recipe (ADR 0011).

        Only policies that compute an output path are checked: they are the
        ones whose injected env var, success predicate, and ledger entry
        must agree with the recipe's write target. The check reads the
        merged recipe (base + .local overlay) BEFORE env substitution —
        rendering replaces ${VAR} with its value, so the reference only
        exists in the pre-substitution prompt. Recipes whose file does not
        exist are skipped: routing to them fails loud on its own, and test
        doubles register policies for recipes never read from disk.
        """
        for recipe_name, policy in self.engine.branch_policies.items():
            if policy.output_path is None:
                continue
            if not _ENV_NAME_RE.match(policy.output_env):
                raise RuntimeError(
                    f"BranchPolicy for {recipe_name!r}: output_env "
                    f"{policy.output_env!r} is not a valid env var name "
                    f"(letters, digits, underscore; no leading digit)."
                )
            recipe_path = Path(self._resolve_recipe_path(recipe_name))
            if not recipe_path.exists():
                continue
            merged = load_layered_recipe(
                recipe_path,
                local_path=_local_overlay_for(recipe_path),
            )
            prompt = str(merged.get("prompt") or "")
            if not _prompt_references_var(prompt, policy.output_env):
                raise RuntimeError(
                    f"recipe {recipe_path} never references "
                    f"${{{policy.output_env}}}, but the BranchPolicy for "
                    f"{recipe_name!r} injects the output path under that "
                    f"name. The recipe's write target and the framework's "
                    f"success check would silently disagree. Fix the recipe "
                    f"to write to ${{{policy.output_env}}}, or set the "
                    f"policy's output_env to the variable the recipe "
                    f"actually uses (ADR 0011)."
                )

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


def _review_output_valid(output: str) -> bool:
    """Default review retry gate: canonical framing plus the full schema.

    A fallback Markdown fence or a JSON object missing load-bearing keys is not
    a successful model attempt. Returning False lets the existing retry loop try
    again instead of accepting a superficially parseable answer and failing only
    after retries are no longer available.
    """
    extracted = extract_json_with_provenance(output)
    if extracted is None or not extracted.is_canonical:
        return False
    try:
        validate_review(extracted.payload)
    except (ProtocolVersionError, ValueError):
        return False
    return True


_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _prompt_references_var(prompt: str, var: str) -> bool:
    """True if the prompt references ${VAR} or bare $VAR.

    Both spellings are what substitute_env resolves at render time, so
    both satisfy the ADR 0011 contract check.
    """
    pattern = r"\$\{" + re.escape(var) + r"\}|\$" + re.escape(var) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, prompt) is not None


def _engine_routing_entry(phase: Phase) -> RoutingEntry:
    """A routing[] record for one engine-built body phase (ADR 0013).

    Engine phases carry a build_env closure rather than a params dict, so
    the record is recipe + reason (the phase's label or name) — enough
    for the plan of record; params stay the model-routing shape's field.
    """
    return {
        "recipe": Path(phase.recipe_path).stem,
        "params": {},
        "reason": phase.label or phase.name,
        "routed_by": "engine",
    }


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
