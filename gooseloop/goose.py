"""Goose CLI invocation: subprocess wrapper with retry and rate-limit handling.

Pure execution layer. Knows about goose, OpenRouter-style rate-limit messages,
and transient errors. Knows nothing about Phases, engines, or sessions.
"""

import os
import re
import subprocess
import sys
import time
from typing import Callable, Optional

from .footer import print_call_footer, recipe_label
from .text import Color, colored


# Per-minute rate-limit windows reset every 60s. 65s gives a 5s buffer so the
# next request lands clearly inside the new window.
RATE_LIMIT_WAIT_SECONDS = 65


_RATE_LIMIT_LINE_RE = re.compile(
    r"^[ \t]*(rate limit|429\b|too many requests)",
    re.MULTILINE | re.IGNORECASE,
)

_TRANSIENT_ERROR_LINE_RE = re.compile(
    # Line-start anchored. Keeps the model's mid-prose narration
    # ("I'll handle server errors here") from tripping the regex.
    r"^[ \t]*("
    r"server error\b"
    r"|provider returned error\b"
    r"|5\d{2}\s+(internal|service|gateway|bad|server|error)"
    r"|error[: ]+5\d{2}\b"
    r"|connection (refused|reset|timed? out)"
    r")",
    re.MULTILINE | re.IGNORECASE,
)

# Stream-level decode failures: goose's HTTP client choked mid-response
# (network blip, provider truncation, partial chunked stream). Real
# observed wording from 2026-06-04: "Ran into this error: Request
# failed: Stream decode error: error decoding response body." The
# prefix "Ran into this error:" means line-start anchoring won't catch
# it — these patterns are specific enough that mid-line search is safe
# (a model isn't going to monologue "stream decode error" by accident).
_STREAM_DECODE_RE = re.compile(
    r"("
    r"stream decode error"
    r"|error decoding response body"
    r"|request failed:.*decode"
    r")",
    re.IGNORECASE,
)

# Persistent failure modes — retrying won't help. Match these before
# the transient check so we fail-fast instead of burning max_retries
# waiting for a provider/model combination that fundamentally can't
# complete the task.
#
# - "filtered for safety": openrouter's safety filter is stripping
#   tool-call payloads (seen with owl-alpha/LongCat).
# - "tool response was empty": goose received nothing parseable back.
# - "tool output is being processed by the underlying provider library":
#   provider-side post-processing swallowed the response.
# - "Invalid recipe": goose couldn't parse the recipe at all (e.g. a
#   MiniJinja template syntax error). The recipe bytes are fixed for the
#   run, so every retry re-parses the same file and fails identically —
#   retrying is pure waste, and the operator needs to see the error now,
#   not after 6 backoffs. (2026-06-05: a context-injected recap broke the
#   summary recipe's template and burned the full retry budget.)
_PERSISTENT_FAILURE_RE = re.compile(
    r"("
    r"filtered for safety"
    r"|tool response was empty"
    r"|tool output is being processed by the underlying provider library"
    r"|Invalid recipe"
    r")",
    re.IGNORECASE,
)


def _is_rate_limit(output: str) -> bool:
    return bool(_RATE_LIMIT_LINE_RE.search(output))


def _is_persistent_failure(output: str) -> bool:
    """Failures that won't get better with retry. Caller should fail fast."""
    return bool(_PERSISTENT_FAILURE_RE.search(output))


def _is_recipe_error(output: str) -> bool:
    """A recipe-parse failure specifically (vs a provider/model failure).

    Deterministic in the recipe bytes, so it's both persistent AND the
    operator's to fix — worth a distinct, actionable message."""
    return "invalid recipe" in output.lower()


def _first_recipe_error_line(output: str) -> str | None:
    """The goose line carrying the recipe-parse error, for the fail-fast
    message — so the operator sees `Invalid recipe: syntax error: ...`
    instead of having to scroll back through the streamed output."""
    for line in output.splitlines():
        if "invalid recipe" in line.lower():
            return line.strip()
    return None


def _is_transient_error(output: str, returncode: int) -> bool:
    if returncode != 0:
        return True
    if _TRANSIENT_ERROR_LINE_RE.search(output):
        return True
    if _STREAM_DECODE_RE.search(output):
        return True
    if _is_rate_limit(output):
        return True
    return False


