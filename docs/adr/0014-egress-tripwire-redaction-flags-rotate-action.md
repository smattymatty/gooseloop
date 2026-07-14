# ADR 0014 — egress tripwire: redact secret-shaped output, flag the event, raise the action

**Status:** Accepted (2026-07-13)

## Context

Recipes paste untrusted content (files, model outputs, fetched pages) into
prompts that drive a tool-equipped model. A hostile line in that content can
steer a shell-capable phase into reading credentials and printing them, at
which point the values sit in the phase transcript, in `summary.md`, and in
the provider's logs. Input-side filtering cannot be made reliable: natural
language has no grammar of intent. The framework CAN act deterministically
on the way out, at the single choke point where phase output becomes a
persisted artifact.

## Decision

`gooseloop.guardrails.scan_and_redact(text)` runs on every phase transcript
and on `summary.md` before either is written:

1. **Redact.** Two passes, order load-bearing: KEY=value assignments whose
   key NAME says secret (`*KEY`, `*SECRET`, `*TOKEN`, `*PASSWORD*`) have
   their value replaced while the name survives — this runs FIRST, because
   a known token shape inside an assignment value would otherwise be
   replaced mid-value and leave fragments. Then known token signatures
   (Stripe, AWS, GitHub, PyPI, Slack, age, PEM blocks including blocks
   torn at end-of-output) are replaced wholesale.
2. **Flag.** The phase's wide event (§14) gains a `flags` entry naming the
   finding kinds and counts — kinds and counts only, never values.
3. **Raise.** The phase appends a `ROTATE CREDENTIALS` operator action to
   the ledger, so the leak is a loud card in the seal queue within one
   poll, not a quiet line in a file nobody tails.

Findings never carry the matched values, and redaction happens before any
byte reaches disk. Detection is signature-based and therefore a SEATBELT:
it catches known shapes, not everything, and anything printed has already
reached the model provider — rotation, not redaction, is the remedy the
raised action demands. Containment is a separate layer (ADR 0015).

## Consequences

- Transcripts and summaries are safe to publish to dashboards and to keep
  long-term; the on-disk trail never carries a live credential the
  detector knows the shape of.
- A phase that trips the wire still counts as whatever it was (ok/failed);
  the tripwire never changes control flow. Telemetry-never-fails-a-pass
  (§14) extends to guardrails.
- False positives cost a redacted fragment of prose and a card the
  operator dismisses; false negatives cost nothing that wasn't already
  lost. The asymmetry is why the patterns lean greedy.
