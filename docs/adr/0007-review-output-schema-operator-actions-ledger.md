## ADR 0007 — Review output schema and operator_actions ledger as framework primitives

**Status:** Accepted (2026-06-04)
**Context:** OSS-extraction design review; depends on [ADR 0006](0006-pipeline-named-slots-framework-owns-review-summary-order.md)

## Context

ADR 0006 made review and summary framework-positioned (always first / always last). That decision is incomplete without a contract on what reviews produce. Two pressures from the 2026-06-04 design review:

1. **Recipe portability across engines.** A user-procured `review.yaml` (say, "code-review review" or "weekly-status review") needs to work with any engine that consumes its output. That requires a stable shape.
2. **Body phases must be able to extend the operator's action queue.** The operator's example: "a part of the body finishes a feature, so a new operator review step would be to manually test/visual test this." The review can't enumerate every action up front; the ledger must grow across the body.

Today the origin engine has an informal pattern that does both: review emits `{summary, insights, suggested_actions, operator_actions, stale_prospects, new_prospect_ideas}` (engine.py:_REVIEW_REQUIRED_KEYS); the engine's review.post_process parses it and spawns branches; `Context.artifacts["operator_actions"]` carries the ledger forward but is untyped `dict[str, Any]`. The agent's code review flagged the typing gap explicitly.

The design review chose to lift this pattern from engine-internal convention to a framework primitive, with a schema that's small enough to be cross-engine portable but explicit enough that the framework can drive routing from it.

## Decision

### Review output schema

Every review recipe must emit JSON conforming to this shape (sentinel-wrapped per the existing `<<<DELIVERABLE_JSON>>>` convention):

```json
{
  "protocol_version": "1.0",
  "status": "done | partial | error",
  "summary": "one-paragraph state, operator-facing",
  "insights": ["observation 1", "observation 2", "..."],
  "routing": [
    {
      "recipe": "to-implement-panel",
      "params": { "panel_id": "ServersTable", "...": "..." },
      "reason": "free-text why this work was routed"
    }
  ],
  "operator_actions": [
    {
      "action": "Manually verify the Cost ribbon renders correctly on ultrawide.",
      "why": "Screenshot-diff is unreliable for sticky elements; a human pair of eyes is the gate.",
      "...": "engines may add fields"
    }
  ]
}
```

Required keys: `protocol_version`, `status`, `summary`, `insights`, `routing`, `operator_actions`. The framework reads these. Engines may add any number of additional keys; they pass through to `Context.artifacts` for engine-internal consumption.

`status` semantics:
- `"done"` — review is complete; no further review cycles needed this run.
- `"partial"` — review couldn't finish (e.g. missing input); operator should re-run after fixing.
- `"error"` — review failed for a reason the model could detect (e.g. invalid handoff structure). Body phases skip.

`routing[]` is what the framework hands to `Engine.branch_policies` to build body phases (per the BranchPolicy registry — see `PROTOCOL.md`).

### operator_actions ledger

`operator_actions` is the session's mutable ledger of work the operator must do by hand:

- The review initializes the ledger.
- Body phases append via the new typed Context method:
  ```python
  def post_process(output: str, ctx: Context) -> None:
      ctx.add_operator_action(
          action="...",
          why="...",
          # arbitrary engine-extension fields:
          panel_id="ServersTable",
      )
  ```
- The summary reads the final state via `ctx.operator_actions` (also a typed property on Context).

The framework enforces the shape at call site: `action` and `why` are required strings; extras are arbitrary keyword args stored on the entry. Dedup is by `(action, why)` to keep the ledger from ballooning on repeated body work.

### Schema versioning

Reviews declare a `protocol_version` field. Framework compares against its own supported range:

- Same major version: accept. Compatible.
- Different major version: refuse the review. Print which protocol versions the framework supports.

Framework starts at `"1.0"`. Backwards-compatible additions (new optional keys) keep the major at `1`. Schema-breaking changes (removing or repurposing a required key) bump to `"2.0"`.

## Consequences

**Good:**

- A `review.yaml` is portable across engines that share its `protocol_version`. The community can publish review recipes; engines consume them generically.
- `ctx.add_operator_action()` gives Context the typed API the code review flagged as missing. Body phases can extend the operator's queue without touching untyped dicts.
- The framework can render a uniform "operator action queue" UI (CLI status command, future dashboard) because the schema is canonical.
- Status enum lets the framework distinguish "review failed, halt" from "review partial, operator action needed" from "review clean, run body."

**Tradeoffs:**

- Engines that want a niche review output (e.g. a review that emits a graph DOT file) must encode it in extension keys. The framework reads required keys only; arbitrary extensions ride alongside.
- Schema versioning adds one required key (`protocol_version`) to every review. Cheap; documented.
- Origin migration: the origin engine's existing review schema (`stale_prospects`, `new_prospect_ideas`, etc.) gets renamed/relocated. `suggested_actions` becomes `routing`; `stale_prospects` and `new_prospect_ideas` are engine extension keys (origin-specific). The pipeline-review.yaml prompt needs updating.

## Migration plan

1. Add `gooseloop/protocol.py` with `ReviewOutput` TypedDict + `OperatorAction` TypedDict + `RoutingEntry` TypedDict matching the schema.
2. Add `ctx.add_operator_action(...)` method on `Context`. Internally append to `ctx.artifacts["operator_actions"]` (typed list of `OperatorAction`).
3. Add `ctx.operator_actions` read-only property exposing the current ledger.
4. Add `ctx.record_output(path)` and `ctx.session_log(msg)` while we're touching Context — same naming convention, same typed-API benefit.
5. Update framework review-phase wrapper to parse `routing[]` from output, build body phases via engine's `BranchPolicy` registry, fail loud on missing required keys.
6. Update the origin engine's pipeline-review.yaml: rename `suggested_actions` → `routing` (with key shape adjustments); declare `protocol_version: "1.0"`.
7. Move `stale_prospects` and `new_prospect_ideas` out of required keys (they're origin-engine extensions).

## Alternatives considered

- **Engine defines its own review schema; framework is blind.** Rejected: reviews are not engine-internal under ADR 0006, they're framework-positioned. A schema-less review means no cross-engine portability for the user-procurable recipes.
- **Two-tier: minimal framework schema (`status` + `routing`) + engine extension keys for everything else.** Considered. Rejected because `operator_actions`, `insights`, and `summary` are universally useful — every engine wants them. Putting them in extensions would mean every engine reinvents the same six fields. The five required keys are the actual common denominator.
- **Use Pydantic models instead of TypedDict.** Rejected for now. TypedDict gives the type-checking benefit without adding a runtime dependency. If validation pressure grows (e.g. third-party engines start shipping nonsense), revisit with Pydantic in a follow-up ADR.
