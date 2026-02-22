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
        snapshot = service.performance_snapshot(agent_id=None if state.view == "detail" else agent_id, refresh=True)
        _sync_selection(state, snapshot)
        _draw(stdscr, snapshot, state, service)

        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), ord("Q")):
            return
        if key in (ord("r"), ord("R")):
            continue

        if state.view == "overview":
            _handle_overview_key(key, state, snapshot, service)
        else:
            _handle_detail_key(key, state, service)


def _handle_overview_key(key: int, state: DashboardState, snapshot: dict[str, Any], service: Any) -> None:
    if key in (ord("v"), ord("V")):
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
    elif key in (curses.KEY_ENTER, 10, 13):
        if rows:
            state.view = "detail"


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

    if key == 9:  # TAB
        state.overview_focus_idx = (state.overview_focus_idx + 1) % 3
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
        state.selected_agent_id = target_agent_id
        state.view = "detail"


def _handle_detail_key(key: int, state: DashboardState, service: Any) -> None:
    if state.purge_confirm:
        if key in (ord("y"), ord("Y")):
            try:
                service.purge_agent(state.selected_agent_id)
            except Exception:
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
    except Exception:
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
    if key in (ord("d"), ord("D")):
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


def _draw(stdscr: Any, snapshot: dict[str, Any], state: DashboardState, service: Any) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    _draw_header(stdscr, snapshot, width)
    _draw_stats(stdscr, snapshot, width)

    if state.view == "overview":
        _draw_overview(stdscr, snapshot, state, service, height, width)
        if state.overview_mode == "agents":
            footer = "q quit | v channels view | Enter details | j/k move | r refresh"
        else:
            footer = "q quit | v agents view | Tab pane | j/k move | a assign | u unassign | c connect | Enter details | r refresh"
    else:
        _draw_detail(stdscr, service, state, height, width)
        if state.purge_confirm:
            footer = "CONFIRM PURGE: y confirm | n cancel"
        else:
            footer = "q quit | b back | Tab section | j/k move | Space/Enter action | prompts: edit/sync/write | a autostart | d purge | r refresh"

    _add(stdscr, height - 1, 0, _fit(footer, width), _color(3))
    stdscr.refresh()


def _draw_header(stdscr: Any, snapshot: dict[str, Any], width: int) -> None:
    title = "CLAWIE DASHBOARD"
    meta = f"provider={snapshot.get('provider', '')} workspace={snapshot.get('workspace', '')}"
    line = f"{title}  |  {meta}"
    _add(stdscr, 0, 0, _fit(line, width), _color(1))


def _draw_stats(stdscr: Any, snapshot: dict[str, Any], width: int) -> None:
    totals = snapshot.get("totals", {})
    line = (
        f"agents {totals.get('agents', 0)}  "
        f"channels {totals.get('channels', 0)}  "
        f"migrated {totals.get('migrated_channels', 0)}  "
        f"cpu {totals.get('cpu_percent', 0)}%  mem {totals.get('mem_percent', 0)}%  "
        f"generated {snapshot.get('generated_at', '')}"
    )
    _add(stdscr, 1, 0, _fit(line, width), _color(0))
    _add(stdscr, 2, 0, "=" * max(0, width - 1), _color(4))
    _add(stdscr, 3, 0, _fit("Legend: @<provider> = local claw for current Linux user", width), _color(3))


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


