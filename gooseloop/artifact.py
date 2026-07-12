"""Versioned artifact contracts: how engines compose without knowing each other.

Per PROTOCOL.md §12. When one engine's output is another engine's input, the
artifact file is the interface between them, and an interface needs a version.
The producing engine stamps a `schema_version` key into the artifact; the
consuming engine checks it at read time with check_artifact_version().

The semantics deliberately mirror the review protocol_version check in
protocol.py, because it is the same problem one level up:

    - Same major, any minor: compatible. Additive schema changes (a new
      optional field, a new enum value the reader ignores) bump the minor.
    - Different major, or an unparseable version: refused loudly with
      ArtifactVersionError. A major mismatch means the reader may
      misinterpret every entry; limping along is the silent time bomb.
    - Missing version: the artifact is still read, and a problem string is
      returned nudging the operator to stamp the file. Fail-safe runs in the
      KEEP direction: data we cannot positively classify is kept and named
      out loud, never refused on a technicality. (Pre-versioning artifacts
      sealed by hand exist; they should not stop working the day the
      contract gains a version key.)
"""

from __future__ import annotations

from typing import Any


class ArtifactVersionError(Exception):
    """The artifact declares a schema version this reader cannot honour."""


def check_artifact_version(
    data: dict[str, Any],
    supported: str,
    *,
    key: str = "schema_version",
    what: str = "artifact",
) -> list[str]:
    """Check a parsed artifact's declared schema version against `supported`.

    Returns a list of problem strings (empty when the version is present and
    compatible). Raises ArtifactVersionError on a major mismatch or an
    unparseable declared version.

    Parameters:
        data: the parsed artifact (the top-level mapping).
        supported: the version this reader ships, e.g. "1.0".
        key: the version key inside the artifact.
        what: human name for the artifact, used in messages.
    """
    supported_major = _major(supported, what=f"supported version for {what}")

    declared = data.get(key)
    if declared is None or str(declared).strip() == "":
        return [
            f"{what} has no {key}; assuming {supported} - "
            f'add `{key} = "{supported}"` to the file'
        ]

    declared = str(declared).strip()
    try:
        declared_major = _major(declared, what=what)
    except ArtifactVersionError:
        raise ArtifactVersionError(
            f"{what} declares {key} {declared!r}, which is not parseable; "
            f"this reader supports major {supported_major}"
        ) from None

    if declared_major != supported_major:
        raise ArtifactVersionError(
            f"{what} declares {key} {declared!r} (major {declared_major}); "
            f"this reader supports major {supported_major} only"
        )
    return []


def _major(version: str, *, what: str) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (ValueError, IndexError):
        raise ArtifactVersionError(f"{what}: version {version!r} is not parseable")
