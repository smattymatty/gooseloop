"""Session folder layout: timestamped dirs, meta, append-only log."""

from __future__ import annotations

import json

from gooseloop.session import log_step, new_session


def test_new_session_creates_layout_and_meta(tmp_path):
    session_dir = new_session(tmp_path / "sessions", "some-model", "some-engine")
    assert session_dir.is_dir()
    assert session_dir.parent == tmp_path / "sessions"
    assert (session_dir / "actions").is_dir()
    meta = json.loads((session_dir / "session.meta.json").read_text())
    assert meta["model"] == "some-model"
    assert meta["engine"] == "some-engine"
    assert "session_started" in meta


def test_log_step_appends_in_order(tmp_path):
    session_dir = new_session(tmp_path / "sessions", "m", "e")
    log_step(session_dir, "first thing")
    log_step(session_dir, "second thing")
    lines = (session_dir / "session.log").read_text().splitlines()
    assert len(lines) == 2
    assert "first thing" in lines[0]
    assert "second thing" in lines[1]
    assert lines[0].startswith("[")  # timestamped
