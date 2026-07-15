"""TypedDicts and validation for the gooseloop review protocol.

See PROTOCOL.md for the canonical contract and ADR 0007 for
the rationale. The framework reads these keys; engines may add arbitrary
extension keys to any of these structures.

Validation is **liberal in what it accepts, strict in what the schema
guarantees on the way out**:

  - Status synonyms (success/ok/complete/failed/incomplete/...) canonicalise
    to the three-value enum. Unknown synonyms raise.
  - Non-load-bearing keys (insights, operator_actions) default to [] when
    missing. They're informational; absence is "model had nothing to say"
    not "model broke the contract."
  - Load-bearing keys (protocol_version, status, summary, routing) are
    strictly required. Absence means the review can't drive the framework.
  - protocol_version major must match what we ship.

This is the validate-side companion to the parser-side leniency in
gooseloop.extract: accept what's obviously the same intent in a slightly
different shape, fail loud on actual contract violations.
"""

from typing import Any, Literal, TypedDict


PROTOCOL_VERSION = "1.0"
PROTOCOL_MAJOR = 1


# Appended by the framework to EVERY review prompt. Engine recipes still state
# their domain routing rules, but correctness of the transport envelope must not
# depend on every private recipe copying the protocol perfectly. Keeping the
# literal markers and full schema here also gives retry validation one stable
# contract to enforce.
REVIEW_OUTPUT_CONTRACT = """\
FRAMEWORK REVIEW OUTPUT CONTRACT (binding; follow this after all domain rules):

Your final assistant message MUST end with exactly one JSON object between these
literal marker lines. Copy the markers character-for-character. Do not use a
Markdown fence, PROTOCOL markers, renamed markers, or prose after the closing
marker.

<<<DELIVERABLE_JSON>>>
{
  "protocol_version": "1.0",
  "status": "done",
  "summary": "one-paragraph operator-facing state",
  "insights": [],
  "routing": [],
  "operator_actions": []
}
<<<END_DELIVERABLE>>>

All six keys shown above must be present. status is exactly one of "done",
"partial", or "error". routing is always a JSON list, empty when the domain
rules route no body work. Each routed item has recipe, params, and reason.

A populated example — copy this shape when the domain rules DO route body work:

<<<DELIVERABLE_JSON>>>
{
  "protocol_version": "1.0",
  "status": "done",
  "summary": "one-paragraph operator-facing state",
  "insights": ["a short observation the operator should see"],
  "routing": [
    {"recipe": "some-body-recipe.yaml", "params": {"id": "unit-1"}, "reason": "why this unit needs a body phase"}
  ],
  "operator_actions": [{"action": "a decision only a human can make", "why": "the context for that decision"}]
}
<<<END_DELIVERABLE>>>

End your final message in this exact shape:
<<<DELIVERABLE_JSON>>>
{ ...the complete six-key JSON object... }
<<<END_DELIVERABLE>>>
"""


def review_repair_prompt(error: str) -> str:
    """Feedback appended to the output contract when a review is rejected, so
    the model corrects with the EXACT reason instead of repeating the mistake.
    Drives the framework's validate-and-repair loop (looper._run_review): a
    static contract a weak model ignored once, plus the specific rejection, is
    what turns a one-shot failure into a corrected pass."""
    return (
        "YOUR PREVIOUS REVIEW OUTPUT WAS REJECTED and must be re-emitted.\n"
        f"Rejection reason: {error}\n\n"
        "Do not apologise or explain. Emit ONE JSON object with the six keys, "
        "between the exact literal markers <<<DELIVERABLE_JSON>>> and "
        "<<<END_DELIVERABLE>>> — no Markdown fence, no PROTOCOL markers, no "
        "renamed markers, no prose after the closing marker. Do not rename or "
        "add keys, and do not invent your own schema."
    )


class RoutingEntry(TypedDict, total=False):
    """One entry in the review's routing[] list.

    routing[] is the pass's plan of record, not just the model's
    instruction channel (ADR 0013). `routed_by` carries provenance:
    "model" entries were emitted by the review and the framework builds
    body phases from them; "engine" entries are appended by the
    FRAMEWORK to record body phases the engine built deterministically
    in pipeline() — they are record, never instruction.
    """
    recipe: str
    params: dict[str, Any]
    reason: str
    routed_by: str  # "model" | "engine"


class OperatorAction(TypedDict, total=False):
    """One entry in the session's operator_actions ledger."""
    action: str
    why: str


ReviewStatus = Literal["done", "partial", "error"]


class ReviewOutput(TypedDict, total=False):
    """Canonical review payload after validation."""
    protocol_version: str
    status: ReviewStatus
    summary: str
    insights: list[str]
    routing: list[RoutingEntry]
    operator_actions: list[OperatorAction]


# Keys the framework can't function without. A review missing any of
# these can't drive routing or the session ledger; refuse loud.
REQUIRED_KEYS = ("protocol_version", "status", "summary", "routing")

