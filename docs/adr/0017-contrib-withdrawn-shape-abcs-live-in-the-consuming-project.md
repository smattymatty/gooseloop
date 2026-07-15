# ADR 0017 — Contrib withdrawn; shape ABCs live in the consuming project

**Status:** Accepted (2026-07-15)
**Context:** doc-drift honesty triage; supersedes the in-wheel half of [ADR 0005](0005-environment-abc-narrows-contrib-mixins.md)

## Context

ADR 0005 narrowed the framework `Environment` ABC to one abstract method
(`env_vars()`) and moved the leftover domain vocabulary onto shape-specific
ABCs. It then shipped those ABCs inside the `gooseloop` wheel, under
`gooseloop.contrib.*`:

- `CustomerPipelineEnvironment` — prospects, lifecycles, outreach, research,
  broadcast, discovery questions, founder journal.
- `ClaudeHandoffEnvironment` — handoff folder, target repo, dev-up probe,
  panel inventory, screenshot baseline.

Both are one specific business's domain vocabulary. `pip install gooseloop`
therefore delivered Storm's customer-acquisition and design-handoff contracts
to every user, inside a framework whose headline is "knows nothing about your
domain." A stranger importing `gooseloop.contrib.CustomerPipelineEnvironment`
found no explanation of what customer-acquisition pipeline it referred to —
made worse once the README's provenance note (which named the extraction) was
removed. ADR 0005 itself flagged the seam: *"Contrib mixins ship in the
`gooseloop` repo for now... Split-out becomes worth the cost when contrib has
third-party maintainers."* The split-out is now worth the cost for a different
reason than anticipated — not third-party maintainers, but the domain-agnostic
promise the framework makes at its front door.

## Decision

The framework ships **no** domain-specific Environment base classes.
`gooseloop/contrib/` is removed from the wheel. The framework's public
Environment surface is exactly `gooseloop.Environment` with its one abstract
method, `env_vars()`.

Shape-specific contracts are the consuming project's own code. A project that
wants a reusable domain ABC defines it in its own tree and has its concrete
environments subclass it:

```python
# in the consuming project, not in gooseloop
class CustomerPipelineEnvironment(gooseloop.Environment):
    @abstractmethod
    def build_digest(self) -> str: ...
    # ...the project's own domain vocabulary
```

Recipes still call whatever methods the concrete instance exposes via
`env_method:<name>`; the source kind dispatches on the live instance and does
not care about the class's lineage. Nothing about the recipe contract changes.

The narrowing from ADR 0005 stays. Only the *location* of the shape ABCs moves:
out of the framework, into the projects that actually have a domain.

## Consequences

**Good:**

- The framework is domain-agnostic in fact, not just in the headline. `pip
  install gooseloop` delivers no business's vocabulary.
- No orphaned domain contract for a stranger to puzzle over. The customer
  pipeline and handoff shapes live where their concrete implementations and
  their explanation already live.
- One less coupling between the OSS framework's release cadence and Storm's
  domain evolution: `CustomerPipelineEnvironment` v1 → v2 is a consuming-project
  change, not a framework release.

**Tradeoffs:**

- Cross-engine recipe portability that ADR 0005 credited to shared contrib ABCs
  now only holds within a project that shares its own ABC — it is no longer a
  framework-level guarantee. Accepted: portability across *unrelated* projects
  was always hypothetical, and the honesty of the boundary is worth more.
- The consuming projects (customer-gooser, the design-handoff engine) must
  re-home the two ABCs into their own trees. This is a mechanical move — the
  method bodies and recipe `env_method:` calls are unchanged — but until it is
  done, those projects' imports of `gooseloop.contrib.*` break. Sequencing is
  the operator's: land the re-home in each consumer, then this removal.

## Migration

1. In each consuming project, add the shape ABC to its own tree (e.g.
   `myproject/environments/customer_pipeline.py`) with the same abstract
   methods that were on `gooseloop.contrib.CustomerPipelineEnvironment`.
2. Repoint the concrete environment's base from
   `gooseloop.contrib.CustomerPipelineEnvironment` to the project-local ABC.
   No method bodies change; no recipe changes.
3. Remove `gooseloop/contrib/` from the framework (this ADR's commit).
4. Scrub the framework docs that advertised contrib (README, PROTOCOL §8,
   ADR 0000 topology, ADR 0005 status).

## Alternatives considered

- **Keep contrib, soften the headline.** Reframe the framework as "ships
  reference domain shapes you can ignore." Rejected: the shapes are one
  business's, not reference material, and an ignorable import is still an
  import a stranger has to understand and a coupling the framework has to
  version.
- **Keep contrib but replace the two ABCs with generic illustrative ones.**
  Rejected: a genuinely generic shape ABC is either empty (no value over bare
  `Environment`) or invents a domain the framework has no business asserting.
- **Move contrib to a separate `gooseloop-contrib` package.** The ADR 0005
  fallback. Rejected for now: with the shapes being one project's, they belong
  in that project, not in a second published package no one else consumes.
