"""GitRecapEngine + GitRecapEnvironment.

Pipeline shape:

    review:   pastes the commit log across all configured repos, asks the
              model to emit routing[] = one summarize-commit entry per
              commit it cares about.
    body:     summarize-commit.yaml (one invocation per commit; writes
              <output_dir>/<sha-prefix>.md).
    summary:  glob-reads the summaries and renders a changelog grouped
              by repo, with operator_actions surfaced for follow-ups.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from gooseloop import (
    BranchPolicy,
    Context,
    Engine,
    Environment,
    Phase,
    Pipeline,
)


_HERE = Path(__file__).resolve().parent


class GitRecapEnvironment(Environment):
    """Configured git repos + window + author + output dir.

    The framework calls `env_vars()`; recipes paste content via
    env_method:<name>. The class is plain-attributed; nothing is read
    until a method is called, so a bad config doesn't blow up at
    construction — it surfaces in the engine's precheck where the
    operator can be told exactly what to fix.
    """

    def __init__(
        self,
        repos: list[Path],
        window_days: int,
        author: str,
        output_dir: Path,
        max_commits: int = 0,
    ) -> None:
        self.repos = repos
        self.window_days = window_days
        self.author = author
        self.output_dir = output_dir
        # Total commit budget across all repos, split evenly. 0 means no cap
        # (keep the whole window). A fast committer racks up hundreds in a
        # 7-day window, most irrelevant and slow to summarise; capping to the
        # N most-recent per repo keeps the recap to what you actually did
        # lately instead of trawling stale commits about approaches you've
        # since replaced.
        self.max_commits = max_commits
        self._resolved_author: str | None = None

    def per_repo_limit(self) -> Optional[int]:
        """The max-commits budget divided evenly across repos, or None.

        None means no cap (max_commits <= 0). With 50 over 2 repos this is 25
        each; the floor never goes below 1 so a repo is never silenced. The
        split is even by design: simple and predictable. A quiet repo just
        returns fewer than its share (the window still bounds it), so the run
        can total less than max_commits, never more.
        """
        if self.max_commits <= 0 or not self.repos:
            return None
        return max(1, self.max_commits // len(self.repos))

    # ---- framework contract --------------------------------------

    def env_vars(self) -> dict[str, str]:
        return {
            "WINDOW_DAYS": str(self.window_days),
            "OUTPUT_DIR": str(self.output_dir),
            "WEEKLY_DIR": str(self.output_dir / "weekly"),
            "RECAP_DATE": self._recap_date(),
            "AUTHOR": self.author_email(),
        }

    @staticmethod
    def _recap_date() -> str:
        """Today's date in UTC, ISO format. Stamped into the weekly recap filename
        so each run lands as a new file in the weekly/ folder instead of
        overwriting the prior week."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ---- content loaders the recipes paste in via env_method: ----

    def commit_log(self) -> str:
        """Formatted commit log across all configured repos, in window.

        One block per repo: header line, then a list of `sha  date  subject`
        rows. Empty repos render as `(no commits in window)`. Used by the
        review recipe via env_method:commit_log.

        Commits that already have a recap on disk are omitted: the review
        only sees commits still worth routing, so it doesn't burn a pass
        re-judging work the summarize-commit skip predicate would drop
        anyway. The "already recapped" test is the SAME predicate the
        BranchPolicy uses downstream (_skip_if_recap_exists over the same
        _summary_path_for), so the filter here and the skip there can never
        disagree — a commit is hidden only if it truly has a non-empty
        recap file. Each repo notes how many it omitted for transparency.
        """
        if not self.repos:
            return "(no repos configured)"
        already_recapped = _skip_if_recap_exists(_summary_path_for(self.output_dir))
        chunks: list[str] = []
        for repo in self.repos:
            chunks.append(f"== {repo} ==")
            rows, error = self._git_log_rows(repo)
            if error:
                chunks.append(f"  {error}")
            elif not rows:
                chunks.append("  (no commits in window)")
            else:
                omitted = 0
                for sha, date, subject in rows:
                    params = {"repo": str(repo), "sha": sha, "subject": subject}
                    if already_recapped(params):
                        omitted += 1
                        continue
                    chunks.append(f"  {sha}  {date}  {subject}")
                if omitted:
                    chunks.append(f"  ({omitted} already recapped, omitted)")
            chunks.append("")
        return "\n".join(chunks).rstrip()

    def diff_for_current(self) -> str:
        """Diff for the SHA + REPO currently in os.environ.

        Used by the body recipe via env_method:diff_for_current. The
        framework injects routing params as uppercase env vars before
        invoking the recipe, so $SHA and $REPO are populated at call
        time.
        """
        sha = os.environ.get("SHA", "").strip()
        repo_str = os.environ.get("REPO", "").strip()
        if not sha:
            return "(no SHA in environment; routing entry missing 'sha' param)"
        if not repo_str:
            return "(no REPO in environment; routing entry missing 'repo' param)"
        repo = Path(repo_str)
        if not (repo / ".git").is_dir():
            return f"(REPO={repo} is not a git repository)"
        return self._git_show(repo, sha)

    # ---- internals -----------------------------------------------

    def author_email(self) -> str:
        if self.author != "auto":
            return self.author
        if self._resolved_author is not None:
            return self._resolved_author
        for repo in self.repos:
            email = self._git_config_email(repo)
            if email:
                self._resolved_author = email
                return email
        # Fall back to global git config.
        email = self._git_config_email(Path.cwd())
        self._resolved_author = email or ""
        return self._resolved_author

    def _git_log_rows(self, repo: Path) -> tuple[list[tuple[str, str, str]], str | None]:
        """Return (rows, error). Each row is (short_sha, date, subject).

        Tab-delimited so the subject (which can contain double spaces) stays
        intact; the caller needs sha and subject as separate fields to test
        whether a recap already exists. On git failure, rows is empty and
        error is a human-readable string the caller renders in place of rows.
        """
        author = self.author_email()
        cmd = [
            "git", "-C", str(repo), "log",
            f"--since={self.window_days} days ago",
            "--pretty=format:%h%x09%as%x09%s",
        ]
        limit = self.per_repo_limit()
        if limit is not None:
            # -n caps to the N most-recent within the window: git walks HEAD
            # backwards and stops after N matches, so a fast week yields the
            # freshest N and the stale tail is dropped.
            cmd.append(f"-n{limit}")
        if author:
            cmd.append(f"--author={author}")
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return [], f"(git log failed: {proc.stderr.strip()})"
        rows: list[tuple[str, str, str]] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) == 3:
                rows.append((parts[0], parts[1], parts[2]))
        return rows, None

    def _git_show(self, repo: Path, sha: str) -> str:
        cmd = ["git", "-C", str(repo), "show", "--stat", "--patch", sha]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return f"(git show {sha} failed in {repo}: {proc.stderr.strip()})"
        return proc.stdout

    @staticmethod
    def _git_config_email(repo: Path) -> str:
        cmd = ["git", "-C", str(repo), "config", "user.email"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()


class GitRecapEngine(Engine):
    """Engine that summarises a window of your recent git activity."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        # Instance-level branch_policies; closes over output_dir so each
        # summarize-commit invocation writes to the right place. The base
        # Engine ABC permits this override (no ClassVar).
        path_fn = _summary_path_for(output_dir)
        self.branch_policies = {
            "summarize-commit": BranchPolicy(
                output_path=path_fn,
                skip_when=_skip_if_recap_exists(path_fn),
                intent="produce",
            ),
        }

    @property
    def name(self) -> str:
        return "git-recap"

    def recipes_dir(self) -> str:
        return str(_HERE / "recipes")

    # ---- precheck: fail loud, fail early -------------------------

    def precheck(self, ctx: Context) -> None:
        env = ctx.environment
        if not isinstance(env, GitRecapEnvironment):
            raise RuntimeError(
                "git-recap: environment must be a GitRecapEnvironment instance. "
                "If you're constructing the engine manually, also construct and "
                "pass the environment."
            )
        if not env.repos:
            raise RuntimeError(_MISSING_REPOS_HELP)
        bad = [r for r in env.repos if not (r / ".git").is_dir()]
        if bad:
            paths = "\n  ".join(str(p) for p in bad)
            raise RuntimeError(
                f"git-recap: these paths are not git repositories:\n  {paths}\n\n"
                f"Check the paths in your gooseloop.toml [git_recap] repos = [...]; "
                f"each one must be a directory containing a `.git` folder."
            )
        if env.window_days <= 0:
            raise RuntimeError(
                f"git-recap: window_days must be a positive integer "
                f"(got {env.window_days!r}). Try 7 for a weekly recap."
            )
        if env.max_commits < 0:
            raise RuntimeError(
                f"git-recap: max_commits can't be negative (got {env.max_commits!r}). "
                f"Use a positive total budget (split evenly across repos), or 0 "
                f"to summarise every commit in the window."
            )
        if env.author == "auto" and not env.author_email():
            raise RuntimeError(
                "git-recap: author='auto' but no `git config user.email` is set "
                "in any configured repo or the current working dir.\n\n"
                "Either set it (`git config --global user.email you@example.com`) "
                "or hardcode the email in gooseloop.toml's [git_recap] author = '...'."
            )

    # ---- pipeline -----------------------------------------------

    def pipeline(self, ctx: Context) -> Pipeline:
        recipes = _HERE / "recipes"
        return Pipeline(
            review=Phase(
                name="review",
                recipe_path=str(recipes / "review.example.yaml"),
            ),
            body=[],  # everything comes from the review's routing[]
            summary=Phase(
                name="summary",
                recipe_path=str(recipes / "summary.example.yaml"),
            ),
        )


def _summary_path_for(output_dir: Path):
    """Return a closure that computes the per-commit summary path.

    Filename shape: <YYYYMMDD-HHMMSS>-<slugified-subject>-<sha8>.md. The
    timestamp prefix is the commit's author date, read straight from git
    via the routing entry's `repo` + `sha`; it sorts lexicographically =
    chronologically so the recaps folder lists in commit order. The slug
    comes from the routing entry's `subject` param (the review supplies it
    from the commit log it just read). If the subject is missing, fall back
    to <prefix-><sha12>.md so the file is still uniquely named. If the date
    can't be resolved (no repo, unknown sha), the prefix is dropped.

    Extracted as a factory so the lambda capture (output_dir) is explicit
    and the policy is easy to test in isolation.
    """
    def compute(params: dict) -> Path | None:
        sha = str(params.get("sha", "")).strip()
        if not sha:
            return None
        repo = str(params.get("repo", "")).strip()
        stamp = _commit_datetime(Path(repo), sha) if repo else ""
        prefix = f"{stamp}-" if stamp else ""
        subject = str(params.get("subject", "")).strip()
        slug = _slugify(subject) if subject else ""
        if slug:
            return output_dir / f"{prefix}{slug}-{sha[:8]}.md"
        return output_dir / f"{prefix}{sha[:12]}.md"
    return compute


def _commit_datetime(repo: Path, sha: str) -> str:
    """Commit author date as a sortable `YYYYMMDD-HHMMSS` stamp, or "".

    Read from git so the filename orders by when the work actually
    happened, not by slug. Returns "" on any failure (path isn't a repo,
    sha unknown) so the caller drops the prefix rather than blowing up.
    """
    if not (repo / ".git").is_dir():
        return ""
    cmd = [
        "git", "-C", str(repo), "show", "-s",
        "--format=%ad", "--date=format:%Y%m%d-%H%M%S", sha,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _skip_if_recap_exists(path_fn):
    """Skip a summarize-commit invocation if the recap file already exists.

    Re-runs become idempotent: only commits without a recap on disk hit
    the model. Operator gets a one-line skip reason in the session log
    naming the existing file. To force re-summarisation, delete the file.
    """
    def check(params: dict) -> str | None:
        path = path_fn(params)
        if path is None:
            return None  # no SHA — let the recipe fail naturally
        if path.exists() and path.stat().st_size > 0:
            return f"recap already on disk: {path.name}"
        return None
    return check


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MULTIPLE_HYPHENS_RE = re.compile(r"-+")
_SLUG_MAX_LEN = 60


def _slugify(text: str) -> str:
    """Lowercase, kebab-case, alnum-only, trimmed to _SLUG_MAX_LEN chars.

    Empty input or input that contains nothing alphanumeric returns an
    empty string; the caller falls back to a SHA-only filename.
    """
    lowered = text.lower()
    hyphenated = _SLUG_NON_ALNUM_RE.sub("-", lowered)
    collapsed = _SLUG_MULTIPLE_HYPHENS_RE.sub("-", hyphenated).strip("-")
    return collapsed[:_SLUG_MAX_LEN].rstrip("-")


_MISSING_REPOS_HELP = (
    "git-recap: no repos configured.\n"
    "\n"
    "Add to your gooseloop.toml:\n"
    "\n"
    "    [git_recap]\n"
    '    repos = ["/home/you/Projects/somerepo", "/home/you/Projects/another"]\n'
    "    window_days = 7\n"
    '    author = "auto"          # or "you@example.com"\n'
    '    output_dir = "recaps"    # where per-commit summaries land\n'
    "\n"
    "Then re-run `gooseloop run -e engines.git_recap`."
)
