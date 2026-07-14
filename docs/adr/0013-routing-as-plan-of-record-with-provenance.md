# ADR 0013 — routing[] is the plan of record, with routed_by provenance

**Status:** Accepted (2026-07-13)
**Context:** a live doc_drift pass surfaced the observability inversion:
the engine deterministically queued 23 drafts, and the persisted review
said `routing: []`

## Context

`routing[]` has been doing two jobs without anyone writing that down: the
**instruction channel** (model → framework: build these phases) and the
**plan of record** (the frozen artifact stating what the pass intended).
In model-routed engines the two coincide. In engine-routed engines the
instruction channel is deliberately unused — doc_drift's design principle
is "the model judges, it does not route" — and the plan of record was
being discarded with it. The result: the more deterministic the loop, the
blinder its artifact. A pass that drafted 23 patches persisted a review
claiming it planned nothing; every consumer of review.json (the dashboard,
planned-step counts, cross-run tooling) under-reported exactly the runs
that were most knowable.

The one legitimate objection to putting engine decisions in routing[] is
that routing is model-authored: having the review recipe echo the engine's
list would push deterministic facts through a nondeterministic channel
(token cost, transcription drift, "improvements"). That is an argument
about the author, not the record.

## Decision

1. **`RoutingEntry` gains `routed_by: "model" | "engine"`.** Additive per
   §9; artifacts without it read as `"model"`.
2. **Validation stamps provenance.** `validate_review` sets every
   model-emitted entry to `routed_by: "model"` regardless of what the
   model claims — `"engine"` is reserved for the framework.
3. **The framework records the engine's plan.** After validation, when
   `status` is `"done"` and the run is not `--review-only`, the looper
   appends one entry per `pipeline.body` phase (`recipe` = the phase's
   recipe stem, `reason` = its label or name, `routed_by: "engine"`)
   before the review is stashed and persisted. Model entries stay first:
   children run before cadence phases (ADR 0006), so routing[] reads in
   execution order.
4. **Engine entries are record, never instruction.** `_build_body_phases`
   skips `routed_by: "engine"` — those phases already exist in
   `pipeline.body`; building them again would run the body twice.
5. **Review recipes are unchanged.** Engine-routed reviews keep their "do
   not emit routing" instruction; the model still judges without routing.

## Considered options

- **Model echoes the engine's plan in its routing output.** Rejected: the
  nondeterministic-channel problem above, and it inverts doc_drift's
  design on purpose statement.
- **A separate plan artifact (plan.json) beside review.json.** Rejected:
  every existing consumer already reads routing[] as "the plan"; a second
  file means every consumer grows a second code path and the two can
  disagree. Provenance inside the one list keeps a single source of truth.
- **Leave it to telemetry (phases.jsonl).** Rejected: telemetry records
  phases as they SETTLE — it is the receipt, not the plan. The plan's
  value is existing before the body runs.

## Consequences

- review.json is honestly "what this pass planned" for every engine;
  consumers need not know the routing mode.
- Dashboards and counters stop under-reporting engine-routed loops (the
  observed "review · 0 routed" over a 23-draft pass).
- A `partial` review or `--review-only` run leaves routing[] as the model
  emitted it — the record never claims work that was skipped.
- Engine entries carry empty `params` (engine phases hold a build_env
  closure, not a params dict); recipe + reason is the record. If richer
  engine-entry params are ever needed, that is an additive follow-up.
