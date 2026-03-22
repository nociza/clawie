from __future__ import annotations

import curses
import os
import subprocess
import sys
import tempfile
import time
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
ICON_TIER_FAST = "\u26a1"      # ⚡
ICON_TIER_BALANCED = "\u2696"  # ⚖
ICON_TIER_POWER = "\u2b50"     # ⭐
_TIER_ICON_MAP: dict[str, str] = {"fast": ICON_TIER_FAST, "balanced": ICON_TIER_BALANCED, "power": ICON_TIER_POWER}

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
    detail_agent: dict[str, Any] | None = None
    detail_agent_id: str = ""
    channel_inventory: dict[str, Any] | None = None
    reload_requested: bool = True
    live_refresh_requested: bool = True
    last_live_refresh_at: float = 0.0


DETAIL_FOCUS_NAMES = ("channels", "plugins", "settings")


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
    stdscr.timeout(100)
    _init_colors()

    state = DashboardState()
    if agent_id:
        state.selected_agent_id = agent_id
        state.view = "detail"

    snapshot: dict[str, Any] = {}
    refresh_interval = max(1.0, float(refresh_seconds))
    while True:
        now = time.monotonic()
        live_refresh = False
        requested_live_refresh = state.live_refresh_requested or not snapshot
        if requested_live_refresh:
            live_refresh = True
        elif (now - state.last_live_refresh_at) >= refresh_interval:
            live_refresh = True

        if live_refresh or state.reload_requested or not snapshot:
            snapshot = service.performance_snapshot(
                agent_id=None if state.view == "detail" else agent_id,
                refresh=live_refresh,
            )
            _sync_selection(state, snapshot)
            if state.view == "detail" and state.selected_agent_id:
                if (
                    state.reload_requested
                    or requested_live_refresh
                    or state.detail_agent is None
                    or state.detail_agent_id != state.selected_agent_id
                ):
                    _load_detail_agent(state, service, force=True)
            if state.overview_mode == "channels":
                if state.reload_requested or requested_live_refresh or state.channel_inventory is None:
                    _load_channel_inventory(state, service, force=True)
            if live_refresh:
                state.last_live_refresh_at = now
                state.live_refresh_requested = False
            state.reload_requested = False

        _draw(stdscr, snapshot, state, service)

        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), ord("Q")):
            return
        if key == 27 and state.view == "overview" and state.overview_mode == "agents" and not state.purge_confirm:
            return
        if key in (ord("r"), ord("R")):
            state.reload_requested = True
            state.live_refresh_requested = True
            continue

        prev_view = state.view
        prev_overview_mode = state.overview_mode
        prev_selected_agent_id = state.selected_agent_id
        if state.view == "overview":
            changed = _handle_overview_key(key, state, snapshot, service)
        else:
            changed = _handle_detail_key(key, state, service)

        if changed:
            _invalidate_view_cache(state)
            state.reload_requested = True
        if (
            state.view != prev_view
            or state.overview_mode != prev_overview_mode
            or state.selected_agent_id != prev_selected_agent_id
        ):
            if state.selected_agent_id != prev_selected_agent_id:
                state.detail_agent = None
                state.detail_agent_id = ""
            if state.overview_mode != prev_overview_mode:
                state.channel_inventory = None
            state.reload_requested = True


# ═════════════════════════════════════════════════════════════════════
#  Key Handlers
# ═════════════════════════════════════════════════════════════════════


def _handle_overview_key(key: int, state: DashboardState, snapshot: dict[str, Any], service: Any) -> bool:
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
            return True
        if key in (ord("n"), ord("N"), 27):
            state.purge_confirm = False
            return False
        return False  # swallow all other keys during confirm

    if key in (ord("v"), ord("V")):
        state.notice = ""
        state.notice_error = False
        _cycle = {"agents": "channels", "channels": "delegation", "delegation": "agents"}
        state.overview_mode = _cycle.get(state.overview_mode, "agents")
        state.overview_focus_idx = 0
        return False

    if state.overview_mode == "channels":
        return _handle_channels_overview_key(key, state, snapshot, service)

    if state.overview_mode == "delegation":
        return False

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
    return False


