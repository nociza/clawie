from __future__ import annotations

from collections.abc import Iterable

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def color(text: str, ansi: str, bold: bool = False) -> str:
    prefix = ansi
    if bold:
        prefix = BOLD + ansi
    return f"{prefix}{text}{RESET}"


def print_success(message: str) -> None:
    print(color(f"OK: {message}", GREEN, bold=True))


def print_warning(message: str) -> None:
    print(color(f"WARN: {message}", YELLOW, bold=True))


def print_error(message: str) -> None:
    print(color(f"ERROR: {message}", RED, bold=True))


def print_info(message: str) -> None:
    print(color(message, CYAN))


def print_panel(title: str, lines: Iterable[str]) -> None:
    body = list(lines)
    width = max([len(title)] + [len(line) for line in body]) + 4
    print("+" + "-" * width + "+")
    print(f"| {title:<{width - 2}}|")
    print("+" + "-" * width + "+")
    for line in body:
        print(f"| {line:<{width - 2}}|")
    print("+" + "-" * width + "+")


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not headers:
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))

    def _fmt(values: list[str]) -> str:
        padded = [values[idx].ljust(widths[idx]) for idx in range(len(widths))]
        return " | ".join(padded)

    print(_fmt(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(_fmt(row))
