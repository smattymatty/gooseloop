"""git-recap engine: config loading, precheck friendly errors, branch policy.

No live git calls in these tests; the env_method content loaders
(commit_log, diff_for_current) are integration-level and exercised
only by manual runs.
"""

import os
import subprocess
from pathlib import Path

import pytest

from engines.git_recap import GitRecapEngine, GitRecapEnvironment
from engines.git_recap.engine import _summary_path_for
from gooseloop.phase import Context


# ---- helpers -----------------------------------------------------

def _ctx(env: GitRecapEnvironment | None) -> Context:
    return Context(model="m", session_dir=None, base_env={}, environment=env)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    return path


# ---- env_vars + author resolution --------------------------------

def test_env_vars_shape(tmp_path):
    env = GitRecapEnvironment(
        repos=[],
        window_days=14,
        author="me@me.com",
        output_dir=tmp_path / "recaps",
    )
    vars_ = env.env_vars()
    assert vars_["WINDOW_DAYS"] == "14"
    assert vars_["OUTPUT_DIR"] == str(tmp_path / "recaps")
    assert vars_["WEEKLY_DIR"] == str(tmp_path / "recaps" / "weekly")
    assert vars_["AUTHOR"] == "me@me.com"


def test_env_vars_includes_iso_recap_date(tmp_path):
    """RECAP_DATE drives the weekly file's filename. Stamped at env_vars()
    time so each run gets today's date even if the looper caches the
    environment across invocations."""
    import re
    env = GitRecapEnvironment(
        repos=[], window_days=7, author="x@y",
        output_dir=tmp_path,
    )
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", env.env_vars()["RECAP_DATE"])


def test_weekly_dir_is_weekly_subdir_of_output_dir(tmp_path):
    env = GitRecapEnvironment(
        repos=[], window_days=7, author="x@y",
        output_dir=tmp_path / "recaps",
    )
    weekly = Path(env.env_vars()["WEEKLY_DIR"])
    assert weekly.parent == (tmp_path / "recaps").resolve() or weekly.parent == tmp_path / "recaps"
    assert weekly.name == "weekly"


def test_author_auto_pulls_from_first_repo(tmp_path):
    repo = _make_repo(tmp_path / "r")
    env = GitRecapEnvironment(
        repos=[repo],
        window_days=7,
        author="auto",
        output_dir=tmp_path / "recaps",
    )
    assert env.env_vars()["AUTHOR"] == "test@example.com"


# ---- precheck failure modes --------------------------------------

def test_precheck_missing_repos_explains_config(tmp_path):
    env = GitRecapEnvironment(repos=[], window_days=7, author="auto",
                              output_dir=tmp_path)
    engine = GitRecapEngine(output_dir=tmp_path)
    with pytest.raises(RuntimeError) as exc:
        engine.precheck(_ctx(env))
    msg = str(exc.value)
    assert "no repos configured" in msg
    assert "[git_recap]" in msg
    assert "repos = " in msg


def test_precheck_non_git_path_lists_the_bad_path(tmp_path):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    env = GitRecapEnvironment(repos=[not_a_repo], window_days=7, author="x@y",
                              output_dir=tmp_path)
    engine = GitRecapEngine(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="not git repositories"):
        engine.precheck(_ctx(env))


def test_precheck_zero_window_days_explains(tmp_path):
    repo = _make_repo(tmp_path / "r")
    env = GitRecapEnvironment(repos=[repo], window_days=0, author="x@y",
                              output_dir=tmp_path)
    engine = GitRecapEngine(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="window_days must be"):
        engine.precheck(_ctx(env))


def test_precheck_negative_max_commits_explains(tmp_path):
    repo = _make_repo(tmp_path / "r")
    env = GitRecapEnvironment(repos=[repo], window_days=7, author="x@y",
                              output_dir=tmp_path, max_commits=-1)
    engine = GitRecapEngine(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="max_commits can't be negative"):
        engine.precheck(_ctx(env))


# ---- commit budget split -----------------------------------------

def _env_with_repos(tmp_path, n_repos, max_commits):
    repos = [tmp_path / f"r{i}" for i in range(n_repos)]
    return GitRecapEnvironment(repos=repos, window_days=7, author="x@y",
                               output_dir=tmp_path, max_commits=max_commits)


def test_per_repo_limit_splits_evenly(tmp_path):
    assert _env_with_repos(tmp_path, 2, 50).per_repo_limit() == 25


def test_per_repo_limit_none_when_uncapped(tmp_path):
    assert _env_with_repos(tmp_path, 2, 0).per_repo_limit() is None


def test_per_repo_limit_floors_at_one(tmp_path):
    # 1 commit budget over 3 repos: never silence a repo, give each 1.
    assert _env_with_repos(tmp_path, 3, 1).per_repo_limit() == 1