def _draw_overview_agents(stdscr: Any, snapshot: dict[str, Any], state: DashboardState, height: int, width: int) -> None:
    rows = snapshot.get("rows", [])
    split = max(40, int(width * 0.68))
    split = min(split, width - 20)
    table_bottom = height - 4

    headers = ["agent", "status", "cpu", "mem", "channels", "provider"]
    _add(stdscr, 4, 0, _fit(" ".join(h.ljust(12) for h in headers), split), _color(4))

    line = 5
    for idx, row in enumerate(rows):
        if line >= table_bottom:
            break
        channels_text = f"{row.get('channels', 0)}/{row.get('channels_total', 0)}"
        text = " ".join(
            [
                _display_agent_id(str(row.get("agent_id", "")))[:12].ljust(12),
                str(row.get("status", ""))[:12].ljust(12),
                f"{float(row.get('cpu_percent', 0.0)):.1f}".ljust(12),
                f"{float(row.get('mem_percent', 0.0)):.1f}".ljust(12),
                channels_text.ljust(12),
                str(row.get("provider", ""))[:12].ljust(12),
            ]
        )
        color = _color(5) if idx == state.selected_row else _color(0)
        _add(stdscr, line, 0, _fit(text, split), color)
        line += 1

    for y in range(4, height - 1):
        _add(stdscr, y, split, "|", _color(4))

    pane_x = split + 2
    if rows:
        selected = rows[state.selected_row]
        _add(stdscr, 4, pane_x, "Selected Agent", _color(1))
        _add(stdscr, 5, pane_x, f"id: {_display_agent_id(str(selected.get('agent_id', '')))}", _color(0))
        _add(stdscr, 6, pane_x, f"display: {selected.get('display_name', '')}", _color(0))
        _add(stdscr, 7, pane_x, f"provider: {selected.get('provider', '')}", _color(0))
        _add(stdscr, 8, pane_x, f"status: {selected.get('status', '')}", _color(0))
        _add(stdscr, 9, pane_x, f"version: {selected.get('version', '')}", _color(0))
        _add(stdscr, 10, pane_x, f"strategy: {selected.get('strategy', '')}", _color(0))

    events = snapshot.get("events", [])
    _add(stdscr, 12, pane_x, "Recent Events", _color(1))
    ev_line = 13
    for event in events[: max(0, height - 15)]:
        text = f"{event.get('timestamp', '')} | {event.get('type', '')}"
        _add(stdscr, ev_line, pane_x, _fit(text, width - pane_x), _color(0))
        ev_line += 1


def _draw_overview_channels(
    stdscr: Any,
    snapshot: dict[str, Any],
    state: DashboardState,
    service: Any,
    height: int,
    width: int,
) -> None:
    _ = snapshot
    col_w = max(24, int((width - 4) / 3))
    col2_x = col_w + 2
    col3_x = (col_w * 2) + 3
    rows_max = max(0, height - 10)

    channels: list[dict[str, Any]] = []
    try:
        inventory = service.channel_inventory()
        channels = list(inventory.get("rows", []))
    except Exception:
        channels = []
    agent_rows = [row for row in snapshot.get("rows", []) if not str(row.get("agent_id", "")).startswith("@local:")]
    state.selected_target_row = min(state.selected_target_row, max(0, len(agent_rows) - 1))
    selected_agent_id = str(agent_rows[state.selected_target_row].get("agent_id", "")) if agent_rows else ""
    available = [row for row in channels if str(row.get("source", "")) in {"local", "pool"}]
    assigned = [row for row in channels if str(row.get("owner_agent_id", "")).strip() == selected_agent_id]
    state.selected_available_row = min(state.selected_available_row, max(0, len(available) - 1))
    state.selected_assigned_row = min(state.selected_assigned_row, max(0, len(assigned) - 1))

    _add(stdscr, 4, 0, "Channels View", _color(1))
    _add(stdscr, 5, 0, "Select an agent, assign from Available, unassign from Assigned, then connect.", _color(0))

    _add(stdscr, 7, 0, f"Target Agents [{'ACTIVE' if state.overview_focus_idx == 0 else '      '}]", _color(4))
    _add(stdscr, 7, col2_x, f"Available Channels [{'ACTIVE' if state.overview_focus_idx == 1 else '      '}]", _color(4))
    _add(stdscr, 7, col3_x, f"Assigned Channels [{'ACTIVE' if state.overview_focus_idx == 2 else '      '}]", _color(4))

    for y in range(6, height - 1):
        _add(stdscr, y, col_w, "|", _color(4))
        _add(stdscr, y, col3_x - 2, "|", _color(4))

    line = 8
    if not agent_rows:
        _add(stdscr, 9, 0, "no managed agents", _color(3))
    for idx, row in enumerate(agent_rows[:rows_max]):
        prefix = "> " if idx == state.selected_target_row else "  "
        text = f"{prefix}{_display_agent_id(str(row.get('agent_id', '')))} [{row.get('status', '')}] {row.get('provider', '')}"
        color = _color(5) if state.overview_focus_idx == 0 and idx == state.selected_target_row else _color(0)
        _add(stdscr, line, 0, _fit(text, col_w - 1), color)
        line += 1

    line = 8
    if not available:
        _add(stdscr, 9, col2_x, "no available channels", _color(3))
    for idx, channel in enumerate(available[:rows_max]):
        source = str(channel.get("source", ""))
        badge = "pool" if source == "pool" else "local"
        text = f"{channel.get('kind', '')}:{channel.get('name', '')} [{badge}]"
        color = _color(5) if state.overview_focus_idx == 1 and idx == state.selected_available_row else _color(0)
        _add(stdscr, line, col2_x, _fit(text, col_w - 1), color)
        line += 1

    line = 8
    if not assigned:
        _add(stdscr, 9, col3_x, "no channels assigned", _color(3))
    for idx, channel in enumerate(assigned[:rows_max]):
        enabled = "on" if bool(channel.get("enabled", True)) else "off"
        text = f"{channel.get('kind', '')}:{channel.get('name', '')} [{enabled}]"
        color = _color(5) if state.overview_focus_idx == 2 and idx == state.selected_assigned_row else _color(0)
        _add(stdscr, line, col3_x, _fit(text, width - col3_x), color)
        line += 1

    if state.notice:
        color = _color(2) if state.notice_error else _color(1)
        _add(stdscr, height - 3, 0, _fit(f"notice: {state.notice}", width), color)