def _countdown_sleep(seconds: int, header: str, color: str | None = None) -> None:
    color = color or Color.YELLOW
    if not sys.stdout.isatty():
        print(colored(f"  {header} — waiting {seconds}s", color))
        time.sleep(seconds)
        return

    width = max(50, len(header) + 6)
    top    = "┌" + "─" * (width - 2) + "┐"
    middle = "│  " + header.ljust(width - 4) + "│"
    bottom = "└" + "─" * (width - 2) + "┘"
    print(colored(top, color))
    print(colored(middle, color))
    print(colored(bottom, color))

    bar_width = 30
    line_pad = bar_width + 40

    try:
        for elapsed in range(seconds):
            remaining = seconds - elapsed
            filled = int(elapsed / seconds * bar_width)
            bar = "▰" * filled + "▱" * (bar_width - filled)
            line = f"  retrying in {remaining:>3}s  {bar}"
            sys.stdout.write(f"\r{colored(line, color)}")
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        sys.stdout.write("\r" + " " * line_pad + "\r")
        sys.stdout.flush()
        raise

    sys.stdout.write("\r" + " " * line_pad + "\r")
    sys.stdout.flush()
    print(colored("  retrying now...", Color.GREEN))


def _run_goose_internal(recipe_path: str, model: str,
                        extra_env: dict[str, str] | None = None) -> tuple[str, int]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    cmd = ["goose", "run", "--recipe", recipe_path, "--model", model]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )
    lines = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    proc.wait()
    return "".join(lines), proc.returncode


def count_shell_calls(output: str) -> int:
    """Goose marks each shell tool call with '▸ shell' in its streamed output."""
    return output.count("▸ shell")


def run_goose_with_retry(
    recipe_path: str,
    model: str,
    extra_env: dict[str, str] | None = None,
    *,
    max_retries: int = 6,
    base_delay: int = 5,
    success_predicate: Optional[Callable[[str], bool]] = None,
    label: str | None = None,
) -> str:
    """Run goose with automatic retry on transient errors.

    `recipe_path` is the effective recipe file to run — callers that
    layer overlays or render context: blocks prepare it first (see
    context_prepend.prepared_recipe) and pass `label` so footers and
    errors name the real recipe, not the rendered temp file.

    Rate-limit errors wait RATE_LIMIT_WAIT_SECONDS (65s). Other transient
    errors use base_delay * (attempt+1) backoff. `success_predicate(output)`
    lets stdout-deliverable recipes keep usable output when a trailing
    rate-limit message follows the real result.

    Raises RuntimeError if all retries exhausted.
    """
    start = time.perf_counter()
    retries_used = 0
    final_output: str | None = None

    for attempt in range(max_retries):
        output, returncode = _run_goose_internal(recipe_path, model, extra_env)

        # Persistent failure shortcuts the retry loop: a provider that
        # filters tool calls or a model that can't speak goose's tool
        # protocol will not improve with another attempt. Fail fast
        # rather than burn max_retries on something structurally broken.
        if _is_persistent_failure(output):
            if _is_recipe_error(output):
                reason = (
                    "Recipe failed to parse; not retrying (the recipe bytes "
                    "are identical every attempt). Fix the recipe/template:"
                )
            else:
                reason = (
                    "Persistent provider/model failure detected; not retrying "
                    "(recipe + model + provider combination appears incompatible)."
                )
            print(colored(reason, Color.RED), file=sys.stderr)
            detail = _first_recipe_error_line(output)
            if detail:
                print(colored(f"  {detail}", Color.RED), file=sys.stderr)
            break

        if success_predicate is not None:
            success = success_predicate(output)
        else:
            success = not _is_transient_error(output, returncode)

        if success:
            final_output = output
            break

        retries_used += 1
        if _is_rate_limit(output):
            delay = RATE_LIMIT_WAIT_SECONDS
            header = f"Rate limit hit  ·  attempt {attempt + 1}/{max_retries}"
            color = Color.YELLOW
        else:
            delay = base_delay * (attempt + 1)
            header = f"Transient error  ·  attempt {attempt + 1}/{max_retries}"
            color = Color.MAGENTA
        _countdown_sleep(delay, header, color=color)

    if final_output is None:
        elapsed = time.perf_counter() - start
        print_call_footer(
            label or recipe_label(recipe_path),
            elapsed=elapsed, shell_calls=0,
            retries=retries_used, status="failed",
        )
        attempts_desc = (
            "without retrying (persistent failure)" if retries_used == 0
            else f"after {retries_used} retr{'y' if retries_used == 1 else 'ies'}"
        )
        raise RuntimeError(
            f"goose failed {attempts_desc}: {label or recipe_path}"
        )

    elapsed = time.perf_counter() - start
    print_call_footer(
        label or recipe_label(recipe_path),
        elapsed=elapsed,
        shell_calls=count_shell_calls(final_output),
        retries=retries_used,
        status="ok",
    )
    return final_output
