# ADR 0004 — Engine and Environment as parallel primitives

**Status:** Accepted (2026-06-01)
**Context:** GooseLooper architectural refactor; sequel to [ADR 0001](0001-engine-returns-pipeline-of-phases.md)

## Context

ADR 0001 introduced the Engine as a pluggable component that owns "what a particular pipeline does." In practice, the first engine (`StormCustomerEngine`) ended up owning two distinct kinds of state:

1. **Pipeline behaviour** — phase factories, branching, intent reconciliation, retry/skip rules, recipe authorship. This is genuinely engine-specific: a security-monitoring engine would have completely different phases.
2. **Project-specific data** — the storm_customer.toml paths, the digest builder that knows what a "prospect" file looks like, the journal location, the storm-context-manifest. None of this is intrinsic to "customer-acquisition pipelines"; it's intrinsic to **Storm Developments**.

These two kinds of state were conflated. A hypothetical second user (call them BetaCo) wanting to run the customer-acquisition pipeline against their own prospects would have to fork the engine entirely, because Storm's specific paths, prospect schema, and journal location are baked into engine code.

The 2026-05-31 grilling session crystallised the missing primitive: **what the pipeline does** (engine, verbs) is a different concern from **what the pipeline has access to** (project data, nouns). The Pass 2 context-prepend mechanism (recipes declare inputs, engine pre-renders to literal text) made this gap acute — recipes need access to project data, but the engine isn't a clean place to host it if we want pluggable projects.

## Decision

Introduce **Environment** as a sibling primitive to Engine. Both are passed into the Looper:

```python
env = StormEnvironment.from_toml("storm_environment.toml")
engine = CustomerAcquisitionEngine()
looper = GooseLooper(engine=engine, environment=env)
```

The canonical rule, sharp enough to settle every future boundary question:

> **The engine is what it does. The environment is what it has access to.**

### Engine owns (verbs)

- Pipeline shape (`pipeline()` returning Phases)
- Branching logic and `post_process` hooks
- Recipes (the prompts; engine-authored, environment-parameterised)
- Intent reconciliation, scoring algorithm, retry/skip rules
- The Phase contract itself

### Environment owns (nouns)

- Paths (input lifecycles, output dirs, journal, manifest, questions workspace)
- Project-data loaders: `build_digest()`, `journal_text()`, `load_prospects()`, `questions_files()`
- Project-specific configuration in `storm_environment.toml` (renamed from `storm_customer.toml`)
- Any future content that varies per-project (brand voice docs, prospect schema, etc. — see "Tier" discussion below)

### Recipes bridge the two

The Pass 2 `context:` schema gains a fourth source kind:

```yaml
context:
  - label: "PROSPECT DIGEST"
    source: env_method:build_digest    # calls environment.build_digest() at prepend time
  - label: "FOUNDER JOURNAL"
    source: env_method:journal_text
```

`context_prepend` receives the Environment instance from the Looper and resolves `env_method:` sources by calling the named method. This keeps recipes engine-owned (the prompts and JSON contracts are pipeline behaviour) while letting them paste environment-supplied content.

### Wiring

`GooseLooper(engine=engine, environment=env)`. The Looper passes Environment through to:

- `engine.precheck(env, ctx)`
- `engine.pipeline(env, ctx)`
- `context_prepend.render_recipe_with_context(path, env, extra_env)`

Engines that don't need an environment can ignore the parameter; engines that do can call into it freely.

## Consequences

**Good:**

- Pluggable projects without forking engine code. BetaCo writes `BetaCoEnvironment`, points it at their paths and prospect format, and runs the existing customer-acquisition engine unchanged. This is the OSS-second-user story that ADR 0001 promised but didn't yet enable.
- The "engine = verbs, environment = nouns" rule is sharper than a tiered list of what-goes-where. It generates boundary answers automatically (digest is data → Environment; phase order is behaviour → Engine).
- Recipes become first-class consumers of project data via `env_method:`. The 2026-05-31 alpha-owl-wanders failure mode is structurally impossible for any input that flows through `env_method:`.
- `storm_environment.toml` is honest about what it configures. Previously `storm_customer.toml` named the *engine* but configured the *environment*.

**Tradeoffs:**

- Engine method signatures grow an `env` parameter wherever they need project data. `engine.pipeline(env, ctx)` is wider than `engine.pipeline(ctx)`. Acceptable cost for the clean separation.
- `context_prepend` is now coupled to Environment (it needs the instance to resolve `env_method:`). The gooseloop package was meant to be engine-agnostic; it is now also *environment-aware*. This is a real coupling, but the alternative (env-method dispatch happens inside the engine, then writes temp files for context_prepend to read) was strictly more moving parts.
- Migration cost: existing `StormCustomerEngine` code that owns paths and the digest builder has to move to `StormEnvironment`. Tests, recipes, and runner.py all need touching. One coherent PR; not landed yet at the time of this ADR.

## Migration plan

The bash-migration for `pipeline-review.yaml` (the original 2026-06-01 grill topic) is *blocked on* this Environment landing. Doing the recipe migration first with workarounds, then re-migrating to `env_method:`, was rejected as throwaway work.

Order of changes:

1. Create `StormEnvironment` class with paths + loaders. Read from renamed `storm_environment.toml`.
2. Add `env_method:` source kind to `gooseloop/context_prepend.py`. Thread Environment from Looper through `_prepared_recipe` to `render_recipe_with_context`.
3. Update `GooseLooper` to accept `environment=` parameter; pass it into engine and context_prepend.
4. Update engine method signatures (`pipeline`, `precheck`) to accept `env`.
5. Move `storm_digest.build_digest`, `python.config.journal_path`, the prospect loader, and the questions lister into `StormEnvironment`. Engine calls into env for these.
6. Migrate `pipeline-review.yaml` to use `context:` block with `env_method:` sources, drop inline bash.
7. Tighten `max_turns:` now that the recipe doesn't need turns for input-gathering bash.

## Alternatives considered

- **Engine owns Environment internally** (`Engine(environment=env)`, Looper sees only Engine). Cleaner from the Looper's perspective; engine method signatures don't grow. Rejected because context_prepend (which lives in `gooseloop/`) couldn't reach Environment, forcing env-derived inputs through temp files or env vars. The sibling design unlocks `env_method:` as a direct bridge.
- **Tier 1 scope: Environment is just paths.** Thin config wrapper, no loaders. BetaCo still has to fork engine code to change prospect schema or digest format. Rejected because it doesn't actually unlock the plug-ability story — the abstraction would be cosmetic.
- **Tier 4 scope: Environment also owns scoring weights.** Maximum project autonomy. Rejected (for now) because the line between "scoring algorithm" (engine) and "scoring weights" (environment) is real but the current code has one prospect-scoring system that works for Storm; abstracting weights pre-emptively is YAGNI. Revisit if a second environment exists.
- **Directory reorganisation (`environments/storm/`, `engines/customer_acquisition/`)** in the same change. Future-proofs the OSS layout but bigger restructure; touches every import path and test fixture. Deferred to a follow-up.
