from __future__ import annotations

import curses
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clawie.ui import print_info, print_table

# ── Box Drawing ──────────────────────────────────────────────────────
H = "\u2500"  # ─
V = "\u2502"  # │
TL = "\u250c"  # ┌
TR = "\u2510"  # ┐
BL = "\u2514"  # └
BR = "\u2518"  # ┘
LT = "\u251c"  # ├
RT = "\u2524"  # ┤
TT = "\u252c"  # ┬
BT = "\u2534"  # ┴
XX = "\u253c"  # ┼

# ── Icons ────────────────────────────────────────────────────────────
ICON_RUN = "\u25cf"  # ●
ICON_STOP = "\u25cb"  # ○
ICON_ON = "\u25c9"  # ◉
ICON_OFF = "\u25cb"  # ○
ICON_PTR = "\u25b8"  # ▸
ICON_DOT = "\u00b7"  # ·
ICON_BRAND = "\u25c8"  # ◈
ICON_BACK = "\u25c2"  # ◂
ICON_WARN = "\u25b2"  # ▲
BAR_FILL = "\u25b0"  # ▰
BAR_EMPTY = "\u25b1"  # ▱

# ── Color Pair IDs ───────────────────────────────────────────────────
C_DEFAULT = 0
C_TITLE = 1  # Cyan – branding, titles, info
C_ERROR = 2  # Red – errors, critical
C_HELP = 3  # Yellow – footer help, legend
C_BORDER = 4  # Blue – borders, structural
C_SELECT = 5  # Black on Cyan – selection highlight
C_OK = 6  # Green – running, success, enabled
C_HEAD = 7  # White bold – section headers
C_ACCENT = 8  # Magenta – badges, accents
C_NOTICE_OK = 9  # Black on Green – success notice bar
C_NOTICE_ERR = 10  # White on Red – error notice bar

# ── Status Abbreviations ────────────────────────────────────────────
_STATUS_SHORT: dict[str, str] = {
    "running": "run",
    "stopped": "stop",
    "starting": "init",
    "active": "run",
    "inactive": "stop",
    "dead": "dead",
}


@dataclass
class DashboardState:
    view: str = "overview"
    overview_mode: str = "agents"
    overview_focus_idx: int = 0
    selected_row: int = 0
    selected_available_row: int = 0
    selected_assigned_row: int = 0
    selected_target_row: int = 0
    selected_agent_id: str = ""
    focus_idx: int = 0
    channel_idx: int = 0
    plugin_idx: int = 0
    prompt_idx: int = 0
    setting_idx: int = 0
    purge_confirm: bool = False
    notice: str = ""
    notice_error: bool = False


# ═════════════════════════════════════════════════════════════════════
#  Entry Points
# ═════════════════════════════════════════════════════════════════════


def run_dashboard(
    service: Any,
    agent_id: str | None = None,
    refresh_seconds: int = 2,
) -> None:
    if not sys.stdout.isatty():
        _print_static(service.performance_snapshot(agent_id=agent_id, refresh=True))
        return

    try:
        curses.wrapper(_loop, service, agent_id, refresh_seconds)
    except curses.error:
        _print_static(service.performance_snapshot(agent_id=agent_id, refresh=True))


def _loop(stdscr: Any, service: Any, agent_id: str | None, refresh_seconds: int) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(max(1, int(refresh_seconds)) * 1000)
    _init_colors()

    state = DashboardState()
    if agent_id:
        state.selected_agent_id = agent_id
        state.view = "detail"

    while True:
        snapshot = service.performance_snapshot(
            agent_id=None if state.view == "detail" else agent_id,
            refresh=True,
        )
        _sync_selection(state, snapshot)
        _draw(stdscr, snapshot, state, service)

        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), ord("Q")):
            return
        if key == 27 and state.view == "overview" and state.overview_mode == "agents" and not state.purge_confirm:
            return
        if key in (ord("r"), ord("R")):
            continue

        if state.view == "overview":
            _handle_overview_key(key, state, snapshot, service)
        else:
            _handle_detail_key(key, state, service)


# ═════════════════════════════════════════════════════════════════════
#  Key Handlers
# ═════════════════════════════════════════════════════════════════════


def _handle_overview_key(key: int, state: DashboardState, snapshot: dict[str, Any], service: Any) -> None:
    # ── Purge confirmation intercept ─────────────────────────────────
    if state.purge_confirm:
        if key in (ord("y"), ord("Y")):
            try:
                service.purge_agent(state.selected_agent_id)
                state.notice = f"purged {_display_agent_id(state.selected_agent_id)}"
                state.notice_error = False
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
            state.purge_confirm = False
            state.selected_agent_id = ""
            return
        if key in (ord("n"), ord("N"), 27):
            state.purge_confirm = False
            return
        return  # swallow all other keys during confirm

    if key in (ord("v"), ord("V")):
        state.notice = ""
        state.notice_error = False
        if state.overview_mode == "agents":
            state.overview_mode = "channels"
            state.overview_focus_idx = 0
        else:
            state.overview_mode = "agents"
            state.overview_focus_idx = 0
        return

    if state.overview_mode == "channels":
        _handle_channels_overview_key(key, state, snapshot, service)
        return

    rows = snapshot.get("rows", [])
    if key in (curses.KEY_DOWN, ord("j")):
        if rows:
            state.selected_row = min(len(rows) - 1, state.selected_row + 1)
            state.selected_agent_id = str(rows[state.selected_row].get("agent_id", ""))
    elif key in (curses.KEY_UP, ord("k")):
        if rows:
            state.selected_row = max(0, state.selected_row - 1)
            state.selected_agent_id = str(rows[state.selected_row].get("agent_id", ""))
    elif key in (ord("g"), curses.KEY_HOME):
        if rows:
            state.selected_row = 0
            state.selected_agent_id = str(rows[0].get("agent_id", ""))
    elif key in (ord("G"), curses.KEY_END):
        if rows:
            state.selected_row = len(rows) - 1
            state.selected_agent_id = str(rows[-1].get("agent_id", ""))
    elif key in (curses.KEY_ENTER, 10, 13):
        if rows:
            state.notice = ""
            state.notice_error = False
            state.view = "detail"
    elif key in (ord("d"), ord("D"), curses.KEY_DC):
        if rows and state.selected_agent_id:
            state.purge_confirm = True


