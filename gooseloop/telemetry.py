"""Per-phase telemetry: one wide structured event per phase, plus the
phase's full goose transcript (PROTOCOL §14).

Until now a body phase's entire goose output — every narrated shell
command, file write, and decision — was checked by the success predicate
and thrown away; session.log kept a single "Phase X completed." line. That
is a metric where a wide event belongs: the framework KNOWS the recipe,
the injected env (routing params, output path), the duration, and the
transcript at the moment the phase settles. Record all of it, once,
durably:

    <session>/phases.jsonl              one JSON object per line, appended
                                        the moment each phase settles —
                                        live-tailable and crash-safe
    <session>/transcripts/<seq>-<name>.txt   the phase's full goose output
    <session>/transcripts/<seq>-<name>.prompt.yaml   the rendered recipe the
                                        phase handed to goose — what the
                                        model SAW, kept with what it said

Consumers (a dashboard, jq, a fleet-wide rollup) read the same artifacts;
nothing here is dashboard-specific. Events record only what the framework
observed — no inferred file reads, no invented structure. Foundation
layer: stdlib only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

PHASES_FILENAME = "phases.jsonl"
TRANSCRIPTS_DIR = "transcripts"

# Env values are usually short (params, paths); anything huge would bloat
# every event reader for no debugging value beyond its head.
_ENV_VALUE_CAP = 500


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "phase"


def record_phase(
    session_dir: Path,
    *,
    seq: int,
    name: str,
    kind: str,  # "review" | "body" | "summary"
    recipe: str,
    label: Optional[str],
    status: str,  # "ok" | "failed" | "skipped"
    started: str,
    duration_s: float,
    env: Optional[dict[str, str]] = None,
    outputs: Optional[list[Any]] = None,
    transcript_text: Optional[str] = None,
    prompt_text: Optional[str] = None,
    error: Optional[str] = None,
    skip_reason: Optional[str] = None,
    attempts: Optional[int] = None,
    attempt_log: Optional[list[dict[str, Any]]] = None,
    actions: Optional[list[dict[str, Any]]] = None,
    flags: Optional[list[str]] = None,
) -> None:
    """Append one wide event; persist the transcript beside it.

    Never raises: telemetry must not be able to fail a pass that the work
    itself did not fail.
    """
    try:
        transcript_ref: Optional[str] = None
        transcript_chars = 0
        if transcript_text is not None:
            tdir = session_dir / TRANSCRIPTS_DIR
            tdir.mkdir(parents=True, exist_ok=True)
            transcript_ref = f"{TRANSCRIPTS_DIR}/{seq:02d}-{_slug(name)}.txt"
            (session_dir / transcript_ref).write_text(transcript_text)
            transcript_chars = len(transcript_text)

        # Every retry attempt keeps its evidence: non-final attempts
        # carry their full output in `output` (the final attempt's IS the
        # main transcript above) — persist each beside the transcript and
        # replace the inline text with a reference.
        attempt_entries: list[dict[str, Any]] = []
        for e in attempt_log or []:
            entry = {k: v for k, v in e.items() if k != "output"}
            text = e.get("output")
            if text is not None:
                tdir = session_dir / TRANSCRIPTS_DIR
                tdir.mkdir(parents=True, exist_ok=True)
                ref = (f"{TRANSCRIPTS_DIR}/{seq:02d}-{_slug(name)}"
                       f".attempt-{e.get('attempt', 0)}.txt")
                (session_dir / ref).write_text(text)
                entry["transcript"] = ref
                entry["transcript_chars"] = len(text)
            else:
                entry["transcript"] = None
                entry["transcript_chars"] = 0
            attempt_entries.append(entry)
        if attempt_entries:
            # The final attempt's transcript IS the phase transcript.
            attempt_entries[-1]["transcript"] = transcript_ref
            attempt_entries[-1]["transcript_chars"] = transcript_chars

        prompt_ref: Optional[str] = None
        prompt_chars = 0
        if prompt_text is not None:
            tdir = session_dir / TRANSCRIPTS_DIR
            tdir.mkdir(parents=True, exist_ok=True)
            prompt_ref = f"{TRANSCRIPTS_DIR}/{seq:02d}-{_slug(name)}.prompt.yaml"
            (session_dir / prompt_ref).write_text(prompt_text)
            prompt_chars = len(prompt_text)

        event = {
            "seq": seq,
            "phase": name,
            "kind": kind,
            "recipe": recipe,
            "label": label,
            "status": status,
            "started": started,
            "duration_s": round(duration_s, 2),
            "env": {
                k: (v if len(v) <= _ENV_VALUE_CAP else v[:_ENV_VALUE_CAP] + "…")
                for k, v in (env or {}).items()
            },
            "outputs": [str(o) for o in (outputs or [])],
            "transcript": transcript_ref,
            "transcript_chars": transcript_chars,
            # The rendered recipe this phase handed to goose — context
            # blocks filled, env substituted, redacted like transcripts.
            # The input half: investigations that dead-ended at "what was
            # actually in the pasted context?" end here instead.
            "prompt": prompt_ref,
            "prompt_chars": prompt_chars,
            "error": error,
            "skip_reason": skip_reason,
            "attempts": attempts,
            # One record per goose invocation: outcome, returncode,
            # duration, the retry delay that followed, and a transcript
            # ref for every non-final attempt (the final attempt's
            # transcript is the phase transcript above). "Needed three
            # tries" investigations end at the actual three outputs.
            "attempt_log": attempt_entries,
            # Operator actions THIS phase raised (per-phase ledger delta) —
            # durable the moment the phase settles, so consumers can show
            # decisions mid-run and crashed passes keep what they raised.
            "actions": actions or [],
            # Deterministic tripwires that fired on this phase's output
            # (e.g. "secret-like content redacted") — consumers render
            # these loud (ADR 0014).
            "flags": flags or [],
        }
        with open(session_dir / PHASES_FILENAME, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def read_phase_events(session_dir: Path) -> list[dict[str, Any]]:
    """Consumer-side reader. Tolerates a torn final line (a live run may
    be mid-append) by skipping anything that does not parse."""
    path = session_dir / PHASES_FILENAME
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out
