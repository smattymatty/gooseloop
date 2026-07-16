# ADR 0018 — Summary deliverable is marker-wrapped; verbatim lives in transcripts

**Status:** Accepted (2026-07-15)
**Context:** journey-witness summary leaked source into the operator report; amends the "Summary output" contract in [PROTOCOL.md](../../PROTOCOL.md) §3, mirrors the review boundary from [ADR 0006](0006-pipeline-named-slots-framework-owns-review-summary-order.md) / [ADR 0007](0007-review-output-schema-operator-actions-ledger.md), and leans on the phase transcripts from [ADR 0012](0012-phase-telemetry-wide-events-and-transcripts.md)

## Context

The summary phase prints markdown to stdout and the looper captured that stdout
verbatim into `summary.md`. PROTOCOL.md §3 codified this: *"the looper writes the
summary phase's full output verbatim to `summary.md` — the one durable copy once
the terminal scrollback is gone."*

That contract predates the phase transcripts added in ADR 0012. Today the full
verbatim phase output is **already** persisted a second time, under
`<session_dir>/transcripts/NN-summary.txt`. So "the one durable copy" was stale:
there were two byte-identical copies, and `summary.md` was doing double duty as
both the operator's report and a redundant transcript.

The cost of that double duty surfaced on 2026-07-15. A `journey-witness summary`
run carried the `developer` extension, and the model explored the codebase —
`tree`, `analyze`, and reading source files including `engines/_corpus.py` —
before printing its ~47-line report. All of that tool output landed in stdout,
so "verbatim" faithfully wrote a 1096-line `summary.md` in which the actual
report was the last 47 lines and the rest was goose transcript and dumped Python.
The dashboard, trying to fold the goose preamble away, then mistook a dumped
`# python comment` for a markdown heading and rendered source as the report.

The review phase had already solved the general version of this problem. Review
output is a framework-owned boundary (ADR 0006/0007, hardened in commit c507db8):
the model wraps its JSON deliverable in `<<<DELIVERABLE_JSON>>>` /
`<<<END_DELIVERABLE>>>`, the framework extracts the last wrapped block, and the
framework appends the contract to every review prompt so correctness does not
depend on each recipe copying it. Summary had no such envelope, so the framework
had no way to tell the report from the exploration around it.

## Decision

The summary deliverable is marker-wrapped, the markdown analogue of the review's
JSON framing. The summary phase wraps its report in:

```
<<<SUMMARY_MD>>>
# ...the markdown report...
<<<END_SUMMARY>>>
```

The looper extracts the content between the markers and writes only that to
`summary.md` (`extract_summary_markdown`, last opener wins — same rationale as
the JSON canonical sentinel). As with review, the framework appends the contract
(`SUMMARY_OUTPUT_CONTRACT`) to every summary prompt, so the envelope does not
depend on private recipes copying it; recipes still show the shape.

`summary.md` becomes the operator-facing report, not a redundant transcript. The
full verbatim phase output is **not** lost: it remains under `transcripts/` per
ADR 0012. This resolves the stale "one durable copy" premise by naming the two
copies and giving each one job — `summary.md` is the report, the transcript is
the record.

**Fail toward keeping content.** If no marker is present — a legacy recipe, or a
model that ignored the contract — the looper falls back to writing the full
output verbatim (the old behavior) and logs that the recipe should be tightened.
An empty or whitespace-only payload between markers is treated as "no marker" for
the same reason. A summary artifact must never fail toward an empty file; the
degraded case is the noisy old artifact, not a lost one.

The summary phase is **not** gated on the marker the way review is gated on its
schema. Review drives routing, so an unparseable review must retry or abort;
`summary.md` drives nothing downstream, so the graceful fallback is correct and a
new failure mode would be a regression.

## Consequences

**Good:**

- The operator report is the report. Exploration transcript and dumped source
  stop leaking into `summary.md` regardless of what tooling a summary recipe
  carries or how a model misbehaves — the robustness comes from the envelope,
  not from trusting the model to stay quiet.
- One clean mental model across phases: review wraps JSON, summary wraps
  markdown, the framework owns both envelopes and appends both contracts.
- No data loss. The verbatim record moves to the artifact that already held it
  (the transcript), instead of being duplicated in the report.

**Costs / risks:**

- A recipe whose model omits the markers silently falls back to the old noisy
  artifact. The logged warning is the only signal; there is no hard gate. This
  is deliberate (see fail-safe above) but means a drifting recipe degrades
  quietly rather than loudly.
- Downstream readers that treated `summary.md` as the full transcript must read
  `transcripts/NN-summary.txt` instead. Nothing in the framework did so; the
  dashboard reads `summary.md` as a report and is unaffected.

## Alternatives considered

- **Constrain the summary phase instead (drop the `developer` extension / forbid
  exploration).** Fixes the root cause without amending the codified contract,
  but relies on model restraint: any tool that echoes content, or a model that
  explores anyway, reopens the leak. The envelope is robust where the constraint
  is only a nudge. Rejected as the primary fix; the no-explore instruction is
  still included in the appended contract as cheap belt-and-suspenders.
- **Leave `summary.md` verbatim and fix only the dashboard split.** The report in
  the 2026-07-15 artifact was glued onto a tool-output line with no newline and
  its title was not line-anchored, so no dashboard heuristic can reliably recover
  it. The malformed input has to be fixed at the source. (A dashboard band-aid
  ships alongside this for already-captured artifacts, but it is not the fix.)
