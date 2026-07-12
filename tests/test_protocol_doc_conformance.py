"""PROTOCOL.md is canonical: disagreements between it and the code are
bugs in the code (its own closing words). These goldens pin the two to
each other so drift turns into a red test instead of a doc lie.

Each test reads the real PROTOCOL.md from the repo root — no fixture
copies, so editing the doc and forgetting the code (or vice versa)
fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest

from gooseloop.extract import (
    DELIVERABLE_END,
    DELIVERABLE_START,
    extract_json_with_provenance,
)
from gooseloop.looper import _params_to_env
from gooseloop.protocol import (
    DEFAULTED_LIST_KEYS,
    PROTOCOL_VERSION,
    REQUIRED_KEYS,
    ReviewStatus,
    validate_review,
)

PROTOCOL_MD = Path(__file__).resolve().parents[1] / "PROTOCOL.md"


@pytest.fixture(scope="module")
def protocol_text() -> str:
    return PROTOCOL_MD.read_text()


@pytest.fixture(scope="module")
def schema_example(protocol_text: str) -> dict:
    """The §2 required-output-schema block, parsed as JSON."""
    m = re.search(r"```json\n(.*?)```", protocol_text, re.DOTALL)
    assert m, "PROTOCOL.md no longer contains a ```json schema block"
    return json.loads(m.group(1))


def test_schema_block_names_exactly_the_keys_the_code_knows(schema_example):
    documented = set(schema_example)
    known_to_code = set(REQUIRED_KEYS) | set(DEFAULTED_LIST_KEYS)
    assert documented == known_to_code


def test_schema_block_protocol_version_matches_shipped_version(schema_example):
    assert schema_example["protocol_version"] == PROTOCOL_VERSION


def test_schema_block_status_enum_matches_the_code_enum(schema_example):
    # The doc writes the enum inline: "done | partial | error".
    documented = [s.strip() for s in schema_example["status"].split("|")]
    assert documented == list(get_args(ReviewStatus))


def test_schema_block_routing_entry_shape(schema_example):
    (entry,) = schema_example["routing"]
    assert set(entry) == {"recipe", "params", "reason"}


def test_schema_block_operator_action_requires_action_and_why(schema_example):
    (entry,) = schema_example["operator_actions"]
    assert {"action", "why"} <= set(entry)


def test_doc_names_the_exact_sentinel_markers(protocol_text):
    assert DELIVERABLE_START in protocol_text
    assert DELIVERABLE_END in protocol_text


def test_documented_framing_round_trips_through_extract_and_validate():
    """A review emitted exactly as §2 prescribes — sentinel-wrapped JSON
    with narration outside — parses canonically and validates."""
    review = {
        "protocol_version": PROTOCOL_VERSION,
        "status": "done",
        "summary": "state assessed, two units routed",
        "insights": ["input files were fresh"],
        "routing": [
            {"recipe": "to-implement-panel",
             "params": {"panel_id": "ServersTable"},
             "reason": "review found it stale"},
        ],
        "operator_actions": [
            {"action": "eyeball the rendered panel", "why": "sticky elements"},
        ],
    }
    output = (
        "Some narration the model produced first.\n"
        f"{DELIVERABLE_START}\n{json.dumps(review)}\n{DELIVERABLE_END}\n"
        "Trailing narration, ignored.\n"
    )
    extracted = extract_json_with_provenance(output)
    assert extracted is not None and extracted.is_canonical
    validated = validate_review(extracted.payload)
    assert validated["status"] == "done"
    assert validated["routing"] == review["routing"]
    assert validated["operator_actions"] == review["operator_actions"]


def test_documented_params_to_env_example_holds():
    """§2: '{"panel_id": "ServersTable"} becomes PANEL_ID=ServersTable'."""
    assert _params_to_env({"panel_id": "ServersTable"}) == {
        "PANEL_ID": "ServersTable"
    }
