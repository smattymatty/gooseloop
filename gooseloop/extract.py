"""Extract a JSON deliverable from LLM output.

LLMs are non-deterministic emitters. A recipe that says "wrap the payload
in <<<DELIVERABLE_JSON>>> ... <<<END_DELIVERABLE>>>" will, against a
weaker or just-creatively-paraphrasing model, get back ``<<<DELIMITED_JSON>>>``
or a markdown ```json fence`` or any other "obviously a wrapped JSON
deliverable" shape. The strict canonical-only parser throws away
parseable responses for vocabulary mismatches; the legacy "any balanced
object anywhere" fallback silently surfaces intermediate scratch dicts.

Both are wrong. The right shape is a small ordered list of wrapper
recognizers. Each one answers: "is there a JSON payload wrapped by
*something* that looks like a deliverable marker?" Tried in order from
most-specific to most-permissive; the first one that yields a parseable
balanced object wins. Bare JSON in prose still fails — every recognizer
requires *some* wrapper, so the "scattered intermediate dict" failure
mode the legacy fallback enabled stays dead.

Provenance is preserved on the way out. The looper logs (and surfaces
as an operator action) when a non-canonical recognizer matched, so the
operator can tighten the recipe at their own pace without the framework
silently absorbing drift.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .text import strip_ansi


# Canonical wrapper. Recipes the framework ships use this verbatim;
# other recognizers exist for tolerance, not to encourage drift.
DELIVERABLE_START = "<<<DELIVERABLE_JSON>>>"
DELIVERABLE_END = "<<<END_DELIVERABLE>>>"


@dataclass(frozen=True)
class Extracted:
    """A parsed deliverable plus which recognizer matched."""
    payload: dict[str, Any]
    recognizer: str  # "canonical" | "angle_sentinel" | "markdown_fence"

    @property
    def is_canonical(self) -> bool:
        return self.recognizer == "canonical"


# --- recognizers ---------------------------------------------------
#
# Each recognizer takes ANSI-stripped text and returns the candidate
# payload string (the bit between wrapper markers) or None if its
# wrapper shape isn't present. extract_json then runs the balanced-
# object scan over the candidate; an unparseable candidate falls
# through to the next recognizer.

WrapperRecognizer = Callable[[str], Optional[str]]


def _canonical_sentinel(text: str) -> Optional[str]:
    """Exact <<<DELIVERABLE_JSON>>> ... <<<END_DELIVERABLE>>>. Last
    occurrence wins (models often echo the spec earlier in their prose,
    then emit the real deliverable at the bottom)."""
    start = text.rfind(DELIVERABLE_START)
    if start == -1:
        return None
    payload_start = start + len(DELIVERABLE_START)
    end = text.find(DELIVERABLE_END, payload_start)
    return text[payload_start:end] if end != -1 else text[payload_start:]


_ANGLE_TAG_RE = re.compile(r"<<<\s*([A-Z][A-Z0-9_]*)\s*>>>")
_PAYLOAD_WORDS = ("JSON", "DELIVERABLE", "PAYLOAD", "OUTPUT", "RESULT")


def _angle_sentinel(text: str) -> Optional[str]:
    """Generic <<<TAG>>> wrapper where TAG looks like a payload marker.

    Catches DELIMITED_JSON, JSON_BEGIN, DELIVERABLE_PAYLOAD, OUTPUT_JSON,
    and anything else triple-angle-wrapped with a JSON/DELIVERABLE/PAYLOAD/
    OUTPUT/RESULT word in the tag. The last matching opener wins. We
    don't try to match a specific close tag — the balanced-object scan
    in extract_json handles trailing close markers as ignorable suffix.
    """
    matches = list(_ANGLE_TAG_RE.finditer(text))
    if not matches:
        return None
    openers = [
        m for m in matches
        if "END" not in m.group(1).split("_")
        and any(w in m.group(1) for w in _PAYLOAD_WORDS)
    ]
    if not openers:
        return None
    return text[openers[-1].end():]


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def _markdown_fence(text: str) -> Optional[str]:
    """Markdown code fence: ```json ... ``` or bare ``` ... ```. Last fence wins.

    The balanced-object scan filters out fences that contain code rather
    than JSON, so we don't need to discriminate on the language tag.
    """
    matches = list(_FENCE_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1)


# Tolerant recognizers — tried only when no canonical opener is present.
# Canonical match is a commitment from the model (it claimed to follow
# the contract); we honour the commitment by parsing-or-refusing the
# canonical payload rather than fishing for JSON elsewhere when the
# canonical payload turns out to be garbage.
_TOLERANT_RECOGNIZERS: list[tuple[str, WrapperRecognizer]] = [
    ("angle_sentinel", _angle_sentinel),
    ("markdown_fence", _markdown_fence),
]


# --- public API ----------------------------------------------------

def extract_json_with_provenance(text: str) -> Optional[Extracted]:
    """Parse the deliverable. Returns Extracted on success, None on refusal.

    Two-stage dispatch:
      1. Canonical opener present  → parse the canonical payload or refuse.
         No fallthrough — the model committed to the canonical contract.
      2. No canonical opener       → try tolerant recognizers in order;
         the first one yielding a parseable dict wins.
    """
    clean = strip_ansi(text)

    if DELIVERABLE_START in clean:
        payload = _try_parse(_canonical_sentinel(clean))
        return Extracted(payload=payload, recognizer="canonical") if payload else None

    for name, recognizer in _TOLERANT_RECOGNIZERS:
        payload = _try_parse(recognizer(clean))
        if payload is not None:
            return Extracted(payload=payload, recognizer=name)
    return None


def _try_parse(candidate: Optional[str]) -> Optional[dict[str, Any]]:
    if candidate is None:
        return None
    balanced = _first_balanced_object(candidate)
    if balanced is None:
        return None
    try:
        payload = json.loads(balanced)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """Backwards-compatible thin wrapper for callers that don't need provenance."""
    result = extract_json_with_provenance(text)
    return result.payload if result else None


# --- balanced-object scan -----------------------------------------

def _first_balanced_object(text: str) -> Optional[str]:
    """Return the first balanced {...} substring, respecting string literals.

    Lives here, not in text.py, because its only consumer is the JSON
    extractor. Pure: depends on nothing else in the package.
    """
    start = text.find("{")
    if start == -1:
        return None
    brace_count = 0
    in_string = False
    escape = False
    for j, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
        if not in_string:
            if ch == "{":
                brace_count += 1
            elif ch == "}":
                brace_count -= 1
                if brace_count == 0:
                    return text[start:j + 1]
    return None
