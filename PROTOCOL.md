# gooseloop protocol — version 1.0

This is the contract anyone implementing or consuming a gooseloop pipeline must
adhere to. It is the source of truth for recipe authors, engine authors, and
environment authors. If the code disagrees with this doc, the code is wrong.

> Status: protocol version **1.0**. Backwards-compatible additions (new optional
> keys) keep the major version. Schema-breaking changes bump to 2.0; the framework
> refuses reviews declaring a different major version than it supports.

## 1. The primitives

gooseloop is a three-primitive framework:

- **Engine** — verbs. Declares the Pipeline shape, owns the BranchPolicy
  registry, ships its recipe defaults.
- **Environment** — nouns. Declares what the engine has access to. Framework
  ABC has one abstract method (`env_vars()`); shape-specific contracts live
  on contrib mixins (`gooseloop.contrib.CustomerPipelineEnvironment`,
  `gooseloop.contrib.ClaudeHandoffEnvironment`, etc.).
- **Pipeline** — the bookend contract. Every engine returns:
  ```python
  @dataclass
  class Pipeline:
      review: Phase    # always runs first
      body: list[Phase]  # cadence + review-spawned, in queue order
      summary: Phase   # always runs last
  ```

The looper orchestrates them. Engines do not invoke goose directly.

## 2. The review contract

The first phase of every pipeline is the review. Its job is to assess state and
emit a structured plan that the framework reads.

### Required output schema

```json
{
  "protocol_version": "1.0",
  "status": "done | partial | error",
  "summary": "one-paragraph state, operator-facing",
  "insights": ["...", "..."],
  "routing": [
    { "recipe": "<recipe-name>", "params": { "...": "..." }, "reason": "..." }
  ],
  "operator_actions": [
    { "action": "...", "why": "...", "...": "engines may add fields" }
  ]
}
```

