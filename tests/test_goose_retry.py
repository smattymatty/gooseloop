"""Retry-loop semantics of run_goose_with_retry.

test_goose_failure_modes.py pins the failure *classifiers* (the regexes);
these tests pin the decision engine that consumes them: which outputs
retry, which fail fast, which delays apply, and what the exhaustion
error names. goose itself is never invoked — _run_goose_internal and
_countdown_sleep are monkeypatched.
"""

from __future__ import annotations

import pytest

from gooseloop.goose import RATE_LIMIT_WAIT_SECONDS, run_goose_with_retry


class _FakeGoose:
    """Feed scripted (output, returncode) pairs; record every call.

    The last pair repeats if the loop asks for more attempts than
    scripted.
    """

    def __init__(self, *results: tuple[str, int]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, recipe_path: str, model: str, extra_env=None) -> tuple[str, int]:
        self.calls.append(recipe_path)
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[idx]


@pytest.fixture
def sleeps(monkeypatch):
    """Disable the countdown UI; record (seconds, header) per retry wait."""
    recorded: list[tuple[int, str]] = []

    def fake_sleep(seconds, header, color=None):
        recorded.append((seconds, header))

    monkeypatch.setattr("gooseloop.goose._countdown_sleep", fake_sleep)
    return recorded


def _patch_goose(monkeypatch, fake: _FakeGoose) -> None:
    monkeypatch.setattr("gooseloop.goose._run_goose_internal", fake)


def test_success_first_attempt_returns_output(monkeypatch, sleeps):
    fake = _FakeGoose(("all done", 0))
    _patch_goose(monkeypatch, fake)
    out = run_goose_with_retry("r.yaml", "model")
    assert out == "all done"
    assert len(fake.calls) == 1
    assert sleeps == []


def test_transient_error_retries_with_linear_backoff(monkeypatch, sleeps):
    fake = _FakeGoose(("server error: upstream hiccup", 0), ("recovered", 0))
    _patch_goose(monkeypatch, fake)
    out = run_goose_with_retry("r.yaml", "model", base_delay=7)
    assert out == "recovered"
    assert len(fake.calls) == 2
    # First retry waits base_delay * 1; header names the transient path.
    assert sleeps == [(7, "Transient error  ·  attempt 1/6")]


def test_nonzero_returncode_is_transient(monkeypatch, sleeps):
    fake = _FakeGoose(("looks fine but goose died", 3), ("ok", 0))
    _patch_goose(monkeypatch, fake)
    assert run_goose_with_retry("r.yaml", "model") == "ok"
    assert len(fake.calls) == 2


def test_rate_limit_waits_the_rate_limit_window(monkeypatch, sleeps):
    fake = _FakeGoose(("rate limit exceeded, slow down", 0), ("ok", 0))
    _patch_goose(monkeypatch, fake)
    assert run_goose_with_retry("r.yaml", "model", base_delay=7) == "ok"
    assert len(sleeps) == 1
    seconds, header = sleeps[0]
    assert seconds == RATE_LIMIT_WAIT_SECONDS
    assert "Rate limit" in header


def test_persistent_failure_fails_fast_without_retry(monkeypatch, sleeps):
    fake = _FakeGoose(("response was filtered for safety", 0))
    _patch_goose(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="without retrying"):
        run_goose_with_retry("r.yaml", "model")
    assert len(fake.calls) == 1
    assert sleeps == []


def test_recipe_error_fails_fast_and_surfaces_the_goose_line(monkeypatch, sleeps, capsys):
    fake = _FakeGoose(
        ("Invalid recipe: syntax error: unexpected end of comment (in prompt)", 0),
    )
    _patch_goose(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="without retrying"):
        run_goose_with_retry("r.yaml", "model")
    err = capsys.readouterr().err
    assert "Recipe failed to parse" in err
    assert "unexpected end of comment" in err
    assert len(fake.calls) == 1


def test_success_predicate_overrides_transient_check(monkeypatch, sleeps):
    # Nonzero returncode would normally be transient; an explicit
    # predicate that accepts the output wins (stdout-deliverable recipes
    # keep usable output when a trailing error follows the real result).
    fake = _FakeGoose(("the deliverable", 1))
    _patch_goose(monkeypatch, fake)
    out = run_goose_with_retry(
        "r.yaml", "model", success_predicate=lambda o: "deliverable" in o,
    )
    assert out == "the deliverable"
    assert len(fake.calls) == 1


def test_success_predicate_rejection_exhausts_and_raises(monkeypatch, sleeps):
    # Clean output + clean exit, but the predicate refuses every attempt
    # (e.g. the review's JSON guard on truncated output).
    fake = _FakeGoose(("not the json you wanted", 0))
    _patch_goose(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="after 2 retries"):
        run_goose_with_retry(
            "r.yaml", "model", max_retries=2, success_predicate=lambda o: False,
        )
    assert len(fake.calls) == 2


def test_exhaustion_error_names_the_label_not_the_temp_path(monkeypatch, sleeps):
    # The looper passes rendered temp paths; label carries the real
    # recipe name so operators aren't shown tmpXXXX.rendered.yaml.
    fake = _FakeGoose(("server error: boom", 0))
    _patch_goose(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="review") as exc_info:
        run_goose_with_retry(
            "/tmp/tmpabc123.rendered.yaml", "model",
            max_retries=1, label="review",
        )
    assert "tmpabc123" not in str(exc_info.value)
