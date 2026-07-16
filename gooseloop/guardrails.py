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
    # JWT (header.payload.signature, both leading segments base64 of `{"…`).
    # Carries dots, so the assignment pass below deliberately skips it; this
    # distinctive shape catches it independent of any key name.
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")),
    ("private key block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        # Torn block (no END marker) still redacts, but BOUNDED: a lone marker
        # quoted in a doc must not greedily redact to end-of-file. 8000 chars
        # covers any real key; standard private keys are under 4 KB.
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8000}",
    )),
)

# KEY=value / KEY: value where the key NAME is a secret-shaped identifier AND
# the value looks like a credential. Both halves are tightened against source
# code, otherwise a false-positive minefield (doc_drift pastes .py files into
# prompts, so `_KEYED_LISTS: dict[str, _Keyer]` used to read as a secret):
#   - the secret word must be a full `_`-delimited component, so `_KEYED_LISTS`,
#     `MONKEY`, and `KEYWORD` do NOT match — only real shapes like API_KEY,
#     STRIPE_SECRET_KEY, DB_PASSWORD.
#   - the value is a credential charset (base64/hex/token): no spaces and no
#     code punctuation (`.`, `[]`, `()`, `,`), so a type annotation
#     `dict[str, X]`, an attribute access `os.environ`, or a short call
#     `get_key()` is not mistaken for a secret. JWTs carry dots and are caught
#     by the token pass above instead.
# The key name survives; the value is redacted.
_SECRET_WORD = r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD)"
_ASSIGNMENT_RE = re.compile(
    # Left boundary: the key must start at a real identifier edge, so `KEY`
    # buried in `MONKEY` (or `KEYED` mid-word) is not a match on its own.
    r"(?<![A-Za-z0-9_])"
    r"(?P<key>_?(?:[A-Za-z0-9]+_)*" + _SECRET_WORD + r"(?:_[A-Za-z0-9]+)*)"
    r"(?P<sep>\s*[=:]\s*[\"']?)"
    # Possessive run + `(?!\()`: the value must not be a function call
    # (SECRET = get_secret_value()). Possessive so the whole run is tested
    # against the trailing `(`, never backtracked into a shorter false match.
    r"(?P<value>[A-Za-z0-9+/=_-]{12,}+)(?!\()",
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