def _draw_detail(stdscr: Any, service: Any, state: DashboardState, height: int, width: int) -> None:
    try:
        agent = service.get_dashboard_agent(state.selected_agent_id)
    except Exception as exc:  # noqa: BLE001
        _add(stdscr, 4, 0, f"Failed loading agent: {exc}", _color(2))
        return

    agent_info = agent.get("agent", {})
    channels = agent.get("channels", [])
    plugins = sorted(agent_info.get("plugins", {}).items())
    prompts = _prompt_items(agent)
    focus_names = ["channels", "plugins", "prompts", "settings"]
    state.focus_idx = min(state.focus_idx, len(focus_names) - 1)
    focus = focus_names[state.focus_idx]

    _add(stdscr, 3, 0, f"Agent Detail: {_display_agent_id(state.selected_agent_id)}", _color(1))
    _add(stdscr, 4, 0, f"provider={agent_info.get('provider', '')}  status={agent_info.get('status', '')}  version={agent_info.get('version', '')}", _color(0))
    _add(stdscr, 5, 0, f"sections: {' | '.join(focus_names)}", _color(3))
    if state.purge_confirm:
        _add(stdscr, 6, 0, "Purge requested: press 'y' to permanently delete agent + Linux user, or 'n' to cancel.", _color(2))

    left_w = max(36, int(width * 0.45))
    mid_w = max(28, int(width * 0.30))
    right_x = left_w + mid_w + 2

    _add(stdscr, 7, 0, f"Channels [{'ACTIVE' if focus == 'channels' else '      '}]")
    line = 8
    for idx, channel in enumerate(channels[: max(0, height - 11)]):
        marker = "[x]" if bool(channel.get("enabled", True)) else "[ ]"
        text = f"{marker} {channel.get('kind', '')}:{channel.get('name', '')}"
        color = _color(5) if focus == "channels" and idx == state.channel_idx else _color(0)
        _add(stdscr, line, 0, _fit(text, left_w - 1), color)
        line += 1

    for y in range(7, height - 1):
        _add(stdscr, y, left_w, "|", _color(4))

    _add(stdscr, 7, left_w + 2, f"Plugins [{'ACTIVE' if focus == 'plugins' else '      '}]")
    line = 8
    for idx, item in enumerate(plugins[: max(0, height - 11)]):
        key, enabled = item
        marker = "[x]" if bool(enabled) else "[ ]"
        text = f"{marker} {key}"
        color = _color(5) if focus == "plugins" and idx == state.plugin_idx else _color(0)
        _add(stdscr, line, left_w + 2, _fit(text, mid_w - 2), color)
        line += 1

    for y in range(7, height - 1):
        _add(stdscr, y, left_w + mid_w + 1, "|", _color(4))

    if focus == "prompts":
        _add(stdscr, 7, right_x, "Prompts [ACTIVE]", _color(1))
        right_rows = prompts
    else:
        _add(stdscr, 7, right_x, f"Settings [{'ACTIVE' if focus == 'settings' else '      '}]")
        right_rows = _settings_items(agent)
    line = 8
    for idx, item in enumerate(right_rows):
        text = str(item.get("label", ""))
        selected = (focus == "prompts" and idx == state.prompt_idx) or (focus == "settings" and idx == state.setting_idx)
        color = _color(5) if selected else _color(0)
        _add(stdscr, line, right_x, _fit(text, width - right_x), color)
        line += 1
    if state.notice:
        color = _color(2) if state.notice_error else _color(1)
        _add(stdscr, min(height - 3, line + 1), 0, _fit(f"notice: {state.notice}", width), color)


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_BLUE, -1)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)


