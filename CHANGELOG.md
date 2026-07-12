# Changelog

All notable changes to gooseloop are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
