## ADR 0006 — Pipeline named slots; framework owns review/summary order

**Status:** Accepted (2026-06-04)
**Context:** OSS-extraction design review; partially supersedes [ADR 0001](0001-engine-returns-pipeline-of-phases.md)

## Context

ADR 0001 placed phase ordering on the engine: `Engine.pipeline(ctx) -> list[Phase]`, the looper runs the list in order, post_process can spawn children. The rationale was that future engines would have nothing in common with the origin customer pipeline at the phase level, so the looper shouldn't bake in a canonical shape.

In practice, both shipped use cases — the origin customer pipeline and the upcoming Claude design-handoff engine — have the same bookend shape:

- **Review phase first**: assesses state, emits a structured routing plan + initial operator-action list.
- **Body phases**: do the work the review routed.
- **Summary phase last**: renders the final ledger for the operator.

The 2026-06-04 design review elevated this convention to a framework-level contract for three reasons:

1. **User-procurable review/summary recipes.** The operator wants users to drop in custom review pipelines (`review.audit.yaml`, `review.daily.yaml`) and have the looper just work. That requires the framework to know what a review is and where it sits.
2. **Mutable session ledger.** Review initializes `operator_actions[]`; body phases append to it; summary reads the final state. The summary's role as "renders the last ledger" only makes sense if it's structurally last.
3. **Cross-engine portability of bookend recipes.** A community-authored `review.yaml` written against the framework schema can be consumed by any engine that conforms.

ADR 0001's rejected alternatives section explicitly considered "Looper hardcodes canonical shape (review → branches → periodic → summary), engine fills hooks" and refused it because of the OSS abstraction-leak concern. This ADR re-opens that question and reaches a different conclusion: the OSS leak is avoided not by leaving ordering to engines but by making the bookend recipes themselves user-procurable (per ADR 0008's overlay merge).

## Decision

`Engine.pipeline()` returns a `Pipeline` dataclass, not `list[Phase]`:

```python
@dataclass
class Pipeline:
    review: Phase
    body: list[Phase]
    summary: Phase
```

The framework runs phases in this order, with no exceptions:

1. `review` phase runs first. Its `post_process` returns child Phases (built by the framework from `review.routing[]` per ADR 0007, using the engine's `BranchPolicy` registry).
2. Children are inserted at the HEAD of `body`. They run before engine-declared cadence phases. This preserves the existing `looper.py:185` `extendleft(reversed(children))` semantics.
3. `body` runs to completion (queue empties, modulo skips and the `max_queue_depth` cap).
4. `summary` runs last.

`body` may be empty. An engine with no engine-declared cadence work returns `Pipeline(review=..., body=[], summary=...)`; all body phases come from the review's routing.

The `--review-only` flag is reinterpreted: stop after the review phase. Body and summary skip.

## Consequences

**Good:**

- Structural enforcement of review/summary presence. An engine cannot accidentally omit either; the type system refuses.
- Summary always sees the final ledger because it's structurally last.
- User-procurable `review.yaml` and `summary.yaml` recipes (per ADR 0007's schema) are cross-engine portable.
- The framework's `__main__.py` (the `gooseloop` CLI) knows enough to render a uniform session ledger UI, regardless of which engine is loaded.

**Tradeoffs:**

- Engines that genuinely have no summary obligation must ship a trivial summary recipe (one that emits a one-line acknowledgement). Cheap; one YAML file. The framework's "hello-world" reference engine demonstrates the trivial case.
- ADR 0001's "engine owns phase order" is partially superseded: engine still owns phase *content* (which recipes, what env, what predicates), but no longer owns ordering at the bookends. The body remains engine-ordered.
- Origin migration: `CustomerAcquisitionEngine.pipeline()` returns `Pipeline(review=self._review_phase(), body=[self._weekly_phase(), self._monthly_phase(), self._competitor_watch_phase(), self._narrative_watch_phase()], summary=self._session_summary_phase())`. The five phase factories don't change shape; only how they're collected.

## Migration plan

1. Update `gooseloop/phase.py` to add the `Pipeline` dataclass alongside `Phase` and `Context`.
2. Update `gooseloop/engine.py` ABC: `pipeline(ctx) -> Pipeline` instead of `-> list[Phase]`.
3. Update `gooseloop/looper.py` to consume the new shape: run review first, drain queue (which body + review-spawned children share), run summary last.
4. Update the origin engine's `pipeline()` to return a `Pipeline(...)` instead of a list.
5. Update tests: `test_runner.py` and `test_environment.py` use the new shape. The no-op engine in `test_environment.py` returns `Pipeline(review=trivial, body=[], summary=trivial)`.

## Alternatives considered

- **Phase metadata role: Literal["review", "body", "summary"]** on the existing `list[Phase]` (soft contract via metadata). Rejected: the ordering invariants get enforced as a runtime check rather than a type, and the "exactly one review at index 0, exactly one summary at index -1" constraint is fragile.
- **Declarative TOML manifest of phases** (engine just supplies post_process hooks per named recipe slot). Rejected: pushes Python logic into config without buying enough — the engine still has to ship per-phase post_process anyway, and the TOML adds a layer with no offsetting clarity.
- **Framework-owned review/summary recipes only; engine has zero say in either.** Rejected: engines often need to attach engine-specific `post_process` hooks (snapshot scores, validate review JSON shape, etc.). Engines should be able to wrap the user-procurable recipes with engine logic, not be locked out.
- **Keep ADR 0001 unchanged; convention enforced via documentation.** Rejected: the design review specifically asked for the framework to enforce this. Convention-only would mean every engine reinvents the bookend shape, defeating the user-procurable goal.
