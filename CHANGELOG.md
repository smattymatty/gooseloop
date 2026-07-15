# Changelog

All notable changes to gooseloop are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- doc-drift canonical-first map-gap discovery: a file that changed within
  the discovery window (`[doc_drift] discovery_window_days`, default 7), sits
  beside a watched canonical, shares its extension, and is not itself
  watched, is raised as an operator action proposing the exact map edit.
  The doc-map is never machine-written; discovery is a helper to the
  operator's seal, not a hand on the wheel. The roaming derived-first
  search is deferred.

### Changed

- doc-drift triage no longer trusts mtime after a pair has history. The
  "derived at least as recent as the canonical" shortcut fires only on
  first sight; once state exists, any token change from the last verified
  revision is a candidate. A timestamp bump (a date change, a typo fix) is
  not proof of reconciliation, and the old shortcut buried real drift in
  the KEEP-quiet direction.

- doc-drift adds a touches gate to keep that honesty affordable: each
  draft records, as a marker, which canonicals it actually relied on, and
  a later change to a canonical a view never relied on is skipped without
  a draft. Pure set membership over state, zero added model cost, and
  fails safe (an unknown or empty touches set never narrows).

- Phase events persist the model's INPUT, not just its output (ADR 0012
  amendment): `prompt`/`prompt_chars` point at
  `transcripts/<seq>-<name>.prompt.yaml`, the rendered recipe exactly as
  goose received it — captured before the spawn so failed phases keep
  theirs, redacted through the egress tripwire, and a secret found in a
  prompt flags the event and raises a rotate action. Closes the
  "investigation dead-ends at what was actually in the pasted context"
  gap.

- §14 events carry `attempt_log`: one record per goose invocation
  (outcome, returncode, duration, retry delay), with every NON-final
  attempt's full output persisted as
  `transcripts/<seq>-<name>.attempt-<n>.txt` — retried phases keep the
  evidence of what the failed tries actually said. Retry outputs pass
  through the egress tripwire like transcripts and prompts: a secret in
  a failed attempt flags the event and raises the rotate action.

- Saved runs keep the boundary's mask MAP (`boundary-masks.json`:
  patterns in force + exact paths masked, paths only), so boundary
  anomalies diff across runs. The map is itself masked — its basename is
  on the built-in floor and the current run's copy is appended to the
  spawn prefix — because a list of where secrets live is denied to the
  goose the same as what it maps.

- THE BOUNDARY (`gooseloop.boundary`, PROTOCOL section 15, ADR 0015):
  when bubblewrap is available, every goose spawn runs inside a mount
  namespace where credential-shaped paths do not exist — a built-in deny
  floor (`.env*`, `*.pem`, `*.key`, `credentials*`, `~/.ssh`, `~/.aws`,
  `~/.gnupg`, …) always applies, and a committed `.gooseignore` at the
  loop root extends it (gitignore-style patterns, no `!` negation).
  Masked files read empty, masked directories list empty; everything
  else — write access, network, environment — is untouched. A
  `.gooseignore` without bubblewrap refuses the run with exit 4 before
  any session artifact exists; neither present is a one-line stderr
  nudge. `session.log` records `boundary: N paths masked (bwrap)` per
  pass. New public exports: `boundary`, `BoundaryUnavailableError`,
  `GOOSEIGNORE_FILENAME`.

- SECURITY.md: the threat model (the attacker is a line of text), the
  four defense layers, what is deliberately NOT protected, and the
  private reporting channel. The egress-tripwire decision record is
  ADR 0014.

- `gooseloop.guardrails` (egress tripwire): phase transcripts and
  summary.md are scanned for secret-shaped content (known token formats,
  KEY=value assignments, private-key blocks) and redacted BEFORE they
  persist; a hit flags the §14 event and auto-raises a rotate-credentials
  operator action. The context preamble now declares every pasted block
  untrusted data, and hello_world validates that guest-list lines look
  like names. Signature-based seatbelts, labeled as such — containment is
  a separate layer.

- hello_world reads its guest list from a config-driven names file
  (`[hello_world] names = "names.txt"`, one name per line, # comments
  skipped) instead of a hardcoded list — procured like every other input:
  `names.example.txt` committed, `names.txt` gitignored, empty/missing
  file refused at precheck with the exact cp command.

