from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from clawie.dashboard import run_dashboard
from clawie.providers import get_provider, provider_names
from clawie.service import (
    SetupError,
    AgentExistsError,
    AgentNotFoundError,
    ZeroClawService,
)
from clawie.store import DEFAULT_CONFIG, StateStore
from clawie.ui import (
    print_error,
    print_info,
    print_panel,
    print_success,
    print_table,
    print_warning,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawie",
        description="Clawie Linux CLI + dashboard control plane",
    )
    parser.add_argument(
        "--config-dir",
        help="Override state directory (default: ~/.clawie)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="Configure provider/workspace")
    setup.add_argument(
        "--provider",
        choices=provider_names(),
        default="zeroclaw",
        help="Agent provider",
    )
    setup.add_argument("--api-key", help="Provider API key (if using api_key auth)")
    setup.add_argument(
        "--auth-mode",
        choices=["linked", "api_key", "none"],
        help="Provider auth mode (default is provider-specific)",
    )
    setup.add_argument("--subscription", default="starter", help="Plan name")
    setup.add_argument("--workspace", default="default", help="Workspace slug")
    setup.add_argument(
        "--spawn-password",
        help="Set a global default Linux password for future spawned users",
    )
    setup.add_argument(
        "--clear-spawn-password",
        action="store_true",
        help="Clear the global default spawn password",
    )
    setup.add_argument(
        "--api-url",
        default=str(DEFAULT_CONFIG["api_url"]),
        help="API base URL",
    )
    setup.add_argument(
        "--install-runtime",
        action="store_true",
        help="Record local runtime as installed",
    )
    setup.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for values interactively",
    )
    setup.add_argument("--status", action="store_true", help="Show setup status only")
    setup.set_defaults(func=cmd_setup)

    agents = subparsers.add_parser("agents", help="Provision and manage agents")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)

    agents_create = agents_sub.add_parser("create", help="Create an agent")
    agents_create.add_argument("--agent-id", required=True, help="Unique agent ID")
    agents_create.add_argument("--display-name", help="Display name")
    agents_create.add_argument("--template", default="baseline", help="Template name")
    agents_create.add_argument(
        "--clone-from",
        help="Clone channels/defaults from source agent ID",
    )
    agents_create.add_argument(
        "--channel-strategy",
        choices=["new", "migrate"],
        default="new",
        help="Use minted channel names or migrated channel names",
    )
    agents_create.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="KIND:NAME",
        help="Add a channel definition (repeatable)",
    )
    agents_create.add_argument(
        "--channels-file",
        help="JSON file with channel definitions: [{\"kind\": ..., \"name\": ...}]",
    )
    agents_create.add_argument("--agent-version", default="1.0.0", help="Agent version")
    agents_create.add_argument(
        "--provider",
        choices=provider_names(),
        help="Override provider for this agent",
    )
    agents_create.set_defaults(func=cmd_agents_create)

    agents_clone = agents_sub.add_parser(
        "clone",
        help="One-click agent clone from an existing agent config",
    )
    agents_clone.add_argument("--from-agent", required=True, help="Existing source agent")
    agents_clone.add_argument("--agent-id", required=True, help="New agent ID")
    agents_clone.add_argument("--display-name", help="Display name")
    agents_clone.add_argument(
        "--channel-strategy",
        choices=["new", "migrate"],
        default="migrate",
        help="Use copied channels as-is (migrate) or mint new names (new)",
    )
    agents_clone.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="KIND:NAME",
        help="Override cloned channels with explicit definitions (repeatable)",
    )
    agents_clone.add_argument(
        "--channels-file",
        help="JSON file with channel definitions to override clone channels",
    )
    agents_clone.add_argument("--agent-version", default="1.0.0", help="Agent version")
    agents_clone.add_argument(
        "--provider",
        choices=provider_names(),
        help="Override provider for this cloned agent",
    )
    agents_clone.set_defaults(func=cmd_agents_clone)

    agents_list = agents_sub.add_parser("list", help="List agents")
    agents_list.set_defaults(func=cmd_agents_list)

    agents_show = agents_sub.add_parser("show", help="Show one agent")
    agents_show.add_argument("--agent-id", required=True)
    agents_show.set_defaults(func=cmd_agents_show)

    agents_delete = agents_sub.add_parser("delete", help="Delete one agent")
    agents_delete.add_argument("--agent-id", required=True)
    agents_delete.set_defaults(func=cmd_agents_delete)

    agents_batch = agents_sub.add_parser(
        "batch-create",
        help="Create agents from JSON array entries",
    )
    agents_batch.add_argument("--file", required=True, help="Input JSON file")
    agents_batch.set_defaults(func=cmd_agents_batch_create)

    list_alias = subparsers.add_parser("list", help="List agents (alias for `agents list`)")
    list_alias.set_defaults(func=cmd_list_alias)

    channels = subparsers.add_parser("channels", help="Channel operations")
    channels_sub = channels.add_subparsers(dest="channels_command", required=True)

    channels_bootstrap = channels_sub.add_parser(
        "bootstrap",
        help="Apply a channel preset to an agent",
    )
    channels_bootstrap.add_argument("--agent-id", required=True)
    channels_bootstrap.add_argument(
        "--preset",
        choices=["minimal", "growth", "enterprise"],
        default="growth",
    )
    channels_bootstrap.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing channels instead of merging",
    )
    channels_bootstrap.set_defaults(func=cmd_channels_bootstrap)

    channels_migrate = channels_sub.add_parser(
        "migrate",
        help="Copy channels from one agent to another",
    )
    channels_migrate.add_argument("--from-agent", required=True)
    channels_migrate.add_argument("--to-agent", required=True)
    channels_migrate.add_argument(
        "--replace",
        action="store_true",
        help="Replace destination channels instead of merging",
    )
    channels_migrate.set_defaults(func=cmd_channels_migrate)

    spawn = subparsers.add_parser(
        "spawn",
        help="Create a Linux user and provision a matching Clawie agent",
    )
    spawn.add_argument("--agent-id", required=True, help="Clawie agent ID")
    spawn.add_argument(
        "--linux-user",
        help="Linux username (defaults to agent-id)",
    )
    spawn.add_argument("--template", default="baseline", help="Template name")
    spawn.add_argument("--agent-version", default="1.0.0", help="Agent version")
    spawn.add_argument(
        "--provider",
        choices=provider_names(),
        help="Provider for the spawned agent (defaults to current setup provider)",
    )
    spawn.add_argument(
        "--source-home",
        help="Source home directory to copy configs from (default: current home)",
    )
    spawn.add_argument(
        "--password",
        help="Set plaintext Linux password for this spawned user only",
    )
    spawn.add_argument(
        "--password-hash",
        help="Set pre-hashed Linux password for this spawned user only (shadow-compatible)",
    )
    spawn.add_argument(
        "--no-global-password",
        action="store_true",
        help="Do not apply global default spawn password",
    )
    spawn.add_argument(
        "--skip-config-copy",
        action="store_true",
        help="Do not copy current user config files to the new Linux user",
    )
    spawn.set_defaults(func=cmd_spawn)

    purge = subparsers.add_parser(
        "purge",
        help="Delete an agent and remove its spawned Linux user/home profile",
    )
    purge.add_argument("agent_id", nargs="?", help="Agent ID to purge")
    purge.add_argument(
        "--agent-id",
        dest="agent_id_flag",
        help="Agent ID to purge (backward-compatible flag form)",
    )
    purge.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt",
    )
    purge.set_defaults(func=cmd_purge)

    monitor = subparsers.add_parser("monitor", help="htop-like agent performance monitor")
    monitor.add_argument("--agent-id", help="Filter monitor to one agent")
    monitor.add_argument("--refresh-seconds", type=int, default=2)
    monitor.set_defaults(func=cmd_monitor)

    claws = subparsers.add_parser("claws", help="Discover and inspect installed claw runtimes")
    claws_sub = claws.add_subparsers(dest="claws_command", required=True)
    claws_detect = claws_sub.add_parser("detect", help="Detect installed claw runtimes")
    claws_detect.add_argument(
        "--source-home",
        help="Home directory to inspect (default: current user home)",
    )
    claws_detect.set_defaults(func=cmd_claws_detect)

    dashboard = subparsers.add_parser("dashboard", help="Unified agent dashboard")
    dashboard.add_argument("--agent-id", help="Filter dashboard to one agent")
    dashboard.add_argument("--refresh-seconds", type=int, default=2)
    dashboard.set_defaults(func=cmd_monitor)

    doctor = subparsers.add_parser("doctor", help="Run health checks")
    doctor.set_defaults(func=cmd_doctor)

    events = subparsers.add_parser("events", help="Event stream")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    events_list = events_sub.add_parser("list", help="List recent events")
    events_list.add_argument("--limit", type=int, default=20)
    events_list.set_defaults(func=cmd_events_list)

    state = subparsers.add_parser("state", help="Export/import local state snapshots")
    state_sub = state.add_subparsers(dest="state_command", required=True)

    state_export = state_sub.add_parser("export", help="Export config + state JSON")
    state_export.add_argument("--output", required=True)
    state_export.set_defaults(func=cmd_state_export)

    state_import = state_sub.add_parser("import", help="Import config + state JSON")
    state_import.add_argument("--input", required=True)
    state_import.add_argument("--merge", action="store_true")
    state_import.set_defaults(func=cmd_state_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = ZeroClawService(StateStore(config_dir=args.config_dir))

    try:
        handler = getattr(args, "func", None)
        if handler is None:
            parser.print_help()
            return 1
        return int(handler(args, service) or 0)
    except (
        SetupError,
        AgentExistsError,
        AgentNotFoundError,
        ValueError,
        FileNotFoundError,
        json.JSONDecodeError,
        PermissionError,
        subprocess.CalledProcessError,
    ) as exc:
        print_error(str(exc))
        return 1
    except KeyboardInterrupt:
        print_warning("Interrupted")
        return 130


def cmd_setup(args: argparse.Namespace, service: ZeroClawService) -> int:
    if args.status:
        return _print_setup_status(service)

    provider = str(args.provider).strip().lower() or "zeroclaw"
    api_key = str(args.api_key or "").strip()
    auth_mode = str(args.auth_mode or "").strip().lower() or None
    spawn_password = args.spawn_password
    subscription = str(args.subscription).strip()
    workspace = str(args.workspace).strip()
    api_url = str(args.api_url).strip()

    if args.interactive:
        print_info("Interactive setup mode")
        provider = _prompt_with_default(
            f"Provider ({'/'.join(provider_names())})",
            provider,
        ).lower()
        if provider not in set(provider_names()):
            raise ValueError("provider must be one of: " + ", ".join(provider_names()))
        spec = get_provider(provider)
        auth_mode = auth_mode or spec.default_auth_mode
        auth_mode = _prompt_with_default(
            f"Auth mode ({'/'.join(spec.auth_modes)})",
            auth_mode,
        ).lower()
        if auth_mode == "api_key":
            api_key = api_key or _prompt_required(f"{provider} API key")
        subscription = _prompt_with_default("Subscription", subscription)
        workspace = _prompt_with_default("Workspace", workspace)
        api_url = _prompt_with_default("API URL", api_url)

    config = service.setup(
        provider=provider,
        api_key=api_key,
        auth_mode=auth_mode,
        spawn_password=spawn_password,
        clear_spawn_password=bool(args.clear_spawn_password),
        subscription=subscription or "starter",
        workspace=workspace or "default",
        api_url=api_url or str(DEFAULT_CONFIG["api_url"]),
        install_runtime=bool(args.install_runtime),
    )
    status = service.setup_status()

    print_success("Clawie setup initialized")
    print_panel(
        "Setup",
        [
            f"provider: {config.get('provider', '')}",
            f"workspace: {config.get('workspace', '')}",
            f"subscription: {config.get('subscription', '')}",
            f"api_url: {config.get('api_url', '')}",
            f"auth_mode: {config.get('auth_mode', '')}",
            f"spawn_password_default: {'set' if status.get('spawn_password_configured') else 'not set'}",
            f"runtime_installed: {bool(config.get('runtime_installed', False))}",
            f"api_key: {status.get('api_key', '')}",
        ],
    )
    return 0


def _print_setup_status(service: ZeroClawService) -> int:
    status = service.setup_status()
    print_panel(
        "Setup Status",
        [
            f"configured: {status.get('configured', False)}",
            f"provider: {status.get('provider', '')}",
            f"workspace: {status.get('workspace', '')}",
            f"subscription: {status.get('subscription', '')}",
            f"api_url: {status.get('api_url', '')}",
            f"auth_mode: {status.get('auth_mode', '')}",
            f"api_key: {status.get('api_key', '')}",
            f"spawn_password_default: {'set' if status.get('spawn_password_configured') else 'not set'}",
            f"runtime_installed: {status.get('runtime_installed', False)}",
            f"updated_at: {status.get('updated_at', '')}",
        ],
    )
    if not status.get("configured"):
        print_warning("Setup is incomplete. Run `clawie setup`.")
        return 1
    return 0


def cmd_agents_create(args: argparse.Namespace, service: ZeroClawService) -> int:
    channels = _resolve_channels(args.channel, args.channels_file)
    agent = service.create_agent(
        agent_id=args.agent_id,
        display_name=args.display_name,
        template=args.template,
        clone_from=args.clone_from,
        channel_strategy=args.channel_strategy,
        channels=channels,
        agent_version=args.agent_version,
        provider=args.provider,
    )
    print_success(f"Provisioned agent {agent['agent_id']}")
    _print_agent(agent)
    return 0


def cmd_agents_clone(args: argparse.Namespace, service: ZeroClawService) -> int:
    channels = _resolve_channels(args.channel, args.channels_file)
    agent = service.create_agent(
        agent_id=args.agent_id,
        display_name=args.display_name,
        template="baseline",
        clone_from=args.from_agent,
        channel_strategy=args.channel_strategy,
        channels=channels,
        agent_version=args.agent_version,
        provider=args.provider,
    )
    print_success(f"Cloned agent config from {args.from_agent} to {agent['agent_id']}")
    _print_agent(agent)
    return 0


def cmd_agents_list(args: argparse.Namespace, service: ZeroClawService) -> int:
    agents = service.list_agents()
    if not agents:
        print_info("No agents provisioned yet.")
        return 0

    rows: list[list[str]] = []
    for row in agents:
        agent = row.get("agent", {})
        channels = row.get("channels", [])
        active_channels = sum(1 for channel in channels if bool(channel.get("enabled", True)))
        migrated = sum(1 for channel in channels if channel.get("migrated_from"))
        plugins = agent.get("plugins", {})
        enabled_plugins = sum(1 for value in plugins.values() if bool(value))
        rows.append(
            [
                str(row.get("agent_id", row.get("user_id", ""))),
                str(row.get("display_name", "")),
                str(agent.get("provider", "")),
                str(row.get("channel_strategy", "")),
                f"{active_channels}/{len(channels)}",
                f"{enabled_plugins}/{len(plugins)}",
                str(migrated),
                str(agent.get("status", "")),
                str(agent.get("version", "")),
            ]
        )
    print_table(
        ["agent_id", "display_name", "provider", "strategy", "channels", "plugins", "migrated", "status", "agent"],
        rows,
    )
    return 0


def cmd_list_alias(args: argparse.Namespace, service: ZeroClawService) -> int:
    return cmd_agents_list(args, service)


def cmd_agents_show(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent = service.get_agent(args.agent_id)
    _print_agent(agent)
    return 0


def cmd_agents_delete(args: argparse.Namespace, service: ZeroClawService) -> int:
    service.delete_agent(args.agent_id)
    print_success(f"Deleted agent {args.agent_id}")
    return 0


def cmd_agents_batch_create(args: argparse.Namespace, service: ZeroClawService) -> int:
    payload = _read_json_file(args.file)
    if not isinstance(payload, list):
        raise ValueError("batch file must be a JSON array")

    entries: list[dict[str, Any]] = []
    for idx, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"batch entry at index {idx} must be an object")
        entries.append(row)

    result = service.batch_create_agents(entries)
    print_panel(
        "Batch Create",
        [
            f"created: {len(result['created'])}",
            f"errors: {len(result['errors'])}",
        ],
    )

    created = result.get("created", [])
    if created:
        print_info("Created agents: " + ", ".join(str(row) for row in created))

    errors = result.get("errors", [])
    if errors:
        rows = [[str(row.get("agent_id", "")), str(row.get("error", ""))] for row in errors]
        print_table(["agent_id", "error"], rows)
        return 1
    return 0


def cmd_channels_bootstrap(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent = service.bootstrap_channels(
        agent_id=args.agent_id,
        preset=args.preset,
        replace=args.replace,
    )
    print_success(
        f"Applied {args.preset} preset for {args.agent_id} ({len(agent.get('channels', []))} channels)"
    )
    return 0


def cmd_channels_migrate(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent = service.migrate_channels(
        from_agent=args.from_agent,
        to_agent=args.to_agent,
        replace=args.replace,
    )
    print_success(
        f"Migrated channels {args.from_agent} -> {args.to_agent} ({len(agent.get('channels', []))} channels)"
    )
    return 0


def cmd_spawn(args: argparse.Namespace, service: ZeroClawService) -> int:
    result = service.spawn_linux_user(
        agent_id=args.agent_id,
        linux_user=args.linux_user,
        copy_configs=not bool(args.skip_config_copy),
        source_home=args.source_home,
        template=args.template,
        agent_version=args.agent_version,
        provider=args.provider,
        password=args.password,
        password_hash=args.password_hash,
        use_global_password=not bool(args.no_global_password),
    )
    print_success(
        f"Spawned linux user {result['linux_user']} and provisioned {result['agent']['agent_id']}"
    )
    source = str(result.get("password_source", "none"))
    print_info(f"Password source: {source}")
    copied = result.get("copied_paths", [])
    if copied:
        print_info("Copied config paths:")
        for path in copied:
            print(f"- {path}")
    _print_agent(result["agent"])
    return 0


def cmd_purge(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = str(args.agent_id or args.agent_id_flag or "").strip()
    if not agent_id:
        raise ValueError("agent_id is required")
    if not args.yes:
        print_warning(f"This will permanently purge agent '{agent_id}' and its Linux user profile.")
        confirmation = input("Proceed? [y/N]: ").strip().lower()
        if confirmation not in {"y", "yes"}:
            raise ValueError("purge cancelled by user")
    result = service.purge_agent(agent_id)
    print_success(f"Purged agent {result.get('agent_id', '')}")
    linux_user = str(result.get("linux_user", "")).strip()
    if linux_user:
        print_info(
            f"linux_user={linux_user} removed={bool(result.get('linux_user_removed', False))} "
            f"home_removed={bool(result.get('home_removed', False))}"
        )
    return 0


def cmd_monitor(args: argparse.Namespace, service: ZeroClawService) -> int:
    refresh = max(1, int(args.refresh_seconds))
    run_dashboard(service, agent_id=args.agent_id, refresh_seconds=refresh)
    return 0


def cmd_claws_detect(args: argparse.Namespace, service: ZeroClawService) -> int:
    rows = service.list_installed_claws(source_home=args.source_home)
    if not rows:
        print_info("No installed claws detected.")
        return 0
    table: list[list[str]] = []
    for row in rows:
        table.append(
            [
                str(row.get("provider", "")),
                str(row.get("root", "")),
                ", ".join(str(item) for item in row.get("markers", [])),
            ]
        )
    print_table(["provider", "root", "markers"], table)
    return 0


def cmd_doctor(args: argparse.Namespace, service: ZeroClawService) -> int:
    report = service.doctor()
    status = str(report.get("status", "unknown"))

    print_panel(
        "Doctor",
        [f"overall: {status}"],
    )

    rows = []
    for check in report.get("checks", []):
        rows.append([str(check.get("status", "")), str(check.get("message", ""))])
    if rows:
        print_table(["status", "check"], rows)

    if status == "healthy":
        print_success("All critical checks passed")
        return 0
    if status == "degraded":
        print_warning("Non-critical warnings found")
        return 0
    print_error("Critical setup issues found")
    return 1


def cmd_events_list(args: argparse.Namespace, service: ZeroClawService) -> int:
    limit = max(1, int(args.limit))
    events = service.list_events(limit=limit)
    if not events:
        print_info("No events yet.")
        return 0

    rows: list[list[str]] = []
    for event in events:
        rows.append(
            [
                str(event.get("timestamp", "")),
                str(event.get("type", "")),
                str(event.get("message", "")),
            ]
        )
    print_table(["timestamp", "type", "message"], rows)
    return 0


def cmd_state_export(args: argparse.Namespace, service: ZeroClawService) -> int:
    target = service.export_state(args.output)
    print_success(f"State exported to {target}")
    return 0


def cmd_state_import(args: argparse.Namespace, service: ZeroClawService) -> int:
    service.import_state(args.input, merge=bool(args.merge))
    if args.merge:
        print_success(f"State merged from {args.input}")
    else:
        print_success(f"State imported from {args.input}")
    return 0


def _print_agent(agent: dict[str, Any]) -> None:
    channels = agent.get("channels", [])
    migrated = sum(1 for row in channels if row.get("migrated_from"))
    print_panel(
        "Agent",
        [
            f"agent_id: {agent.get('agent_id', agent.get('user_id', ''))}",
            f"display_name: {agent.get('display_name', '')}",
            f"template: {agent.get('source_template', '')}",
            f"clone_from: {agent.get('clone_from', '')}",
            f"channel_strategy: {agent.get('channel_strategy', '')}",
            f"channels: {len(channels)}",
            f"migrated_channels: {migrated}",
            f"provider: {agent.get('agent', {}).get('provider', '')}",
            f"auth_mode: {agent.get('agent', {}).get('auth_mode', '')}",
            f"autostart: {agent.get('agent', {}).get('autostart', True)}",
            f"service_status: {agent.get('agent', {}).get('service_status', 'unknown')}",
            f"agent_status: {agent.get('agent', {}).get('status', '')}",
            f"agent_version: {agent.get('agent', {}).get('version', '')}",
        ],
    )

    if channels:
        rows = []
        for channel in channels:
            rows.append(
                [
                    str(channel.get("kind", "")),
                    str(channel.get("name", "")),
                    str(bool(channel.get("enabled", True))),
                    str(channel.get("external_id", "")),
                    str(channel.get("migrated_from", "")),
                ]
            )
        print_table(["kind", "name", "enabled", "external_id", "migrated_from"], rows)

    plugins = agent.get("agent", {}).get("plugins", {})
    if plugins:
        plugin_rows = [[str(key), str(bool(value))] for key, value in sorted(plugins.items())]
        print()
        print_table(["plugin", "enabled"], plugin_rows)


def _resolve_channels(
    channel_args: list[str],
    channels_file: str | None,
) -> list[dict[str, str]] | None:
    if channel_args and channels_file:
        raise ValueError("use either --channel or --channels-file, not both")
    if channels_file:
        payload = _read_json_file(channels_file)
        if not isinstance(payload, list):
            raise ValueError("channels file must be a JSON array")
        channels: list[dict[str, str]] = []
        for idx, row in enumerate(payload):
            if not isinstance(row, dict):
                raise ValueError(f"channel entry at index {idx} must be an object")
            kind = str(row.get("kind", "")).strip()
            name = str(row.get("name", "")).strip()
            if not kind or not name:
                raise ValueError(f"channel entry at index {idx} requires kind and name")
            channels.append({"kind": kind, "name": name})
        return channels
    if channel_args:
        return _parse_channel_args(channel_args)
    return None


def _parse_channel_args(values: list[str]) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for raw in values:
        token = raw.strip()
        if ":" not in token:
            raise ValueError(f"invalid channel '{raw}'. expected KIND:NAME")
        kind, name = token.split(":", 1)
        kind = kind.strip()
        name = name.strip()
        if not kind or not name:
            raise ValueError(f"invalid channel '{raw}'. expected KIND:NAME")
        parsed.append({"kind": kind, "name": name})
    return parsed


def _read_json_file(path: str) -> Any:
    source = Path(path).expanduser()
    with source.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _prompt_required(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print_warning(f"{label} is required.")


def _prompt_with_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


if __name__ == "__main__":
    raise SystemExit(main())
