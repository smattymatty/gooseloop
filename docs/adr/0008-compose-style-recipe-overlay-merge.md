## ADR 0008 — Compose-style recipe overlay merge

**Status:** Accepted (2026-06-04)
**Context:** OSS-extraction design review; depends on [ADR 0006](0006-pipeline-named-slots-framework-owns-review-summary-order.md) and [ADR 0007](0007-review-output-schema-operator-actions-ledger.md)

## Context

ADRs 0006 and 0007 made review.yaml and summary.yaml user-procurable: each project owns its own (`cp review.example.yaml review.yaml`), and engines ship the example as a starting template. The 2026-06-04 design review went further: users want multiple recipe variants per project (`review.daily.yaml`, `review.audit.yaml`, `review.specificexception.yaml`), with the option to compose them docker-compose-style.

Three concrete pressures from the design review:

1. **Per-deployment context tuning.** A "production" review might be the base with one overlay that bumps `max_turns` from 4 to 8. Re-declaring the whole recipe to change one setting is the kind of duplication the compose pattern was designed to eliminate.
2. **Per-machine tweaks.** The operator wants the docker-compose `.override.yml` convention: a gitignored `review.local.yaml` that auto-applies on the developer's machine without polluting the committed base.
3. **CLI experimentation.** Operators want to try a tweaked recipe for one run (`gooseloop run --review-overlay review.experimental.yaml`) without permanently changing files.

Goose recipes are YAML with non-trivial structure: a `prompt` string, a `context` list of `{label, source, optional?}` entries, a `settings` dict, an `extensions` list of `{type, name}`. A naive "later-key-wins" merge doesn't capture what operators expect — for instance, adding a context entry should append, not replace the whole `context` list.

## Decision

Recipes compose via a layered overlay merge. The active recipe for any run is the result of merging layers in this order (top to bottom; later overrides earlier):

1. **Base layer**: the recipe declared in `gooseloop.toml` (default `review.yaml` or `summary.yaml`).
2. **Local overlay**: `review.local.yaml` (or `summary.local.yaml`), auto-applied if present in the project. Conventionally gitignored. Optional.
3. **CLI overlays**: any `--review-overlay X.yaml` (or `--summary-overlay X.yaml`) passed on the command line, in declared order. Zero or more.

### Merge rules per recipe field

The merge walks two YAML documents recursively. Rules are dispatched by the value's type and (for keyed lists) by the field name:

| Field shape | Rule |
|---|---|
| Scalars (`version`, `title`, `description`, `prompt`, `intent`) | Later wins. Full replace. |
| Dicts (`settings`, anything nested under it) | Deep-merge recursively. Scalar leaves: later wins. |
| Keyed list: `context` (keyed by `label`) | Merge by `label`. Overlay entry with same label overrides base entry's `source` / `optional` fields. New labels append. |
| Keyed list: `extensions` (keyed by `(type, name)` pair) | Merge by key. Same `(type, name)` overrides; new combinations append. |
| Plain list (no identity field) | Later replaces fully. |

Removal semantics: an overlay entry with `source: REMOVE` (sentinel string) removes that keyed-list entry from the merged output. This lets overlays subtract from the base without rewriting it.

The framework ships `gooseloop recipe --resolve review` as a debug command that prints the fully-merged recipe, so "why did my prompt come out that way?" is answerable in one command.

### Layer ordering rationale

Local before CLI: a developer's per-machine `review.local.yaml` is a stable preference; a CLI overlay is an ad-hoc experiment. The experiment wins, so CLI overrides local.

### Goose-side compatibility

Goose itself sees one resolved recipe per phase invocation (the framework writes a temp file with the merged YAML and hands that to `goose run --recipe`). Goose has no knowledge of overlays; the merge is purely a looper-side construct.

## Consequences

**Good:**

- Per-deployment-context tuning without re-declaring whole recipes.
- Familiar pattern for anyone who has used docker-compose.
- `review.local.yaml` convention preserves per-machine tweaks across team members cleanly (gitignored by convention).
- The `--review-overlay` flag enables one-off experiments without file shuffling.
- Removal sentinel covers the "subtract from base" case without inventing more YAML directives.

**Tradeoffs:**

- The merge-rules table is load-bearing documentation. Operators who skip reading it will produce surprising merge results. Mitigation: `gooseloop recipe --resolve` shows the truth.
- YAML list merging is genuinely ambiguous for new readers; the keyed-list-vs-plain-list distinction has to be internalized. Mitigation: only two lists in the gooseloop recipe schema use the keyed rule (`context`, `extensions`); both have a natural identity field.
- Implementation cost: the merge engine is non-trivial (recursive, type-dispatched, with the keyed-list rule). Approximately one focused day. Tests cover the rules table line-by-line.
- Diagnostic burden: when a recipe behaves unexpectedly, the operator's first move is `gooseloop recipe --resolve`. This is one more tool in the loop; ship it alongside the merge.

## Migration plan

1. Implement `gooseloop/recipe_merge.py` with one public function: `merge_recipes(base: dict, *overlays: dict) -> dict`. Pure; recursive; dispatches per the rules table.
2. Implement `gooseloop/cli.py` (or extend `__main__.py`) with `gooseloop recipe --resolve <name>` and the `--review-overlay` / `--summary-overlay` flags.
3. Update the looper to compose layers before handing the recipe to `_prepared_recipe` (which already writes a temp file).
4. Document the rules table in `PROTOCOL.md`.
5. Tests in `tests/test_recipe_merge.py`: one test per row of the rules table, plus the removal sentinel and the layer-precedence cases.

## Alternatives considered

- **Single-file selection only** (no merging; `--review review.audit.yaml` picks one). Considered as the 90% solution. Rejected because the design review explicitly chose the docker-compose pattern. The per-machine `.local.yaml` use case alone justifies the merge cost.
- **Hybrid: pick one base + one optional `.local.yaml` overlay (no N-layer chain).** Considered as the middle ground. Rejected because the CLI-overlay use case (one-off experiments) is the second most common after `.local.yaml`. Capping at two layers would forbid it.
- **JSON Patch / RFC 6902.** Considered. Rejected because the operator-facing surface should be "a YAML file that looks like the base but only contains the bits you want to change," not a patch dialect. JSON Patch is precise but unfriendly.
- **Inline includes (`!include other.yaml`).** Considered (the Ansible/Salt approach). Rejected because it pushes composition into the recipe file itself, mixing what-to-do with how-to-compose. Layered overlays keep composition external.
