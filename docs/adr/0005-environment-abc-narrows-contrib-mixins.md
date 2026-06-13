## ADR 0005 — Environment ABC narrows; contrib mixins carry shape

**Status:** Accepted (2026-06-04)
**Context:** OSS extraction grill; sequel to [ADR 0004](0004-engine-and-environment-as-siblings.md)

## Context

ADR 0004 promised the OSS second-user story: "BetaCo writes `BetaCoEnvironment`, points it at their paths and prospect format, and runs the existing customer-acquisition engine unchanged." The Environment primitive landed, but the abstract surface ended up Storm-shaped: eleven `@abstractmethod` entries on `gooseloop.Environment`, of which only `env_vars()` is genuinely framework-level. The other ten — `core_dir`, `journal_path`, `questions_dir`, `insight_dir`, `lifecycle_dirs`, `lifecycle_dir`, `output_dir`, `build_digest`, `journal_text`, `manifest_text`, `repo_activity`, `questions_listing` — are vocabulary from one specific domain (customer-acquisition pipelines).

A second-user writing a non-customer-pipeline engine (e.g. a Claude design-handoff engine) inherits ten stub obligations that have nothing to do with their work. The "tier 2 scope" hedge in ADR 0004 ("paths + opaque loaders") was not enforced; loaders smuggled their shapes through method names.

The 2026-06-04 OSS-extraction grill rejected the wide ABC and asked for the Django class-based-view inheritance pattern: a minimal framework primitive plus shape-specific mixins.

## Decision

The framework `gooseloop.Environment` ABC has exactly one abstract method:

```python
class Environment(ABC):
    @abstractmethod
    def env_vars(self) -> dict[str, str]:
        """Env vars merged into every recipe call."""
```

Shape-specific contracts live as separate ABCs under `gooseloop.contrib.*`:

```python
# gooseloop/contrib/customer_pipeline.py
class CustomerPipelineEnvironment(Environment):
    """Environment shape for customer-acquisition pipelines."""
    @abstractmethod
    def lifecycle_dirs(self) -> list[tuple[str, Path]]: ...
    @abstractmethod
    def build_digest(self) -> str: ...
    @abstractmethod
    def journal_text(self) -> str: ...
    # ...etc, the Storm-shaped methods

# gooseloop/contrib/claude_handoff.py
class ClaudeHandoffEnvironment(Environment):
    """Environment shape for Claude design-handoff engines."""
    @abstractmethod
    def handoff_dir(self) -> Path: ...
    @abstractmethod
    def target_repo(self) -> Path: ...
    @abstractmethod
    def dev_up_probe(self) -> str: ...
    # ...etc
```

Concrete environments choose their lineage:

```python
class StormEnvironment(CustomerPipelineEnvironment):
    # implements both the framework primitive AND the customer-pipeline contract
    ...

class WebsiteHandoffEnvironment(ClaudeHandoffEnvironment):
    # implements the framework primitive AND the handoff contract
    ...
```

An engine author with no fit for any contrib mixin subclasses bare `Environment` and writes only `env_vars()`. Recipes call the env's named methods via `env_method:<name>` regardless of mixin lineage; the source kind is unchanged.

## Consequences

**Good:**

- A new engine for a brand-new domain implements one abstract method, not eleven. The OSS-second-user story works at the front door.
- Shape-specific contracts are versionable independently. `CustomerPipelineEnvironment` v1 → v2 doesn't perturb `ClaudeHandoffEnvironment`.
- Mirrors the Django class-based-view inheritance hierarchy operators already know from web work.
- Reusable contrib environments unlock cross-engine recipe portability: any engine consuming `CustomerPipelineEnvironment` can swap in a `review.yaml` written against that contract.
- Removes the dishonesty in ADR 0004's "tier 2" claim. Tier 2 is now actually what the framework ABC enforces.

**Tradeoffs:**

- Three layers in the concrete class hierarchy (`Environment` → contrib mixin → concrete). Acceptable for the contract clarity; matches Django depth.
- Contrib mixins ship in the `gooseloop` repo for now. If one grows heavy or develops third-party variants, it can be split into a separate package later. Not premature.
- Storm migration: `StormEnvironment` becomes `class StormEnvironment(CustomerPipelineEnvironment)`. The eleven methods stay on the customer-pipeline ABC; Storm subclasses provide concretes for that ABC. One file move; no semantic change to recipes.

## Migration plan

1. Create `gooseloop/contrib/__init__.py` and `gooseloop/contrib/customer_pipeline.py`. Move the ten Storm-shaped abstract methods from `gooseloop/environment.py` into `CustomerPipelineEnvironment`.
2. `gooseloop/environment.py` keeps only `env_vars()`.
3. `environments/storm_customer/environment.py` changes its base from `gooseloop.Environment` to `gooseloop.contrib.CustomerPipelineEnvironment`. No method body changes.
4. Recipe `env_method:` calls keep working unchanged (the source kind dispatches on the concrete instance, not the ABC).
5. Update the engine's documentation to note which contrib mixin (if any) it expects.
6. Create `gooseloop/contrib/claude_handoff.py` when the handoff engine is implemented; not before.

## Alternatives considered

- **Keep the wide ABC** (status quo from ADR 0004). Rejected because it blocks the OSS-second-user story at the abstraction boundary, contradicting the original goal of ADR 0004.
- **One framework ABC, every engine inlines its own contract.** Rejected because it loses cross-engine portability for shape-specific recipes. A customer-acquisition `review.yaml` should be usable by any engine that conforms to the customer-pipeline shape, not just Storm's.
- **Protocol classes instead of ABCs.** Considered. Rejected because the explicit subclassing of contrib mixins matches Mathew's Django mental model better than structural subtyping. Protocol classes would have lower ceremony but worse discoverability ("which methods does this env need to implement?" is answered by `help(CustomerPipelineEnvironment)`, not by inspecting recipe usage).
- **Move contrib to a separate repo (`gooseloop-contrib`).** Rejected for now. With a single concrete user (Storm) and one upcoming user (claude-handoff), shipping contrib in-tree is simpler. Split-out becomes worth the cost when contrib has third-party maintainers.
