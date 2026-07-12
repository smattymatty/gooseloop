"""Footers: per-call summary line and per-session multi-line wrap-up."""

from pathlib import Path
from typing import Any

from .text import Color, colored


def _fmt_duration(seconds: float) -> str:
    """e.g. 47s, 1m 23s, 12m 5s."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def print_call_footer(label: str, elapsed: float, shell_calls: int,
                      retries: int, status: str = "ok") -> None:
    """One-line footer after a single goose call."""
    parts = [
        label,
        _fmt_duration(elapsed),
        f"{shell_calls} shell",
    ]
    if retries:
        parts.append(f"{retries} retr{'y' if retries == 1 else 'ies'}")
    parts.append(status)
    line = "  ─  " + "  ·  ".join(parts) + "  ─"

    color = Color.GREEN if status in ("ok", "skipped") else Color.RED
    print(colored(line, color))


def print_session_footer(elapsed: float,
                         goose_calls: int,
                         actions_planned: int,
                         actions_ran: int,
                         actions_skipped: int,
                         outputs: list[str],
                         operator_actions: list[dict[str, Any]] | None = None,
                         session_dir: Path | None = None) -> None:
    """Multi-line summary at end of a begin_loop() pass."""
    title = f"═══ session complete · {_fmt_duration(elapsed)} ═══"
    print()
    print(colored(title, Color.CYAN))

    rows: list[tuple[str, str]] = [
        ("goose calls", str(goose_calls)),
        ("actions",     f"{actions_planned} planned  ·  {actions_ran} ran  ·  {actions_skipped} skipped"),
    ]
    label_width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(colored(f"  {k:<{label_width}}   {v}", Color.CYAN))

    if outputs or session_dir:
        print(colored(f"  {'outputs':<{label_width}}", Color.CYAN))
        if session_dir:
            print(colored(f"  {' ' * label_width}   {session_dir}/", Color.CYAN))
        for out in outputs:
            print(colored(f"  {' ' * label_width}   {out}", Color.CYAN))

    if operator_actions:
        print(colored(f"  {'operator':<{label_width}}", Color.YELLOW))
        for entry in operator_actions:
            action = entry.get("action", "(no action)")
            why = entry.get("why", "")
            print(colored(f"  {' ' * label_width}   - {action}", Color.YELLOW))
            if why:
                print(colored(f"  {' ' * label_width}     why: {why}", Color.YELLOW))
    print()


def recipe_label(recipe_path: str, suffix: str | None = None) -> str:
    """Compact label for a recipe path. 'recipes/to-outreach.yaml' -> 'to-outreach'."""
    name = Path(recipe_path).stem
    return f"{name}[{suffix}]" if suffix else name