def _handle_channels_overview_key(
    key: int,
    state: DashboardState,
    snapshot: dict[str, Any],
    service: Any,
) -> bool:
    inventory = _load_channel_inventory(state, service)
    agent_rows = [row for row in snapshot.get("rows", []) if not str(row.get("agent_id", "")).startswith("@local:")]
    state.selected_target_row = min(state.selected_target_row, max(0, len(agent_rows) - 1))
    if not agent_rows:
        return False
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
        return False

    if key == 9:  # TAB
        state.overview_focus_idx = (state.overview_focus_idx + 1) % 3
        return False
    if key == curses.KEY_BTAB:  # Shift-Tab
        state.overview_focus_idx = (state.overview_focus_idx - 1) % 3
        return False
    if key == curses.KEY_RIGHT:
        state.overview_focus_idx = min(2, state.overview_focus_idx + 1)
        return False
    if key == curses.KEY_LEFT:
        state.overview_focus_idx = max(0, state.overview_focus_idx - 1)
        return False

    if key in (curses.KEY_DOWN, ord("j")):
        if state.overview_focus_idx == 0 and agent_rows:
            state.selected_target_row = min(len(agent_rows) - 1, state.selected_target_row + 1)
        elif state.overview_focus_idx == 1 and available:
            state.selected_available_row = min(len(available) - 1, state.selected_available_row + 1)
        elif state.overview_focus_idx == 2 and assigned:
            state.selected_assigned_row = min(len(assigned) - 1, state.selected_assigned_row + 1)
        return False
    if key in (curses.KEY_UP, ord("k")):
        if state.overview_focus_idx == 0 and agent_rows:
            state.selected_target_row = max(0, state.selected_target_row - 1)
        elif state.overview_focus_idx == 1 and available:
            state.selected_available_row = max(0, state.selected_available_row - 1)
        elif state.overview_focus_idx == 2 and assigned:
            state.selected_assigned_row = max(0, state.selected_assigned_row - 1)
        return False
    if key in (ord("g"), curses.KEY_HOME):
        if state.overview_focus_idx == 0:
            state.selected_target_row = 0
        elif state.overview_focus_idx == 1:
            state.selected_available_row = 0
        else:
            state.selected_assigned_row = 0
        return False
    if key in (ord("G"), curses.KEY_END):
        if state.overview_focus_idx == 0:
            state.selected_target_row = max(0, len(agent_rows) - 1)
        elif state.overview_focus_idx == 1:
            state.selected_available_row = max(0, len(available) - 1)
        else:
            state.selected_assigned_row = max(0, len(assigned) - 1)
        return False

    selected_available = available[state.selected_available_row] if available else {}
    selected_assigned = assigned[state.selected_assigned_row] if assigned else {}
    source_agent_id = str(selected_available.get("owner_agent_id", ""))
    available_kind = str(selected_available.get("kind", ""))
    available_name = str(selected_available.get("name", ""))
    assigned_kind = str(selected_assigned.get("kind", ""))
    assigned_name = str(selected_assigned.get("name", ""))

    if key in (ord("a"), ord("A")):
        if not available_kind or not available_name:
            return False
        try:
            service.assign_channel_to_agent(source_agent_id, available_kind, available_name, target_agent_id)
            state.notice = f"assigned {available_kind}:{available_name} -> {target_agent_id}"
            state.notice_error = False
            return True
        except Exception as exc:  # noqa: BLE001
            state.notice = str(exc)
            state.notice_error = True
        return False
    if key in (ord("c"), ord("C")):
        kind = ""
        name = ""
        try:
            if state.overview_focus_idx == 1:
                kind, name = available_kind, available_name
                if not kind or not name:
                    return False
                service.assign_channel_to_agent(source_agent_id, kind, name, target_agent_id)
            else:
                kind, name = assigned_kind, assigned_name
                if not kind or not name:
                    return False
            service.connect_agent_channel(target_agent_id, kind, name)
            state.notice = f"connected {kind}:{name} for {target_agent_id}"
            state.notice_error = False
            return True
        except Exception as exc:  # noqa: BLE001
            state.notice = str(exc)
            state.notice_error = True
        return False
    if key in (ord("u"), ord("U")):
        if not assigned_kind or not assigned_name:
            return False
        try:
            service.unassign_channel_from_agent(target_agent_id, assigned_kind, assigned_name)
            state.notice = f"unassigned {assigned_kind}:{assigned_name}"
            state.notice_error = False
            return True
        except Exception as exc:  # noqa: BLE001
            state.notice = str(exc)
            state.notice_error = True
        return False
    if key in (curses.KEY_ENTER, 10, 13):
        state.notice = ""
        state.notice_error = False
        state.selected_agent_id = target_agent_id
        state.view = "detail"
    return False


def _handle_detail_key(key: int, state: DashboardState, service: Any) -> bool:
    if state.purge_confirm:
        if key in (ord("y"), ord("Y")):
            try:
                service.purge_agent(state.selected_agent_id)
            except Exception:  # noqa: BLE001
                state.purge_confirm = False
                return False
            state.purge_confirm = False
            state.view = "overview"
            state.selected_agent_id = ""
            return True
        if key in (ord("n"), ord("N"), 27):
            state.purge_confirm = False
            return False
        return False

    if key in (27, ord("b"), ord("B")):  # ESC/back
        state.view = "overview"
        state.notice = ""
        state.notice_error = False
        return False

    state.focus_idx = max(0, min(state.focus_idx, len(DETAIL_FOCUS_NAMES) - 1))
    focus = DETAIL_FOCUS_NAMES[state.focus_idx]

    try:
        agent = _load_detail_agent(state, service)
    except Exception:  # noqa: BLE001
        return False

    channels = agent.get("channels", [])
    plugins = sorted(agent.get("agent", {}).get("plugins", {}).items())
    state.channel_idx = min(state.channel_idx, max(0, len(channels) - 1))
    state.plugin_idx = min(state.plugin_idx, max(0, len(plugins) - 1))
    settings = _settings_items(
        agent,
        _dashboard_provider_choices(service, agent),
        service.addon_options() if hasattr(service, "addon_options") else [],
        _selected_channel(channels, state.channel_idx),
    )
    state.setting_idx = min(state.setting_idx, max(0, len(settings) - 1))

    if key == 9:  # TAB
        state.focus_idx = (state.focus_idx + 1) % len(DETAIL_FOCUS_NAMES)
        return False
    if key == curses.KEY_BTAB:  # Shift-Tab
        state.focus_idx = (state.focus_idx - 1) % len(DETAIL_FOCUS_NAMES)
        return False
    if key == curses.KEY_RIGHT:
        state.focus_idx = min(len(DETAIL_FOCUS_NAMES) - 1, state.focus_idx + 1)
        return False
    if key == curses.KEY_LEFT:
        state.focus_idx = max(0, state.focus_idx - 1)
        return False

    if key in (curses.KEY_DOWN, ord("j")):
        if focus == "channels":
            state.channel_idx = min(max(0, len(channels) - 1), state.channel_idx + 1)
        elif focus == "plugins":
            state.plugin_idx = min(max(0, len(plugins) - 1), state.plugin_idx + 1)
        else:
            state.setting_idx = min(max(0, len(settings) - 1), state.setting_idx + 1)
        return False

    if key in (curses.KEY_UP, ord("k")):
        if focus == "channels":
            state.channel_idx = max(0, state.channel_idx - 1)
        elif focus == "plugins":
            state.plugin_idx = max(0, state.plugin_idx - 1)
        else:
            state.setting_idx = max(0, state.setting_idx - 1)
        return False

    if key in (ord("g"), curses.KEY_HOME):
        if focus == "channels":
            state.channel_idx = 0
        elif focus == "plugins":
            state.plugin_idx = 0
        else:
            state.setting_idx = 0
        return False
    if key in (ord("G"), curses.KEY_END):
        if focus == "channels":
            state.channel_idx = max(0, len(channels) - 1)
        elif focus == "plugins":
            state.plugin_idx = max(0, len(plugins) - 1)
        else:
            state.setting_idx = max(0, len(settings) - 1)
        return False

    if key == ord("a"):
        if state.selected_agent_id.startswith("@local:"):
            state.notice = "autostart not applicable for local-user claw"
            state.notice_error = False
        else:
            try:
                service.toggle_agent_autostart(state.selected_agent_id)
                state.notice = "autostart toggled"
                state.notice_error = False
                return True
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
        return False
    if focus == "channels" and key in (ord("n"),):
        return _run_channel_detail_action(service, state, "add")
    if focus == "channels" and key in (ord("N"),):
        return _run_channel_detail_action(service, state, "add_connect")
    if focus == "channels" and key in (ord("c"), ord("C"), ord("l"), ord("L")):
        selected_channel = channels[state.channel_idx] if channels else None
        return _run_channel_detail_action(service, state, "connect", selected_channel)
    if focus == "channels" and key in (ord("u"), ord("U")):
        selected_channel = channels[state.channel_idx] if channels else None
        return _run_channel_detail_action(service, state, "unlink", selected_channel)
    if focus == "channels" and key in (ord("s"), ord("S")):
        return _run_channel_detail_action(service, state, "sync")
    if key in (ord("d"), ord("D"), curses.KEY_DC):
        state.purge_confirm = True
        return False

    if key in (ord(" "), curses.KEY_ENTER, 10, 13):
        if focus == "channels" and channels:
            try:
                service.toggle_agent_channel(state.selected_agent_id, state.channel_idx)
                state.notice = "channel toggled"
                state.notice_error = False
                return True
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
        elif focus == "plugins" and plugins:
            try:
                plugin = str(plugins[state.plugin_idx][0])
                service.toggle_agent_plugin(state.selected_agent_id, plugin)
                state.notice = f"plugin {plugin} toggled"
                state.notice_error = False
                return True
            except Exception as exc:  # noqa: BLE001
                state.notice = str(exc)
                state.notice_error = True
        elif focus == "settings":
            item = settings[state.setting_idx] if settings else None
            if item:
                return _run_setting_action(service, state, item)
    return False


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


