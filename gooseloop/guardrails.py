"""Egress tripwire: secret-shaped content in phase output (ADR 0014).

Threat model: recipes paste untrusted content (files, model outputs, live
pages) into prompts that drive a tool-equipped model. Injection cannot be
reliably filtered on the way IN, and anything a phase outputs has already
reached the model provider. What a framework CAN do deterministically is
guard the way OUT:

  1. REDACT secret-shaped values before a transcript persists, so the
     on-disk artifact trail (transcripts, summary.md) never carries live
     credentials.
  2. FLAG the phase event (§14 `flags`) and raise an operator action, so
     a leak is a loud card in the operator's queue within one poll, never
     a quiet file.

Detection is signature-based and therefore a SEATBELT: it catches known
token shapes and KEY=value assignments, not everything. Containment
(sandboxed spawns) is a separate layer.
Foundation layer: stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# High-confidence token shapes. Each pattern's match is a VALUE worth
# redacting on sight, independent of surrounding text.
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stripe key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("stripe webhook secret", re.compile(r"\bwhsec_[A-Za-z0-9]{8,}")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("aws-style access key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("pypi token", re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}")),
    ("slack token", re.compile(r"\bxox[bapors]-[A-Za-z0-9-]{10,}")),
    ("age secret key", re.compile(r"\bAGE-SECRET-KEY-[A-Z0-9]{8,}")),
    ("private key block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*",  # torn block still redacts
    )),
)

# KEY=value / KEY: value where the key NAME says secret and the value is
# long enough to be one. The name survives; the value is redacted.
_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Za-z0-9_]*)"
    r"(?P<sep>\s*[=:]\s*[\"']?)"
    r"(?P<value>[^\s\"']{8,})",
)


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    count: int


def scan_and_redact(text: str) -> tuple[str, list[SecretFinding]]:
    """Redact secret-shaped values in `text`; report what was found.

    Returns (redacted_text, findings). Findings carry kinds and counts
    only — never values. Deterministic, no model involved.
    """
    counts: dict[str, int] = {}

    def bump(kind: str, n: int) -> None:
        if n:
            counts[kind] = counts.get(kind, 0) + n

    # Assignments first: a token INSIDE a KEY=value would otherwise be
    # replaced mid-value, leaving fragments for the assignment pass.
    def _assign_sub(m: re.Match[str]) -> str:
        return f"{m.group('key')}{m.group('sep')}[REDACTED:assigned secret]"

    text, n = _ASSIGNMENT_RE.subn(_assign_sub, text)
    bump("assigned secret (KEY=value)", n)

    for kind, pattern in _TOKEN_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{kind}]", text)
        bump(kind, n)

    findings = [SecretFinding(kind=k, count=c) for k, c in sorted(counts.items())]
    return text, findings