def _handle_channels_overview_key(
    key: int,
    state: DashboardState,
    snapshot: dict[str, Any],
    service: Any,
) -> None:
    inventory = service.channel_inventory()
    agent_rows = [row for row in snapshot.get("rows", []) if not str(row.get("agent_id", "")).startswith("@local:")]
    state.selected_target_row = min(state.selected_target_row, max(0, len(agent_rows) - 1))
    if not agent_rows:
        return
    target_agent_id = str(agent_rows[state.selected_target_row].get("agent_id", ""))
    rows = list(inventory.get("rows", []))
    available = [row for row in rows if str(row.get("source", "")) in {"local", "pool"}]
    assigned = [row for row in rows if str(row.get("owner_agent_id", "")).strip() == target_agent_id]
    state.selected_available_row = min(state.selected_available_row, max(0, len(available) - 1))
    state.selected_assigned_row = min(state.selected_assigned_row, max(0, len(assigned) - 1))

    if key == 27:  # ESC – back to agents overview
        state.overview_mode = "agents"
        state.overview_focus_idx = 0
        state.notice = ""
        state.notice_error = False
        return

    if key == 9:  # TAB
        state.overview_focus_idx = (state.overview_focus_idx + 1) % 3
        return
    if key == curses.KEY_BTAB:  # Shift-Tab
        state.overview_focus_idx = (state.overview_focus_idx - 1) % 3
        return
    if key == curses.KEY_RIGHT:
        state.overview_focus_idx = min(2, state.overview_focus_idx + 1)
        return
    if key == curses.KEY_LEFT:
        state.overview_focus_idx = max(0, state.overview_focus_idx - 1)
        return

    if key in (curses.KEY_DOWN, ord("j")):
        if state.overview_focus_idx == 0 and agent_rows:
            state.selected_target_row = min(len(agent_rows) - 1, state.selected_target_row + 1)
        elif state.overview_focus_idx == 1 and available:
            state.selected_available_row = min(len(available) - 1, state.selected_available_row + 1)
        elif state.overview_focus_idx == 2 and assigned:
            state.selected_assigned_row = min(len(assigned) - 1, state.selected_assigned_row + 1)
        return
    if key in (curses.KEY_UP, ord("k")):
        if state.overview_focus_idx == 0 and agent_rows:
            state.selected_target_row = max(0, state.selected_target_row - 1)
        elif state.overview_focus_idx == 1 and available:
            state.selected_available_row = max(0, state.selected_available_row - 1)
        elif state.overview_focus_idx == 2 and assigned:
            state.selected_assigned_row = max(0, state.selected_assigned_row - 1)
        return
    if key in (ord("g"), curses.KEY_HOME):
        if state.overview_focus_idx == 0:
            state.selected_target_row = 0
        elif state.overview_focus_idx == 1:
            state.selected_available_row = 0
        else:
            state.selected_assigned_row = 0
        return
    if key in (ord("G"), curses.KEY_END):
        if state.overview_focus_idx == 0:
            state.selected_target_row = max(0, len(agent_rows) - 1)
        elif state.overview_focus_idx == 1:
            state.selected_available_row = max(0, len(available) - 1)
        else:
            state.selected_assigned_row = max(0, len(assigned) - 1)
        return

    selected_available = available[state.selected_available_row] if available else {}
    selected_assigned = assigned[state.selected_assigned_row] if assigned else {}
    source_agent_id = str(selected_available.get("owner_agent_id", ""))
    available_kind = str(selected_available.get("kind", ""))
    available_name = str(selected_available.get("name", ""))
    assigned_kind = str(selected_assigned.get("kind", ""))
    assigned_name = str(selected_assigned.get("name", ""))

    if key in (ord("a"), ord("A")):
        if not available_kind or not available_name:
            return
        try:
            service.assign_channel_to_agent(source_agent_id, available_kind, available_name, target_agent_id)
            state.notice = f"assigned {available_kind}:{available_name} -> {target_agent_id}"
            state.notice_error = False
        except Exception as exc:  # noqa: BLE001
            state.notice = str(exc)
            state.notice_error = True
        return
    if key in (ord("c"), ord("C")):
        kind = ""
        name = ""
        try:
            if state.overview_focus_idx == 1:
                kind, name = available_kind, available_name
                if not kind or not name:
                    return
                service.assign_channel_to_agent(source_agent_id, kind, name, target_agent_id)
            else:
                kind, name = assigned_kind, assigned_name
                if not kind or not name:
                    return
            service.connect_agent_channel(target_agent_id, kind, name)
            state.notice = f"connected {kind}:{name} for {target_agent_id}"
            state.notice_error = False
        except Exception as exc:  # noqa: BLE001
            state.notice = str(exc)
            state.notice_error = True
        return
    if key in (ord("u"), ord("U")):
        if not assigned_kind or not assigned_name:
            return
        try:
            service.unassign_channel_from_agent(target_agent_id, assigned_kind, assigned_name)
            state.notice = f"unassigned {assigned_kind}:{assigned_name}"
            state.notice_error = False
        except Exception as exc:  # noqa: BLE001
            state.notice = str(exc)
            state.notice_error = True
        return
    if key in (curses.KEY_ENTER, 10, 13):
        state.notice = ""
        state.notice_error = False
        state.selected_agent_id = target_agent_id
        state.view = "detail"