def test_per_repo_limit_none_when_no_repos(tmp_path):
    assert _env_with_repos(tmp_path, 0, 50).per_repo_limit() is None


def test_precheck_wrong_environment_type_caught(tmp_path):
    """If someone wires git-recap with a different Environment subclass,
    fail with a clear hint instead of crashing inside a content loader."""
    from gooseloop import Environment

    class WrongEnv(Environment):
        def env_vars(self): return {}

    engine = GitRecapEngine(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="GitRecapEnvironment"):
        engine.precheck(_ctx(WrongEnv()))


def test_precheck_passes_on_clean_config(tmp_path):
    repo = _make_repo(tmp_path / "r")
    env = GitRecapEnvironment(repos=[repo], window_days=7, author="auto",
                              output_dir=tmp_path)
    engine = GitRecapEngine(output_dir=tmp_path)
    engine.precheck(_ctx(env))  # should not raise


# ---- branch policy ------------------------------------------------

def test_summary_path_uses_slugified_subject_when_provided(tmp_path):
    compute = _summary_path_for(tmp_path / "recaps")
    p = compute({
        "sha": "abcdef1234567890",
        "subject": "feat: rebrand buckets across templates and serializers",
        "repo": "/x",
    })
    assert p == tmp_path / "recaps" / "feat-rebrand-buckets-across-templates-and-serializers-abcdef12.md"


def test_summary_path_prepends_commit_datetime_when_repo_resolves(tmp_path):
    """A real repo + sha yields a sortable YYYYMMDD-HHMMSS prefix."""
    repo = _make_repo(tmp_path / "r")
    (repo / "f.txt").write_text("x")
    env = {"GIT_AUTHOR_DATE": "2026-06-04T13:19:00", "GIT_COMMITTER_DATE": "2026-06-04T13:19:00"}
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "feat: add f"],
        check=True, env={**os.environ, **env},
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    compute = _summary_path_for(tmp_path / "recaps")
    p = compute({"sha": sha, "subject": "feat: add f", "repo": str(repo)})
    assert p == tmp_path / "recaps" / f"20260604-131900-feat-add-f-{sha[:8]}.md"


def test_summary_path_truncates_long_subject(tmp_path):
    compute = _summary_path_for(tmp_path / "recaps")
    long_subject = "this is a really long commit subject that goes on and on and on"
    p = compute({"sha": "1234567890", "subject": long_subject})
    assert p is not None
    # Slug part is capped; sha8 always appended.
    assert p.name.endswith("-12345678.md")
    assert len(p.stem.rsplit("-", 1)[0]) <= 60


def test_summary_path_falls_back_to_sha_when_subject_missing(tmp_path):
    compute = _summary_path_for(tmp_path / "recaps")
    p = compute({"sha": "abcdef1234567890", "repo": "/x"})
    assert p == tmp_path / "recaps" / "abcdef123456.md"


def test_summary_path_falls_back_to_sha_when_subject_is_unslugifiable(tmp_path):
    """A subject made entirely of punctuation slugifies to empty; fall back."""
    compute = _summary_path_for(tmp_path / "recaps")
    p = compute({"sha": "abcdef1234567890", "subject": "!!! @@@ ###"})
    assert p == tmp_path / "recaps" / "abcdef123456.md"


def test_summary_path_none_when_no_sha(tmp_path):
    compute = _summary_path_for(tmp_path / "recaps")
    assert compute({"repo": "/x"}) is None
    assert compute({"sha": "  "}) is None
    assert compute({"sha": "  ", "subject": "still no sha"}) is None


def test_slugify_handles_unicode_and_punctuation():
    from engines.git_recap.engine import _slugify
    assert _slugify("Fix: don't lose state on close ✨") == "fix-don-t-lose-state-on-close"
    assert _slugify("docs(adr-0007): add operator_actions ledger") == "docs-adr-0007-add-operator-actions-ledger"
    assert _slugify("---") == ""
    assert _slugify("") == ""


def test_engine_branch_policies_register_summarize_commit(tmp_path):
    engine = GitRecapEngine(output_dir=tmp_path / "recaps")
    assert "summarize-commit" in engine.branch_policies
    policy = engine.branch_policies["summarize-commit"]
    assert policy.intent == "produce"
    assert policy.output_path is not None
    assert policy.skip_when is not None


def test_skip_when_skips_if_recap_file_already_exists(tmp_path):
    """Regression 2026-06-04: every run re-summarised every commit even if
    a recap was already on disk. Now skip-with-reason if the file exists
    and has content."""
    output_dir = tmp_path / "recaps"
    output_dir.mkdir()
    engine = GitRecapEngine(output_dir=output_dir)
    policy = engine.branch_policies["summarize-commit"]

    params = {"sha": "abc12345", "subject": "feat: add x", "repo": "/r"}
    # Pre-create the recap.
    existing = output_dir / "feat-add-x-abc12345.md"
    existing.write_text("already summarised\n")

    reason = policy.skip_when(params)
    assert isinstance(reason, str)
    assert "feat-add-x-abc12345.md" in reason