Four keys are required: `protocol_version`, `status`, `summary`, and
`routing`. `insights` and `operator_actions` are optional and default to `[]`
when missing (validation is liberal in what it accepts: status synonyms
collapse to the enum, bare-string operator actions gain an empty `why`,
malformed entries are dropped). Review recipes should still emit all six so
the model's output shape stays stable. Engines may add additional keys; they
pass through to `Context.artifacts` for engine-internal consumption (a
customer-pipeline engine's `stale_prospects` list rides this way).

### Output framing

The review's final assistant message must wrap the JSON in sentinel markers:

```
<<<DELIVERABLE_JSON>>>
{ ... }
<<<END_DELIVERABLE>>>
```

The framework's `extract_json` parses the last occurrence. Anything outside the
sentinels is narration and ignored.

### status semantics

- `"done"` — review is complete; framework proceeds to body.
- `"partial"` — review could not finish (missing input, ambiguous handoff,
  operator action required first). Framework records operator_actions, skips
  body, runs summary.
- `"error"` — review failed for a model-detectable reason (invalid handoff
  structure, contradicting inputs). Framework records the review, skips body
  and summary, exits non-zero.

### routing[] semantics

Each entry is `{ recipe, params, reason }`. The framework constructs body
Phases from these entries using the engine's `BranchPolicy` registry (§5).
`reason` is operator-facing free text.

`params` is a dict of values the framework injects as env vars into the phase
recipe. Keys become uppercase env-var names: `{"panel_id": "ServersTable"}`
becomes `PANEL_ID=ServersTable`. Engines that need richer typing should
declare it in their `BranchPolicy`.

## 3. The summary contract

The last phase of every pipeline is the summary. Its job is to render the
final session ledger.

### What summary sees

The summary phase has access to:

- All `operator_actions` (initial + body-appended).
- All `routing` entries from the review.
- `ctx.artifacts["outputs_written"]` — list of files body phases wrote.
- Session metadata (start time, elapsed, phases ran/skipped) via `ctx`.
- Any engine extension keys the review stashed in `ctx.artifacts`.

### Summary output

The summary's output is human-facing. No required JSON schema. Convention:
markdown to stdout. The looper writes the summary phase's full output verbatim
to `<session_dir>/summary.md` — the one durable copy once the terminal
scrollback is gone. This is the only phase output the framework persists in
full; body phases persist only what they explicitly write via
`ctx.record_output`.

## 4. Body phase rules

Body phases run between review and summary. They do work routed by the review.

### What body phases can mutate

Body phases interact with the session via typed methods on `Context`:

```python
ctx.add_operator_action(action: str, why: str, **extras)
ctx.record_output(path: Path | str)
ctx.session_log(message: str)
```

- `add_operator_action` appends to the ledger. Dedup is by `(action, why)`.
- `record_output` appends to `ctx.artifacts["outputs_written"]`. The session
  footer renders these.
- `session_log` writes a timestamped line to the session's `session.log`.

At the end of the pass the looper writes the FINAL contents of both — every
body-appended action, every recorded output, not just the review's seed —
to `<session_dir>/ledger.json`. This is the durable copy once the terminal
footer is gone; `actions/review.json` (when an engine writes one) is only
the seed, frozen before the body ever ran.

Body phases must NOT mutate `routing[]`, `insights[]`, `summary`, or `status`.
Those are the review's output, frozen at the bookend.

### Body phase failure modes

A body phase can:

- Succeed (predicate returns True, looper proceeds).
- Skip via `skip_if` (looper logs and proceeds).
- Fail (predicate returns False or recipe throws). The looper logs the failure
  to `session.log` and the session footer; body continues with the next phase.
  The summary sees the failure via session metadata.

A body phase failure does NOT abort the pipeline. The summary runs regardless,
and the operator sees what worked and what didn't via the rendered ledger.

## 5. BranchPolicy registry

When the framework builds a body Phase from a `routing[]` entry, it consults
the engine's `branch_policies` registry to apply per-recipe rules:

```python
@dataclass
class BranchPolicy:
    skip_when: Callable[[dict], bool | str] | None = None
    output_path: Callable[[dict], Path | None] | None = None
    predicate: Callable[[str], bool] | None = None
    intent: Literal["produce", "edit", "edit-or-produce"] | None = None

class MyEngine(Engine):
    branch_policies = {
        "to-outreach": BranchPolicy(
            skip_when=lambda p: (output_dir("outreach") / f"{p['slug']}_draft.md").exists(),
            output_path=lambda p: output_dir("outreach") / f"{p['slug']}_draft.md",
            intent="produce",
        ),
        # Unregistered recipes get BranchPolicy(): no skip, no path tracking,
        # default transient-error-only predicate, intent unchecked.
    }
```

The framework calls `engine.branch_policies.get(recipe_name, BranchPolicy())`
for each routing entry. Defaults are sensible — an engine with no special-case
recipes does not need to register anything.

## 6. Recipe overlay merge

Recipes compose via a layered overlay merge. Layer order (later overrides
earlier):

1. **Base**: declared in `gooseloop.toml` (`review_recipe = "review.yaml"`).
2. **Local overlay**: `review.local.yaml` (gitignored convention), auto-applied.
3. **CLI overlays**: any `--review-overlay X.yaml`, in declared order.

### Merge rules

| Field shape | Rule |
|---|---|
| Scalar (`prompt`, `version`, `description`, `title`, `intent`) | Later wins. Full replace. |
| Dict (`settings`, nested under it) | Deep-merge recursively. Scalar leaves: later wins. |
| Keyed list: `context` (key: `label`) | Merge by `label`. Same label overrides. New labels append. |
| Keyed list: `extensions` (key: `(type, name)`) | Merge by key. Same key overrides. New combinations append. |
| Plain list | Later replaces fully. |

### Removal sentinel

A keyed-list overlay entry with `source: REMOVE` deletes that entry from the
merge result. Lets overlays subtract from the base without redeclaring it.

### Debug command

`gooseloop recipe --resolve review` prints the fully-merged recipe. First stop
for "why did my recipe behave that way?" debugging.

## 7. The recipe `context:` block

Recipes declare load-bearing inputs via a top-level `context:` list. Each
entry is `{ label, source, optional? }`:

```yaml
context:
  - label: "JOURNAL"
    source: env_method:journal_text
  - label: "PROSPECT FILES"
    source: glob:${POTENTIAL_DIR}/*.md
  - label: "REVIEW JSON"
    source: env_file:REVIEW_JSON_PATH
    optional: true
```

### Source kinds

- `env_file:VAR` — read the file whose path is in env var `VAR`.
- `file:PATH` — read `PATH` directly (env vars substituted).
- `glob:PATTERN` — glob `PATTERN` (env vars substituted), concatenate sorted
  matches with per-file headers.
- `env_method:NAME` — call `environment.NAME()` and paste the return value as
  literal text. Requires the looper to have an Environment instance.

### Strictness

By default, an unresolvable source raises `RuntimeError` at render time. The
phase fails loud; goose never sees a half-rendered recipe.

`optional: true` softens the rule: an unresolvable optional source renders a
sentinel placeholder block (`(env var X is unset; skipped)`, `(no files matched
pattern: Y)`) instead of raising. Use for "this input is meaningful when
present but a fresh install legitimately has nothing here" cases.

### Introspection and dry-run preview

Tooling (the CLI, dashboards) can ask "what sources are available, and would
each one resolve" without rendering anything. `gooseloop.introspect` provides:

- `list_env_methods(environment)` — every method usable as an `env_method:`
  source. Qualification mirrors what render time accepts: public, callable
  with zero arguments, not annotated to return anything other than `str`.
  `env_vars` is excluded (it is the ABC's own contract, not a context
  source). Each entry carries the method's first docstring line, so
  documenting env_methods pays off directly in tooling.
- `preview_source(source, env, environment=...)` and
  `preview_recipe_context(recipe, env, environment=...)` — dry-run sources.
  Previews stat files (paths and sizes) but never read bodies, and check
  that an env_method exists but never call it: calling is real work (a
  journal digest, a URL fetch) and belongs to render time or an explicit
  "preview content" action in the calling tool.

The CLI face is `gooseloop recipe --sources NAME [--json] [-e MODULE]`. It
merges the recipe's overlay layers (§6), previews every context entry against
the engine's env scope (`environment.env_vars()` + `engine.base_env()` over
the process env), and lists the env_methods and env vars available. Exit 0
when every required source resolves; exit 1 when a required source would fail
the render. Optional failures are reported but tolerated, matching render-time
strictness. `--json` emits the same data machine-readable, for dashboards.

