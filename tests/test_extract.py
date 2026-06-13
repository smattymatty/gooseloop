"""extract_json: ordered wrapper recognizers + provenance.

Previously a binary canonical-or-None match (test_text.py). The parser
now accepts a small set of "obviously a wrapped JSON deliverable"
shapes; each recognizer is tested in isolation, plus end-to-end
provenance reporting.
"""

import pytest

from gooseloop.extract import (
    DELIVERABLE_END,
    DELIVERABLE_START,
    Extracted,
    extract_json,
    extract_json_with_provenance,
)


# ---- canonical sentinel (tier 1) ---------------------------------

def test_canonical_sentinel_pair_parses():
    out = (
        "narration\n"
        + DELIVERABLE_START + "\n"
        + '{"status": "done", "n": 1}\n'
        + DELIVERABLE_END + "\n"
    )
    result = extract_json_with_provenance(out)
    assert result == Extracted(payload={"status": "done", "n": 1}, recognizer="canonical")
    assert result.is_canonical is True


def test_canonical_last_sentinel_wins_when_model_echoes_spec():
    out = (
        f"Here's the spec: {DELIVERABLE_START}{{\"fake\": true}}{DELIVERABLE_END}\n"
        + DELIVERABLE_START + "\n"
        + '{"status": "done"}\n'
        + DELIVERABLE_END + "\n"
    )
    assert extract_json(out) == {"status": "done"}


def test_canonical_strips_ansi_before_parsing():
    out = (
        f"\x1b[32m{DELIVERABLE_START}\x1b[0m\n"
        + '{"status": "done"}\n'
        + DELIVERABLE_END + "\n"
    )
    assert extract_json(out) == {"status": "done"}


def test_canonical_with_garbage_payload_returns_none_not_fallthrough():
    """Canonical wrapper present but payload unparseable: extract_json
    should still try the next recognizer. If no other recognizer matches,
    return None — do NOT silently grab JSON from elsewhere in the prose."""
    out = (
        f"{DELIVERABLE_START}\n"
        "not actually json\n"
        f"{DELIVERABLE_END}\n"
        '{"a": 1}\n'  # bare JSON in prose — must NOT be picked up
    )
    assert extract_json(out) is None


# ---- angle_sentinel (tier 2) -------------------------------------

def test_angle_sentinel_picks_up_DELIMITED_JSON_synonym():
    """2026-06-04 regression: owl-alpha emitted <<<DELIMITED_JSON>>>
    instead of the canonical <<<DELIVERABLE_JSON>>>."""
    out = (
        "<<<DELIMITED_JSON>>>\n"
        '{"status": "done", "summary": "ok"}\n'
        "<<<END_DELIMITED>>>\n"
    )
    result = extract_json_with_provenance(out)
    assert result is not None
    assert result.payload == {"status": "done", "summary": "ok"}
    assert result.recognizer == "angle_sentinel"
    assert result.is_canonical is False


def test_angle_sentinel_picks_up_JSON_BEGIN_variant():
    out = (
        "<<<JSON_BEGIN>>>\n"
        '{"status": "done"}\n'
        "<<<JSON_END>>>\n"
    )
    result = extract_json_with_provenance(out)
    assert result is not None
    assert result.recognizer == "angle_sentinel"


def test_angle_sentinel_payload_word_required():
    """Angle wrapper without a payload-word in the tag is ignored.
    <<<NOTE>>> isn't claiming to be a deliverable wrapper."""
    out = (
        "<<<NOTE>>>\n"
        '{"status": "done"}\n'
        "<<<END_NOTE>>>\n"
    )
    assert extract_json(out) is None


def test_angle_sentinel_last_opener_wins():
    out = (
        "<<<JSON_INTRO>>> {\"earlier\": true} <<<END>>>\n"
        "<<<DELIVERABLE_PAYLOAD>>>\n"
        '{"status": "done"}\n'
        "<<<END_PAYLOAD>>>\n"
    )
    assert extract_json(out) == {"status": "done"}


# ---- markdown_fence (tier 3) -------------------------------------

def test_markdown_json_fence_parses():
    out = (
        "Sure, here it is:\n"
        "```json\n"
        '{"status": "done"}\n'
        "```\n"
    )
    result = extract_json_with_provenance(out)
    assert result is not None
    assert result.payload == {"status": "done"}
    assert result.recognizer == "markdown_fence"


def test_markdown_bare_fence_with_json_inside_parses():
    out = (
        "```\n"
        '{"status": "done"}\n'
        "```\n"
    )
    assert extract_json(out) == {"status": "done"}


def test_markdown_fence_with_non_json_content_returns_none():
    out = (
        "```python\n"
        "def f(): pass\n"
        "```\n"
    )
    assert extract_json(out) is None


def test_markdown_fence_last_one_wins():
    out = (
        "```json\n"
        '{"first": true}\n'
        "```\n"
        "Then I realised...\n"
        "```json\n"
        '{"status": "done"}\n'
        "```\n"
    )
    assert extract_json(out) == {"status": "done"}


# ---- recognizer precedence ---------------------------------------

def test_canonical_beats_angle_synonym_when_both_present():
    """The canonical wrapper wins even if an angle synonym also matches.
    This is what the operator wrote in the recipe; honour it."""
    out = (
        "<<<DELIMITED_JSON>>>\n"
        '{"wrong": true}\n'
        "<<<END_DELIMITED>>>\n"
        + DELIVERABLE_START + "\n"
        + '{"status": "done"}\n'
        + DELIVERABLE_END + "\n"
    )
    result = extract_json_with_provenance(out)
    assert result.payload == {"status": "done"}
    assert result.recognizer == "canonical"


def test_angle_beats_markdown_when_both_present():
    out = (
        "<<<DELIVERABLE_JSON_BLOCK>>>\n"
        '{"from": "angle"}\n'
        "<<<END_BLOCK>>>\n"
        "```json\n"
        '{"from": "markdown"}\n'
        "```\n"
    )
    result = extract_json_with_provenance(out)
    assert result.payload == {"from": "angle"}
    assert result.recognizer == "angle_sentinel"


# ---- hard refusals (the legacy-fallback failure modes) -----------

def test_returns_none_when_no_wrapper_at_all():
    """Bare JSON in prose stays unparseable — the deleted legacy
    fallback's failure mode (intermediate stub dicts surfacing as
    deliverables) must stay dead."""
    out = '{"this": "is", "not": "wrapped"}\n'
    assert extract_json(out) is None
    assert extract_json_with_provenance(out) is None


def test_returns_none_when_canonical_payload_is_garbage_and_no_other_wrapper():
    out = (
        f"{DELIVERABLE_START}\n"
        "not actually json\n"
        f"{DELIVERABLE_END}\n"
    )
    assert extract_json(out) is None


def test_returns_none_when_json_array_not_object():
    """We extract objects, not arrays. A wrapped JSON array isn't
    a review payload; it's a different shape we don't claim to handle."""
    out = (
        f"{DELIVERABLE_START}\n"
        "[1, 2, 3]\n"
        f"{DELIVERABLE_END}\n"
    )
    assert extract_json(out) is None


# ---- back-compat: plain extract_json keeps working ---------------

def test_plain_extract_json_returns_dict_or_none():
    """Callers that don't need provenance (predicates, tests) keep the
    dict|None signature they had."""
    canonical = (
        f"{DELIVERABLE_START}\n"
        '{"status": "done"}\n'
        f"{DELIVERABLE_END}\n"
    )
    assert extract_json(canonical) == {"status": "done"}
    assert extract_json("no wrapper at all") is None
