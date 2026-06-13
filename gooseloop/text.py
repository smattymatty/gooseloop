"""Terminal text helpers: ANSI colours, banners, ANSI stripping.

JSON extraction used to live here; it moved to gooseloop/extract.py once
it grew a recognizer dispatch table. text.py is now scoped strictly to
terminal-output utilities.
"""

import re


class Color:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN    = '\033[96m'
    WHITE   = '\033[97m'


def colored(text: str, color: str) -> str:
    return f"{color}{text}{Color.RESET}"


_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def banner(text: str, color: str = Color.CYAN, width: int = 70) -> None:
    """Print a colourful double-line box around the given text."""
    top = "╔" + "═" * (width - 2) + "╗"
    bot = "╚" + "═" * (width - 2) + "╝"

    max_text_width = width - 4
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        if len(current) + len(w) + 1 > max_text_width:
            lines.append(current.strip())
            current = w
        else:
            current = (current + " " + w).strip() if current else w
    if current:
        lines.append(current.strip())
    if not lines:
        lines = [""]

    print(colored(top, color))
    for line in lines:
        padding = max_text_width - len(line)
        left = padding // 2
        right = padding - left
        print(colored("║ " + " " * left + line + " " * right + " ║", color))
    print(colored(bot, color))