def _invalidate_view_cache(state: DashboardState) -> None:
    state.detail_agent = None
    state.detail_agent_id = ""
    state.channel_inventory = None


def _load_detail_agent(state: DashboardState, service: Any, force: bool = False) -> dict[str, Any]:
    agent_id = str(state.selected_agent_id).strip()
    if not agent_id:
        raise ValueError("selected_agent_id is required")
    if not force and state.detail_agent is not None and state.detail_agent_id == agent_id:
        return state.detail_agent
    payload = service.get_dashboard_agent(agent_id)
    state.detail_agent = payload
    state.detail_agent_id = agent_id
    return payload


def _load_channel_inventory(state: DashboardState, service: Any, force: bool = False) -> dict[str, Any]:
    if not force and state.channel_inventory is not None:
        return state.channel_inventory
    payload = service.channel_inventory()
    state.channel_inventory = payload
    return payload


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
        elif state.overview_mode == "delegation":
            footer = (
                f"q quit {ICON_DOT} v agents {ICON_DOT} r refresh"
            )
        else:
            footer = (
                f"j/k navigate {ICON_DOT} Tab/\u2190\u2192 pane {ICON_DOT} "
                f"a assign {ICON_DOT} u unassign {ICON_DOT} c connect {ICON_DOT} "
                f"Esc back {ICON_DOT} v delegation {ICON_DOT} q quit"
            )
    else:
        _draw_detail(stdscr, service, state, height, width)
        if state.purge_confirm:
            footer = f"{ICON_WARN} CONFIRM PURGE: y confirm {ICON_DOT} n cancel"
        else:
            focus = _focus_name(state)
            if focus == "channels":
                footer = (
                    f"j/k navigate {ICON_DOT} Space enable {ICON_DOT} "
                    f"n add {ICON_DOT} N add+link {ICON_DOT} c link {ICON_DOT} u unlink {ICON_DOT} "
                    f"s sync live {ICON_DOT} "
                    f"Tab/\u2190\u2192 section {ICON_DOT} Esc/b back {ICON_DOT} q quit"
                )
            elif focus == "plugins":
                footer = (
                    f"j/k navigate {ICON_DOT} Space toggle plugin {ICON_DOT} "
                    f"Tab/\u2190\u2192 section {ICON_DOT} Esc/b back {ICON_DOT} q quit"
                )
            else:
                footer = (
                    f"j/k navigate {ICON_DOT} Space run action {ICON_DOT} "
                    f"auth/provider/channel/prompt {ICON_DOT} a autostart {ICON_DOT} d purge {ICON_DOT} "
                    f"Tab/\u2190\u2192 section {ICON_DOT} Esc/b back {ICON_DOT} q quit"
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
    if state.overview_mode == "delegation":
        _draw_overview_delegation(stdscr, snapshot, state, service, height, width)
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
    row_window = max(0, content_bottom - 4)
    row_start, visible_rows = _window_slice(list(rows), state.selected_row, row_window)
    line = 4
    for offset, row in enumerate(visible_rows):
        if line >= content_bottom:
            break
        idx = row_start + offset
        agent_id = _display_agent_id(str(row.get("agent_id", "")))
        status = str(row.get("status", ""))
        icon = _status_icon(status)
        cpu = f"{float(row.get('cpu_percent', 0.0)):.1f}"
        mem = f"{float(row.get('mem_percent', 0.0)):.1f}"
        ch = f"{row.get('channels', 0)}/{row.get('channels_total', 0)}"
        prov = str(row.get("provider", ""))
        if str(row.get("provider_status", "ok")) != "ok" and prov:
            prov = f"{prov}!"
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
    info_lines: list[tuple[str, int]] = []
    if rows and state.selected_row < len(rows):
        sel = rows[state.selected_row]
        provider_label = str(sel.get("provider", ""))
        if str(sel.get("provider_status", "ok")) != "ok" and provider_label:
            provider_label = f"{provider_label}!"
        info_lines = [
            (_display_agent_id(str(sel.get("agent_id", ""))), _color(C_TITLE, bold=True)),
            (f"display  {sel.get('display_name', '')}", _color(C_DEFAULT)),
            (f"provider {provider_label}", _color(C_DEFAULT)),
            (f"status   {_status_icon(str(sel.get('status', '')))} {sel.get('status', '')}", _color(C_DEFAULT)),
            (f"version  {sel.get('version', '')}", _color(C_DEFAULT)),
            (f"strategy {sel.get('strategy', '')}", _color(C_DEFAULT)),
        ]
        issue = str(sel.get("provider_issue", "")).strip()
        remediation = str(sel.get("provider_remediation", "")).strip()
        if issue:
            info_lines.append((f"issue    {issue}", _color(C_ERROR)))
        if remediation:
            info_lines.append((f"fix      {remediation}", _color(C_HELP)))
        for i, (text, attr) in enumerate(info_lines):
            y = 4 + i
            if y >= content_bottom:
                break
            _add(stdscr, y, right_x, _fit(text, right_w), attr)

    # ── Right panel: recent events ───────────────────────────────────
    events = snapshot.get("events", [])
    events_start = min(content_bottom, 5 + len(info_lines))
    if events and events_start < content_bottom:
        _add(stdscr, events_start, right_x, "RECENT EVENTS", _color(C_HEAD, bold=True))
        ev_line = events_start + 1
        for event in events[: max(0, content_bottom - ev_line)]:
            ts = str(event.get("timestamp", ""))
            etype = str(event.get("type", ""))
            _add(stdscr, ev_line, right_x, _fit(f"{ts} {etype}", right_w), _color(C_DEFAULT, dim=True))
            ev_line += 1


# ═════════════════════════════════════════════════════════════════════
#  Overview – Delegation
# ═════════════════════════════════════════════════════════════════════


def _draw_overview_delegation(
    stdscr: Any,
    snapshot: dict[str, Any],
    state: DashboardState,
    service: Any,
    height: int,
    width: int,
) -> None:
    start_y = 5
    mid = width // 2

    # Left panel: delegation trees
    try:
        from clawie.delegation import list_active_agents

        active = list_active_agents()
    except ImportError:
        active = []

    tree_lines: list[str] = []
    rows = snapshot.get("rows", [])
    seen_roots: set[str] = set()
    for row in rows:
        agent_id = str(row.get("agent_id", ""))
        if agent_id and agent_id not in seen_roots:
            seen_roots.add(agent_id)
            try:
                lines = service.delegation_tree_lines(agent_id)
                if lines:
                    tree_lines.extend(lines)
                    tree_lines.append("")
            except Exception:
                pass

    _safe_addstr(stdscr, start_y, 1, "Delegation Trees", curses.A_BOLD)
    if tree_lines:
        for i, line in enumerate(tree_lines[: height - start_y - 4]):
            _safe_addstr(stdscr, start_y + 1 + i, 2, line[: mid - 3])
    else:
        _safe_addstr(stdscr, start_y + 1, 2, "(no delegation trees)")

    # Right panel: active sockets + recent tasks
    _safe_addstr(stdscr, start_y, mid + 1, "Active Sockets", curses.A_BOLD)
    if active:
        for i, a in enumerate(active[: (height - start_y - 4) // 2]):
            text = f"{a['agent_id']:16} alive={a['alive']}  age={a['age_seconds']}s"
            _safe_addstr(stdscr, start_y + 1 + i, mid + 2, text[: width - mid - 3])
    else:
        _safe_addstr(stdscr, start_y + 1, mid + 2, "(none)")

    task_y = start_y + max(len(active), 1) + 2
    _safe_addstr(stdscr, task_y, mid + 1, "Recent Tasks", curses.A_BOLD)
    try:
        tasks = service.delegation_tasks(limit=10)
    except Exception:
        tasks = []
    if tasks:
        for i, t in enumerate(tasks[: height - task_y - 3]):
            tier_str = t.get("model_tier", "")
            tier_icon = _TIER_ICON_MAP.get(tier_str, " ")
            text = (
                f"{t['task_id'][:8]} {t['parent_agent_id']:10}->"
                f"{t['child_agent_id']:10} {tier_icon}{t['status']}"
            )
            _safe_addstr(stdscr, task_y + 1 + i, mid + 2, text[: width - mid - 3])
    else:
        _safe_addstr(stdscr, task_y + 1, mid + 2, "(no tasks)")


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
        inventory = _load_channel_inventory(state, service)
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
    agent_window = max(0, rows_max)
    agent_start, visible_agents = _window_slice(agent_rows, state.selected_target_row, agent_window)
    line = 5
    if not agent_rows:
        _add(stdscr, line, 1, "no managed agents", _color(C_HELP, dim=True))
    for offset, row in enumerate(visible_agents):
        if line >= content_bottom:
            break
        idx = agent_start + offset
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
    available_start, visible_available = _window_slice(available, state.selected_available_row, agent_window)
    line = 5
    if not available:
        _add(stdscr, line, col2_x + 1, "none", _color(C_HELP, dim=True))
    for offset, channel in enumerate(visible_available):
        if line >= content_bottom:
            break
        idx = available_start + offset
        source = str(channel.get("source", ""))
        badge = "pool" if source == "pool" else "local"
        text = f" {channel.get('kind', '')}:{channel.get('name', '')} [{badge}]"
        focused = state.overview_focus_idx == 1
        is_sel = idx == state.selected_available_row
        attr = _color(C_SELECT) if focused and is_sel else _color(C_DEFAULT)
        _add(stdscr, line, col2_x, _fit(text, col_w - 1), attr)
        line += 1

    # ── Column 3: Assigned channels ──────────────────────────────────
    assigned_start, visible_assigned = _window_slice(assigned, state.selected_assigned_row, agent_window)
    line = 5
    if not assigned:
        _add(stdscr, line, col3_x + 1, "none", _color(C_HELP, dim=True))
    for offset, channel in enumerate(visible_assigned):
        if line >= content_bottom:
            break
        idx = assigned_start + offset
        enabled = bool(channel.get("enabled", True))
        icon = ICON_ON if enabled else ICON_OFF
        text = f" {icon} {channel.get('kind', '')}:{channel.get('name', '')}"
        source = str(channel.get("source", ""))
        if source == "live":
            text += " [live]"
        elif source == "discovered":
            text += " [disc]"
        elif source == "stale":
            text += " [stale]"
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
        agent = _load_detail_agent(state, service)
    except Exception as exc:  # noqa: BLE001
        _add(stdscr, 2, 1, f"failed loading agent: {exc}", _color(C_ERROR))
        return

    agent_info = agent.get("agent", {})
    channels = agent.get("channels", [])
    plugins = sorted(agent_info.get("plugins", {}).items())
    state.focus_idx = max(0, min(state.focus_idx, len(DETAIL_FOCUS_NAMES) - 1))
    focus = DETAIL_FOCUS_NAMES[state.focus_idx]
    settings = _settings_items(
        agent,
        _dashboard_provider_choices(service, agent),
        service.addon_options() if hasattr(service, "addon_options") else [],
        _selected_channel(channels, state.channel_idx),
    )
    content_bottom = height - 3

    # ── Agent info bar (line 1) ──────────────────────────────────────
    status = str(agent_info.get("service_status", agent_info.get("status", "")))
    icon = _status_icon(status)
    auth_status = str(agent_info.get("auth_status", "unknown"))
    provider_summary = str(agent_info.get("provider", ""))
    if str(agent_info.get("provider_status", "ok")) != "ok" and provider_summary:
        provider_summary = f"{provider_summary}!"
    agent_tier = str(agent_info.get("model_tier", ""))
    tier_display = f" {ICON_DOT} {_TIER_ICON_MAP.get(agent_tier, '')}{agent_tier}" if agent_tier else ""
    info_line = (
        f" {ICON_BACK} {_display_agent_id(state.selected_agent_id)}"
        f"   {provider_summary}"
        f" {ICON_DOT} {icon} {status}"
        f" {ICON_DOT} auth {auth_status}"
        f" {ICON_DOT} v{agent_info.get('version', '')}"
        f"{tier_display}"
    )
    _add(stdscr, 1, 0, _fit(info_line, width), _color(C_TITLE))

    if state.purge_confirm:
        warn_text = f" {ICON_WARN} permanently delete agent + Linux user? press y to confirm, n to cancel "
        _add(stdscr, 1, 0, warn_text.ljust(width - 1), _color(C_NOTICE_ERR))

    # ── Layout: three real columns ───────────────────────────────────
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
    st_attr = _color(C_TITLE, bold=True) if focus == "settings" else _color(C_HELP, dim=True)

    _add(stdscr, 3, 1, "CHANNELS", ch_attr)
    _add(stdscr, 3, col2_x + 1, "PLUGINS", pl_attr)
    _add(stdscr, 3, col3_x + 1, "SETTINGS", st_attr)

    # ── Vertical dividers ────────────────────────────────────────────
    for y in range(2, content_bottom + 1):
        _add(stdscr, y, left_w, V, _color(C_BORDER))
        _add(stdscr, y, col3_x - 1, V, _color(C_BORDER))

    # ── Bottom separator ─────────────────────────────────────────────
    if content_bottom > 5:
        _hline(stdscr, content_bottom, width, junctions={left_w: BT, col3_x - 1: BT})

    # ── Column 1: Channels ───────────────────────────────────────────
    column_window = max(0, content_bottom - 4)
    channel_start, visible_channels = _window_slice(channels, state.channel_idx, column_window)
    line = 4
    for offset, channel in enumerate(visible_channels):
        if line >= content_bottom:
            break
        idx = channel_start + offset
        enabled = bool(channel.get("enabled", True))
        ch_icon = ICON_ON if enabled else ICON_OFF
        text = f" {ch_icon} {channel.get('kind', '')}:{channel.get('name', '')}"
        source = str(channel.get("channel_source", "state"))
        if source == "live":
            text += " [live]"
        elif source == "discovered":
            text += " [disc]"
        elif source == "stale":
            text += " [stale]"
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
    plugin_start, visible_plugins = _window_slice(plugins, state.plugin_idx, column_window)
    line = 4
    for offset, (key, enabled) in enumerate(visible_plugins):
        if line >= content_bottom:
            break
        idx = plugin_start + offset
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

    # ── Column 3: Settings & actions ─────────────────────────────────
    right_rows = settings
    right_selected_idx = state.setting_idx
    right_start, visible_right_rows = _window_slice(right_rows, right_selected_idx, column_window)
    line = 4
    for offset, item in enumerate(visible_right_rows):
        if line >= content_bottom:
            break
        idx = right_start + offset
        label = str(item.get("label", ""))
        kind = str(item.get("kind", ""))

        # Pick an icon based on item type
        if kind == "autostart":
            r_icon = ICON_ON if "on" in label else ICON_OFF
            label = label.replace("autostart: on", f"autostart {ICON_DOT} on")
            label = label.replace("autostart: off", f"autostart {ICON_DOT} off")
        elif kind.startswith("cred_bundle:"):
            r_icon = ICON_ON if "on" in label else ICON_OFF
        elif kind == "channel_status":
            r_icon = " "
        elif kind.startswith("channel_"):
            r_icon = ICON_PTR
        elif kind.startswith("service_") and kind not in ("service_status",):
            r_icon = ICON_PTR
        elif kind == "auth_login":
            r_icon = ICON_PTR
        elif kind.startswith("provider_switch:"):
            r_icon = ICON_PTR
        elif kind in {"cred_sync_now", "cred_revoke_now"}:
            r_icon = ICON_PTR
        elif kind == "prompt_edit":
            r_icon = ICON_PTR
        elif kind.startswith("prompt_"):
            r_icon = ICON_PTR
        else:
            r_icon = " "

        text = f" {r_icon} {label}"
        is_sel = focus == "settings" and idx == state.setting_idx
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


def _window_start(total: int, selected: int, window: int) -> int:
    if total <= 0 or window <= 0 or total <= window:
        return 0
    current = min(max(0, selected), total - 1)
    start = current - (window // 2)
    if start < 0:
        return 0
    return min(start, total - window)


def _window_slice(items: list[Any], selected: int, window: int) -> tuple[int, list[Any]]:
    start = _window_start(len(items), selected, window)
    if window <= 0:
        return (start, [])
    return (start, items[start : start + window])


def _selected_channel(channels: list[dict[str, Any]], selected: int) -> dict[str, Any] | None:
    if not channels:
        return None
    current = min(max(0, selected), len(channels) - 1)
    item = channels[current]
    return item if isinstance(item, dict) else None


def _selected_channel_label(channel: dict[str, Any] | None) -> str:
    if not channel:
        return "selected channel"
    kind = str(channel.get("kind", "")).strip().lower()
    name = str(channel.get("name", "")).strip()
    if kind and name:
        return f"{kind}:{name}"
    return "selected channel"


def _dashboard_provider_choices(service: Any, agent: dict[str, Any]) -> list[str]:
    current = str(agent.get("agent", {}).get("provider", "")).strip().lower()
    ordered: list[str] = []
    seen: set[str] = set()
    for item in [current] + list(getattr(service, "configured_provider_names", lambda: [])()):
        token = str(item or "").strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


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
                    "label": f"prompt: edit {name} ({len(content)} chars)",
                }
            )
    prompt_rows.append({"kind": "prompt_sync_from_disk", "label": "prompt: sync from disk"})
    prompt_rows.append({"kind": "prompt_write_to_disk", "label": "prompt: write to disk"})
    return prompt_rows


def _run_prompt_action(service: Any, state: DashboardState, item: dict[str, str]) -> bool:
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
                state.notice_error = False
                return False
            else:
                service.set_agent_core_prompt(state.selected_agent_id, prompt, updated, sync_to_disk=True)
                state.notice = f"updated {prompt}"
        else:
            state.notice = "unknown prompt action"
        state.notice_error = False
        return True
    except Exception as exc:  # noqa: BLE001
        state.notice = str(exc)
        state.notice_error = True
        return False


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


def _prompt_value(label: str, default: str = "") -> str | None:
    prompt = label.strip() or "value"
    try:
        curses.def_prog_mode()
    except curses.error:
        pass
    try:
        curses.endwin()
    except curses.error:
        pass
    try:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        return default or None
    finally:
        try:
            curses.reset_prog_mode()
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _prompt_channel_values(default_kind: str = "", default_name: str = "") -> tuple[str, str] | None:
    kind = _prompt_value("Channel kind", default_kind)
    if not kind:
        return None
    name = _prompt_value("Channel name", default_name)
    if not name:
        return None
    return (str(kind).strip(), str(name).strip())


def _focus_name(state: DashboardState) -> str:
    return DETAIL_FOCUS_NAMES[min(max(0, state.focus_idx), len(DETAIL_FOCUS_NAMES) - 1)]


def _run_channel_detail_action(
    service: Any,
    state: DashboardState,
    action: str,
    channel: dict[str, Any] | None = None,
) -> bool:
    if state.selected_agent_id.startswith("@local:"):
        state.notice = "channel management not supported for local-user claw"
        state.notice_error = False
        return False

    kind = str((channel or {}).get("kind", "")).strip().lower()
    name = str((channel or {}).get("name", "")).strip()
    source = str((channel or {}).get("channel_source", "")).strip().lower()
    try:
        if action == "add":
            payload = _prompt_channel_values()
            if payload is None:
                state.notice = "channel add cancelled"
                state.notice_error = False
                return False
            kind, name = payload
            service.assign_channel_to_agent("", kind, name, state.selected_agent_id)
            state.notice = f"added {kind}:{name}"
        elif action == "add_connect":
            payload = _prompt_channel_values()
            if payload is None:
                state.notice = "channel add cancelled"
                state.notice_error = False
                return False
            kind, name = payload
            service.connect_agent_channel(state.selected_agent_id, kind, name)
            state.notice = f"added + linked {kind}:{name}"
        elif action == "connect":
            if not kind or not name:
                state.notice = "select a channel to link"
                state.notice_error = False
                return False
            if source == "discovered":
                service.assign_channel_to_agent("", kind, name, state.selected_agent_id)
                state.notice = f"tracked live channel {kind}:{name}"
            else:
                service.connect_agent_channel(state.selected_agent_id, kind, name)
                state.notice = f"linked {kind}:{name}"
        elif action == "unlink":
            if not kind or not name:
                state.notice = "select a channel to unlink"
                state.notice_error = False
                return False
            if source == "discovered":
                state.notice = "channel is live but not tracked; sync from provider first"
                state.notice_error = False
                return False
            service.unassign_channel_from_agent(state.selected_agent_id, kind, name)
            state.channel_idx = max(0, state.channel_idx - 1)
            state.notice = f"unlinked {kind}:{name}"
        elif action == "sync":
            service.sync_agent_channels_from_provider(state.selected_agent_id)
            state.notice = "synced channels from provider"
        else:
            state.notice = "unknown channel action"
        state.notice_error = False
        return True
    except Exception as exc:  # noqa: BLE001
        state.notice = str(exc)
        state.notice_error = True
        return False


# ═════════════════════════════════════════════════════════════════════
#  Settings & Actions
# ═════════════════════════════════════════════════════════════════════


def _settings_items(
    agent: dict[str, Any],
    provider_choices: list[str] | None = None,
    addon_choices: list[dict[str, Any]] | None = None,
    selected_channel: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    info = agent.get("agent", {})
    is_local = bool(info.get("local_user", False))
    prefix = "(local) " if is_local else ""
    current_provider = str(info.get("provider", "")).strip().lower()
    auth_profile = str(info.get("auth_profile", "")).strip()
    auth_source = str(info.get("auth_source", "")).strip()
    provider_issue = str(info.get("provider_issue", "")).strip()
    provider_remediation = str(info.get("provider_remediation", "")).strip()
    auth_summary = f"{info.get('auth_status', 'unknown')} {ICON_DOT} {info.get('auth_mode', '')}"
    selected_channel_label = _selected_channel_label(selected_channel)
    if auth_profile:
        auth_summary += f" {ICON_DOT} {auth_profile}"
    elif auth_source:
        auth_summary += f" {ICON_DOT} {auth_source}"
    channel_rows: list[dict[str, str]] = []
    if not is_local:
        channel_rows = [
            {
                "kind": "channel_status",
                "label": (
                    f"channels: {info.get('channel_status_source', 'state')} {ICON_DOT} "
                    f"live {int(info.get('live_channel_count', 0))} {ICON_DOT} "
                    f"stale {int(info.get('stale_channel_count', 0))}"
                ),
            },
            {"kind": "channel_sync", "label": "channel: sync from provider"},
            {"kind": "channel_add", "label": "channel: add"},
            {"kind": "channel_add_connect", "label": "channel: add + link"},
            {"kind": "channel_connect", "label": f"channel: link {selected_channel_label}"},
            {"kind": "channel_unlink", "label": f"channel: unlink {selected_channel_label}"},
        ]
    switch_rows: list[dict[str, str]] = []
    if not is_local:
        for provider in provider_choices or []:
            token = str(provider).strip().lower()
            if not token or token == current_provider:
                continue
            switch_rows.append(
                {
                    "kind": f"provider_switch:{token}",
                    "label": f"switch provider {ICON_DOT} {token}",
                }
            )
    provider_rows: list[dict[str, str]] = [
        {"kind": "provider_current", "label": f"{prefix}provider: {current_provider or 'unknown'}"},
    ]
    if provider_issue:
        provider_rows.append({"kind": "provider_issue", "label": f"{prefix}provider issue: {provider_issue}"})
    if provider_remediation:
        provider_rows.append({"kind": "provider_fix", "label": f"{prefix}provider fix: {provider_remediation}"})
    rows: list[dict[str, str]] = channel_rows + [
        *provider_rows,
        *switch_rows,
        {"kind": "auth_status", "label": f"{prefix}auth: {auth_summary}"},
        {"kind": "auth_login", "label": f"{prefix}refresh / re-run login"},
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
        {"kind": "auth_mode", "label": f"{prefix}auth mode: {info.get('auth_mode', '')}"},
    ]
    if not is_local:
        rows.append(
            {
                "kind": "autostart",
                "label": f"{prefix}autostart: {'on' if bool(info.get('autostart', True)) else 'off'}",
            }
        )
    # Model tier
    current_tier = str(info.get("model_tier", "balanced"))
    tier_icon = _TIER_ICON_MAP.get(current_tier, "")
    rows.append(
        {
            "kind": "model_tier",
            "label": f"{prefix}model tier: {tier_icon}{current_tier}",
        }
    )
    rows.extend(_prompt_items(agent))
    if is_local:
        return rows
    credential_sync = agent.get("credential_sync", {})
    selected = {
        str(item).strip().lower()
        for item in credential_sync.get("bundles", [])
        if str(item).strip()
    }
    rows.extend(
        [
            {
                "kind": "cred_bundle:provider-auth",
                "label": f"cred provider-auth: {'on' if 'provider-auth' in selected else 'off'}",
            },
            {
                "kind": "cred_bundle:git",
                "label": f"cred git: {'on' if 'git' in selected else 'off'}",
            },
            {"kind": "cred_sync_now", "label": "sync credentials now"},
            {"kind": "cred_revoke_now", "label": "revoke selected credential access"},
            {
                "kind": "cred_last_sync",
                "label": f"cred last_sync: {str(credential_sync.get('last_synced_at', '')) or 'never'}",
            },
        ]
    )
    addon_access = agent.get("addon_access", {})
    addon_rows = []
    addon_map = {
        str(item.get("addon", "")).strip().lower(): item
        for item in addon_access.get("addons", [])
        if isinstance(item, dict)
    }
    agent_addons = agent.get("addons", {})
    for addon in addon_choices or []:
        addon_id = str(addon.get("id", "")).strip().lower()
        if not addon_id:
            continue
        item = addon_map.get(addon_id, {})
        enabled = bool(item.get("enabled", False))
        applied = bool(item.get("applied", False))
        auth_status = str(item.get("auth_status", "unknown"))

        if addon_id == "display":
            display_data = agent_addons.get("display", {}) if isinstance(agent_addons, dict) else {}
            display_num = display_data.get("display_number", "") if isinstance(display_data, dict) else ""
            novnc_port = display_data.get("novnc_port", "") if isinstance(display_data, dict) else ""
            display_detail = ""
            if display_num:
                display_detail = f" :{display_num} novnc={novnc_port}"
            addon_rows.extend(
                [
                    {
                        "kind": f"addon_status:{addon_id}",
                        "label": f"display: {'on' if enabled else 'off'}{display_detail}",
                    },
                    {
                        "kind": f"addon_enable:{addon_id}",
                        "label": "display: enable",
                    },
                    {
                        "kind": f"addon_disable:{addon_id}",
                        "label": "display: disable",
                    },
                ]
            )
            continue

        addon_rows.extend(
            [
                {
                    "kind": f"addon_status:{addon_id}",
                    "label": (
                        f"addon {addon_id}: {'on' if enabled else 'off'} {ICON_DOT} "
                        f"auth {auth_status} {ICON_DOT} {'applied' if applied else 'pending'}"
                    ),
                },
                {
                    "kind": f"addon_enable:{addon_id}",
                    "label": f"addon {addon_id}: enable",
                },
                {
                    "kind": f"addon_disable:{addon_id}",
                    "label": f"addon {addon_id}: disable",
                },
                {
                    "kind": f"addon_apply:{addon_id}",
                    "label": f"addon {addon_id}: apply",
                },
                {
                    "kind": f"addon_login:{addon_id}",
                    "label": f"addon {addon_id}: shared login",
                },
            ]
        )
    rows.extend(addon_rows)
    return rows


def _run_setting_action(service: Any, state: DashboardState, item: dict[str, str]) -> bool:
    kind = str(item.get("kind", ""))
    is_local = state.selected_agent_id.startswith("@local:")
    provider = state.selected_agent_id.split(":", 1)[1] if is_local else ""
    try:
        if kind.startswith("channel_"):
            channel = None
            try:
                agent = _load_detail_agent(state, service)
                channel = _selected_channel(agent.get("channels", []), state.channel_idx)
            except Exception:  # noqa: BLE001
                channel = None
            action_map = {
                "channel_add": "add",
                "channel_add_connect": "add_connect",
                "channel_connect": "connect",
                "channel_unlink": "unlink",
                "channel_sync": "sync",
            }
            action = action_map.get(kind)
            if action:
                return _run_channel_detail_action(service, state, action, channel)
        if kind.startswith("prompt_"):
            return _run_prompt_action(service, state, item)
        if kind == "model_tier":
            new_tier = service.set_agent_model_tier(state.selected_agent_id, "")
            state.notice = f"model tier changed to {new_tier}"
            return True
        if kind == "autostart":
            if is_local:
                state.notice = "autostart not applicable for local-user claw"
            else:
                service.toggle_agent_autostart(state.selected_agent_id)
                state.notice = "autostart toggled"
        elif kind == "auth_login":
            result = service.agent_auth_login(state.selected_agent_id)
            action = str(result.get("action_performed", "login"))
            if action == "status":
                state.notice = "auth already ready"
            elif action == "refresh":
                state.notice = "auth refreshed"
            else:
                state.notice = "auth login completed"
        elif kind.startswith("provider_switch:"):
            if is_local:
                state.notice = "provider switching not supported for local-user claw"
            else:
                target = kind.split(":", 1)[1]
                runner = getattr(service, "switch_agent_provider", None)
                if callable(runner):
                    result = runner(state.selected_agent_id, target)
                    changed = bool(result.get("changed", True))
                else:
                    service.set_agent_provider(state.selected_agent_id, target)
                    changed = True
                state.notice = (
                    f"provider changed to {target}" if changed else f"provider reconciled for {target}"
                )
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
        elif kind.startswith("cred_bundle:"):
            if is_local:
                state.notice = "credential bundle policy not applicable for local-user claw"
            else:
                bundle = kind.split(":", 1)[1]
                service.toggle_agent_credential_bundle(state.selected_agent_id, bundle)
                state.notice = f"credential bundle toggled: {bundle}"
        elif kind == "cred_sync_now":
            if is_local:
                state.notice = "credential sync not applicable for local-user claw"
            else:
                result = service.sync_agent_credentials(state.selected_agent_id)
                state.notice = f"credentials synced ({len(result.get('copied_paths', []))} paths)"
        elif kind == "cred_revoke_now":
            if is_local:
                state.notice = "credential revoke not applicable for local-user claw"
            else:
                result = service.revoke_agent_credentials(state.selected_agent_id)
                state.notice = f"credentials revoked ({len(result.get('removed_paths', []))} paths)"
        elif kind.startswith("addon_enable:"):
            if is_local:
                state.notice = "addon management not supported for local-user claw"
            else:
                addon = kind.split(":", 1)[1]
                result = service.enable_agent_addon(state.selected_agent_id, addon)
                if result.get("pending", False):
                    state.notice = f"addon {addon} enabled (pending home apply)"
                else:
                    state.notice = f"addon {addon} enabled"
        elif kind.startswith("addon_disable:"):
            if is_local:
                state.notice = "addon management not supported for local-user claw"
            else:
                addon = kind.split(":", 1)[1]
                service.disable_agent_addon(state.selected_agent_id, addon)
                state.notice = f"addon {addon} disabled"
        elif kind.startswith("addon_apply:"):
            if is_local:
                state.notice = "addon management not supported for local-user claw"
            else:
                addon = kind.split(":", 1)[1]
                service.apply_agent_addons(state.selected_agent_id, addons=[addon])
                state.notice = f"addon {addon} applied"
        elif kind.startswith("addon_login:"):
            addon = kind.split(":", 1)[1]
            result = service.shared_addon_auth_login(addon)
            action = str(result.get("action_performed", "login"))
            if action == "status":
                state.notice = f"addon {addon} auth already ready"
            else:
                state.notice = f"addon {addon} login completed"
        else:
            state.notice = "read-only setting"
        state.notice_error = False
        return kind not in {
            "provider_current",
            "provider_issue",
            "provider_fix",
            "auth_status",
            "service_status",
            "heartbeat",
            "auth_mode",
            "cred_last_sync",
            "channel_status",
        } and not kind.startswith("addon_status:")
    except Exception as exc:  # noqa: BLE001
        state.notice = str(exc)
        state.notice_error = True
        return False


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
        provider = str(row.get("provider", ""))
        if str(row.get("provider_status", "ok")) != "ok" and provider:
            provider = f"{provider}!"
        rows.append(
            [
                _display_agent_id(str(row.get("agent_id", row.get("user_id", "")))),
                row.get("display_name", ""),
                provider,
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
