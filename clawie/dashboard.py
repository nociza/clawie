from __future__ import annotations

import curses
import sys
from dataclasses import dataclass
from typing import Any

from clawie.ui import print_info, print_table


@dataclass
class DashboardState:
    view: str = "overview"
    selected_row: int = 0
    selected_agent_id: str = ""
    focus_idx: int = 0
    channel_idx: int = 0
    plugin_idx: int = 0
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
            _handle_overview_key(key, state, snapshot)
        else:
            _handle_detail_key(key, state, service)


def _handle_overview_key(key: int, state: DashboardState, snapshot: dict[str, Any]) -> None:
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

    focus_names = ["channels", "plugins", "settings"]
    focus = focus_names[state.focus_idx]

    try:
        agent = service.get_dashboard_agent(state.selected_agent_id)
    except Exception:
        return

    channels = agent.get("channels", [])
    plugins = sorted(agent.get("agent", {}).get("plugins", {}).items())
    state.channel_idx = min(state.channel_idx, max(0, len(channels) - 1))
    state.plugin_idx = min(state.plugin_idx, max(0, len(plugins) - 1))
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
        else:
            state.setting_idx = min(2, state.setting_idx + 1)
        return

    if key in (curses.KEY_UP, ord("k")):
        if focus == "channels":
            state.channel_idx = max(0, state.channel_idx - 1)
        elif focus == "plugins":
            state.plugin_idx = max(0, state.plugin_idx - 1)
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
        _draw_overview(stdscr, snapshot, state, height, width)
        footer = "q quit | Enter details | j/k move | r refresh"
    else:
        _draw_detail(stdscr, service, state, height, width)
        if state.purge_confirm:
            footer = "CONFIRM PURGE: y confirm | n cancel"
        else:
            footer = "q quit | b back | Tab section | j/k move | Space/Enter action | a autostart | d purge | r refresh"

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


def _draw_overview(stdscr: Any, snapshot: dict[str, Any], state: DashboardState, height: int, width: int) -> None:
    rows = snapshot.get("rows", [])
    split = max(40, int(width * 0.68))
    split = min(split, width - 20)
    table_bottom = height - 4

    headers = ["agent", "status", "cpu", "mem", "channels", "provider"]
    _add(stdscr, 3, 0, _fit(" ".join(h.ljust(12) for h in headers), split), _color(4))

    line = 4
    for idx, row in enumerate(rows):
        if line >= table_bottom:
            break
        channels_text = f"{row.get('channels', 0)}/{row.get('channels_total', 0)}"
        text = " ".join(
            [
                str(row.get("agent_id", ""))[:12].ljust(12),
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

    for y in range(3, height - 1):
        _add(stdscr, y, split, "|", _color(4))

    pane_x = split + 2
    if rows:
        selected = rows[state.selected_row]
        _add(stdscr, 3, pane_x, "Selected Agent", _color(1))
        _add(stdscr, 4, pane_x, f"id: {selected.get('agent_id', '')}", _color(0))
        _add(stdscr, 5, pane_x, f"display: {selected.get('display_name', '')}", _color(0))
        _add(stdscr, 6, pane_x, f"provider: {selected.get('provider', '')}", _color(0))
        _add(stdscr, 7, pane_x, f"status: {selected.get('status', '')}", _color(0))
        _add(stdscr, 8, pane_x, f"version: {selected.get('version', '')}", _color(0))
        _add(stdscr, 9, pane_x, f"strategy: {selected.get('strategy', '')}", _color(0))

    events = snapshot.get("events", [])
    _add(stdscr, 11, pane_x, "Recent Events", _color(1))
    ev_line = 12
    for event in events[: max(0, height - 15)]:
        text = f"{event.get('timestamp', '')} | {event.get('type', '')}"
        _add(stdscr, ev_line, pane_x, _fit(text, width - pane_x), _color(0))
        ev_line += 1


def _draw_detail(stdscr: Any, service: Any, state: DashboardState, height: int, width: int) -> None:
    try:
        agent = service.get_dashboard_agent(state.selected_agent_id)
    except Exception as exc:  # noqa: BLE001
        _add(stdscr, 4, 0, f"Failed loading agent: {exc}", _color(2))
        return

    agent_info = agent.get("agent", {})
    channels = agent.get("channels", [])
    plugins = sorted(agent_info.get("plugins", {}).items())
    focus_names = ["channels", "plugins", "settings"]
    focus = focus_names[state.focus_idx]

    _add(stdscr, 3, 0, f"Agent Detail: {state.selected_agent_id}", _color(1))
    _add(stdscr, 4, 0, f"provider={agent_info.get('provider', '')}  status={agent_info.get('status', '')}  version={agent_info.get('version', '')}", _color(0))
    if state.purge_confirm:
        _add(stdscr, 5, 0, "Purge requested: press 'y' to permanently delete agent + Linux user, or 'n' to cancel.", _color(2))

    left_w = max(36, int(width * 0.45))
    mid_w = max(28, int(width * 0.30))
    right_x = left_w + mid_w + 2

    _add(stdscr, 6, 0, f"Channels [{'ACTIVE' if focus == 'channels' else '      '}]")
    line = 7
    for idx, channel in enumerate(channels[: max(0, height - 10)]):
        marker = "[x]" if bool(channel.get("enabled", True)) else "[ ]"
        text = f"{marker} {channel.get('kind', '')}:{channel.get('name', '')}"
        color = _color(5) if focus == "channels" and idx == state.channel_idx else _color(0)
        _add(stdscr, line, 0, _fit(text, left_w - 1), color)
        line += 1

    for y in range(6, height - 1):
        _add(stdscr, y, left_w, "|", _color(4))

    _add(stdscr, 6, left_w + 2, f"Plugins [{'ACTIVE' if focus == 'plugins' else '      '}]")
    line = 7
    for idx, item in enumerate(plugins[: max(0, height - 10)]):
        key, enabled = item
        marker = "[x]" if bool(enabled) else "[ ]"
        text = f"{marker} {key}"
        color = _color(5) if focus == "plugins" and idx == state.plugin_idx else _color(0)
        _add(stdscr, line, left_w + 2, _fit(text, mid_w - 2), color)
        line += 1

    for y in range(6, height - 1):
        _add(stdscr, y, left_w + mid_w + 1, "|", _color(4))

    _add(stdscr, 6, right_x, f"Settings [{'ACTIVE' if focus == 'settings' else '      '}]")
    setting_rows = _settings_items(agent)
    line = 7
    for idx, item in enumerate(setting_rows):
        text = str(item["label"])
        color = _color(5) if focus == "settings" and idx == state.setting_idx else _color(0)
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
                row.get("agent_id", row.get("user_id", "")),
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