## 8. Environment ABC and contrib mixins

The framework `Environment` ABC has exactly one abstract method:

```python
class Environment(ABC):
    @abstractmethod
    def env_vars(self) -> dict[str, str]:
        """Env vars merged into every recipe call."""
```

Shape-specific contracts live under `gooseloop.contrib.*`. Two ship initially:

- `gooseloop.contrib.CustomerPipelineEnvironment` — for customer-acquisition
  pipelines. Adds `lifecycle_dirs`, `build_digest`, `journal_text`,
  `questions_dir`, etc.
- `gooseloop.contrib.ClaudeHandoffEnvironment` — for Claude design-handoff
  engines. Adds `handoff_dir`, `target_repo`, `dev_up_probe`, etc.

Concrete environments subclass either bare `Environment` (no obligations) or
a contrib mixin (inherits its contract). See [ADR 0005](docs/adr/0005-environment-abc-narrows-contrib-mixins.md).

Recipes call any concrete method via `env_method:<name>` regardless of mixin
lineage; the source kind dispatches by name on the live instance.

## 9. Compatibility

The framework guarantees backwards compatibility within a major version of
this protocol. Specifically:

- New optional keys may be added to the review schema. Old reviews keep
  working.
- New source kinds may be added to the `context:` block. Old recipes keep
  working.
- New methods may be added to contrib mixins. Concrete environments that
  subclass an older version keep working until they upgrade.
- BranchPolicy fields may be added with sensible defaults. Old engines that
  don't set them keep working.

Breaking changes (renaming a required key, removing a source kind, changing
default behaviour) bump the major version. Engines + recipes that declare a
different major version than the framework supports are refused at load time
with a clear error.

## 10. Where things live

