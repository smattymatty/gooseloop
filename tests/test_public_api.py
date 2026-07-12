"""The public API surface, pinned.

CLAUDE.md tells loop authors: "Public surface is whatever
gooseloop/__init__.py exports. Import from there, not from submodules."
For a published package that sentence is a compatibility promise —
these tests turn accidental breakage of it into a red test instead of
a user's broken upgrade. Removing or renaming an export is a
deliberate act: update the snapshot here, the module docstring, and
the changelog together.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import gooseloop

# The v0.x public surface. Additions extend this set; removals are a
# breaking change.
PUBLIC_SURFACE = {
    "BranchPolicy",
    "Context",
    "Engine",
    "Environment",
    "GooseLooper",
    "LooperConfig",
    "Phase",
    "Pipeline",
    "artifact",
    "predicates",
    "protocol",
    "toolkit",
}


def test_all_matches_the_pinned_surface():
    assert set(gooseloop.__all__) == PUBLIC_SURFACE


def test_every_export_actually_resolves():
    for name in gooseloop.__all__:
        assert getattr(gooseloop, name, None) is not None, (
            f"__all__ names {name!r} but gooseloop.{name} does not resolve"
        )


def test_docstring_documents_every_export():
    doc = gooseloop.__doc__ or ""
    missing = [n for n in gooseloop.__all__ if n not in doc]
    assert not missing, (
        f"package docstring's 'Public surface' listing omits: {missing}"
    )


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert gooseloop.__version__ == declared