def _handle_detail_key(key: int, state: DashboardState, service: Any) -> None:
    if state.purge_confirm:
        if key in (ord("y"), ord("Y")):
            try:
                service.purge_agent(state.selected_agent_id)
            except Exception:  # noqa: BLE001
                state.purge_confirm = False
                return
            state.purge_confirm = False
            state.view = "overview"
            state.selected_agent_id = ""
            return
        if key in (ord("n"), ord("N"), 27):
            state.purge_confirm = False
            return
        return

    if key in (27, ord("b"), ord("B")):  # ESC/back
        state.view = "overview"
        state.notice = ""
        state.notice_error = False
        return

    focus_names = ["channels", "plugins", "prompts", "settings"]
    state.focus_idx = min(state.focus_idx, len(focus_names) - 1)
    focus = focus_names[state.focus_idx]

    try:
        agent = service.get_dashboard_agent(state.selected_agent_id)
    except Exception:  # noqa: BLE001
        return

    channels = agent.get("channels", [])
    plugins = sorted(agent.get("agent", {}).get("plugins", {}).items())
    prompts = _prompt_items(agent)
    state.channel_idx = min(state.channel_idx, max(0, len(channels) - 1))
    state.plugin_idx = min(state.plugin_idx, max(0, len(plugins) - 1))
    state.prompt_idx = min(state.prompt_idx, max(0, len(prompts) - 1))
    settings = _settings_items(agent)
    state.setting_idx = min(state.setting_idx, max(0, len(settings) - 1))

    if key == 9:  # TAB
        state.focus_idx = (state.focus_idx + 1) % len(focus_names)
        return
    if key == curses.KEY_BTAB:  # Shift-Tab
        state.focus_idx = (state.focus_idx - 1) % len(focus_names)
        return
    if key == curses.KEY_RIGHT:
        state.focus_idx = min(len(focus_names) - 1, state.focus_idx + 1)
        return
    if key == curses.KEY_LEFT:
        state.focus_idx = max(0, state.focus_idx - 1)
        return

    if key in (curses.KEY_DOWN, ord("j")):
        if focus == "channels":
            state.channel_idx = min(max(0, len(channels) - 1), state.channel_idx + 1)
        elif focus == "plugins":
            state.plugin_idx = min(max(0, len(plugins) - 1), state.plugin_idx + 1)
        elif focus == "prompts":
            state.prompt_idx = min(max(0, len(prompts) - 1), state.prompt_idx + 1)
        else:
            state.setting_idx = min(max(0, len(settings) - 1), state.setting_idx + 1)
        return

    if key in (curses.KEY_UP, ord("k")):
        if focus == "channels":
            state.channel_idx = max(0, state.channel_idx - 1)
        elif focus == "plugins":
            state.plugin_idx = max(0, state.plugin_idx - 1)
        elif focus == "prompts":
            state.prompt_idx = max(0, state.prompt_idx - 1)
        else:
            state.setting_idx = max(0, state.setting_idx - 1)
        return

    if key in (ord("g"), curses.KEY_HOME):
        if focus == "channels":
            state.channel_idx = 0
        elif focus == "plugins":
            state.plugin_idx = 0
        elif focus == "prompts":
            state.prompt_idx = 0
        else:
            state.setting_idx = 0
        return
    if key in (ord("G"), curses.KEY_END):
        if focus == "channels":
            state.channel_idx = max(0, len(channels) - 1)
        elif focus == "plugins":
            state.plugin_idx = max(0, len(plugins) - 1)
        elif focus == "prompts":
            state.prompt_idx = max(0, len(prompts) - 1)
        else:
            state.setting_idx = max(0, len(settings) - 1)
        return

    if key == ord("a"):
        if state.selected_agent_id.startswith("@local:"):
            state.notice = "autostart not applicable for local-user claw"
            state.notice_error = False
        else:
            try:
                service.toggle_agent_autostart(state.selected_agent_id)
                state.notice = "autostart toggled"
                state.notice_error = False
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
        return
    if key in (ord("d"), ord("D"), curses.KEY_DC):
        state.purge_confirm = True
        return

    if key in (ord(" "), curses.KEY_ENTER, 10, 13):
        if focus == "channels" and channels:
            try:
                service.toggle_agent_channel(state.selected_agent_id, state.channel_idx)
                state.notice = "channel toggled"
                state.notice_error = False
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
        elif focus == "plugins" and plugins:
            try:
                plugin = str(plugins[state.plugin_idx][0])
                service.toggle_agent_plugin(state.selected_agent_id, plugin)
                state.notice = f"plugin {plugin} toggled"
                state.notice_error = False
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
        elif focus == "prompts" and prompts:
            item = prompts[state.prompt_idx]
            _run_prompt_action(service, state, item)
        elif focus == "settings":
            item = settings[state.setting_idx] if settings else None
            if item:
                _run_setting_action(service, state, item)


# ═════════════════════════════════════════════════════════════════════
#  Selection Sync
# ═════════════════════════════════════════════════════════════════════


def _sync_selection(state: DashboardState, snapshot: dict[str, Any]) -> None:
    rows = snapshot.get("rows", [])
    if not rows:
        state.selected_row = 0
        state.selected_agent_id = ""
        state.view = "overview"
        return

    if state.selected_agent_id:
        for idx, row in enumerate(rows):
            if str(row.get("agent_id", "")) == state.selected_agent_id:
                state.selected_row = idx
                break
        else:
            state.selected_row = min(state.selected_row, len(rows) - 1)
            state.selected_agent_id = str(rows[state.selected_row].get("agent_id", ""))
    else:
        state.selected_row = min(state.selected_row, len(rows) - 1)
        state.selected_agent_id = str(rows[state.selected_row].get("agent_id", ""))


# ═════════════════════════════════════════════════════════════════════
#  Main Draw Orchestrator
# ═════════════════════════════════════════════════════════════════════


