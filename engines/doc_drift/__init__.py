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
    return DocDriftEnvironment(
        map_path=map_path,
        state_path=state_path,
        drafts_dir=drafts_dir,
        recaps_dir=_recaps_dir(data, cfg),
    )


def _recaps_dir(data: dict, cfg: dict) -> Path:
    """Where git-recap's per-commit summaries land, for the optional
    "what changed and why" context.

    Prefers an explicit [doc_drift] recaps_dir; otherwise borrows
    [git_recap] output_dir (default "recaps") so the two reference engines
    line up with no extra config. The path is only read if it exists on disk
    at bundle time, so a stale or absent dir is harmless.
    """
    raw = cfg.get("recaps_dir") or data.get("git_recap", {}).get("output_dir", "recaps")
    return Path(raw).expanduser().resolve()


def engine() -> DocDriftEngine:
    return DocDriftEngine()


__all__ = ["DocDriftEngine", "DocDriftEnvironment", "engine", "environment"]
