"""validate_review: load-bearing keys hard-required, status canonicalised, soft defaults."""

import pytest

from gooseloop.protocol import (
    DEFAULTED_LIST_KEYS,
    REQUIRED_KEYS,
    ProtocolVersionError,
    validate_review,
)


def _payload(**overrides) -> dict:
    base = {
        "protocol_version": "1.0",
        "status": "done",
        "summary": "ok",
        "insights": [],
        "routing": [],
        "operator_actions": [],
    }
    base.update(overrides)
    return base


# ---- hard-required keys ------------------------------------------

def test_valid_payload_accepted():
    assert validate_review(_payload())["status"] == "done"


@pytest.mark.parametrize("key", list(REQUIRED_KEYS))
def test_missing_required_key_raises(key):
    payload = _payload()
    payload.pop(key)
    with pytest.raises(ValueError, match=key):
        validate_review(payload)


# ---- soft-default keys -------------------------------------------

@pytest.mark.parametrize("key", list(DEFAULTED_LIST_KEYS))
def test_missing_soft_key_defaults_to_empty_list(key):
    """insights and operator_actions are 'model had nothing to add' shaped —
    absence is not a contract violation. The framework fills with []."""
    payload = _payload()
    payload.pop(key)
    out = validate_review(payload)
    assert out[key] == []


@pytest.mark.parametrize("key", list(DEFAULTED_LIST_KEYS))
def test_explicit_null_soft_key_treated_as_default(key):
    out = validate_review(_payload(**{key: None}))
    assert out[key] == []


# ---- status canonicalisation -------------------------------------

@pytest.mark.parametrize("synonym,canonical", [
    ("done", "done"),
    ("success", "done"),
    ("ok", "done"),
    ("complete", "done"),
    ("completed", "done"),
    ("finished", "done"),
    ("DONE", "done"),
    ("  Success  ", "done"),
    ("partial", "partial"),
    ("incomplete", "partial"),
    ("in_progress", "partial"),
    ("in-progress", "partial"),
    ("pending", "partial"),
    ("error", "error"),
    ("errored", "error"),
    ("failed", "error"),
    ("failure", "error"),
    ("broken", "error"),
])
def test_status_synonyms_canonicalise(synonym, canonical):
    out = validate_review(_payload(status=synonym))
    assert out["status"] == canonical


def test_unknown_status_still_rejected():
    """Synonyms make life easier; pure nonsense is still loud failure."""
    with pytest.raises(ValueError, match="not recognised"):
        validate_review(_payload(status="quantum"))


def test_canonicalisation_does_not_mutate_input():
    payload = _payload(status="success")
    validate_review(payload)
    assert payload["status"] == "success"


# ---- protocol version --------------------------------------------

def test_protocol_major_mismatch_rejected():
    with pytest.raises(ProtocolVersionError, match="2.0"):
        validate_review(_payload(protocol_version="2.0"))


def test_protocol_minor_accepted_within_major():
    out = validate_review(_payload(protocol_version="1.9"))
    assert out["protocol_version"] == "1.9"


def test_unparseable_protocol_raises():
    with pytest.raises(ProtocolVersionError, match="parseable"):
        validate_review(_payload(protocol_version="abc"))


# ---- operator_actions normalisation ------------------------------
# Regression 2026-06-04: git-recap review emitted operator_actions
# as a list of bare strings ("Draft release notes ..."). The seeding
# loop crashed on str.get(). Now validate_review normalises bare
# strings to {action: <str>, why: ""}.

def test_operator_actions_string_entries_normalised():
    p = _payload(operator_actions=[
        "Draft release notes covering buckets rebrand",
        "Verify CustomerKey migration ran cleanly in prod",
    ])
    out = validate_review(p)
    assert out["operator_actions"] == [
        {"action": "Draft release notes covering buckets rebrand", "why": ""},
        {"action": "Verify CustomerKey migration ran cleanly in prod", "why": ""},
    ]


def test_operator_actions_dict_entries_default_missing_why():
    p = _payload(operator_actions=[{"action": "do X"}])
    out = validate_review(p)
    assert out["operator_actions"] == [{"action": "do X", "why": ""}]


def test_operator_actions_extras_preserved():
    p = _payload(operator_actions=[
        {"action": "verify", "why": "visual", "panel_id": "ServersTable"},
    ])
    out = validate_review(p)
    assert out["operator_actions"][0]["panel_id"] == "ServersTable"


def test_operator_actions_malformed_entries_dropped():
    p = _payload(operator_actions=[
        "valid string",
        "",                            # empty string -> dropped
        {"action": "valid dict"},
        {"why": "no action"},          # dict without action -> dropped
        42,                            # non-str non-dict -> dropped
        None,                          # null -> dropped
    ])
    out = validate_review(p)
    assert len(out["operator_actions"]) == 2
    assert out["operator_actions"][0]["action"] == "valid string"
    assert out["operator_actions"][1]["action"] == "valid dict"


def test_operator_actions_non_list_becomes_empty():
    p = _payload(operator_actions="not a list at all")
    out = validate_review(p)
    assert out["operator_actions"] == []


# ---- routing normalisation ---------------------------------------

def test_routing_dict_entries_default_missing_fields():
    p = _payload(routing=[{"recipe": "summarize-commit"}])
    out = validate_review(p)
    assert out["routing"] == [
        {"recipe": "summarize-commit", "params": {}, "reason": ""},
    ]


def test_routing_entries_without_recipe_dropped():
    p = _payload(routing=[
        {"recipe": "valid"},
        {"params": {"x": 1}},          # no recipe -> dropped
        {"recipe": ""},                # empty recipe -> dropped
        "not a dict",                  # bare string -> dropped
        {"recipe": "also valid", "reason": "ok"},
    ])
    out = validate_review(p)
    assert [e["recipe"] for e in out["routing"]] == ["valid", "also valid"]


def test_routing_non_dict_params_becomes_empty():
    p = _payload(routing=[{"recipe": "x", "params": "not a dict"}])
    out = validate_review(p)
    assert out["routing"][0]["params"] == {}