def _draw(stdscr: Any, snapshot: dict[str, Any], state: DashboardState, service: Any) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    if height < 8 or width < 40:
        _add(stdscr, 0, 0, _fit("terminal too small (min 40x8)", width), _color(C_ERROR))
        stdscr.refresh()
        return

    _draw_header(stdscr, snapshot, width)

    if state.view == "overview":
        _draw_stats(stdscr, snapshot, width)
        _draw_overview(stdscr, snapshot, state, service, height, width)
        if state.purge_confirm:
            agent_display = _display_agent_id(state.selected_agent_id)
            footer = f"{ICON_WARN} PURGE {agent_display}: y confirm {ICON_DOT} n cancel"
        elif state.overview_mode == "agents":
            footer = (
                f"j/k navigate {ICON_DOT} Enter open {ICON_DOT} d purge {ICON_DOT} "
                f"v channels {ICON_DOT} Esc/q quit"
            )
        else:
            footer = (
                f"j/k navigate {ICON_DOT} Tab/\u2190\u2192 pane {ICON_DOT} "
                f"a assign {ICON_DOT} u unassign {ICON_DOT} c connect {ICON_DOT} "
                f"Esc back {ICON_DOT} v agents {ICON_DOT} q quit"
            )
    else:
        _draw_detail(stdscr, service, state, height, width)
        if state.purge_confirm:
            footer = f"{ICON_WARN} CONFIRM PURGE: y confirm {ICON_DOT} n cancel"
        else:
            footer = (
                f"j/k navigate {ICON_DOT} Tab/\u2190\u2192 section {ICON_DOT} "
                f"Space action {ICON_DOT} a autostart {ICON_DOT} d purge {ICON_DOT} "
                f"Esc/b back {ICON_DOT} q quit"
            )

    # ── Notice bar ───────────────────────────────────────────────────
    if state.purge_confirm and state.view == "overview":
        agent_display = _display_agent_id(state.selected_agent_id)
        warn = f" {ICON_WARN} purge {agent_display}? permanently deletes agent + Linux user "
        _add(stdscr, height - 2, 0, warn.ljust(width - 1), _color(C_NOTICE_ERR))
    elif state.notice:
        ncolor = _color(C_NOTICE_ERR) if state.notice_error else _color(C_NOTICE_OK)
        notice_text = f" {state.notice} "
        _add(stdscr, height - 2, 0, notice_text.ljust(width - 1), ncolor)

    # ── Footer ───────────────────────────────────────────────────────
    _add(stdscr, height - 1, 0, _fit(f" {footer}", width), _color(C_HELP))
    stdscr.refresh()


# ═════════════════════════════════════════════════════════════════════
#  Header & Stats
# ═════════════════════════════════════════════════════════════════════


def _draw_header(stdscr: Any, snapshot: dict[str, Any], width: int) -> None:
    provider = snapshot.get("provider", "")
    workspace = snapshot.get("workspace", "")
    generated = snapshot.get("generated_at", "")

    left = f" {ICON_BRAND} CLAWIE"
    right = f"{provider} {ICON_DOT} {workspace} {ICON_DOT} {generated} "

    _add(stdscr, 0, 0, left, _color(C_TITLE, bold=True))
    right_x = max(len(left) + 2, width - len(right))
    _add(stdscr, 0, right_x, _fit(right, width - right_x), _color(C_TITLE, dim=True))


def _draw_stats(stdscr: Any, snapshot: dict[str, Any], width: int) -> None:
    totals = snapshot.get("totals", {})
    agents = totals.get("agents", 0)
    channels = totals.get("channels", 0)
    migrated = totals.get("migrated_channels", 0)
    cpu = float(totals.get("cpu_percent", 0))
    mem = float(totals.get("mem_percent", 0))

    left = f" {agents} agents  {channels} channels  {migrated} migrated"
    right = f"cpu {_bar(cpu)} {cpu:4.1f}%  mem {_bar(mem)} {mem:4.1f}% "

    _add(stdscr, 1, 0, left, _color(C_DEFAULT))
    right_x = max(len(left) + 2, width - len(right))
    _add(stdscr, 1, right_x, _fit(right, width - right_x), _color(C_DEFAULT))


# ═════════════════════════════════════════════════════════════════════
#  Overview Router
# ═════════════════════════════════════════════════════════════════════


def _draw_overview(
    stdscr: Any,
    snapshot: dict[str, Any],
    state: DashboardState,
    service: Any,
    height: int,
    width: int,
) -> None:
    if state.overview_mode == "channels":
        _draw_overview_channels(stdscr, snapshot, state, service, height, width)
        return
    _draw_overview_agents(stdscr, snapshot, state, height, width)


# ═════════════════════════════════════════════════════════════════════
#  Overview – Agents
# ═════════════════════════════════════════════════════════════════════


