# Changelog

All notable changes to gooseloop are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
