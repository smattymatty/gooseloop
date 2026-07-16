"""Egress tripwire (ADR 0014): secret-shaped output is redacted before it
persists, flagged on the phase event, and raised as an operator action."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from gooseloop import (
    Engine,
    Environment,
    GooseLooper,
    LooperConfig,
    Phase,
    Pipeline,
    telemetry,
)
from gooseloop.guardrails import scan_and_redact


# ---- the detector ----------------------------------------------------


def test_token_shapes_redact_and_report():
    text = (
        "here is sk_live_a1B2c3D4e5F6g7H8 and AKIA0123456789ABCDEF and\n"
        "ghp_abcdefghij0123456789 plus pypi-AgEIcHlwaS5vcmcabc\n"
    )
    redacted, findings = scan_and_redact(text)
    assert "sk_live_" not in redacted
    assert "AKIA" not in redacted
    assert "ghp_" not in redacted
    assert "pypi-AgE" not in redacted
    kinds = {f.kind for f in findings}
    assert {"stripe key", "aws-style access key", "github token", "pypi token"} <= kinds


def test_assignment_redacts_value_keeps_key_name():
    redacted, findings = scan_and_redact("STRIPE_SECRET_KEY=sk_live_deadbeef12345678\n"
                                         "RESEND_API_KEY: re_abcdef0123456789\n")
    assert "STRIPE_SECRET_KEY=" in redacted
    assert "RESEND_API_KEY:" in redacted
    assert "deadbeef" not in redacted
    assert "re_abcdef" not in redacted
    assert findings  # reported, never silent


def test_private_key_block_redacts_even_torn():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nlines\n"
    redacted, findings = scan_and_redact(pem)
    assert "MIIEowIBAAKCAQEA" not in redacted
    assert any("private key" in f.kind for f in findings)


def test_clean_text_untouched():
    text = "wrote greetings/Canada.txt — Hello, Canada! keys to success: effort\n"
    redacted, findings = scan_and_redact(text)
    assert redacted == text
    assert findings == []


def test_source_code_is_not_flagged_as_assigned_secret():
    """The doc_drift failure: engines paste .py files into prompts, and the
    old KEY=value rule read type annotations, attribute access, and function
    calls as secrets. None of these is a credential; none may be flagged."""
    code = (
        "_KEYED_LISTS: dict[str, _Keyer] = {\n"   # substring KEY + code value
        "TOKEN_RE = re.compile('x')\n"            # secret-named, dotted value
        "MONKEY = bananarama123456\n"             # substring KEY, not a component
        "SECRET_VALUE = get_secret_value()\n"     # secret-named, function call
        "API_KEY = os.environ.get('X')\n"         # attribute access + call
    )
    redacted, findings = scan_and_redact(code)
    assert redacted == code          # nothing touched
    assert findings == []            # nothing flagged


def test_real_assigned_secrets_still_redacted():
    """Coverage preserved: a genuine credential in KEY=value still redacts,
    and a JWT (which carries dots the assignment charset now excludes) is
    caught by its own token shape."""
    aws = "AWS_SECRET_ACCESS_KEY = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
    red, findings = scan_and_redact(aws)
    assert "wJalrXUt" not in red
    assert "AWS_SECRET_ACCESS_KEY" in red          # key name survives
    assert any("assigned secret" in f.kind for f in findings)

    jwt = ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9."
           "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV\n")
    red2, findings2 = scan_and_redact(jwt)
    assert "SflKxwRJ" not in red2
    assert any(f.kind == "jwt" for f in findings2)


def test_torn_private_key_block_is_bounded_not_to_eof():
    """A lone BEGIN marker (e.g. quoted in a doc) redacts conservatively but
    must not greedily eat the rest of the document."""
    doc = "-----BEGIN PRIVATE KEY-----\n" + "A" * 10000 + "\nTAIL_SURVIVES\n"
    redacted, findings = scan_and_redact(doc)
    assert "[REDACTED:private key block]" in redacted
    assert "TAIL_SURVIVES" in redacted          # bounded: the tail is intact
    assert any("private key" in f.kind for f in findings)


# ---- the looper wiring -------------------------------------------------


REVIEW_OUTPUT = (
    "<<<DELIVERABLE_JSON>>>\n"
    + json.dumps({
        "protocol_version": "1.0", "status": "done", "summary": "s",
        "insights": [], "routing": [], "operator_actions": [],
    })
    + "\n<<<END_DELIVERABLE>>>\n"
)
LEAKY_SUMMARY = "report done. also STRIPE_SECRET_KEY=sk_live_leak1234567890 oops\n"


class _Env(Environment):
    def env_vars(self) -> dict[str, str]:
        return {}


class _E(Engine):
    @property
    def name(self) -> str:
        return "guard-test"

    def pipeline(self, ctx) -> Pipeline:
        return Pipeline(
            review=Phase(name="review", recipe_path="review.yaml"),
            summary=Phase(name="summary", recipe_path="summary.yaml"),
        )


@contextlib.contextmanager
def _unprepared(recipe_path, extra_env=None, **kwargs):
    yield str(recipe_path)


def test_leaky_phase_is_redacted_flagged_and_raised(tmp_path, monkeypatch):
    def run(recipe_path, model, extra_env=None, *, stats=None, **kwargs):
        return LEAKY_SUMMARY if "summary" in recipe_path else REVIEW_OUTPUT

    monkeypatch.setattr("gooseloop.looper.prepared_recipe", _unprepared)
    monkeypatch.setattr("gooseloop.looper.run_goose_with_retry", run)
    looper = GooseLooper(
        engine=_E(), environment=_Env(),
        config=LooperConfig.load(anchor=tmp_path, warn_on_missing=False),
        save=True,
    )
    result = looper.begin_loop()
    session_dir = Path(result["session_dir"])

    # 1. The persisted transcript and summary.md carry no live value.
    summary_event = [e for e in telemetry.read_phase_events(session_dir)
                     if e["kind"] == "summary"][0]
    transcript = (session_dir / summary_event["transcript"]).read_text()
    assert "sk_live_leak" not in transcript
    assert "[REDACTED" in transcript
    assert "sk_live_leak" not in (session_dir / "summary.md").read_text()

    # 2. The event is flagged.
    assert any("secret-like content redacted" in f for f in summary_event["flags"])

    # 3. The rotate action reached the ledger — the seal queue goes red.
    ledger = json.loads((session_dir / "ledger.json").read_text())
    assert any("ROTATE CREDENTIALS" in a["action"]
               for a in ledger["operator_actions"])