```
PROTOCOL.md                  # this file, at the repo root beside the README
gooseloop/                   # the framework package (the OSS extraction target)
├── __init__.py             # public surface
├── __main__.py             # `gooseloop` CLI entry
├── engine.py               # Engine ABC, registry
├── environment.py          # Environment ABC (just env_vars)
├── phase.py                # Phase + Pipeline + Context dataclasses
├── protocol.py             # ReviewOutput, OperatorAction, RoutingEntry TypedDicts
├── looper.py               # GooseLooper
├── goose.py                # subprocess wrapper, retry, rate-limit handling
├── context_prepend.py      # recipe render-time input resolution
├── extract.py              # deliverable JSON extraction, with provenance
├── recipe_merge.py         # overlay merge engine (per ADR 0008)
├── predicates.py           # success_predicate factories
├── toolkit.py              # stdlib-only engine helpers (Source, fetch, state io)
├── artifact.py             # versioned artifact contracts (see §12)
├── runlock.py              # run.lock, one run per loop root (see §13)
├── session.py              # session folder management
├── footer.py               # per-call and per-session footers
├── text.py                 # ANSI, banners
├── config.py               # gooseloop.toml loader (LooperConfig)
└── contrib/
    ├── __init__.py
    ├── customer_pipeline.py    # CustomerPipelineEnvironment ABC
    └── claude_handoff.py       # ClaudeHandoffEnvironment ABC

# A consuming project's layout:
my-project/
├── gooseloop.toml          # which engine, which review/summary recipe, etc.
├── run.lock                # present only while a run is in flight (§13);
│                           # gitignore it
├── review.yaml             # user-procured; cp'd from engine's review.example.yaml
├── review.local.yaml       # gitignored; per-machine tweaks
├── summary.yaml            # user-procured; cp'd from summary.example.yaml
├── recipes/                # body recipes (engine-bundled or user-supplied)
├── reviews/sessions/       # gooseloop-managed session output
│   └── <timestamp>/
│       ├── session.meta.json   # model, engine, timestamps
│       ├── session.log         # append-only event log
│       ├── summary.md          # the summary phase's full verbatim output
│       ├── ledger.json         # FINAL operator_actions + outputs_written
│       └── actions/            # engine-specific (e.g. review.json)
└── ...                     # engine-specific files (inputs, journals, output dirs)
```

## 11. Quick reference for new authors

**I want to write a recipe.** Start from the engine's `review.example.yaml` or
the relevant `<recipe>.example.yaml`. Read §7 (context block). Validate with
`gooseloop recipe --resolve <name>` before running.

**I want to write an engine.** Subclass `gooseloop.Engine`. Return a
`Pipeline(review, body, summary)` from `pipeline()`. Declare `branch_policies`
if your recipes need per-recipe rules. Ship `review.example.yaml` and
`summary.example.yaml` in the engine's recipes directory.

**I want to write an environment.** Subclass `gooseloop.Environment` (if your
domain doesn't fit existing contrib mixins) or a contrib mixin (to inherit a
documented contract). Implement `env_vars()` and whatever shape-specific
methods your recipes call via `env_method:`.

**I want to override a recipe in my project.** `cp engine/review.example.yaml
./review.yaml`. Edit. Add a `review.local.yaml` for per-machine tweaks
(gitignore it). Use `--review-overlay X.yaml` for one-off experiments.

**I want to debug a recipe.** `gooseloop recipe --resolve <name>` prints the
fully-merged recipe. The looper writes a temp file with the rendered context
block; set `GOOSER_KEEP_RENDERED=1` to preserve it on disk.

## 12. Engine composition

Two engines cooperate by sharing an artifact on disk, never by importing each
other. The rule follows ADR 0004: an upstream engine's output is something the
downstream loop *has access to*, so it enters the downstream loop as a noun on
its Environment, exactly like any other project file. Engines stay strangers;
the artifact is the interface. The canonical shape (proven by the
pain-harvest / site-pitch pair):

```
stage A (harvester)   drafts candidate blocks into its output dir
operator              reviews each draft, appends approved blocks to the
                      sealed artifact by hand
stage B (consumer)    reads the sealed artifact via its Environment
```

