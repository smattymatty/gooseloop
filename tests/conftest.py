"""Common pytest plumbing — make the project root importable, and keep
looper tests hermetic: THE BOUNDARY resolves against the real $HOME (a
multi-second filesystem scan), so tests get "no sandbox" by default.
test_boundary.py is exempt; wiring tests re-patch with a fake Boundary.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_boundary_scan(request, monkeypatch):
    if "test_boundary" in request.node.nodeid:
        yield
        return
    monkeypatch.setattr(
        "gooseloop.looper.resolve_sandbox", lambda anchor: None, raising=True
    )
    yield
