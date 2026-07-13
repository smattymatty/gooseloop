"""run.lock — one run at a time per loop root.

ADR 0010 is the decision record; PROTOCOL section 13 is the
consumer-facing contract. One lock per loop root (the directory holding
gooseloop.toml), held for the whole pass, whatever the engine and
whatever the flags: --no-save skips the session folder, not the side
effects, so it locks too.

Consumers may READ the lock file to answer "is a run in flight, and
which engine". Only gooseloop creates, replaces, or removes it — a
canceller signals the pid and lets the run's own cleanup delete the
file. Foundation layer: stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RUN_LOCK_FILENAME = "run.lock"

# `gooseloop run` exit code when a live run already holds the lock —
# distinct from 1 (run error) and 2 (usage error) so a supervisor can
# tell "busy" from "failed".
EXIT_LOCKED = 3


class RunLockHeldError(RuntimeError):
    """A live run already holds this loop root's run.lock."""

    def __init__(self, path: Path, holder: dict[str, Any]):
        self.path = path
        self.holder = holder
        engine = holder.get("engine", "<unknown engine>")
        pid = holder.get("pid", "?")
        started = holder.get("started", "?")
        super().__init__(
            f"a run of {engine} has been in flight since {started} "
            f"(pid {pid}); refusing to start a second one in this loop "
            f"root. If that run is truly gone, remove {path} by hand."
        )


def _pid_alive(pid: int) -> Optional[bool]:
    """Whether `pid` is a live process, or None if it cannot be probed
    safely here. Signal 0 is a pure existence check on POSIX; on Windows
    os.kill with an arbitrary signal TERMINATES the target, so no probe —
    the caller must refuse conservatively rather than reclaim."""
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but belongs to another user: alive.
        return True
    return True


class RunLock:
    """The holder's side of the lock. Consumers never use this class;
    they read the JSON file per PROTOCOL section 13."""

    def __init__(self, anchor: Path):
        self.path = anchor / RUN_LOCK_FILENAME
        self._held = False
        self._payload: dict[str, Any] = {}

    def acquire(self, *, engine: str, session_id: Optional[str]) -> None:
        """Take the lock or raise RunLockHeldError.

        A lock whose pid is provably dead is a crashed run, not a live
        one: reclaim it with a stderr warning instead of leaving the
        operator a cleanup chore (ADR 0010). Two O_EXCL losses in a row
        mean another process reclaimed the same stale lock and won —
        its lock is live by construction, so refuse.
        """
        payload = {
            "pid": os.getpid(),
            "started": datetime.now(timezone.utc).isoformat(),
            "engine": engine,
            "session_id": session_id,
        }
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                self._reclaim_if_stale()  # raises RunLockHeldError if live
                continue
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(payload, indent=2) + "\n")
            self._held = True
            self._payload = payload
            return
        raise RunLockHeldError(self.path, self._read() or {})

    def annotate(self, *, session_id: str) -> None:
        """Record the session id once the session folder exists. Rewriting
        in place is safe: we hold the lock, and there is one writer."""
        if not self._held:
            return
        self._payload["session_id"] = session_id
        self.path.write_text(json.dumps(self._payload, indent=2) + "\n")

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        self._unlink_quiet()

    # ------------------------------------------------------------------

    def _reclaim_if_stale(self) -> None:
        """Unlink a provably-dead holder's lock, or raise RunLockHeldError."""
        holder = self._read()
        if holder is None:
            # Unreadable or gone. Gone: the holder released between our
            # O_EXCL loss and this read — retry will win. Corrupt (a crash
            # mid-write): no pid to probe, but also no evidence of a live
            # run — reclaim loudly rather than refuse forever.
            if self.path.exists():
                print(f"[gooseloop] reclaiming corrupt {self.path}",
                      file=sys.stderr)
                self._unlink_quiet()
            return
        pid = holder.get("pid")
        alive = _pid_alive(pid) if isinstance(pid, int) else False
        if alive is None or alive:
            # Alive, or unprobeable on this platform: refuse. A reused pid
            # can only push a stale lock into this branch — refusal is the
            # safe failure, reclaiming a live run would not be.
            raise RunLockHeldError(self.path, holder)
        print(
            f"[gooseloop] reclaiming stale {RUN_LOCK_FILENAME}: pid {pid} "
            f"({holder.get('engine', '?')}, started "
            f"{holder.get('started', '?')}) is dead — that run crashed "
            f"without cleaning up.",
            file=sys.stderr,
        )
        self._unlink_quiet()

    def _read(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _unlink_quiet(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
