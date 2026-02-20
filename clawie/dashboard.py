from __future__ import annotations

import curses
import sys
import time
from typing import Any

from clawie.ui import print_info, print_table


def run_dashboard(
    service: Any,
    user_id: str | None = None,
    refresh_seconds: int = 2,
) -> None:
    if not sys.stdout.isatty():
        _print_static(service.performance_snapshot(user_id=user_id, refresh=True))
        return

    try:
        curses.wrapper(_loop, service, user_id, refresh_seconds)
    except curses.error:
        _print_static(service.performance_snapshot(user_id=user_id, refresh=True))


def _loop(stdscr: Any, service: Any, user_id: str | None, refresh_seconds: int) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(refresh_seconds * 1000)

    while True:
        snapshot = service.performance_snapshot(user_id=user_id, refresh=True)
        _draw(stdscr, snapshot, user_id)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return
        if key in (ord("r"), ord("R")):
            continue
        time.sleep(0.05)


def _draw(stdscr: Any, snapshot: dict[str, Any], user_id: str | None) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    line = 0

    header = (
        f"Clawie Monitor | provider={snapshot.get('provider', '')} workspace={snapshot['workspace']} "
        f"| generated={snapshot['generated_at']}"
    )
    stdscr.addstr(line, 0, _fit(header, width))
    line += 1

    totals = snapshot["totals"]
    summary = (
        f"users={totals['users']} channels={totals['channels']} "
        f"migrated={totals['migrated_channels']} cpu={totals.get('cpu_percent', 0)}% "
        f"mem={totals.get('mem_percent', 0)}% filter={user_id or 'all'}"
    )
    stdscr.addstr(line, 0, _fit(summary, width))
    line += 2

    columns = [
        ("USER", 14),
        ("DISPLAY", 18),
        ("STATUS", 8),
        ("PID", 7),
        ("CPU%", 6),
        ("MEM%", 6),
        ("RSSKB", 8),
        ("VER", 8),
        ("STRATEGY", 10),
        ("CH", 4),
        ("MIG", 5),
        ("LAST_SYNC", 20),
    ]
    stdscr.addstr(line, 0, _fit(_row([name for name, _ in columns], columns), width))
    line += 1
    stdscr.addstr(line, 0, _fit("-" * (sum(size for _, size in columns) + len(columns) - 1), width))
    line += 1

    for row in snapshot["rows"]:
        if line >= height - 6:
            break
        text = _row(
            [
                row.get("user_id", ""),
                row.get("display_name", ""),
                row.get("status", ""),
                str(row.get("pid", 0)),
                f"{float(row.get('cpu_percent', 0.0)):.1f}",
                f"{float(row.get('mem_percent', 0.0)):.1f}",
                str(row.get("rss_kb", 0)),
                row.get("version", ""),
                row.get("strategy", ""),
                str(row.get("channels", 0)),
                str(row.get("migrated", 0)),
                row.get("last_sync", ""),
            ],
            columns,
        )
        stdscr.addstr(line, 0, _fit(text, width))
        line += 1

    line += 1
    if line < height - 2:
        stdscr.addstr(line, 0, _fit("Recent Events:", width))
        line += 1

    for event in snapshot.get("events", []):
        if line >= height - 2:
            break
        event_line = f"{event.get('timestamp', '')} | {event.get('type', '')} | {event.get('message', '')}"
        stdscr.addstr(line, 0, _fit(event_line, width))
        line += 1

    if height >= 1:
        instruction = "Press q to quit, r to refresh"
        stdscr.addstr(height - 1, 0, _fit(instruction, width))

    stdscr.refresh()


def _row(values: list[str], columns: list[tuple[str, int]]) -> str:
    padded = []
    for idx, value in enumerate(values):
        size = columns[idx][1]
        padded.append(str(value)[:size].ljust(size))
    return " ".join(padded)


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width - 1:
        return text
    return text[: max(0, width - 4)] + "..."


def _print_static(snapshot: dict[str, Any]) -> None:
    totals = snapshot["totals"]
    print_info(
        "Clawie Monitor "
        f"provider={snapshot.get('provider', '')} workspace={snapshot['workspace']} users={totals['users']} "
        f"channels={totals['channels']} migrated={totals['migrated_channels']} "
        f"cpu={totals.get('cpu_percent', 0)}% mem={totals.get('mem_percent', 0)}%"
    )

    rows = []
    for row in snapshot["rows"]:
        rows.append(
            [
                row.get("user_id", ""),
                row.get("display_name", ""),
                row.get("status", ""),
                str(row.get("pid", 0)),
                f"{float(row.get('cpu_percent', 0.0)):.1f}",
                f"{float(row.get('mem_percent', 0.0)):.1f}",
                str(row.get("rss_kb", 0)),
                row.get("strategy", ""),
                str(row.get("channels", 0)),
                str(row.get("migrated", 0)),
            ]
        )

    if rows:
        print_table(
            ["user", "display", "status", "pid", "cpu%", "mem%", "rss_kb", "strategy", "channels", "migrated"],
            rows,
        )

    events = snapshot.get("events", [])
    if events:
        print("\nRecent events:")
        for event in events:
            print(
                f"- {event.get('timestamp', '')} {event.get('type', '')} {event.get('message', '')}"
            )
