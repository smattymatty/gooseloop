# ADR 0009 — `default_engine` and short-name engine resolution

**Status:** Accepted (2026-07-13)
**Context:** sequel to ADR 0002 (config as the engine source of truth); the
multi-engine reality proven by every real consuming project

## Context

`gooseloop.toml` carried a top-level `engine_module` key — a fossil of the
framework's original one-engine-per-project assumption. Reality diverged:
one loop root routinely hosts several engines (this repo ships three; the
first real consuming project runs three of its own), each with its own
`[section]` in the same toml, selected per run via `-e`. The singular key's
name misstated its own job and misled a dashboard into computing per-loop
facts from it (the routing-mode-badge bug, 2026-07-13). The operator called
it directly: "one gooseloop.toml can serve many engines."

## Decision

1. **Rename `engine_module` to `default_engine`.** The key is exactly what
   the new name says: the engine a bare `gooseloop run` runs — not a claim
   about how many engines a project has. The old key keeps working with a
   rename nudge on stderr (the package is published; configs must not break
   on upgrade), and `LooperConfig.engine_module` survives as a deprecated
   property alias.
2. **Short-name resolution** (`gooseloop.config.resolve_engine_module`):
   `gooseloop run doc_drift` resolves to `engines.doc_drift` by scanning
   the loop root — `<name>.py`, `<name>/`, then `<top-level-package>/<name>/`.
   Ambiguity is refused with the candidate list, never guessed. The
   convention was validated by the dashboard's engine discovery before the
   framework adopted it. Both the positional/`-e` argument and
   `default_engine` itself accept short names.
3. **`gooseloop engines` lists every engine in the loop root** (siblings of
   the default's parent package), marking the default — replacing the old
   behaviour of printing only the configured module.

## Consequences

- Additive per PROTOCOL §9: no consumer breaks, dotted module paths work
  exactly as before.
- The resolver is public API in `config.py` (Foundation, stdlib-only) so
  consumers like gooseloop-dash resolve names the same way the CLI does —
  one convention, two directions, zero drift.
- A project whose engines do NOT follow the package-per-engine convention
  simply keeps using dotted paths; resolution never runs for them.