# Keys the framework reads but treats as optional in the schema. Missing
# = "model had nothing to add"; default to empty list and proceed.
DEFAULTED_LIST_KEYS = ("insights", "operator_actions")

# Status synonyms. Models drift on the status enum more than on any
# other field; canonicalise here so downstream code only sees the three
# canonical values. Unknown values raise (a fresh synonym should land
# in this table, not slip past the validator).
_STATUS_SYNONYMS: dict[str, ReviewStatus] = {
    "done": "done",
    "success": "done",
    "ok": "done",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "partial": "partial",
    "incomplete": "partial",
    "in_progress": "partial",
    "in-progress": "partial",
    "pending": "partial",
    "error": "error",
    "errored": "error",
    "failed": "error",
    "failure": "error",
    "broken": "error",
}


class ProtocolVersionError(RuntimeError):
    """Raised when a review declares a major version the framework does not support."""


def validate_review(payload: dict[str, Any]) -> ReviewOutput:
    """Canonicalise + validate a review payload.

    Returns a NEW dict with synonyms canonicalised, defaulted lists
    filled in, and entry shapes normalised. The original payload is
    not mutated.

    Normalisations performed (liberal in what we accept):

      - status: synonyms ("success", "ok", ...) collapse to the
        three-value enum.
      - insights/operator_actions: missing or None becomes [].
      - operator_actions entries: a bare string becomes
        {"action": <string>, "why": ""}. A dict missing "why" gets
        why="". Malformed entries (non-string, non-dict, or dict with
        no "action") are dropped.
      - routing entries: a dict missing optional fields gets default
        params={} and reason="". Entries without a "recipe" string
        are dropped.
    """
    missing = [k for k in REQUIRED_KEYS if k not in payload]
    if missing:
        raise ValueError(f"review missing required keys: {missing}")

    _check_protocol_version(str(payload["protocol_version"]))

    raw_status = str(payload["status"]).strip().lower()
    canonical_status = _STATUS_SYNONYMS.get(raw_status)
    if canonical_status is None:
        raise ValueError(
            f"review status {payload['status']!r} not recognised "
            f"(known: {sorted(set(_STATUS_SYNONYMS))})"
        )

    out: dict[str, Any] = dict(payload)
    out["status"] = canonical_status
    for key in DEFAULTED_LIST_KEYS:
        if key not in out or out[key] is None:
            out[key] = []
    # A bare-string insights (a model writing prose where a list belongs)
    # becomes a one-element list; non-string members are coerced. Caught
    # live 2026-07-13: a review shipped insights as one string, the run
    # proceeded fine, and every strict downstream reader choked on the
    # artifact.
    if isinstance(out["insights"], str):
        out["insights"] = [out["insights"]]
    elif isinstance(out["insights"], list):
        out["insights"] = [str(i) for i in out["insights"]]
    else:
        out["insights"] = []
    out["operator_actions"] = _normalise_operator_actions(out["operator_actions"])
    out["routing"] = _normalise_routing(out["routing"])
    return out  # type: ignore[return-value]


def _normalise_operator_actions(entries: Any) -> list[OperatorAction]:
    """Coerce loose shapes into well-formed OperatorAction dicts."""
    if not isinstance(entries, list):
        return []
    out: list[OperatorAction] = []
    for entry in entries:
        if isinstance(entry, str):
            action = entry.strip()
            if action:
                out.append({"action": action, "why": ""})
            continue
        if isinstance(entry, dict):
            action = str(entry.get("action", "")).strip()
            if not action:
                continue
            normalised: OperatorAction = {
                "action": action,
                "why": str(entry.get("why", "")),
            }
            for k, v in entry.items():
                if k not in ("action", "why"):
                    normalised[k] = v  # type: ignore[literal-required]
            out.append(normalised)
    return out


def _normalise_routing(entries: Any) -> list[RoutingEntry]:
    """Coerce loose shapes into well-formed RoutingEntry dicts."""
    if not isinstance(entries, list):
        return []
    out: list[RoutingEntry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        recipe = entry.get("recipe")
        if not isinstance(recipe, str) or not recipe.strip():
            continue
        params = entry.get("params")
        normalised: RoutingEntry = {
            "recipe": recipe.strip(),
            "params": params if isinstance(params, dict) else {},
            "reason": str(entry.get("reason", "")),
            # Model-emitted entries are "model" regardless of what the
            # model claims: "engine" provenance is reserved for entries
            # the framework itself appends (ADR 0013).
            "routed_by": "model",
        }
        out.append(normalised)
    return out


def _check_protocol_version(declared: str) -> None:
    try:
        major = int(declared.split(".", 1)[0])
    except (ValueError, IndexError):
        raise ProtocolVersionError(
            f"protocol_version {declared!r} is not parseable; "
            f"framework supports major {PROTOCOL_MAJOR}"
        )
    if major != PROTOCOL_MAJOR:
        raise ProtocolVersionError(
            f"review declares protocol_version {declared!r} (major {major}); "
            f"framework supports major {PROTOCOL_MAJOR} only"
        )

