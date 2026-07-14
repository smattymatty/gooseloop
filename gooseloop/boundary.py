"""THE BOUNDARY: OS-enforced filesystem masking around every goose spawn.

Recipes drive a tool-equipped model; whatever that model's shell can see,
a sufficiently manipulated phase can read. Prompt-level rules cannot bind
a shell — the operating system can. Every goose invocation runs inside a
bubblewrap sandbox where denied paths simply do not exist: a masked file
reads as empty, a masked directory as an empty tmpfs, and `find` returns
nothing because there is nothing.

The rules (grill, 2026-07-13):

- Deny-list over normal access: the filesystem looks like an ordinary
  run, minus the masks. Built-in defaults (below) always apply when
  bubblewrap is available; a `.gooseignore` at the loop root EXTENDS
  them — gitignore syntax, goose-may-not-touch meaning. It is committed,
  so the boundary travels with the repo.
- `.gooseignore` present but bubblewrap missing: the run is REFUSED
  (exit 4) — the operator demanded a boundary that cannot be provided.
  No file and no bubblewrap: runs proceed as before, with a one-line
  stderr nudge that a boundary exists.

Pattern semantics (deliberately small, documented in PROTOCOL §15):

- A bare name or name-glob (`.env*`, `*.pem`, `id_*`) masks every file
  OR directory with a matching basename, anywhere under the scanned
  scopes.
- A pattern containing `/` (or starting with `~`) is an anchored path,
  masked as a whole.
- `#` comments and blank lines are skipped. No `!` negation in v1 — a
  hole you can punch in a security boundary is a boundary with a hole.

Foundation layer: stdlib only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

GOOSEIGNORE_FILENAME = ".gooseignore"

# `gooseloop run` exit code when a configured boundary cannot be
# enforced — distinct from 1 (run error), 2 (usage), 3 (run.lock held).
EXIT_BOUNDARY = 4

# The floor. Applies whenever bubblewrap is available, file or no file.
BUILTIN_DENY: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials*",
    "*.keyring",
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.kube",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.local/share/keyrings",
    "~/.docker/config.json",
    # Every session's own mask map (below): a list of where secrets live
    # is reconnaissance material, so the map is denied to the goose the
    # same as what it maps. Past runs' maps are caught here by the scan;
    # the current run's is appended to the prefix after it is written.
    "boundary-masks.json",
)

# Directories never descended during the scan. Masked-as-directories
# are pruned automatically; these are toolchain caches and package
# registries — huge, and their `credentials.rs` / `server.pem` hits are
# crate test fixtures, not secrets (measured: scanning ~/.cargo alone
# cost 15s and 800 false masks). Real secrets that live under skipped
# trees are covered by anchored BUILTIN entries instead (keyrings,
# docker config).
_SCAN_PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv",
               ".mypy_cache", ".pytest_cache", ".cache",
               ".cargo", ".rustup", ".npm", ".nvm", ".pyenv", ".conda",
               ".gradle", ".m2", ".local", ".steam", ".wine", ".var",
               ".mozilla", ".thunderbird"}


def bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def load_gooseignore(anchor: Path) -> list[str]:
    """Patterns from the loop root's .gooseignore, or []. Comments and
    blanks skipped; `!` negation refused loudly (see module docstring)."""
    path = anchor / GOOSEIGNORE_FILENAME
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            raise ValueError(
                f"{GOOSEIGNORE_FILENAME}: negation ({stripped!r}) is not "
                "supported — a hole you can punch in a boundary is a "
                "boundary with a hole."
            )
        out.append(stripped)
    return out


def gooseignore_present(anchor: Path) -> bool:
    return (anchor / GOOSEIGNORE_FILENAME).exists()


def _split_patterns(patterns: list[str]) -> tuple[list[str], list[Path]]:
    """(basename globs, anchored paths)."""
    names: list[str] = []
    anchored: list[Path] = []
    for raw in patterns:
        if raw.startswith("~"):
            anchored.append(Path(raw).expanduser())
        elif "/" in raw:
            anchored.append(Path(raw).expanduser().resolve())
        else:
            names.append(raw)
    return names, anchored


def find_masks(patterns: list[str], scopes: list[Path]) -> list[Path]:
    """Every existing path the patterns mask, found deterministically.

    Basename globs are matched by walking `scopes` (a matched directory
    is masked whole and never descended). Anchored paths are included
    when they exist. Symlinks are never followed."""
    names, anchored = _split_patterns(patterns)
    masks: list[Path] = [p for p in anchored if p.exists() or p.is_symlink()]

    def matches(basename: str) -> bool:
        return any(fnmatch.fnmatch(basename, g) for g in names)

    for scope in scopes:
        if not scope.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(scope, followlinks=False):
            keep: list[str] = []
            for d in dirnames:
                if matches(d):
                    masks.append(Path(dirpath) / d)
                elif d not in _SCAN_PRUNE:
                    keep.append(d)
            dirnames[:] = keep
            for f in filenames:
                if matches(f):
                    masks.append(Path(dirpath) / f)
    # Deduplicate; parents win over children so bwrap gets one mount each.
    unique = sorted(set(masks))
    out: list[Path] = []
    for p in unique:
        if not any(p.is_relative_to(kept) for kept in out):
            out.append(p)
    return out


# File masks bind over an EMPTY REGULAR FILE, not /dev/null: bwrap's
# --ro-bind mounts nodev, so a bound device node answers reads with
# EPERM — noisy where the contract says "reads as empty". One empty
# 0444 source file per process, created lazily; it must outlive every
# spawn that uses the prefix, which module lifetime guarantees.
_EMPTY_MASK_SOURCE: Path | None = None


def _empty_mask_source() -> Path:
    global _EMPTY_MASK_SOURCE
    if _EMPTY_MASK_SOURCE is None or not _EMPTY_MASK_SOURCE.exists():
        fd, name = tempfile.mkstemp(prefix="gooseloop-boundary-empty-")
        os.close(fd)
        os.chmod(name, 0o444)
        _EMPTY_MASK_SOURCE = Path(name)
    return _EMPTY_MASK_SOURCE


def bwrap_prefix(masks: list[Path]) -> list[str]:
    """The command prefix that runs its argument inside the boundary.

    `--dev-bind / /` keeps the filesystem, devices, and network exactly
    as an unsandboxed run would see them — the boundary is ONLY the
    masks: directories become empty tmpfs, files read as empty.
    """
    args = ["bwrap", "--die-with-parent", "--dev-bind", "/", "/"]
    empty = str(_empty_mask_source())
    for mask in masks:
        if mask.is_dir() and not mask.is_symlink():
            args += ["--tmpfs", str(mask)]
        else:
            args += ["--ro-bind", empty, str(mask)]
    args.append("--")
    return args


class BoundaryUnavailableError(RuntimeError):
    """A .gooseignore demands a boundary bubblewrap cannot provide."""

    def __init__(self, anchor: Path):
        super().__init__(
            f"{GOOSEIGNORE_FILENAME} found in {anchor} but bubblewrap is "
            "not installed — refusing to run without the boundary you "
            "configured.\n"
            "  pacman -S bubblewrap    (Arch)\n"
            "  apt install bubblewrap  (Debian/Ubuntu)\n"
            "  dnf install bubblewrap  (Fedora)"
        )


@dataclass(frozen=True)
class Boundary:
    """A resolved boundary: the spawn prefix, what it masks, and the
    patterns that produced the masks (floor + .gooseignore, in order —
    the provenance the session artifact records)."""

    prefix: list[str]
    masks: list[Path] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


def resolve_sandbox(anchor: Path, home: Path | None = None) -> Boundary | None:
    """The Boundary for this loop root, or None to run unsandboxed.

    - bubblewrap available: builtin floor + .gooseignore extensions,
      masks scanned under $HOME (and the anchor when outside it).
    - unavailable + .gooseignore present: BoundaryUnavailableError.
    - unavailable + no file: None, with a one-line stderr nudge.
    """
    home = home or Path.home()
    if not bwrap_available():
        if gooseignore_present(anchor):
            raise BoundaryUnavailableError(anchor)
        print(
            "[gooseloop] bubblewrap not found: running without the "
            "filesystem boundary (a .gooseignore would make this an error).",
            file=sys.stderr,
        )
        return None
    patterns = list(BUILTIN_DENY) + load_gooseignore(anchor)
    scopes = [home]
    if not anchor.resolve().is_relative_to(home.resolve()):
        scopes.append(anchor.resolve())
    masks = find_masks(patterns, scopes)
    return Boundary(prefix=bwrap_prefix(masks), masks=masks, patterns=patterns)


BOUNDARY_MASKS_FILENAME = "boundary-masks.json"


def persist_masks(session_dir: Path, boundary: Boundary | None) -> Path | None:
    """Write the session's mask map: which patterns were in force and
    exactly which paths were masked, so run A's boundary can be diffed
    against run B's ("phase read an empty config" investigations end at
    this file). Paths only, never contents.

    The map itself is secret-shaped — a list of where credentials live —
    so it gets the boundary's own treatment: BUILTIN_DENY carries its
    basename (past runs' maps are masked by the scan), and the caller
    appends the returned path to the prefix so the CURRENT run's map is
    masked too. Inside the sandbox this file reads empty; the operator
    and the dash read it normally. Returns None on an unwritable dir
    (telemetry must not fail the pass)."""
    home = str(Path.home())
    payload = {
        "enforced": boundary is not None,
        "patterns": list(boundary.patterns) if boundary else [],
        "mask_count": len(boundary.masks) if boundary else 0,
        "masks": [str(m).replace(home, "~") for m in boundary.masks]
                 if boundary else [],
    }
    path = session_dir / BOUNDARY_MASKS_FILENAME
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError:
        return None
    return path
