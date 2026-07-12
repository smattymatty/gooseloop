# ADR 0000 — Internal module architecture: four-layer import topology

**Status:** Proposed (adopted with zero violations; acceptance is the
maintainer's call)
**Context:** foundational; every other ADR's code lands inside this structure

## Context

This ADR governs the `gooseloop/` Python package: ~20 modules shipped on PyPI
as the execution shell other projects build engines on. The package grew
organically from an extraction (ADR 0001) and stayed small enough to hold in
one head, which is exactly the moment to freeze the shape it grew into: the
import graph is clean today, and a rule adopted at zero violations costs
nothing to keep and everything to retrofit later.

A framework has a sharper reason to legislate imports than an application
does: its module boundaries are public surface. Engines and environments in
consuming projects import from this package; a tangled interior eventually
leaks into the contract (ADR 0005 records exactly that failure: domain
vocabulary smuggled into the framework ABC through method names).

## Decision

The `gooseloop/` package is organized into four layers. Every module belongs
to exactly one. Imports flow downward only.

| Layer | Members | Role |
|-------|---------|------|
| **Entry** | `looper.py`, `__main__.py` | composition: drive a full pass |
| **Machinery** | `goose.py`, `context_prepend.py`, `predicates.py`, `footer.py` | the working parts of one recipe invocation |
| **Contracts** | `engine.py`, `phase.py`, `branch_policy.py`, `environment.py`, `contrib/` | the extension surface engines implement |
| **Foundation** | `protocol.py`, `artifact.py`, `toolkit.py`, `text.py`, `extract.py`, `session.py`, `config.py`, `recipe_merge.py` | wire formats, plain data, stdlib-only helpers |

- **Foundation** is the substrate: the review schema and its validation
  (`protocol.py`), versioned artifact contracts (`artifact.py`), the engine
  toolkit (`toolkit.py`), and io/parsing helpers. Foundation members import
  only other Foundation members.
- **Contracts** are what an engine author touches: the Engine and Environment
  ABCs, Phase/Pipeline/Context, BranchPolicy, and the shape-specific contrib
  mixins. Contracts may import Foundation.
- **Machinery** runs one recipe call: rendering context into the prompt,
  invoking goose with retry, success predicates, footers. Machinery may
  import Contracts and Foundation. Machinery above Contracts is deliberate:
  the plumbing may know the types, but a type must never need the plumbing.
- **Entry** is composition. `looper.py` wires a whole pass; `__main__.py` is
  the CLI. Nothing imports Entry.

**Two rules govern imports.**

**Rule 1 — Layer topology.** A module may import only from its own layer or a
lower one. Circular imports are a symptom of this rule breaking, not a
separate rule.

**Rule 2 — No cross-module private imports.** A single-leading-underscore
name is private to its defining module; to be used elsewhere it must be made
public first. Importing a public name and aliasing it privately
(`from .toolkit import ZWSP as _ZWSP`) is fine; the boundary is what the
defining module exports, not what the consumer calls it.

**One placement worth recording explicitly:** `session.py` sits in Foundation,
not Machinery, because `phase.py` (Contracts) calls `session.log_step` from
`Context.session_log`. The alternative — Contracts importing up into
Machinery — would invert the topology for one function. Session-dir io is
substrate; the placement follows the dependency, and the dependency is
correct.

## Consequences

**Positive:**

- A module's legal dependencies are knowable from its layer alone.
- The extension surface (Contracts) mechanically cannot grow dependencies on
  the execution plumbing, which is the structural version of ADR 0005's
  lesson.
- Adopted at zero violations, so the contract documents reality rather than
  aspiration.

**Negative:**

- A helper serving two Machinery modules must be hoisted to Foundation even
  when that feels premature. Forcing the "where does this belong?" question
  at the second consumer is the point, but the friction is real.
- Dynamic coupling — `env_method:` lookups by name, `engine_module` loading
  from config — is invisible to both rules. It stays a review concern.
- The layer table and `.importlinter` must not drift; a new module is
  classified in the commit that adds it.

## Governance

- Rule 1 is enforced by the import-linter contract in `.importlinter`, run
  as `make fitness`, which is part of the `make check` umbrella and therefore
  of CI and the release gate.
- Rule 2 is currently held by review, not machinery. If it is ever violated,
  the fix is to make the name public or hoist it, never to grant an
  exception; a mechanical check can be added the day review misses one.
