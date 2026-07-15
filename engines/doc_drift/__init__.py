"""doc-drift reference engine.

Reads a canonical→derived document map, finds derived docs that fell behind
their canonical source (the canonical changed more recently and the derived
didn't follow), and drafts a patch to the derived side for an operator to seal.

Exposed at module level for the gooseloop CLI:
    engine         - DocDriftEngine factory.
    environment    - DocDriftEnvironment factory (reads [doc_drift] from
                     gooseloop.toml in the current working directory).

Configuration (in gooseloop.toml):

    [doc_drift]
    map = "doc-map.toml"             # canonical→derived map (hand-maintained)
    state = "doc-map.state.json"     # engine's cross-run memory (machine-written)
    drafts_dir = "doc-drift-drafts"  # where per-pair patch drafts land
    discovery_window_days = 7        # doc-root discovery window in days;
                                     # 0 disables discovery. Defaults to 7.
    discovery_roots = ["../website"] # dirs discovery may scan for unmapped doc
                                     # dirs; empty (default) = discovery off.

All three paths are relative to the current working directory, like
git_recap's output_dir — run gooseloop from the directory holding the map.
Missing or malformed config trips the engine's precheck with an
operator-actionable error before the run starts.
"""

from pathlib import Path
import tomllib

from .engine import DocDriftEngine, DocDriftEnvironment


def _load_config() -> dict:
    """Read cwd's gooseloop.toml in full.

    Returns an empty dict if no toml is present. The engine's precheck turns a
    missing [doc_drift] section into a friendly error rather than failing
    mid-pipeline.
    """
    path = Path.cwd() / "gooseloop.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def environment() -> DocDriftEnvironment:
    data = _load_config()
    cfg = data.get("doc_drift", {})
    map_path = Path(cfg.get("map", "doc-map.toml")).expanduser().resolve()
    state_path = Path(cfg.get("state", "doc-map.state.json")).expanduser().resolve()
    drafts_dir = Path(cfg.get("drafts_dir", "doc-drift-drafts")).expanduser().resolve()
    map_dir = map_path.parent
    return DocDriftEnvironment(
        map_path=map_path,
        state_path=state_path,
        drafts_dir=drafts_dir,
        journal_dir=_journal_dir(data, cfg),
        discovery_window_days=_discovery_window(data, cfg),
        discovery_roots=_discovery_roots(cfg, map_dir),
    )


def _discovery_window(data: dict, cfg: dict) -> int:
    """Days back that doc-root discovery treats as "recently changed".

    Reads [doc_drift] discovery_window_days; if unset, falls through a
    legacy [git_recap] window_days lookup to a default of 7. git_recap
    defines no window_days key of its own (its window is first_run_days),
    so absent an explicit discovery_window_days this is simply 7.
    0 disables discovery.
    """
    raw = cfg.get("discovery_window_days")
    if raw is None:
        raw = data.get("git_recap", {}).get("window_days", 7)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 7


def _discovery_roots(cfg: dict, map_dir: Path) -> list[Path]:
    """Directories doc-root discovery may scan for unmapped doc dirs. Empty
    (the default) turns discovery off — it never roams the tree uninvited.
    Relative entries resolve against the map's directory, like everything else
    in the map."""
    raw = cfg.get("discovery_roots")
    if not isinstance(raw, list):
        return []
    roots: list[Path] = []
    for entry in raw:
        p = Path(str(entry)).expanduser()
        roots.append(p if p.is_absolute() else (map_dir / p).resolve())
    return roots


def _journal_dir(data: dict, cfg: dict) -> Path:
    """Where git-recap's journal lives, for the optional "what changed and
    why" context (daily entries for the days the canonical changed).

    Prefers an explicit [doc_drift] journal_dir; otherwise borrows
    [git_recap] journal_dir (default "journal") so the two reference
    engines compose with no extra config — same loop root, same journal,
    the artifact on disk is the pipe. The path is only read if it exists
    at bundle time, so a stale or absent dir is harmless.
    """
    raw = cfg.get("journal_dir") or data.get("git_recap", {}).get("journal_dir", "journal")
    return Path(raw).expanduser().resolve()


def engine() -> DocDriftEngine:
    return DocDriftEngine()


__all__ = ["DocDriftEngine", "DocDriftEnvironment", "engine", "environment"]
