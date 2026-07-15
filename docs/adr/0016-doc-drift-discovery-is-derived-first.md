# ADR 0016 — doc-drift discovery is derived-first, not canonical-first

**Status:** Accepted (2026-07-14)

## Context

doc-drift checks derived views (docs) against canonical sources (code). A
map declares the pairs and collections to check. Discovery is the engine's
help keeping that map itself coherent: proposing map edits as operator
actions the operator seals by hand. The map is never machine-written.

The first discovery pass shipped was **canonical-first**: for every
directory holding a watched canonical, flag any recently-changed file of the
same extension not itself watched, as a likely un-declared canonical. The
theory: a new source file sitting beside a watched one is probably worth
watching too.

It failed the moment it ran against a real map. This map's canonicals are
**code files in large code directories** (`base/pricing.py`,
`stormbuckets/*.py`). "Sibling of a canonical, same extension" therefore
matched every `.py` file in those trees. One run raised **50 operator
actions** — `Add canonical .../stormbuckets/tasks.py`, `.../urls.py`,
`.../__init__.py` — all noise, dumped into the seal queue at once. The
signal (a genuinely new canonical) was drowned; the premise (canonicals
cluster in small doc-like dirs) was simply false for how doc-drift is used.

Alternatives considered:

- **Narrow the canonical-first heuristic** (require the file be referenced
  by the derived view, or match a name pattern). Rejected: the mismatch is
  structural — canonicals live in code trees — so any threshold either still
  leaks or suppresses everything.
- **Derived-first claim scan** (read a page's claims, find the code that
  grounds them, flag unmapped canonicals). The most powerful, but expensive
  (per-page model work) and complex. Deferred.

## Decision

Remove canonical-first discovery entirely. Replace it with **unmapped
doc-root discovery**, which proposes the one edit that is actually
high-signal for this map: *add a collection for a directory of docs nobody
watches.*

- Discovery scans only the operator-declared `[doc_drift] discovery_roots`.
  Empty (the default) = discovery off. It never roams the tree uninvited —
  the anti-sprawl bound the canonical-first pass lacked.
- Within those roots (pruning `.git`, `node_modules`, `.venv`, `__pycache__`,
  build/dist/cache dirs), a directory qualifies as a gap when it holds
  `>= 3` markdown files that no collection glob covers, at least one changed
  within `discovery_window_days` (the window borrowed from git-recap). Each
  gap raises one action: *"Add a collection for `<dir>` (N unmapped docs)."*
- Derived docs INSIDE a mapped root need no discovery: collections are
  globs, so new files there are already checked for free.
- The genuinely useful actions the old pass sat beside are kept unchanged:
  broken pairs (`Fix doc-map entry …`) and broken/empty collections
  (`Fix doc-map collection …`).

Pointed at the same tree that produced 50 false positives, the new pass
returns **5** real doc areas (`_architecture/adrs/{buckets,core,developer}`,
`_architecture/specs`, the repo root) — unmapped ADR and spec directories, a
real coverage gap, one action each.

## Consequences

- Discovery is opt-in and bounded: no `discovery_roots`, no discovery. A
  wrong config over-proposes within named roots at worst, never across the
  whole disk.
- doc-drift no longer proposes canonicals at all. If a genuinely new
  canonical source appears, the operator adds it by hand — the same hand
  that curates every canonical today. This is accepted: canonicals are
  hand-picked to ground specific claims, and "spot a new one automatically"
  was never reliable enough to be worth the noise.
- The derived-first claim scan remains open as future work, above this
  bounded seed rather than beside the removed one.
