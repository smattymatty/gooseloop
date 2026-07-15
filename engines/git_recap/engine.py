"""GitRecapEngine v2 — a work journal, not a commit pile.

The redesign (grill, 2026-07-13): no per-commit files. The unit of output
is the DAY — one combined entry per date across every configured repo,
sectioned by project — plus a weekly review when an ISO week closes.

Pipeline shape (model-routed, with deterministic seatbelts):

    review:   pastes the journal status (per-repo watermarks + fresh
              commit counts, recent dailies, last weekly, whether a
              weekly is due) and emits routing[]: one `daily` entry for
              today, plus one `weekly` entry when the closed week's
              review is missing. The model PROPOSES; BranchPolicy
              skip_when VERIFIES — wrong date, weekly-not-due, or
              nothing-new all skip deterministically. Date math is never
              trusted to a language model.
    body:     daily.yaml  — writes journal/daily/<date>.md with the last
              few dailies + the last weekly + today's existing entry as
              context. Amend semantics: run twice a day and today's page
              is rewritten as ONE coherent entry covering both.
              weekly.yaml — writes journal/weekly/<ISO-week>.md from the
              closed week's dailies (however many exist), with the
              previous weekly as arc context.
    summary:  renders what was journaled and what was skipped, and — in
              post_process, ONLY after today's daily verifiably exists —
              advances the per-repo commit watermarks. A failed write
              never swallows commits.

Cross-run state (git-recap.state.json, machine-written):

    { "watermarks": { "<repo path>": "<sha>" } }

A daily covers exactly watermark..HEAD per repo: skip three days and the
gap is still covered; run twice and only the new commits are added. The
first run (no watermark yet) bounds its window by where the journal left
off — commits since the most recent existing daily's date (that day
included, fail-safe toward keeping commits) — so a repo whose history is
already partly journaled never gets its old commits re-scooped. Only a
truly empty journal falls back to `first_run_days`.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from gooseloop import (
    BranchPolicy,
    Context,
    Engine,
    Environment,
    Phase,
    Pipeline,
)
from gooseloop.toolkit import cap, load_state, save_state

_HERE = Path(__file__).resolve().parent

# How many recent dailies get pasted as no-repetition context.
CONTEXT_DAILIES = 5


# ---- date helpers (shared by env methods AND skip_when seatbelts, so
# ---- the model's routing is always checked against the same arithmetic)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _iso_week_id(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _closed_week(today: Optional[date] = None) -> str:
    """The most recently CLOSED ISO week — the week before the one
    containing `today`."""
    t = today or datetime.now(timezone.utc).date()
    monday = t - timedelta(days=t.weekday())
    return _iso_week_id(monday - timedelta(days=1))


def _week_dates(week_id: str) -> list[str]:
    """The seven ISO dates of `2026-W28`, Monday..Sunday."""
    year, week = week_id.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


# ---- environment ----------------------------------------------------


class GitRecapEnvironment(Environment):
    """Configured repos + the journal on disk + the watermark state.

    Nothing is read at construction; content loads when a recipe calls an
    env_method, and a bad config surfaces in precheck with an actionable
    message."""

    def __init__(
        self,
        repos: list[Path],
        author: str,
        journal_dir: Path,
        state_path: Path,
        first_run_days: int = 7,
    ) -> None:
        self.repos = repos
        self.author = author
        self.journal_dir = journal_dir
        self.state_path = state_path
        self.first_run_days = first_run_days
        self._resolved_author: str | None = None
        # HEAD shas captured when fresh_commits() ran — the exact commits
        # the daily was shown. The summary's post_process advances the
        # watermarks to THESE, never to a later HEAD a mid-run commit
        # could have moved (write what you read).
        self.seen_heads: dict[str, str] = {}

    # -- framework contract ------------------------------------------

    def env_vars(self) -> dict[str, str]:
        return {
            "JOURNAL_DIR": str(self.journal_dir),
            "DAILY_DIR": str(self.daily_dir),
            "WEEKLY_DIR": str(self.weekly_dir),
            "TODAY": _today(),
            "CLOSED_WEEK": _closed_week(),
            "AUTHOR": self.author,
        }

    # -- derived paths -------------------------------------------------

    @property
    def daily_dir(self) -> Path:
        return self.journal_dir / "daily"

    @property
    def weekly_dir(self) -> Path:
        return self.journal_dir / "weekly"

    def daily_path(self, day: str) -> Path:
        return self.daily_dir / f"{day}.md"

    def weekly_path(self, week_id: str) -> Path:
        return self.weekly_dir / f"{week_id}.md"

    # -- state ----------------------------------------------------------

    def watermarks(self) -> dict[str, str]:
        state = load_state(self.state_path, {"watermarks": {}})
        marks = state.get("watermarks", {})
        return marks if isinstance(marks, dict) else {}

    def advance_watermarks(self) -> None:
        """Persist the HEADs the daily actually saw. Called by the
        engine's summary post_process only after the entry exists."""
        if not self.seen_heads:
            return
        state = load_state(self.state_path, {"watermarks": {}})
        state.setdefault("watermarks", {}).update(self.seen_heads)
        save_state(self.state_path, state)

    # -- git ---------------------------------------------------------------

    def _git(self, repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def resolved_author(self) -> str:
        if self._resolved_author is None:
            if self.author != "auto":
                self._resolved_author = self.author
            else:
                first = self.repos[0] if self.repos else None
                self._resolved_author = (
                    self._git(first, "config", "user.email") if first else ""
                ) or "auto"
        return self._resolved_author

    def _most_recent_daily_date(self) -> str | None:
        """The date of the newest daily on disk, or None if the journal is
        empty. Filenames are ISO dates, so lexical max is chronological."""
        if not self.daily_dir.is_dir():
            return None
        stems = sorted(p.stem for p in self.daily_dir.glob("*.md"))
        return stems[-1] if stems else None

    def _fresh_range(self, repo: Path) -> list[str]:
        mark = self.watermarks().get(str(repo))
        if mark and self._git(repo, "cat-file", "-t", mark) == "commit":
            return [f"{mark}..HEAD"]
        # First run (no watermark): bound the window by where the journal
        # left off, not a blind first_run_days dragnet — otherwise a repo
        # whose history is already partly journaled gets its old commits
        # re-scooped and double-counted. The boundary day is INCLUDED
        # (--since=<date> is that day's 00:00), fail-safe toward KEEP: a
        # commit made after that day's daily ran is never silently lost,
        # and the one day of overlap is deduped by the body model, which
        # gets that daily as no-repeat context. Only a truly empty journal
        # falls back to first_run_days.
        last_daily = self._most_recent_daily_date()
        if last_daily:
            return [f"--since={last_daily} 00:00"]
        return [f"--since={self.first_run_days} days ago"]

    def fresh_commit_shas(self, repo: Path) -> list[str]:
        out = self._git(repo, "log", *self._fresh_range(repo),
                        f"--author={self.resolved_author()}", "--format=%H")
        return [s for s in out.splitlines() if s]

    # -- env_methods (no arguments; recipes paste these) -----------------

    def journal_status(self) -> str:
        """The review's one table: per-repo fresh counts, journal shape,
        whether a weekly is due — everything routing needs, computed
        deterministically so the model reports rather than derives."""
        lines = [
            f"journal: {self.journal_dir} (dailies in daily/, weeklies in weekly/)",
            f"today's entry path: {self.daily_path(_today())}",
            "repo | fresh commits since watermark",
        ]
        total = 0
        for repo in self.repos:
            n = len(self.fresh_commit_shas(repo))
            total += n
            lines.append(f"{repo.name} | {n}")
        lines.append(f"TOTAL fresh: {total}")
        lines.append(f"today: {_today()}"
                     f" | today's entry exists: {self.daily_path(_today()).exists()}")
        closed = _closed_week()
        lines.append(f"closed week: {closed}"
                     f" | weekly exists: {self.weekly_path(closed).exists()}"
                     f" | weekly due: {self.weekly_due()}")
        recent = [p.stem for p in self._recent_daily_paths(CONTEXT_DAILIES)]
        lines.append(f"recent dailies: {', '.join(recent) or '(none yet)'}")
        return "\n".join(lines)

    def fresh_commits(self) -> str:
        """Empirical material for the daily: per repo, every commit since
        the watermark with its file-level stat. Also captures the HEAD
        seen per repo, for the post-write watermark advance."""
        chunks: list[str] = []
        for repo in self.repos:
            head = self._git(repo, "rev-parse", "HEAD")
            if head:
                self.seen_heads[str(repo)] = head
            log = self._git(
                repo, "log", *self._fresh_range(repo),
                f"--author={self.resolved_author()}",
                "--format=%h %ad %s", "--date=format:%H:%M", "--stat",
            )
            chunks.append(f"### {repo.name}\n{log or '(no fresh commits)'}")
        return cap("\n\n".join(chunks) or "(no repos configured)")

    def _recent_daily_paths(self, n: int) -> list[Path]:
        if not self.daily_dir.is_dir():
            return []
        return sorted(self.daily_dir.glob("*.md"))[-n:]

    def recent_dailies(self) -> str:
        """The last few dailies, oldest first — the no-repetition context
        and the arc ('polished further', 'finally shipped')."""
        paths = [p for p in self._recent_daily_paths(CONTEXT_DAILIES + 1)
                 if p.stem != _today()][-CONTEXT_DAILIES:]
        if not paths:
            return "(no previous dailies — this is the journal's first entry)"
        return cap("\n\n".join(
            f"--- {p.stem} ---\n{p.read_text()}" for p in paths
        ))

    def todays_entry(self) -> str:
        """Today's existing entry, for amend runs. The daily recipe
        rewrites the file as ONE coherent entry covering old + new."""
        path = self.daily_path(_today())
        if not path.exists():
            return "(none yet — this is today's first entry)"
        return cap(path.read_text())

    def last_weekly(self) -> str:
        """The most recent weekly on disk. For a daily this is arc
        context; for the weekly being written it is the PREVIOUS week."""
        if not self.weekly_dir.is_dir():
            return "(no weekly reviews yet)"
        paths = sorted(self.weekly_dir.glob("*.md"))
        if not paths:
            return "(no weekly reviews yet)"
        latest = paths[-1]
        return cap(f"--- {latest.stem} ---\n{latest.read_text()}")

    def closed_week_dailies(self) -> str:
        """Every daily belonging to the most recently closed ISO week —
        the weekly recipe's whole input."""
        week = _closed_week()
        present = [p for p in (self.daily_path(d) for d in _week_dates(week))
                   if p.exists()]
        if not present:
            return f"(no dailies exist for {week})"
        return cap("\n\n".join(
            f"--- {p.stem} ---\n{p.read_text()}" for p in present
        ))

    # -- seatbelt inputs -----------------------------------------------------

    def total_fresh(self) -> int:
        return sum(len(self.fresh_commit_shas(r)) for r in self.repos)

    def weekly_due(self) -> bool:
        week = _closed_week()
        if self.weekly_path(week).exists():
            return False
        return any(self.daily_path(d).exists() for d in _week_dates(week))


# ---- engine ---------------------------------------------------------


class GitRecapEngine(Engine):
    """Journal engine: one combined daily per date, weekly on week close."""

    def __init__(self, env: GitRecapEnvironment) -> None:
        self.env = env
        # The model proposes via routing[]; these verify. Every guard uses
        # the same arithmetic the env methods used, so the review can
        # never talk the body into writing the wrong file.
        self.branch_policies = {
            "daily": BranchPolicy(
                skip_when=self._skip_daily,
                output_path=lambda p: env.daily_path(str(p.get("date", ""))),
                intent="edit-or-produce",  # amend runs rewrite the file
            ),
            "weekly": BranchPolicy(
                skip_when=self._skip_weekly,
                output_path=lambda p: env.weekly_path(str(p.get("week", ""))),
                intent="produce",
            ),
        }

    @property
    def name(self) -> str:
        return "git-recap"

    def recipes_dir(self) -> str:
        return str(_HERE / "recipes")

    def planned_bound(self, ctx: Context) -> int | None:
        """Max body phases this pass could route, known before the model
        routes: a daily iff there are fresh commits, a weekly iff one is due.
        The model proposes; the skip_when seatbelts can only remove from this
        set, never add — so it's a true ceiling. Lets the dash show [1/<=N]
        instead of [1/?] for this model-routed engine."""
        env = ctx.environment
        if not isinstance(env, GitRecapEnvironment):
            return None
        return (1 if env.total_fresh() > 0 else 0) + (1 if env.weekly_due() else 0)

    # -- precheck: fail loud, fail early ---------------------------------

    def precheck(self, ctx: Context) -> None:
        env = ctx.environment
        if not isinstance(env, GitRecapEnvironment):
            raise RuntimeError(
                "git-recap: environment must be a GitRecapEnvironment "
                "instance. If you're constructing the engine manually, "
                "construct and pass the environment too."
            )
        if not env.repos:
            raise RuntimeError(
                "git-recap: no repos configured. Add them to gooseloop.toml:\n"
                "  [git_recap]\n  repos = [\"/path/to/repo\"]"
            )
        bad = [r for r in env.repos if not (r / ".git").is_dir()]
        if bad:
            paths = "\n  ".join(str(p) for p in bad)
            raise RuntimeError(
                f"git-recap: these paths are not git repositories:\n  {paths}\n"
                f"Each [git_recap] repos entry must contain a .git folder."
            )
        env.daily_dir.mkdir(parents=True, exist_ok=True)
        env.weekly_dir.mkdir(parents=True, exist_ok=True)

    # -- seatbelts ------------------------------------------------------------

    def _skip_daily(self, params: dict[str, Any]) -> bool | str:
        routed = str(params.get("date", ""))
        if routed != _today():
            return f"review routed a daily for {routed!r}; today is {_today()}"
        if self.env.total_fresh() == 0:
            return "no commits since the watermarks — nothing to journal"
        return False

    def _skip_weekly(self, params: dict[str, Any]) -> bool | str:
        routed = str(params.get("week", ""))
        closed = _closed_week()
        if routed != closed:
            return f"review routed weekly {routed!r}; the closed week is {closed}"
        if self.env.weekly_path(closed).exists():
            return f"{closed} already reviewed — weeklies are written once"
        if not self.env.weekly_due():
            return f"no dailies exist for {closed} — nothing to review"
        return False

    # -- pipeline ----------------------------------------------------------

    def pipeline(self, ctx: Context) -> Pipeline:
        def advance_after_verified_write(_output: str, c: Context) -> None:
            # The watermarks move ONLY once today's entry demonstrably
            # exists and is non-empty — a failed daily never swallows the
            # commits it was shown (grill decision, 2026-07-13).
            #
            # Act on the env the RUN used (ctx.environment): the CLI
            # constructs the engine's env and the looper's env as two
            # instances, and seen_heads is captured on the looper's one
            # when the daily's context renders fresh_commits. Reading
            # self.env here silently advanced 0 repos (caught live,
            # 2026-07-13 21:55 — the first real daily's watermarks never
            # moved).
            env = c.environment if isinstance(c.environment, GitRecapEnvironment) else self.env
            path = env.daily_path(_today())
            if path.exists() and path.stat().st_size > 0:
                if env.seen_heads:
                    env.advance_watermarks()
                    if c.session_dir:
                        c.session_log(
                            f"watermarks advanced ({len(env.seen_heads)} repo(s))"
                        )
                else:
                    c.add_operator_action(
                        action="investigate the empty watermark capture",
                        why="the daily wrote but no repo HEADs were captured "
                            "during its render — watermarks NOT advanced; the "
                            "next run re-covers the same commits",
                    )
            else:
                c.add_operator_action(
                    action="investigate the failed daily entry",
                    why="today's daily never wrote, so the commit watermarks "
                        "were NOT advanced — the next run re-covers everything",
                )

        recipes = _HERE / "recipes"
        return Pipeline(
            review=Phase(
                name="review",
                recipe_path=str(recipes / "review.example.yaml"),
            ),
            summary=Phase(
                name="summary",
                recipe_path=str(recipes / "summary.example.yaml"),
                post_process=advance_after_verified_write,
            ),
        )
