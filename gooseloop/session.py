"""Session folder management: create timestamped dirs, log steps.

The Looper owns the session layout:

    <sessions_dir>/<UTC-timestamp>/
        session.meta.json   - model, engine, timestamps
        session.log         - append-only event log
        actions/            - Phase outputs land here
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .text import Color, banner


def new_session(sessions_dir: Path, model: str, engine_name: str) -> Path:
    """Create a timestamped session directory and print a banner."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    session_dir = sessions_dir / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "actions").mkdir(exist_ok=True)

    meta = {
        "session_started": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "engine": engine_name,
    }
    with open(session_dir / "session.meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    banner(f"Session started: {session_dir}", Color.GREEN)
    return session_dir


def log_step(session_dir: Path, message: str) -> None:
    """Append a timestamped line to session.log."""
    ts = datetime.now(timezone.utc).isoformat()
    with open(session_dir / "session.log", "a") as f:
        f.write(f"[{ts}] {message}\n")