def _draw_overview_agents(
    stdscr: Any,
    snapshot: dict[str, Any],
    state: DashboardState,
    height: int,
    width: int,
) -> None:
    rows = snapshot.get("rows", [])
    split = max(40, int(width * 0.65))
    split = min(split, width - 22)
    content_bottom = height - 3  # leave room for notice + footer
    right_x = split + 2
    right_w = max(0, width - right_x)

    # ── Top separator ────────────────────────────────────────────────
    _hline(stdscr, 2, width, junctions={split: TT})

    # ── Column headers ───────────────────────────────────────────────
    aw, pw = _agent_col_widths(split - 2)
    header = (
        f"   {'AGENT':<{aw}}  {'STATUS':<7}  {'CPU':>5}  {'MEM':>5}  {'CH':>5}  {'PROVIDER':<{pw}}"
    )
    _add(stdscr, 3, 0, _fit(header, split), _color(C_HEAD, bold=True))
    _add(stdscr, 3, right_x, "SELECTED AGENT", _color(C_HEAD, bold=True))

    # ── Vertical divider ─────────────────────────────────────────────
    for y in range(2, content_bottom + 1):
        _add(stdscr, y, split, V, _color(C_BORDER))

    # ── Bottom separator ─────────────────────────────────────────────
    if content_bottom > 4:
        _hline(stdscr, content_bottom, width, junctions={split: BT})

    # ── Agent rows ───────────────────────────────────────────────────
    line = 4
    for idx, row in enumerate(rows):
        if line >= content_bottom:
            break
        agent_id = _display_agent_id(str(row.get("agent_id", "")))
        status = str(row.get("status", ""))
        icon = _status_icon(status)
        cpu = f"{float(row.get('cpu_percent', 0.0)):.1f}"
        mem = f"{float(row.get('mem_percent', 0.0)):.1f}"
        ch = f"{row.get('channels', 0)}/{row.get('channels_total', 0)}"
        prov = str(row.get("provider", ""))
        is_sel = idx == state.selected_row
        ptr = ICON_PTR if is_sel else " "

        text = (
            f" {ptr} {agent_id:<{aw}.{aw}}  {icon} {_status_short(status):<4}  "
            f"{cpu:>5}  {mem:>5}  {ch:>5}  {prov:<{pw}.{pw}}"
        )
        attr = _color(C_SELECT) if is_sel else _color(C_DEFAULT)
        _add(stdscr, line, 0, _fit(text, split), attr)

        # status icon color on non-selected rows
        if not is_sel:
            icon_attr = _color(C_OK) if _is_running(status) else _color(C_HELP, dim=True)
            icon_x = 4 + aw + 2
            if icon_x < split:
                _add(stdscr, line, icon_x, icon, icon_attr)

        line += 1

    if not rows:
        _add(stdscr, 4, 2, "no agents found", _color(C_HELP, dim=True))

    # ── Right panel: selected agent ──────────────────────────────────
    if rows and state.selected_row < len(rows):
        sel = rows[state.selected_row]
        info_lines = [
            (_display_agent_id(str(sel.get("agent_id", ""))), _color(C_TITLE, bold=True)),
            (f"display  {sel.get('display_name', '')}", _color(C_DEFAULT)),
            (f"provider {sel.get('provider', '')}", _color(C_DEFAULT)),
            (f"status   {_status_icon(str(sel.get('status', '')))} {sel.get('status', '')}", _color(C_DEFAULT)),
            (f"version  {sel.get('version', '')}", _color(C_DEFAULT)),
            (f"strategy {sel.get('strategy', '')}", _color(C_DEFAULT)),
        ]
        for i, (text, attr) in enumerate(info_lines):
            y = 4 + i
            if y >= content_bottom:
                break
            _add(stdscr, y, right_x, _fit(text, right_w), attr)

    # ── Right panel: recent events ───────────────────────────────────
    events = snapshot.get("events", [])
    events_start = 11
    if events and events_start < content_bottom:
        _add(stdscr, events_start, right_x, "RECENT EVENTS", _color(C_HEAD, bold=True))
        ev_line = events_start + 1
        for event in events[: max(0, content_bottom - ev_line)]:
            ts = str(event.get("timestamp", ""))
            etype = str(event.get("type", ""))
            _add(stdscr, ev_line, right_x, _fit(f"{ts} {etype}", right_w), _color(C_DEFAULT, dim=True))
            ev_line += 1


# ═════════════════════════════════════════════════════════════════════
#  Overview – Channels
# ═════════════════════════════════════════════════════════════════════


def _draw_overview_channels(
    stdscr: Any,
    snapshot: dict[str, Any],
    state: DashboardState,
    service: Any,
    height: int,
    width: int,
) -> None:
    col_w = max(22, int((width - 4) / 3))
    col2_x = col_w + 1
    col3_x = (col_w * 2) + 2
    content_bottom = height - 3
    rows_max = max(0, content_bottom - 6)

    channels: list[dict[str, Any]] = []
    try:
        inventory = service.channel_inventory()
        channels = list(inventory.get("rows", []))
    except Exception:  # noqa: BLE001
        channels = []
    agent_rows = [row for row in snapshot.get("rows", []) if not str(row.get("agent_id", "")).startswith("@local:")]
    state.selected_target_row = min(state.selected_target_row, max(0, len(agent_rows) - 1))
    selected_agent_id = str(agent_rows[state.selected_target_row].get("agent_id", "")) if agent_rows else ""
    available = [row for row in channels if str(row.get("source", "")) in {"local", "pool"}]
    assigned = [row for row in channels if str(row.get("owner_agent_id", "")).strip() == selected_agent_id]
    state.selected_available_row = min(state.selected_available_row, max(0, len(available) - 1))
    state.selected_assigned_row = min(state.selected_assigned_row, max(0, len(assigned) - 1))

    # ── Top separator ────────────────────────────────────────────────
    _hline(stdscr, 2, width, junctions={col_w: TT, col3_x - 1: TT})

    # ── Section headers ──────────────────────────────────────────────
    hdr_attrs = [_color(C_HELP, dim=True)] * 3
    hdr_attrs[state.overview_focus_idx] = _color(C_TITLE, bold=True)

    _add(stdscr, 3, 1, "TARGET AGENTS", hdr_attrs[0])
    _add(stdscr, 3, col2_x + 1, "AVAILABLE", hdr_attrs[1])
    _add(stdscr, 3, col3_x + 1, "ASSIGNED", hdr_attrs[2])

    # ── Sub-header description ───────────────────────────────────────
    _add(stdscr, 4, 1, _fit("select agent, assign/connect channels", col_w - 2), _color(C_HELP, dim=True))

    # ── Vertical dividers ────────────────────────────────────────────
    for y in range(2, content_bottom + 1):
        _add(stdscr, y, col_w, V, _color(C_BORDER))
        _add(stdscr, y, col3_x - 1, V, _color(C_BORDER))

    # ── Bottom separator ─────────────────────────────────────────────
    if content_bottom > 5:
        _hline(stdscr, content_bottom, width, junctions={col_w: BT, col3_x - 1: BT})

    # ── Column 1: Target agents ──────────────────────────────────────
    line = 5
    if not agent_rows:
        _add(stdscr, line, 1, "no managed agents", _color(C_HELP, dim=True))
    for idx, row in enumerate(agent_rows[:rows_max]):
        if line >= content_bottom:
            break
        is_sel = idx == state.selected_target_row
        ptr = ICON_PTR if is_sel else " "
        status = str(row.get("status", ""))
        icon = _status_icon(status)
        agent_id = _display_agent_id(str(row.get("agent_id", "")))
        text = f" {ptr} {agent_id} {icon} {_status_short(status)}"
        focused = state.overview_focus_idx == 0
        attr = _color(C_SELECT) if focused and is_sel else _color(C_DEFAULT)
        _add(stdscr, line, 0, _fit(text, col_w), attr)
        line += 1

    # ── Column 2: Available channels ─────────────────────────────────
    line = 5
    if not available:
        _add(stdscr, line, col2_x + 1, "none", _color(C_HELP, dim=True))
    for idx, channel in enumerate(available[:rows_max]):
        if line >= content_bottom:
            break
        source = str(channel.get("source", ""))
        badge = "pool" if source == "pool" else "local"
        text = f" {channel.get('kind', '')}:{channel.get('name', '')} [{badge}]"
        focused = state.overview_focus_idx == 1
        is_sel = idx == state.selected_available_row
        attr = _color(C_SELECT) if focused and is_sel else _color(C_DEFAULT)
        _add(stdscr, line, col2_x, _fit(text, col_w - 1), attr)
        line += 1

    # ── Column 3: Assigned channels ──────────────────────────────────
    line = 5
    if not assigned:
        _add(stdscr, line, col3_x + 1, "none", _color(C_HELP, dim=True))
    for idx, channel in enumerate(assigned[:rows_max]):
        if line >= content_bottom:
            break
        enabled = bool(channel.get("enabled", True))
        icon = ICON_ON if enabled else ICON_OFF
        text = f" {icon} {channel.get('kind', '')}:{channel.get('name', '')}"
        focused = state.overview_focus_idx == 2
        is_sel = idx == state.selected_assigned_row
        attr = _color(C_SELECT) if focused and is_sel else _color(C_DEFAULT)
        _add(stdscr, line, col3_x, _fit(text, width - col3_x), attr)
        line += 1


