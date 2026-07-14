"""Hello-world reference engine.

A minimal Engine + Environment pair that demonstrates the framework
contract end-to-end without depending on any domain (no prospects, no
panels, no project-specific files).

Exposed at module level:
    engine        - HelloEngine class. The CLI instantiates this.
    environment   - factory reading [hello_world] from cwd's gooseloop.toml.

Configuration (in gooseloop.toml):

    [hello_world]
    names = "names.txt"          # one name per line; # comments and blanks skipped
    greetings_dir = "greetings"  # where greet.yaml writes its files

The names file is user-procured, like every input in this repo: copy the
committed `names.example.txt` to `names.txt` (gitignored) and put anyone
you like in it. A missing or empty file trips precheck with the exact cp
command — the engine never silently greets nobody.

Recipes ship under recipes/:
    review.example.yaml   - the bookend; emits a routing entry per name.
    greet.yaml            - body recipe; greets one name.
    summary.example.yaml  - the bookend; renders the ledger.
"""

from pathlib import Path
import tomllib

from .engine import HelloEngine, HelloEnvironment


def _load_hello_config() -> dict:
    """Read the [hello_world] section from cwd's gooseloop.toml. Empty
    dict when absent; precheck turns missing inputs into a friendly
    error instead of failing mid-pipeline."""
    path = Path.cwd() / "gooseloop.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("hello_world", {})


def _read_names(path: Path) -> list[str]:
    """One name per line; blanks and #-comment lines skipped."""
    if not path.exists():
        return []
    names: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            names.append(stripped)
    return names


def environment() -> HelloEnvironment:
    cfg = _load_hello_config()
    names_file = Path(cfg.get("names", "names.txt")).expanduser().resolve()
    greetings_dir = Path(cfg.get("greetings_dir", "greetings")).expanduser().resolve()
    return HelloEnvironment(
        names=_read_names(names_file),
        greetings_dir=greetings_dir,
        names_file=names_file,
    )


engine = HelloEngine

__all__ = ["HelloEngine", "HelloEnvironment", "engine", "environment"]
