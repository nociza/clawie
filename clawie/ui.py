from __future__ import annotations

from collections.abc import Iterable

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
WHITE = "\033[37m"

# Box-drawing characters
_H = "\u2500"  # ─
_V = "\u2502"  # │
_TL = "\u250c"  # ┌
_TR = "\u2510"  # ┐
_BL = "\u2514"  # └
_BR = "\u2518"  # ┘
_LT = "\u251c"  # ├
_RT = "\u2524"  # ┤
_XX = "\u253c"  # ┼


def color(text: str, ansi: str, bold: bool = False) -> str:
    prefix = ansi
    if bold:
        prefix = BOLD + ansi
    return f"{prefix}{text}{RESET}"


def print_success(message: str) -> None:
    print(color(f"  \u25cf {message}", GREEN, bold=True))


def print_warning(message: str) -> None:
    print(color(f"  \u25b2 {message}", YELLOW, bold=True))


def print_error(message: str) -> None:
    print(color(f"  \u2717 {message}", RED, bold=True))


def print_info(message: str) -> None:
    print(color(f"  {message}", CYAN))


def print_panel(title: str, lines: Iterable[str]) -> None:
    body = list(lines)
    inner_w = max([len(title)] + [len(line) for line in body]) + 2
    border = color(_V, BLUE)
    hbar = _H * inner_w
    print(color(f"  {_TL}{hbar}{_TR}", BLUE))
    padding = inner_w - len(title) - 1
    print(f"  {border} {color(title, CYAN, bold=True)}{' ' * padding}{border}")
    print(color(f"  {_LT}{hbar}{_RT}", BLUE))
    for line in body:
        padding = inner_w - len(line) - 1
        print(f"  {border} {line}{' ' * padding}{border}")
    print(color(f"  {_BL}{hbar}{_BR}", BLUE))


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not headers:
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, col in enumerate(row):
            if idx < len(widths):
                widths[idx] = max(widths[idx], len(col))

    def _fmt(values: list[str]) -> str:
        padded = [
            values[idx].ljust(widths[idx]) if idx < len(values) else " " * widths[idx]
            for idx in range(len(widths))
        ]
        sep = f" {_V} "
        return sep.join(padded)

    sep_line = f"{_H}{_XX}{_H}".join(_H * w for w in widths)
    print(f"  {color(_fmt(headers), CYAN, bold=True)}")
    print(f"  {color(sep_line, BLUE)}")
    for row in rows:
        print(f"  {_fmt(row)}")
