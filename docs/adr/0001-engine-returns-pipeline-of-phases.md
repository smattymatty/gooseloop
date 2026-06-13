# ADR 0001 — Engine returns a Pipeline of Phases

**Status:** Accepted (2026-05-28)
**Context:** GooseLooper architectural refactor

## Context

`runner.py` today hardcodes the Storm customer-pipeline shape: validate → review → snapshot → branch → periodic → summary. We plan to extract `GooseLooper` as a generic OSS execution shell on gitforge.ca, with Storm's customer pipeline becoming the first **Engine** and future engines (e.g. `StormSecurity`) plugging in alongside.

The crux question: who owns the phase order — the Looper or the Engine? A security-monitoring engine has nothing in common with a customer-pipeline engine at the phase level. Baking Storm's shape into the Looper would force every future engine into the same mold.

## Decision

The Engine owns the phase order. Engines expose `pipeline()` returning an ordered list of `Phase` objects. The Looper executes Phases from a queue — pull, run recipe with retry, call `post_process`, enqueue any returned child Phases, repeat until empty.

A `Phase` has this contract:

- `name` — for logs and banners
- `recipe_path` — relative to the engine's recipes dir
- `build_env(ctx)` — returns the env vars for this recipe invocation
- `success_predicate(output) -> bool` — optional; defaults to "no transient error"
- `post_process(output, ctx) -> list[Phase] | None` — optional; returned phases are enqueued

Dynamic branching (one review phase producing N follow-up branches) is expressed by `post_process` returning child Phases. The Engine never invokes goose directly.

## Consequences

**Good:**

- Looper has zero knowledge of prospects, scoring, or any Storm concept. Extracting it as a standalone OSS package is a clean cut.
- Engines unit-test their `pipeline()` and each Phase's `post_process` as pure functions, without needing goose. Highest testability we can get given the LLM-in-the-loop reality.
- New engines (`StormSecurity`, third-party) are additive — they ship their own `pipeline()` and recipes, no Looper changes needed.
- The Phase queue is a single point where retry, logging, and footer accounting happen, regardless of engine.

**Tradeoffs:**

- Engine code carries more weight than a hook-style design. The Engine author must understand the Phase contract; they can't just "fill in the blanks" of a fixed template.
- No shared template for "the typical pipeline shape" — if Storm's review → branch → wrap pattern is common, future engines may end up duplicating it. Mitigation: ship a `CommonPipelinePatterns` helper module in the Looper *or* in a separate `gooseloop-recipes` package once a second engine exists. **Do not abstract speculatively before the second engine.**
- `post_process` returning Phases means the queue depth is data-driven; it's possible (though unlikely) for a misbehaving engine to enqueue unbounded children. Looper should cap queue depth as a safety net (config-tunable, default e.g. 50).

## Alternatives considered

- **Looper hardcodes canonical shape (review → branches → periodic → summary), engine fills hooks.** Simpler, lower learning curve, but bakes Storm's pipeline into the OSS core. Rejected because the OSS goal makes Storm-shaped abstractions a liability.
- **Hybrid: Looper defines abstract phase categories (observe/act/report) with hooks; engine fills.** Tried to give structure without locking in order. Rejected as the worst of both — still constrains engines, still requires Looper to know about categories that won't survive the next engine.
