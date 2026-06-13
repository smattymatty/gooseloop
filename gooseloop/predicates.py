"""Reusable success-predicate factories.

A Phase's success_predicate decides whether a recipe attempt succeeded.
Three named shapes cover the common cases:

    file_freshly_touched(path, pre_mtime)  - strict: exists, non-empty, newer than pre_mtime
    file_nonempty(path)                    - loose: exists and non-empty
    json_in_stdout(required_keys)          - sentinel JSON parses and has keys

Phase(success_predicate=None) falls through to the looper's transient-error
check; that is the explicit "no extra validation" option. No no-op
predicate factory is shipped (per YAGNI; six characters of lambda if a
recipe ever genuinely needs one).
"""

from pathlib import Path
from typing import Callable, Iterable

from .extract import extract_json


SuccessPredicate = Callable[[str], bool]


def file_freshly_touched(path: Path, pre_mtime: float = 0.0) -> SuccessPredicate:
    """Strict: `path` exists, is non-empty, AND its mtime is strictly newer
    than `pre_mtime`.

    Snapshot pre_mtime BEFORE the recipe runs; if the file didn't exist,
    pass 0.0. Use when the recipe writes to a deterministic path and you
    need to detect both "model didn't write" and "model wrote a different
    file so this one is stale."
    """
    def predicate(_output: str) -> bool:
        if not path.exists():
            return False
        if path.stat().st_size == 0:
            return False
        return path.stat().st_mtime > pre_mtime
    return predicate


def file_nonempty(path: Path) -> SuccessPredicate:
    """Loose: `path` exists and is non-empty.

    Safe only when you already know the file didn't exist before the
    recipe ran (otherwise a pre-existing file passes without proof the
    recipe wrote anything).
    """
    def predicate(_output: str) -> bool:
        return path.exists() and path.stat().st_size > 0
    return predicate


def json_in_stdout(required_keys: Iterable[str] | None = None) -> SuccessPredicate:
    """The recipe's stdout contains sentinel-wrapped JSON with all required_keys.

    Use for stdout-deliverable recipes (the JSON IS the artifact). Catches
    the "model emitted a thin stub instead of the full schema" failure
    mode.
    """
    keys = list(required_keys or [])

    def predicate(output: str) -> bool:
        data = extract_json(output)
        if data is None:
            return False
        return all(k in data for k in keys)
    return predicate
