# ADR 0002 — Decorator-only engine registry

**Status:** Superseded by [ADR 0009](0009-default-engine-and-short-name-resolution.md) (2026-07-13)
**Context:** GooseLooper architectural refactor — engine discovery

> **Superseded.** The `@register_engine` decorator and its runtime registry
> described below never shipped. The framework instead reads the engine class
> from the module named by `default_engine` in `gooseloop.toml` (or a short
> name resolved against the loop root); there is no decorator, no
> self-registration, and no registry dict. See `gooseloop/engine.py` and
> ADR 0009 for the live model. Read this for the reasoning that was abandoned,
> not for how engines are discovered today.

## Context

Engines need a way to be looked up by string name for CLI use (`gooseloop run customer-acquisition`) and config (`engine = "customer-acquisition"` in `gooseloop.toml`). For programmatic use, passing the class directly is sufficient and bypasses any registry.

The canonical Python pattern for plugin discovery is `[project.entry-points]` in `pyproject.toml`. Pip-installing a plugin package makes it automatically discoverable; no import statement required. It's how pytest plugins, click extensions, and most modern plugin systems work.

## Decision

The Looper exposes a `@register_engine("slug")` class decorator. Engines self-register at module import time. The Looper's config or CLI takes a list of engine module paths to import, which triggers the decorators and populates the registry.

Entry points are explicitly **not** used.

## Consequences

**Good:**

- Minimal mechanism. The registry is a dict; the decorator inserts into it. Easy to reason about, easy to debug.
- No `pyproject.toml` ceremony required to ship a new engine — a single-file engine in a script directory works.
- Fits the project philosophy: own your tools, build for the problem in front of you, not for a hypothetical plugin ecosystem.

**Tradeoffs:**

- Operators must list engine modules in config (or on the CLI) so the Looper knows what to import. Slightly more friction than "pip install and it's there."
- If a third-party engine ecosystem ever emerges, switching to entry points becomes a migration. We accept this — when the second or third external engine ships, the answer is to add entry-point support *alongside* the decorator, not replace it.

## Alternatives considered

- **Entry points + decorator fallback.** Standard Python plugin pattern. Rejected for now because we have one engine and no external authors; the ceremony is overhead until both change.
- **No registry — fully-qualified dotted paths.** `gooseloop run --engine customer_engine:CustomerAcquisitionEngine`. Smallest surface area but loses the short-name UX in config and CLI. Rejected on ergonomics.