The rules that make this a contract rather than a happy accident:

- **The artifact is versioned.** The producing side stamps a `schema_version`
  key into the file; the consuming side checks it at read time with
  `gooseloop.artifact.check_artifact_version()`. Same major is compatible
  (additive changes bump the minor). A different major or an unparseable
  version is refused loudly with `ArtifactVersionError`; a missing version is
  read anyway with a recorded problem nudging the operator to stamp the file
  (fail-safe runs in the KEEP direction, so hand-sealed pre-versioning
  artifacts keep working).

- **The seam is operator-gated by default.** Upstream proposes, the operator
  seals, downstream consumes only sealed data. An unsealed artifact flowing
  agent-to-agent turns one model's hallucination into the next model's ground
  truth. Automate a seam only after deciding, explicitly, that it does not
  need a human gate.

- **Contract tests pin both sides of the seam.** The producer's suite asserts
  its rendered drafts parse and validate against the artifact schema. The
  consumer's suite reads fixture artifacts, never live producer output.
  Either engine may then change internals freely; the artifact holds.

- **Sequencing stays outside the framework.** Run the stages as separate
  `gooseloop` invocations from a shell script, make target, or cron. Each
  engine keeps its own full review -> body -> summary pass; pipelines are
  never merged, so the ADR 0006 ordering guarantee holds per stage.

Shared mechanics for engine authors (Source parsing, hardened URL fetch,
paste caps, slug safety, JSON state io) live in `gooseloop.toolkit`, extracted
from the engines that proved the need.

## 13. The run lock

One run at a time per loop root. `GooseLooper.begin_loop()` acquires
`<loop root>/run.lock` before any phase runs and removes it when the pass
ends, success or failure. A second run started while the lock is held is
refused before doing any work: the CLI exits with code 3 (distinct from
1 = run error and 2 = usage error, so a supervisor can tell "busy" from
"failed"); library callers get `gooseloop.RunLockHeldError`.

The lock file is JSON:

```json
{
  "pid": 48213,
  "started": "2026-07-13T14:02:11+00:00",
  "engine": "engines.doc_drift",
  "session_id": "2026-07-13T14-02-11"
}
```

- `pid` — the process running the pass.
- `started` — ISO 8601 UTC, when the lock was acquired.
- `engine` — the resolved dotted module path of the engine in flight.
- `session_id` — the session folder name; `null` until the folder exists,
  and for the whole run under `--no-save`.

Rules:

- **Scope is the loop root, not the engine.** Two different engines in the
  same root still serialize: they share the working tree, the sessions dir,
  and any cross-run state.
- **Every `gooseloop run` locks — no flag exceptions.** `--no-save` and
  `--review-only` skip artifacts, not side effects. `recipe` and `engines`
  are read-only and never lock.
- **Stale locks self-heal.** If the lock's pid is dead, the next run
  reclaims it with a stderr warning naming the crashed run. Where pid
  liveness cannot be probed safely, the run refuses conservatively.
- **One writer.** Consumers (dashboards, supervisors) may read `run.lock`
  to answer "is a run in flight, and which engine" — pid liveness is the
  authoritative check, not the file's existence alone. Only gooseloop
  creates, replaces, or removes the file. To cancel a run, signal the pid
  and let the run's own cleanup delete the lock. A consumer that deletes
  `run.lock` is violating this protocol, not exercising an API.

The run's session records the same attribution durably:
`session.meta.json` carries `engine_module` (the resolved dotted path)
alongside the short `engine` display slug.

Consuming projects gitignore `run.lock` (§10 layout).

Decision record: ADR 0010.

---

This protocol is canonical. Disagreements between this document and the code
are bugs in the code.

For the design history, see the ADRs in [`docs/adr/`](docs/adr/) —
particularly 0000 (four-layer import topology), 0001 (Engine returns
Pipeline), 0004 (Engine + Environment as siblings), 0005 (Environment ABC
narrows), 0006 (Pipeline named slots), 0007 (Review output schema +
operator_actions ledger), 0008 (recipe overlay merge), and 0010 (the run
lock).