def test_skip_when_does_not_skip_when_recap_absent(tmp_path):
    output_dir = tmp_path / "recaps"
    output_dir.mkdir()
    engine = GitRecapEngine(output_dir=output_dir)
    policy = engine.branch_policies["summarize-commit"]
    reason = policy.skip_when({"sha": "abc12345", "subject": "feat: add x"})
    assert reason is None


def test_skip_when_does_not_skip_when_recap_is_empty(tmp_path):
    """An empty file means the recipe started but didn't complete; we
    want to retry, not skip."""
    output_dir = tmp_path / "recaps"
    output_dir.mkdir()
    engine = GitRecapEngine(output_dir=output_dir)
    policy = engine.branch_policies["summarize-commit"]
    params = {"sha": "abc12345", "subject": "feat: add x"}
    (output_dir / "feat-add-x-abc12345.md").write_text("")
    assert policy.skip_when(params) is None


# ---- commit_log omits already-recapped commits -------------------
# So the review phase doesn't burn a pass re-judging commits the
# summarize-commit skip predicate would drop downstream anyway. The
# filter reuses that exact predicate, so it can never hide a commit
# that doesn't truly have a non-empty recap on disk.

def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", message],
        check=True,
    )


def test_commit_log_omits_already_recapped(tmp_path):
    from engines.git_recap.engine import _summary_path_for

    repo = _make_repo(tmp_path / "r")
    _commit(repo, "feat: add alpha")
    _commit(repo, "feat: add beta")
    output_dir = tmp_path / "recaps"
    output_dir.mkdir()
    env = GitRecapEnvironment(
        repos=[repo], window_days=7, author="test@example.com",
        output_dir=output_dir,
    )

    # Before any recap exists, the review sees both commits.
    log1 = env.commit_log()
    assert "feat: add alpha" in log1
    assert "feat: add beta" in log1
    assert "already recapped" not in log1

    # Recap exactly one, writing to the SAME path the skip predicate computes.
    rows, error = env._git_log_rows(repo)
    assert error is None
    sha_by_subject = {subject: sha for sha, _date, subject in rows}
    recap_path = _summary_path_for(output_dir)({
        "repo": str(repo),
        "sha": sha_by_subject["feat: add alpha"],
        "subject": "feat: add alpha",
    })
    recap_path.write_text("recap body\n")

    # Now the review only sees the un-recapped commit, and is told one was
    # omitted so an empty-looking repo block isn't mistaken for no activity.
    log2 = env.commit_log()
    assert "feat: add beta" in log2
    assert "feat: add alpha" not in log2
    assert "(1 already recapped, omitted)" in log2


def test_commit_log_empty_recap_does_not_omit(tmp_path):
    """An empty recap file (interrupted run) must NOT hide the commit —
    same contract as the skip predicate, which retries empty files."""
    from engines.git_recap.engine import _summary_path_for

    repo = _make_repo(tmp_path / "r")
    _commit(repo, "feat: add alpha")
    output_dir = tmp_path / "recaps"
    output_dir.mkdir()
    env = GitRecapEnvironment(
        repos=[repo], window_days=7, author="test@example.com",
        output_dir=output_dir,
    )
    rows, _ = env._git_log_rows(repo)
    sha = rows[0][0]
    _summary_path_for(output_dir)(
        {"repo": str(repo), "sha": sha, "subject": "feat: add alpha"}
    ).write_text("")  # empty -> incomplete

    log = env.commit_log()
    assert "feat: add alpha" in log
    assert "already recapped" not in log


def test_skip_when_handles_missing_sha_gracefully(tmp_path):
    """If sha is missing, the path can't be computed; skip_when returns
    None so the framework lets the recipe run (and fail with a clearer
    error from the recipe side)."""
    engine = GitRecapEngine(output_dir=tmp_path)
    policy = engine.branch_policies["summarize-commit"]
    assert policy.skip_when({"subject": "no sha here"}) is None


# ---- engine identity + recipes_dir -------------------------------

def test_engine_name():
    assert GitRecapEngine(output_dir=Path("/tmp")).name == "git-recap"


def test_recipes_dir_points_at_shipped_yamls(tmp_path):
    engine = GitRecapEngine(output_dir=tmp_path)
    recipes = Path(engine.recipes_dir())
    assert (recipes / "review.example.yaml").exists()
    assert (recipes / "summarize-commit.yaml").exists()
    assert (recipes / "summary.example.yaml").exists()
