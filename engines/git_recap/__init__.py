"""git-recap reference engine — a work journal across your repos.

One combined daily entry per date (sectioned by project), plus a weekly
review when an ISO week closes. Model-routed with deterministic
skip_when seatbelts; per-repo commit watermarks make every daily cover
exactly the commits no daily has covered.

Exposed at module level for the gooseloop CLI:
    engine         - GitRecapEngine factory.
    environment    - GitRecapEnvironment factory (reads [git_recap] from
                     gooseloop.toml in the current working directory).

Configuration (in gooseloop.toml):

    [git_recap]
    repos = ["/path/to/repo1", "/path/to/repo2"]
    author = "auto"                       # "auto" => git config user.email of first repo
    journal_dir = "journal"               # daily/ and weekly/ land under here
    state = "git-recap.state.json"        # per-repo watermarks (machine-written)
    first_run_days = 7                    # window for a repo's very first daily

Missing or malformed config trips the engine's precheck with an
operator-actionable error before the run starts. The engine never
silently degrades.
"""

from pathlib import Path
import tomllib

from .engine import GitRecapEngine, GitRecapEnvironment


def _load_git_recap_config() -> dict:
    """Read the [git_recap] section from cwd's gooseloop.toml.

    Returns an empty dict if no toml or no section is present; precheck
    turns that into a friendly error instead of failing mid-pipeline.
    """
    path = Path.cwd() / "gooseloop.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("git_recap", {})


def environment() -> GitRecapEnvironment:
    cfg = _load_git_recap_config()
    return GitRecapEnvironment(
        repos=[Path(p).expanduser().resolve() for p in cfg.get("repos", [])],
        author=str(cfg.get("author", "auto")),
        journal_dir=Path(cfg.get("journal_dir", "journal")).expanduser().resolve(),
        state_path=Path(cfg.get("state", "git-recap.state.json")).expanduser().resolve(),
        first_run_days=int(cfg.get("first_run_days", 7)),
    )


def engine() -> GitRecapEngine:
    return GitRecapEngine(env=environment())


__all__ = ["GitRecapEngine", "GitRecapEnvironment", "engine", "environment"]
