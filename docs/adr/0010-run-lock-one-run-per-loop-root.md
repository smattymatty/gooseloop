# ADR 0010 — `run.lock`: one run at a time per loop root

**Status:** Accepted (2026-07-13)
**Context:** prerequisite for any consumer that starts runs (the dashboard's
"run and manage loops" pass); also a standing today-bug for plain terminal
use — two overlapping `gooseloop run` invocations race on the working tree
and on engines' cross-run state

## Context

gooseloop had no concurrency story. Two runs of the same loop root could
interleave: both drive goose against the same working tree, both write the
same `sessions_dir`, and engines like doc_drift read-modify-write cross-run
state (`state.json`). Nothing detected it, nothing refused it. Separately,
consumers had no honest way to answer "is a run in flight right now" — the
dashboard shipped a session.log-mtime heuristic and documented it as a
best-effort guess.

## Decision

1. **`GooseLooper.begin_loop()` acquires `<loop root>/run.lock`** before any
   phase runs and removes it in a `finally`. Library-level, not CLI-level:
   any embedder gets the same safety.
2. **Scope is the loop root, not the engine.** One run at a time per
   `gooseloop.toml`, even for different engines. The working tree,
   `sessions_dir`, and cross-run state are root-shared; parallel engines in
   one root is exactly the racy case. Per-engine parallelism can be a later
   ADR if a real need appears.
3. **Every run locks — no flag exceptions.** `--no-save` and
   `--review-only` still drive goose, which can touch the working tree.
   `recipe` and `engines` are read-only and never lock.
4. **Acquisition is `O_CREAT | O_EXCL`.** The lock body is JSON:
   `pid`, `started` (ISO 8601 UTC), `engine` (resolved dotted module path),
   `session_id` (null for `--no-save`).
5. **Stale locks auto-reclaim with a warning.** If `run.lock` exists but its
   pid is dead, the run is provably not in flight: replace the lock and warn
   on stderr naming the crashed session. A reused pid can only make a stale
   lock look live — it refuses, never wrongly reclaims. Where pid liveness
   cannot be probed safely (non-POSIX), refuse conservatively.
6. **A held lock refuses with exit code 3** and a message naming the engine,
   pid, and start time — distinct from exit 1 (run error) and 2 (usage), so
   supervisors can tell "busy" from "failed".
7. **Public contract, one writer.** PROTOCOL §13 documents the fields,
   lifecycle, and staleness rule. Consumers may read `run.lock`; only
   gooseloop creates, replaces, or removes it. A canceller signals the pid
   and lets gooseloop's own `finally` clean up.
8. **`session.meta.json` gains `engine_module`** (the resolved dotted path)
   alongside the existing short `engine` slug, so finished sessions can be
   attributed to their real engine, not the loop's default.

## Consequences

- Additive per PROTOCOL §9: no field removed, no behaviour changed for
  sequential use beyond a new file appearing during runs (consuming
  projects gitignore `run.lock`; PROTOCOL §10's layout example gains the
  line).
- The dashboard's mtime heuristic dies: liveness = lock exists and pid is
  alive; the lock's `engine` field also fixes live-session engine
  attribution.
- Crash recovery is automatic (dead pid → reclaim), so no operator chore
  accrues from SIGKILL or power loss.
- The one-writer rule means a consumer that deletes `run.lock` is violating
  the protocol, not exercising an API.