# ═════════════════════════════════════════════════════════════════════
#  Detail View
# ═════════════════════════════════════════════════════════════════════


def _draw_detail(stdscr: Any, service: Any, state: DashboardState, height: int, width: int) -> None:
    try:
        agent = service.get_dashboard_agent(state.selected_agent_id)
    except Exception as exc:  # noqa: BLE001
        _add(stdscr, 2, 1, f"failed loading agent: {exc}", _color(C_ERROR))
        return

    agent_info = agent.get("agent", {})
    channels = agent.get("channels", [])
    plugins = sorted(agent_info.get("plugins", {}).items())
    prompts = _prompt_items(agent)
    focus_names = ["channels", "plugins", "prompts", "settings"]
    state.focus_idx = min(state.focus_idx, len(focus_names) - 1)
    focus = focus_names[state.focus_idx]
    content_bottom = height - 3

    # ── Agent info bar (line 1) ──────────────────────────────────────
    status = str(agent_info.get("status", ""))
    icon = _status_icon(status)
    info_line = (
        f" {ICON_BACK} {_display_agent_id(state.selected_agent_id)}"
        f"   {agent_info.get('provider', '')}"
        f" {ICON_DOT} {icon} {status}"
        f" {ICON_DOT} v{agent_info.get('version', '')}"
    )
    _add(stdscr, 1, 0, _fit(info_line, width), _color(C_TITLE))

    if state.purge_confirm:
        warn_text = f" {ICON_WARN} permanently delete agent + Linux user? press y to confirm, n to cancel "
        _add(stdscr, 1, 0, warn_text.ljust(width - 1), _color(C_NOTICE_ERR))

    # ── Layout: three visual columns ─────────────────────────────────
    # Four logical sections (channels, plugins, prompts, settings) –
    # prompts and settings share the right column, toggled by focus.
    left_w = max(24, int(width * 0.38))
    mid_w = max(20, int(width * 0.28))
    col2_x = left_w + 1
    col3_x = left_w + mid_w + 2
    right_w = max(0, width - col3_x)

    # ── Top separator ────────────────────────────────────────────────
    _hline(stdscr, 2, width, junctions={left_w: TT, col3_x - 1: TT})

    # ── Section headers ──────────────────────────────────────────────
    ch_attr = _color(C_TITLE, bold=True) if focus == "channels" else _color(C_HELP, dim=True)
    pl_attr = _color(C_TITLE, bold=True) if focus == "plugins" else _color(C_HELP, dim=True)

    _add(stdscr, 3, 1, "CHANNELS", ch_attr)
    _add(stdscr, 3, col2_x + 1, "PLUGINS", pl_attr)

    # Right column header: show Prompts when focused, otherwise Settings
    if focus == "prompts":
        _add(stdscr, 3, col3_x + 1, "PROMPTS", _color(C_TITLE, bold=True))
    else:
        st_attr = _color(C_TITLE, bold=True) if focus == "settings" else _color(C_HELP, dim=True)
        _add(stdscr, 3, col3_x + 1, "SETTINGS", st_attr)

    # ── Vertical dividers ────────────────────────────────────────────
    for y in range(2, content_bottom + 1):
        _add(stdscr, y, left_w, V, _color(C_BORDER))
        _add(stdscr, y, col3_x - 1, V, _color(C_BORDER))

    # ── Bottom separator ─────────────────────────────────────────────
    if content_bottom > 5:
        _hline(stdscr, content_bottom, width, junctions={left_w: BT, col3_x - 1: BT})

    # ── Column 1: Channels ───────────────────────────────────────────
    line = 4
    for idx, channel in enumerate(channels[: max(0, content_bottom - 4)]):
        if line >= content_bottom:
            break
        enabled = bool(channel.get("enabled", True))
        ch_icon = ICON_ON if enabled else ICON_OFF
        text = f" {ch_icon} {channel.get('kind', '')}:{channel.get('name', '')}"
        is_sel = focus == "channels" and idx == state.channel_idx
        attr = _color(C_SELECT) if is_sel else _color(C_DEFAULT)
        _add(stdscr, line, 0, _fit(text, left_w), attr)
        if not is_sel:
            icon_attr = _color(C_OK) if enabled else _color(C_HELP, dim=True)
            _add(stdscr, line, 1, ch_icon, icon_attr)
        line += 1

    if not channels:
        _add(stdscr, 4, 1, "no channels", _color(C_HELP, dim=True))

    # ── Column 2: Plugins ────────────────────────────────────────────
    line = 4
    for idx, (key, enabled) in enumerate(plugins[: max(0, content_bottom - 4)]):
        if line >= content_bottom:
            break
        pl_icon = ICON_ON if bool(enabled) else ICON_OFF
        text = f" {pl_icon} {key}"
        is_sel = focus == "plugins" and idx == state.plugin_idx
        attr = _color(C_SELECT) if is_sel else _color(C_DEFAULT)
        _add(stdscr, line, col2_x, _fit(text, mid_w), attr)
        if not is_sel:
            icon_attr = _color(C_OK) if bool(enabled) else _color(C_HELP, dim=True)
            _add(stdscr, line, col2_x + 1, pl_icon, icon_attr)
        line += 1

    if not plugins:
        _add(stdscr, 4, col2_x + 1, "no plugins", _color(C_HELP, dim=True))

    # ── Column 3: Prompts (when focused) or Settings ─────────────────
    if focus == "prompts":
        right_rows = prompts
    else:
        right_rows = _settings_items(agent)

    line = 4
    for idx, item in enumerate(right_rows):
        if line >= content_bottom:
            break
        label = str(item.get("label", ""))
        kind = str(item.get("kind", ""))

        # Pick an icon based on item type
        if kind == "autostart":
            r_icon = ICON_ON if "on" in label else ICON_OFF
            label = label.replace("autostart: on", f"autostart {ICON_DOT} on")
            label = label.replace("autostart: off", f"autostart {ICON_DOT} off")
        elif kind.startswith("service_") and kind not in ("service_status",):
            r_icon = ICON_PTR
        elif kind == "prompt_edit":
            r_icon = ICON_PTR
        elif kind.startswith("prompt_"):
            r_icon = ICON_PTR
        else:
            r_icon = " "

        text = f" {r_icon} {label}"
        is_sel = (focus == "prompts" and idx == state.prompt_idx) or (focus == "settings" and idx == state.setting_idx)
        attr = _color(C_SELECT) if is_sel else _color(C_DEFAULT)
        _add(stdscr, line, col3_x, _fit(text, right_w), attr)
        line += 1

    if not right_rows:
        _add(stdscr, 4, col3_x + 1, "none", _color(C_HELP, dim=True))


