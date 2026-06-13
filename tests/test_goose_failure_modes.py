"""Persistent-failure detection: shortcut the retry loop when nothing will help.

Regression 2026-06-04: owl-alpha + openrouter combination produced
"filtered for safety" / "Tool response was empty" / "tool output is being
processed by the underlying provider library" responses. The looper saw
those as ambiguous, the success_predicate (file_nonempty) saw missing
output, and we burned all six retries waiting for a fundamentally
incompatible model+provider combination to recover.

Persistent failures must short-circuit, not retry.
"""

from gooseloop.goose import (
    _first_recipe_error_line,
    _is_persistent_failure,
    _is_recipe_error,
    _is_transient_error,
)


# ---- the exact strings observed in the wild ----------------------

def test_openrouter_safety_filter_message_detected():
    output = (
        "> Tool output: The tool output is being processed by the underlying "
        "provider library used for code editing. The content has been filtered "
        "for safety purposes.\n"
    )
    assert _is_persistent_failure(output) is True


def test_tool_response_was_empty_detected():
    output = "> Tool response was empty\n"
    assert _is_persistent_failure(output) is True


def test_filtered_for_safety_anywhere_in_output_detected():
    output = "Some narration here.\nThe content has been filtered for safety purposes.\nMore.\n"
    assert _is_persistent_failure(output) is True


# ---- false positives -------------------------------------------

def test_normal_output_not_flagged():
    assert _is_persistent_failure("wrote greeting to /tmp/g.txt") is False


def test_actual_transient_error_not_flagged_persistent():
    """A real 502 isn't persistent; the existing retry loop should handle it.
    Our persistent-failure shortcut must not absorb actually-transient errors."""
    output = "Server error 502 from anthropic\n"
    assert _is_persistent_failure(output) is False
    assert _is_transient_error(output, returncode=0) is True


def test_rate_limit_not_flagged_persistent():
    output = "rate limit reached; back off\n"
    assert _is_persistent_failure(output) is False


# ---- stream decode errors (regression 2026-06-04) -----------------
# Long reviews truncated mid-emit with "Request failed: Stream decode
# error: error decoding response body." The old regex didn't catch
# it, so the retry loop accepted the truncated output as success and
# the downstream JSON parse failed. Should be a retry, not a fatal.

def test_stream_decode_error_at_line_start_is_transient():
    output = "Request failed: Stream decode error: error decoding response body.\n"
    assert _is_transient_error(output, returncode=0) is True


def test_stream_decode_error_with_goose_prefix_is_transient():
    """Real shape from 2026-06-04: goose wraps the error in its own
    'Ran into this error:' prefix, so the relevant substring is mid-line.
    Must still trigger a retry."""
    output = (
        "...some prior output...\n"
        "Ran into this error: Request failed: Stream decode error: "
        "error decoding response body.\n"
        "Please retry if you think this is a transient or recoverable error.\n"
    )
    assert _is_transient_error(output, returncode=0) is True


def test_error_decoding_response_body_anywhere_is_transient():
    output = "Some other prefix... error decoding response body\n"
    assert _is_transient_error(output, returncode=0) is True


def test_stream_decode_error_not_classified_persistent():
    output = "Request failed: Stream decode error: error decoding response body.\n"
    assert _is_persistent_failure(output) is False


# ---- recipe-parse errors are persistent (regression 2026-06-05) ---
# A context-injected recap broke the summary recipe's MiniJinja template,
# goose exited non-zero with "Invalid recipe: syntax error: ...", and the
# retry loop (returncode != 0 => transient) burned all six attempts re-
# parsing identical bytes. A recipe that won't parse won't parse on retry;
# it must fail fast so the operator sees the error immediately.

def test_recipe_syntax_error_end_of_raw_block_is_persistent():
    output = (
        "Error: Invalid recipe: syntax error: unexpected end of raw block "
        "(in recipe:615)\n"
    )
    assert _is_persistent_failure(output) is True
    assert _is_recipe_error(output) is True


def test_recipe_syntax_error_end_of_comment_is_persistent():
    output = (
        "Error: Invalid recipe: syntax error: unexpected end of comment "
        "(in recipe:485)\n"
    )
    assert _is_persistent_failure(output) is True
    assert _is_recipe_error(output) is True


def test_provider_failure_is_persistent_but_not_a_recipe_error():
    """Persistent, but not the operator's recipe to fix — keep the buckets
    distinct so the fail-fast message points at the right cause."""
    output = "The content has been filtered for safety purposes.\n"
    assert _is_persistent_failure(output) is True
    assert _is_recipe_error(output) is False


def test_normal_output_is_not_a_recipe_error():
    assert _is_recipe_error("wrote weekly recap to recaps/weekly/...\n") is False


def test_first_recipe_error_line_extracts_the_goose_line():
    output = (
        "some prior narration\n"
        "Error: Invalid recipe: syntax error: unexpected end of raw block "
        "(in recipe:615)\n"
        "trailing noise\n"
    )
    line = _first_recipe_error_line(output)
    assert line is not None
    assert "Invalid recipe: syntax error: unexpected end of raw block" in line
    assert line == line.strip()  # no leading/trailing whitespace


def test_first_recipe_error_line_none_when_absent():
    assert _first_recipe_error_line("all good here\n") is None


# ---- the asymmetry the looper relies on ---------------------------

def test_persistent_and_transient_are_disjoint():
    """A line that's persistent must not also register as transient,
    otherwise the looper would short-circuit and the diagnostic would
    be confusing. They share a code path; keep the buckets clean."""
    for output in (
        "The content has been filtered for safety purposes.",
        "Tool response was empty",
        "the tool output is being processed by the underlying provider library",
    ):
        assert _is_persistent_failure(output) is True
        assert _is_transient_error(output, returncode=0) is False
