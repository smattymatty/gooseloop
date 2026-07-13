# Changelog

All notable changes to gooseloop are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `BranchPolicy.output_env` (ADR 0011, PROTOCOL §5): the engine names the
  env var its computed output path is injected under (default
  `OUTPUT_PATH`, so existing engines are untouched). The contract is
  verified, not trusted: before any phase runs, the framework checks that
  each registered recipe references `${<output_env>}` and refuses the pass
  with a hard error on a mismatch, so no model call is spent on a
  recipe/policy pairing that would silently disagree. hello_world now
  demonstrates the wire explicitly (`output_env="GREETING_FILE"` in the
  engine, `${GREETING_FILE}` in greet.yaml) and drops the reserved,
  unenforced `intent` tag from the teaching example.
- `run.lock`: one run at a time per loop root (ADR 0010, PROTOCOL §13).
  `begin_loop()` holds `<loop root>/run.lock` for the whole pass; a second
  run is refused before doing any work — CLI exit code 3, library callers
  get `gooseloop.RunLockHeldError`. Stale locks from crashed runs
  self-heal (dead pid → reclaim with a stderr warning). Consumers may read
  the lock (pid, started, engine, session_id) for exact "is a run in
  flight" detection; only gooseloop writes or removes it. Consuming
  projects should gitignore `run.lock`.
- `session.meta.json` now records `engine_module` (the resolved dotted
  module path) alongside the short `engine` slug, so a session is
  attributable to the engine that actually ran it, not the loop's default.

- `gooseloop run <engine>`: run any engine by short name (`gooseloop run
  doc_drift`) or dotted module path, as a positional argument. Short names
  resolve by scanning the loop root (ADR 0009); ambiguity is refused with
  the candidate list, never guessed. `gooseloop.config.resolve_engine_module`
  is public so consumers resolve names the same way the CLI does.
- `gooseloop engines` now lists every engine in the loop root and marks the
  default, instead of printing only the configured module.
- `gooseloop.introspect` (PROTOCOL §7): `list_env_methods()` enumerates the
  methods an Environment offers as `env_method:` sources (with their first
  docstring line), and `preview_source()` / `preview_recipe_context()`
  dry-run context sources — stat, never read; check env_methods exist,
  never call them. Built as the data layer for context tooling
  (gooseloop-dash's source chips, glob previewer, and context editor).
- `gooseloop recipe --sources NAME [--json] [-e MODULE]`: preview every
  context source of a recipe against an engine's env scope — glob matches
  with file sizes, unset env vars, missing files, unknown env_methods —
  plus the env_methods and env vars available. Exit 1 when a required
  source would fail the render, so it doubles as a preflight check.
  `--json` is the machine-readable face dashboards consume.

### Changed

- `gooseloop.toml`'s `engine_module` key is renamed to `default_engine` —
  it is the engine a bare `gooseloop run` runs, not a claim that a project
  has one engine. The old key keeps working with a rename nudge on stderr,
  and `LooperConfig.engine_module` remains as a deprecated property alias.
  Constructing `LooperConfig(engine_module=...)` directly must switch to
  `default_engine=...`.

## [0.1.1] - 2026-07-13

### Fixed

- `<name>.local.yaml` overlays now actually apply. The candidate path was
  built with `with_suffix`, which treats `.local` as a suffix and replaces
  it, collapsing `review.local.yaml` back to `review.yaml` — the base file,
  which always exists, so every run merged the base with itself and the
  per-machine overlay layer (PROTOCOL section 6, ADR 0008) was silently a
  no-op. Affected both the looper and `gooseloop recipe --resolve`. Caught
  by the new CLI test suite.

### Added

- The looper now writes `<session_dir>/summary.md` (the summary phase's full
  verbatim output) and `<session_dir>/ledger.json` (the FINAL
  operator_actions + outputs_written, not just the review's frozen seed).
  Previously both existed only in the terminal footer, gone once the
  scrollback was — found while building a session-reading dashboard that
  had nowhere to read either from.
- `gooseloop/py.typed` (PEP 561 marker), so consumers get real type
  information from gooseloop's own mypy-strict codebase instead of an
  untyped-package fallback. Ships in the wheel via
  `[tool.setuptools.package-data]`.

### Changed

- Relicensed from MIT to Apache 2.0, matching the license of the goose layer
  gooseloop builds on. Adds an explicit patent grant and Apache 2.0 section 5
  contribution terms. The already-published 0.1.0 remains MIT on PyPI
  (releases are immutable); 0.1.1 onward ships Apache 2.0.

## [0.1.0] - 2026-07-12

### Added

- The framework: Engine / Environment / Pipeline primitives, GooseLooper,
  the review -> body -> summary sandwich, BranchPolicy routing, recipe
  overlay merge, and the context prepend mechanism. PROTOCOL.md is the
  canonical contract.
- `gooseloop.toolkit`: stdlib-only engine helpers (Source parsing, hardened
  URL fetch, paste caps, template delimiter neutralizing, slug safety,
  JSON state io), extracted from three independent per-engine copies.
- `gooseloop.artifact`: versioned artifact contracts for engine composition
  (PROTOCOL section 12).
- Three reference engines: hello_world, git_recap, doc_drift.