def _color(idx: int) -> int:
    if not curses.has_colors():
        return curses.A_NORMAL
    return curses.color_pair(idx)


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
    return text[: max(0, width - 4)] + "..."


def _display_agent_id(agent_id: str) -> str:
    token = str(agent_id).strip()
    if token.startswith("@local:"):
        provider = token.split(":", 1)[1]
        return f"@{provider}" if provider else "@"
    return token


def _print_static(snapshot: dict[str, Any]) -> None:
    totals = snapshot["totals"]
    print_info(
        "Clawie Monitor "
        f"provider={snapshot.get('provider', '')} workspace={snapshot['workspace']} agents={totals['agents']} "
        f"channels={totals['channels']} migrated={totals['migrated_channels']} "
        f"cpu={totals.get('cpu_percent', 0)}% mem={totals.get('mem_percent', 0)}%"
    )

    rows = []
    for row in snapshot["rows"]:
        rows.append(
            [
                _display_agent_id(str(row.get("agent_id", row.get("user_id", "")))),
                row.get("display_name", ""),
                row.get("provider", ""),
                row.get("status", ""),
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
        print("\nRecent events:")
        for event in events:
            print(f"- {event.get('timestamp', '')} {event.get('type', '')} {event.get('message', '')}")


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


def _settings_items(agent: dict[str, Any]) -> list[dict[str, str]]:
    info = agent.get("agent", {})
    is_local = bool(info.get("local_user", False))
    prefix = "(local-user) " if is_local else ""
    return [
        {
            "kind": "autostart",
            "label": f"{prefix}autostart: {'on' if bool(info.get('autostart', True)) else 'off'} (toggle)",
        },
        {
            "kind": "service_status",
            "label": f"{prefix}service_status: {info.get('service_status', 'unknown')} mode={info.get('service_mode', 'unknown')}",
        },
        {"kind": "service_start", "label": f"{prefix}service start"},
        {"kind": "service_stop", "label": f"{prefix}service stop"},
        {"kind": "service_restart", "label": f"{prefix}service restart"},
        {"kind": "service_status_refresh", "label": f"{prefix}service status (refresh)"},
        {
            "kind": "heartbeat",
            "label": f"{prefix}heartbeat_seconds: {int(info.get('heartbeat_seconds', 30))}",
        },
        {"kind": "auth_mode", "label": f"{prefix}auth_mode: {info.get('auth_mode', '')}"},
    ]


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
        result = subprocess.run([editor, path], check=False)
        curses.reset_prog_mode()
        curses.curs_set(0)
        if result.returncode != 0:
            return None
        return Path(path).read_text(encoding="utf-8")
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


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