# ═════════════════════════════════════════════════════════════════════
#  Color & Drawing Utilities
# ═════════════════════════════════════════════════════════════════════


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_CYAN, -1)
    curses.init_pair(C_ERROR, curses.COLOR_RED, -1)
    curses.init_pair(C_HELP, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_BORDER, curses.COLOR_BLUE, -1)
    curses.init_pair(C_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
    curses.init_pair(C_HEAD, curses.COLOR_WHITE, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_NOTICE_OK, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(C_NOTICE_ERR, curses.COLOR_WHITE, curses.COLOR_RED)


def _color(idx: int, bold: bool = False, dim: bool = False) -> int:
    if not curses.has_colors():
        attr = curses.A_NORMAL
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        return attr
    attr = curses.color_pair(idx)
    if bold:
        attr |= curses.A_BOLD
    if dim:
        attr |= curses.A_DIM
    return attr


def _add(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    if y < 0 or x < 0:
        return
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        return


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width - 1:
        return text
    if width <= 3:
        return text[: max(0, width - 1)]
    return text[: width - 4] + "..."


def _hline(stdscr: Any, y: int, width: int, junctions: dict[int, str] | None = None) -> None:
    """Draw a horizontal box-drawing line with optional junction characters."""
    junctions = junctions or {}
    chars = []
    for x in range(max(0, width - 1)):
        chars.append(junctions.get(x, H))
    _add(stdscr, y, 0, "".join(chars), _color(C_BORDER))


def _bar(value: float, max_val: float = 100.0, width: int = 6) -> str:
    """Render a small resource usage bar: ▰▰▰▱▱▱"""
    ratio = min(1.0, max(0.0, value / max_val)) if max_val > 0 else 0.0
    filled = round(ratio * width)
    return BAR_FILL * filled + BAR_EMPTY * (width - filled)


def _status_icon(status: str) -> str:
    return ICON_RUN if _is_running(status) else ICON_STOP


def _status_short(status: str) -> str:
    return _STATUS_SHORT.get(status.lower().strip(), status[:4])


def _is_running(status: str) -> bool:
    return status.lower().strip() in ("running", "active", "run")


def _agent_col_widths(table_w: int) -> tuple[int, int]:
    """Return (agent_name_width, provider_width) for the agent table."""
    # fixed: status(7) + cpu(5) + mem(5) + ch(5) + 5 gaps of 2 = 32
    remaining = max(0, table_w - 32)
    aw = max(6, int(remaining * 0.6))
    pw = max(4, remaining - aw)
    return aw, pw


def _display_agent_id(agent_id: str) -> str:
    token = str(agent_id).strip()
    if token.startswith("@local:"):
        provider = token.split(":", 1)[1]
        return f"@{provider}" if provider else "@"
    return token


# ═════════════════════════════════════════════════════════════════════
#  Prompts
# ═════════════════════════════════════════════════════════════════════


def _prompt_items(agent: dict[str, Any]) -> list[dict[str, str]]:
    prompt_rows: list[dict[str, str]] = []
    prompts = agent.get("core_prompts", {})
    if isinstance(prompts, dict):
        for name in sorted(prompts):
            content = str(prompts.get(name, ""))
            prompt_rows.append(
                {
                    "kind": "prompt_edit",
                    "prompt": str(name),
                    "label": f"edit {name} ({len(content)} chars)",
                }
            )
    prompt_rows.append({"kind": "prompt_sync_from_disk", "label": "sync prompts from disk"})
    prompt_rows.append({"kind": "prompt_write_to_disk", "label": "write prompts to disk"})
    return prompt_rows


def _run_prompt_action(service: Any, state: DashboardState, item: dict[str, str]) -> None:
    kind = str(item.get("kind", ""))
    try:
        if kind == "prompt_sync_from_disk":
            service.sync_agent_core_prompts_from_disk(state.selected_agent_id)
            state.notice = "prompts synced from disk"
        elif kind == "prompt_write_to_disk":
            service.write_agent_core_prompts_to_disk(state.selected_agent_id)
            state.notice = "prompts written to disk"
        elif kind == "prompt_edit":
            prompt = str(item.get("prompt", ""))
            payload = service.get_agent_core_prompt(state.selected_agent_id, prompt)
            updated = _edit_text_in_editor(
                initial=str(payload.get("content", "")),
                suffix=Path(prompt).suffix or ".md",
            )
            if updated is None:
                state.notice = "prompt edit cancelled"
            else:
                service.set_agent_core_prompt(state.selected_agent_id, prompt, updated, sync_to_disk=True)
                state.notice = f"updated {prompt}"
        else:
            state.notice = "unknown prompt action"
        state.notice_error = False
    except Exception as exc:  # noqa: BLE001
        state.notice = str(exc)
        state.notice_error = True


def _edit_text_in_editor(initial: str, suffix: str = ".md") -> str | None:
    editor = str(os.environ.get("EDITOR", "")).strip() or "vi"
    fd, path = tempfile.mkstemp(prefix="clawie-prompt-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(initial)
        curses.def_prog_mode()
        curses.endwin()
        result = subprocess.run([editor, path], check=False)  # noqa: S603
        curses.reset_prog_mode()
        curses.curs_set(0)
        if result.returncode != 0:
            return None
        return Path(path).read_text(encoding="utf-8")
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# ═════════════════════════════════════════════════════════════════════
#  Settings & Actions
# ═════════════════════════════════════════════════════════════════════


def _settings_items(agent: dict[str, Any]) -> list[dict[str, str]]:
    info = agent.get("agent", {})
    is_local = bool(info.get("local_user", False))
    prefix = "(local) " if is_local else ""
    return [
        {
            "kind": "autostart",
            "label": f"{prefix}autostart: {'on' if bool(info.get('autostart', True)) else 'off'}",
        },
        {
            "kind": "service_status",
            "label": f"{prefix}status: {info.get('service_status', 'unknown')} {ICON_DOT} {info.get('service_mode', 'unknown')}",
        },
        {"kind": "service_start", "label": f"{prefix}start service"},
        {"kind": "service_stop", "label": f"{prefix}stop service"},
        {"kind": "service_restart", "label": f"{prefix}restart service"},
        {"kind": "service_status_refresh", "label": f"{prefix}refresh status"},
        {
            "kind": "heartbeat",
            "label": f"{prefix}heartbeat: {int(info.get('heartbeat_seconds', 30))}s",
        },
        {"kind": "auth_mode", "label": f"{prefix}auth: {info.get('auth_mode', '')}"},
    ]


def _run_setting_action(service: Any, state: DashboardState, item: dict[str, str]) -> None:
    kind = str(item.get("kind", ""))
    is_local = state.selected_agent_id.startswith("@local:")
    provider = state.selected_agent_id.split(":", 1)[1] if is_local else ""
    try:
        if kind == "autostart":
            if is_local:
                state.notice = "autostart not applicable for local-user claw"
            else:
                service.toggle_agent_autostart(state.selected_agent_id)
                state.notice = "autostart toggled"
        elif kind == "service_start":
            if is_local:
                service.local_claw_service_action(provider, "start")
            else:
                service.agent_service_action(state.selected_agent_id, "start")
            state.notice = "service start requested"
        elif kind == "service_stop":
            if is_local:
                service.local_claw_service_action(provider, "stop")
            else:
                service.agent_service_action(state.selected_agent_id, "stop")
            state.notice = "service stop requested"
        elif kind == "service_restart":
            if is_local:
                service.local_claw_service_action(provider, "restart")
            else:
                service.agent_service_action(state.selected_agent_id, "restart")
            state.notice = "service restart requested"
        elif kind == "service_status_refresh":
            if is_local:
                result = service.local_claw_service_action(provider, "status")
            else:
                result = service.agent_service_action(state.selected_agent_id, "status")
            state.notice = f"service status: {result.get('service_status', 'unknown')}"
        else:
            state.notice = "read-only setting"
        state.notice_error = False
    except Exception as exc:  # noqa: BLE001
        state.notice = str(exc)
        state.notice_error = True


# ═════════════════════════════════════════════════════════════════════
#  Static Fallback (non-TTY output)
# ═════════════════════════════════════════════════════════════════════


def _print_static(snapshot: dict[str, Any]) -> None:
    totals = snapshot["totals"]
    print_info(
        f"Clawie Monitor  "
        f"provider={snapshot.get('provider', '')} workspace={snapshot['workspace']} agents={totals['agents']} "
        f"channels={totals['channels']} migrated={totals['migrated_channels']} "
        f"cpu={totals.get('cpu_percent', 0)}% mem={totals.get('mem_percent', 0)}%"
    )

    rows = []
    for row in snapshot["rows"]:
        status = str(row.get("status", ""))
        rows.append(
            [
                _display_agent_id(str(row.get("agent_id", row.get("user_id", "")))),
                row.get("display_name", ""),
                row.get("provider", ""),
                f"{_status_icon(status)} {status}",
                str(row.get("pid", 0)),
                f"{float(row.get('cpu_percent', 0.0)):.1f}",
                f"{float(row.get('mem_percent', 0.0)):.1f}",
                f"{row.get('channels', 0)}/{row.get('channels_total', 0)}",
            ]
        )

    if rows:
        print_table(
            ["agent", "display", "provider", "status", "pid", "cpu%", "mem%", "channels"],
            rows,
        )

    events = snapshot.get("events", [])
    if events:
        print(f"\n  RECENT EVENTS")
        for event in events:
            print(f"  {ICON_DOT} {event.get('timestamp', '')} {event.get('type', '')} {event.get('message', '')}")
