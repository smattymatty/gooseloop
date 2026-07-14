# ADR 0012 — Phase telemetry: wide events + persisted transcripts

**Status:** Accepted (2026-07-13)
**Context:** the dashboard's body accordions could show only "Phase X
completed." — the single line session.log durably records per phase. The
phase's full goose transcript (every narrated command and file write) was
checked by the success predicate and discarded, and everything else the
framework knew at that moment (recipe, injected env, duration, attempts)
was never written down. A metric where a wide event belongs.

## Decision

1. **One wide structured event per phase, appended to
   `<session>/phases.jsonl` the moment the phase settles.** Append-per-line
   makes it live-tailable mid-run and crash-safe; consumers tolerate a torn
   final line. Fields: `seq`, `phase`, `kind` (`review|body|summary`),
   `recipe`, `label`, `status` (`ok|failed|skipped`), `started`,
   `duration_s`, `env`, `outputs`, `transcript`, `transcript_chars`,
   `error`, `skip_reason`, `attempts`.
2. **The full goose transcript persists per phase** at
   `<session>/transcripts/<seq>-<name>.txt`, referenced from the event.
   Full, always, no cap and no knob: session folders are local, gitignored,
   and operator-pruned as a unit, and a truncated transcript is exactly the
   one you need whole when a phase misbehaves. An unconsumed toggle would
   be the Pulse `retention_days` lie again.
3. **All three courses get events** — review, body, summary, uniformly
   (`kind` distinguishes them). The review transcript is the most
   debuggable artifact this adds: a review that emits malformed JSON
   finally leaves its evidence behind instead of evaporating with the
   retry.
4. **Failed phases keep their last attempt's transcript.**
   `run_goose_with_retry` gains an optional `stats` out-param (additive —
   existing callers unaffected) reporting `attempts` and the final
   attempt's output, so a `failed` event records both the error and what
   the model actually said.
5. **`env` records the phase-specific injection only** (routing params,
   output path — the per-phase cardinality that makes events comparable);
   the session-constant base env is recorded once in `session.meta.json`
   as `base_env`. Values cap at 500 chars. `outputs` is the per-phase
   delta of `ctx.artifacts["outputs_written"]` — only what the framework
   observed, no inferred file reads, no invented structure.
6. **PROTOCOL §14 makes it a public contract.** Any consumer reads the
   same artifacts — the dashboard's accordions today, `jq` tomorrow, a
   fleet-wide rollup later. New event keys may be added freely (additive
   per §9); existing keys never change meaning. One writer: gooseloop.
7. **Telemetry can never fail a pass.** Recording is best-effort
   (`OSError` swallowed); the work's own success is judged exactly as
   before.

## Consequences

- session.log stays what it is — the human-readable narrative — and every
  existing parser keeps working; the wide events live beside it, not
  inside it.
- Pre-telemetry sessions simply have no phases.jsonl; consumers fall back
  to log parsing (the dashboard keeps its segmentation path for them).
- Disk cost is trivial at this scale (the longest observed run, 23
  phases, adds well under a megabyte), and the artifacts age out with
  their session folder — no new retention surface.
- The observability framing is deliberate: high-dimensionality events
  plus full-context transcripts turn "which phase wrote this file, and
  why is #14 slow" from archaeology into a query.

## Amendment (2026-07-14): events keep the input, not just the output

Reviewing the design against the "can you keep drilling without a dead
end" test showed a wall: we persisted everything the model SAID and
nothing it SAW. The rendered recipe — context blocks filled, env
substituted — was a temp file `prepared_recipe` deleted at phase end, so
any anomaly that traced to "what was actually in the pasted context?"
stopped there, and the prompt had to be reconstructed from source code
and guesswork.

Events now carry `prompt` / `prompt_chars`, pointing at
`transcripts/<seq>-<name>.prompt.yaml`: the exact bytes handed to goose,
captured before the spawn (so failed phases keep theirs — failure
investigations need the input most) and redacted through the same egress
tripwire as transcripts. A secret found in a prompt flags the event and
raises a rotate action, because a secret pasted into the input reached
the provider the same as one printed out. Additive keys per §9;
pre-amendment sessions read with `prompt: null`.

## Amendment (2026-07-14): every retry attempt keeps its evidence

`attempts: 3` said a phase was retried twice and threw away what the two
failures actually said — the last dead end from the drill-down audit.
Events now carry `attempt_log`: one record per goose invocation with
outcome, returncode, duration, the retry delay that followed, and a
transcript ref. Non-final attempts persist their full output as
`transcripts/<seq>-<name>.attempt-<n>.txt`; the final entry points at
the phase transcript (never stored twice). Retry outputs pass through
the same egress tripwire — a secret printed by attempt 1 reached the
provider even if attempt 2 settled clean, so it redacts, flags, and
raises the rotate action like any other leak. Additive keys per §9.
