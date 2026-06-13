"""git-recap reference engine.

Walks a set of configured git repos, finds your commits in a recent
window, summarises each via the body recipe, and renders a changelog
summary at the end.

Exposed at module level for the gooseloop CLI:
    engine         - GitRecapEngine factory.
    environment    - GitRecapEnvironment factory (reads [git_recap] from
                     gooseloop.toml in the current working directory).

Configuration (in gooseloop.toml):

    [git_recap]
    repos = ["/path/to/repo1", "/path/to/repo2"]
    window_days = 7              # how far back to look
    max_commits = 50             # total budget, split evenly across repos (0 = no cap)
    author = "auto"              # "auto" => git config user.email of first repo
    output_dir = "recaps"        # body recipes write summaries here

Missing or malformed config trips the engine's precheck and prints an
operator-actionable error before the run starts. The engine never
silently degrades.
"""

from pathlib import Path
import tomllib

from .engine import GitRecapEngine, GitRecapEnvironment


def _load_git_recap_config() -> dict:
    """Read the [git_recap] section from cwd's gooseloop.toml.

    Returns an empty dict if no toml or no section is present. The
    engine's precheck turns that into a friendly error instead of
    failing partway through the pipeline.
    """
    path = Path.cwd() / "gooseloop.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("git_recap", {})


def environment() -> GitRecapEnvironment:
    cfg = _load_git_recap_config()
    repos = [Path(p).expanduser().resolve() for p in cfg.get("repos", [])]
    window_days = int(cfg.get("window_days", 7))
    max_commits = int(cfg.get("max_commits", 50))
    author = str(cfg.get("author", "auto"))
    output_dir = Path(cfg.get("output_dir", "recaps")).expanduser().resolve()
    return GitRecapEnvironment(
        repos=repos,
        window_days=window_days,
        author=author,
        output_dir=output_dir,
        max_commits=max_commits,
    )


def engine() -> GitRecapEngine:
    return GitRecapEngine(output_dir=environment().output_dir)


__all__ = ["GitRecapEngine", "GitRecapEnvironment", "engine", "environment"]
