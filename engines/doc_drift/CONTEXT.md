# doc-drift — context glossary

The language of the doc-drift engine. Glossary only; no implementation detail.

## Core

- **Canonical** — the source of truth for a fact. In this map, canonicals are
  almost always *code or data files* (e.g. `base/pricing.py`), not prose. The
  canonical always wins; drift is measured against it.
- **Derived view** — a human-facing document that restates what the canonical
  says (a guide page, a marketing page, `terms.md`). It "drifts" when its
  claims no longer match the canonical.
- **Pair** — one canonical set checked against one derived view.
- **Collection** — a glob that expands to many derived views (e.g. every
  `guide_content/**/*.md`), each checked against a shared canonical set.
  Because collections are globs, new derived docs inside a mapped root are
  discovered automatically — no manual map edit needed.
- **Drift / candidate / in-sync** — a pair is a *candidate* until checked; the
  body phase drafts a patch and marks it *drift=yes* (real) or *drift=none*
  (false positive, in sync).

## Discovery — keeping the map itself coherent

- **Discovery** — surfacing map edits for the operator to seal by hand. The map
  is *never machine-written*; discovery only proposes.
- **Canonical-first discovery** *(rejected, see [[adr-doc-drift-discovery]])* —
  scanning for un-watched files *beside* an existing canonical. Rejected: this
  map's canonicals live in large code directories, so it flags every sibling
  source file (50 false positives observed). Removed, not narrowed.
- **Doc-root** — a directory whose contents are docs (markdown), treated as a
  candidate for a new **collection**.
- **Unmapped doc-root discovery** *(the chosen "explore")* — within explicit
  **discovery_roots**, find doc-roots that no collection glob covers and
  propose adding a collection. High-signal by construction: bounded scan,
  count + recency threshold.
- **discovery_roots** — operator-declared directories that discovery is allowed
  to scan. Discovery never roams outside them (the anti-sprawl bound).

## Related (gooseloop core, not doc-drift specific)

- **Planned bound** — an engine's declared *maximum* body-phase count, known
  before routing runs, so the dash shows `[1/<=N]` instead of `[1/?]` for
  model-routed engines. Resolves to the exact count once routing is verified.