- routing[] is now the whole pass's plan of record (ADR 0013, PROTOCOL
  section 2). Every entry carries `routed_by: "model" | "engine"`:
  validation stamps model-emitted entries, and the framework appends one
  `routed_by: "engine"` entry per engine-built body phase before the
  review is persisted — so an engine-routed pass (doc_drift drafting 23
  patches) no longer persists a review claiming it planned nothing.
  Engine entries are record, never instruction: phases are only built
  from model entries. Pre-0013 artifacts read as model-routed.

### Added

- §14 events carry `actions` — the operator actions each phase raised
  (per-phase ledger delta). doc_drift now raises its seal decision the
  moment a drift=yes draft lands (body-phase post_process, deduped
  against the summary's re-raise), so decisions surface mid-run and a
  crashed pass keeps what it raised instead of losing it with the
  unwritten ledger.

- `Engine.injected_env()` (PROTOCOL §7 introspection): engines declare the
  env vars they inject at phase-BUILD time (per routing entry / per body
  phase), which by nature never appear in the static env scope. Preview
  tooling (`recipe --sources`, dashboards) renders a declared
  `env_file:` var as "injected per phase by the engine" instead of a
  false "unset" failure — doc_drift's per-pair `CONTEXT_FILE` bundle was
  the motivating red herring. doc_drift also gains
  `env_method:recent_journal` (last 5 dailies + 2 weeklies, capped) as a
  DECLARED context source on the draft recipe, replacing the invisible
  in-bundle journal section.

- git_recap rewritten as a journal engine (grill, 2026-07-13): one
  combined daily entry per date across all configured repos
  (`journal/daily/<date>.md`, sectioned by project), plus a weekly review
  when an ISO week closes (`journal/weekly/<year>-W<ww>.md`). Per-repo
  commit watermarks (`git-recap.state.json`) make each daily cover exactly
  the commits no daily has covered — gaps and same-day amend runs
  included; watermarks advance only after the entry verifiably writes.
  The review routes `daily`/`weekly` and deterministic `skip_when`
  seatbelts verify every routing (wrong date, weekly-not-due, nothing
  new). Replaces the per-commit recap files; `[git_recap]` config keys
  are now `repos`, `author`, `journal_dir`, `state`, `first_run_days`.
- doc_drift's "what changed in the canonical" bundle section now reads
  git_recap's journal dailies (date-matched to the commits that touched
  the canonical) instead of the retired per-commit recap files; the
  engines compose with zero config when they share a loop root
  (`[doc_drift] journal_dir` overrides; borrows `[git_recap] journal_dir`
  otherwise).

- Phase telemetry (ADR 0012, PROTOCOL §14): every saved run now writes
  `<session>/phases.jsonl` — one wide structured event per phase (review,
  body, and summary uniformly: status, duration, injected env, recorded
  outputs, attempts), appended as each phase settles so it live-tails —
  plus the phase's full goose transcript under `<session>/transcripts/`.
  Failed phases keep their last attempt's transcript, so a review that
  emitted malformed JSON finally leaves evidence. The session-constant
  base env is recorded once in `session.meta.json` as `base_env`.
  `gooseloop.telemetry` (public) ships the torn-line-tolerant reader.
  `run_goose_with_retry` gains an optional `stats` out-param (additive)
  reporting attempts and the final attempt's output.

- `BranchPolicy.output_env` (ADR 0011, PROTOCOL §5): the engine names the
  env var its computed output path is injected under (default
  `OUTPUT_PATH`, so existing engines are untouched). The contract is
  verified, not trusted: before any phase runs, the framework checks that
  each registered recipe references `${<output_env>}` and refuses the pass
  with a hard error on a mismatch, so no model call is spent on a
  recipe/policy pairing that would silently disagree. hello_world now
  demonstrates the wire explicitly (`output_env="GREETING_FILE"` in the
  engine, `${GREETING_FILE}` in greet.yaml), registers a `skip_when` so
  re-runs skip names already greeted on disk (the git_recap idempotency
  pattern, now in the first example an author reads), and drops the
  reserved, unenforced `intent` tag from the teaching example.
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
