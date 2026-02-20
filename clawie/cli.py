from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from clawie.dashboard import run_dashboard
from clawie.service import (
    SetupError,
    UserExistsError,
    UserNotFoundError,
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
        description="ZeroClaw Linux CLI + dashboard control plane",
    )
    parser.add_argument(
        "--config-dir",
        help="Override config directory (default: ~/.config/clawie)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="Configure ZeroClaw API/workspace")
    setup_sub = setup.add_subparsers(dest="setup_command", required=True)

    setup_init = setup_sub.add_parser("init", help="Initialize configuration")
    setup_init.add_argument("--api-key", help="ZeroClaw API key")
    setup_init.add_argument("--subscription", default="starter", help="Plan name")
    setup_init.add_argument("--workspace", default="default", help="Workspace slug")
    setup_init.add_argument(
        "--api-url",
        default=str(DEFAULT_CONFIG["api_url"]),
        help="ZeroClaw API base URL",
    )
    setup_init.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for values interactively",
    )
    setup_init.set_defaults(func=cmd_setup_init)

    setup_status = setup_sub.add_parser("status", help="Show setup status")
    setup_status.set_defaults(func=cmd_setup_status)

    users = subparsers.add_parser("users", help="Provision and manage users")
    users_sub = users.add_subparsers(dest="users_command", required=True)

    users_create = users_sub.add_parser("create", help="Create a user")
    users_create.add_argument("--user-id", required=True, help="Unique user ID")
    users_create.add_argument("--display-name", help="Display name")
    users_create.add_argument("--template", default="baseline", help="Template name")
    users_create.add_argument("--clone-from", help="Clone channels/defaults from user ID")
    users_create.add_argument(
        "--channel-strategy",
        choices=["new", "migrate"],
        default="new",
        help="Use minted channel names or migrated channel names",
    )
    users_create.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="KIND:NAME",
        help="Add a channel definition (repeatable)",
    )
    users_create.add_argument(
        "--channels-file",
        help="JSON file with channel definitions: [{\"kind\": ..., \"name\": ...}]",
    )
    users_create.add_argument("--agent-version", default="1.0.0", help="Agent version")
    users_create.set_defaults(func=cmd_users_create)

    users_clone = users_sub.add_parser(
        "clone",
        help="One-click user clone from an existing user config",
    )
    users_clone.add_argument("--from-user", required=True, help="Existing source user")
    users_clone.add_argument("--user-id", required=True, help="New user ID")
    users_clone.add_argument("--display-name", help="Display name")
    users_clone.add_argument(
        "--channel-strategy",
        choices=["new", "migrate"],
        default="migrate",
        help="Use copied channels as-is (migrate) or mint new names (new)",
    )
    users_clone.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="KIND:NAME",
        help="Override cloned channels with explicit definitions (repeatable)",
    )
    users_clone.add_argument(
        "--channels-file",
        help="JSON file with channel definitions to override clone channels",
    )
    users_clone.add_argument("--agent-version", default="1.0.0", help="Agent version")
    users_clone.set_defaults(func=cmd_users_clone)

    users_list = users_sub.add_parser("list", help="List users")
    users_list.set_defaults(func=cmd_users_list)

    users_show = users_sub.add_parser("show", help="Show one user")
    users_show.add_argument("--user-id", required=True)
    users_show.set_defaults(func=cmd_users_show)

    users_delete = users_sub.add_parser("delete", help="Delete one user")
    users_delete.add_argument("--user-id", required=True)
    users_delete.set_defaults(func=cmd_users_delete)

    users_batch = users_sub.add_parser(
        "batch-create",
        help="Create users from JSON array entries",
    )
    users_batch.add_argument("--file", required=True, help="Input JSON file")
    users_batch.set_defaults(func=cmd_users_batch_create)

    channels = subparsers.add_parser("channels", help="Channel operations")
    channels_sub = channels.add_subparsers(dest="channels_command", required=True)

    channels_bootstrap = channels_sub.add_parser(
        "bootstrap",
        help="Apply a channel preset to a user",
    )
    channels_bootstrap.add_argument("--user-id", required=True)
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
        help="Copy channels from one user to another",
    )
    channels_migrate.add_argument("--from-user", required=True)
    channels_migrate.add_argument("--to-user", required=True)
    channels_migrate.add_argument(
        "--replace",
        action="store_true",
        help="Replace destination channels instead of merging",
    )
    channels_migrate.set_defaults(func=cmd_channels_migrate)

    dashboard = subparsers.add_parser("dashboard", help="Unified agent dashboard")
    dashboard.add_argument("--user-id", help="Filter dashboard to one user")
    dashboard.add_argument("--refresh-seconds", type=int, default=2)
    dashboard.set_defaults(func=cmd_dashboard)

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
        UserExistsError,
        UserNotFoundError,
        ValueError,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as exc:
        print_error(str(exc))
        return 1
    except KeyboardInterrupt:
        print_warning("Interrupted")
        return 130


def cmd_setup_init(args: argparse.Namespace, service: ZeroClawService) -> int:
    api_key = str(args.api_key or "").strip()
    subscription = str(args.subscription).strip()
    workspace = str(args.workspace).strip()
    api_url = str(args.api_url).strip()

    if args.interactive:
        print_info("Interactive setup mode")
        api_key = api_key or _prompt_required("ZeroClaw API key")
        subscription = _prompt_with_default("Subscription", subscription)
        workspace = _prompt_with_default("Workspace", workspace)
        api_url = _prompt_with_default("API URL", api_url)

    if not api_key:
        raise ValueError("API key is required. Use --api-key or --interactive.")

    config = service.setup(
        api_key=api_key,
        subscription=subscription or "starter",
        workspace=workspace or "default",
        api_url=api_url or str(DEFAULT_CONFIG["api_url"]),
    )
    status = service.setup_status()

    print_success("ZeroClaw setup initialized")
    print_panel(
        "Setup",
        [
            f"workspace: {config.get('workspace', '')}",
            f"subscription: {config.get('subscription', '')}",
            f"api_url: {config.get('api_url', '')}",
            f"api_key: {status.get('api_key', '')}",
        ],
    )
    return 0


def cmd_setup_status(args: argparse.Namespace, service: ZeroClawService) -> int:
    status = service.setup_status()
    print_panel(
        "Setup Status",
        [
            f"configured: {status.get('configured', False)}",
            f"workspace: {status.get('workspace', '')}",
            f"subscription: {status.get('subscription', '')}",
            f"api_url: {status.get('api_url', '')}",
            f"api_key: {status.get('api_key', '')}",
            f"updated_at: {status.get('updated_at', '')}",
        ],
    )
    if not status.get("configured"):
        print_warning("Setup is incomplete. Run `clawie setup init`.")
        return 1
    return 0


def cmd_users_create(args: argparse.Namespace, service: ZeroClawService) -> int:
    channels = _resolve_channels(args.channel, args.channels_file)
    user = service.create_user(
        user_id=args.user_id,
        display_name=args.display_name,
        template=args.template,
        clone_from=args.clone_from,
        channel_strategy=args.channel_strategy,
        channels=channels,
        agent_version=args.agent_version,
    )
    print_success(f"Provisioned user {user['user_id']}")
    _print_user(user)
    return 0


def cmd_users_clone(args: argparse.Namespace, service: ZeroClawService) -> int:
    channels = _resolve_channels(args.channel, args.channels_file)
    user = service.create_user(
        user_id=args.user_id,
        display_name=args.display_name,
        template="baseline",
        clone_from=args.from_user,
        channel_strategy=args.channel_strategy,
        channels=channels,
        agent_version=args.agent_version,
    )
    print_success(f"Cloned user config from {args.from_user} to {user['user_id']}")
    _print_user(user)
    return 0


def cmd_users_list(args: argparse.Namespace, service: ZeroClawService) -> int:
    users = service.list_users()
    if not users:
        print_info("No users provisioned yet.")
        return 0

    rows: list[list[str]] = []
    for user in users:
        agent = user.get("agent", {})
        channels = user.get("channels", [])
        migrated = sum(1 for channel in channels if channel.get("migrated_from"))
        rows.append(
            [
                str(user.get("user_id", "")),
                str(user.get("display_name", "")),
                str(user.get("channel_strategy", "")),
                str(len(channels)),
                str(migrated),
                str(agent.get("status", "")),
                str(agent.get("version", "")),
            ]
        )
    print_table(
        ["user_id", "display_name", "strategy", "channels", "migrated", "status", "agent"],
        rows,
    )
    return 0


def cmd_users_show(args: argparse.Namespace, service: ZeroClawService) -> int:
    user = service.get_user(args.user_id)
    _print_user(user)
    return 0


def cmd_users_delete(args: argparse.Namespace, service: ZeroClawService) -> int:
    service.delete_user(args.user_id)
    print_success(f"Deleted user {args.user_id}")
    return 0


def cmd_users_batch_create(args: argparse.Namespace, service: ZeroClawService) -> int:
    payload = _read_json_file(args.file)
    if not isinstance(payload, list):
        raise ValueError("batch file must be a JSON array")

    entries: list[dict[str, Any]] = []
    for idx, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"batch entry at index {idx} must be an object")
        entries.append(row)

    result = service.batch_create_users(entries)
    print_panel(
        "Batch Create",
        [
            f"created: {len(result['created'])}",
            f"errors: {len(result['errors'])}",
        ],
    )

    created = result.get("created", [])
    if created:
        print_info("Created users: " + ", ".join(str(row) for row in created))

    errors = result.get("errors", [])
    if errors:
        rows = [[str(row.get("user_id", "")), str(row.get("error", ""))] for row in errors]
        print_table(["user_id", "error"], rows)
        return 1
    return 0


def cmd_channels_bootstrap(args: argparse.Namespace, service: ZeroClawService) -> int:
    user = service.bootstrap_channels(
        user_id=args.user_id,
        preset=args.preset,
        replace=args.replace,
    )
    print_success(
        f"Applied {args.preset} preset for {args.user_id} ({len(user.get('channels', []))} channels)"
    )
    return 0


def cmd_channels_migrate(args: argparse.Namespace, service: ZeroClawService) -> int:
    user = service.migrate_channels(
        from_user=args.from_user,
        to_user=args.to_user,
        replace=args.replace,
    )
    print_success(
        f"Migrated channels {args.from_user} -> {args.to_user} ({len(user.get('channels', []))} channels)"
    )
    return 0


def cmd_dashboard(args: argparse.Namespace, service: ZeroClawService) -> int:
    refresh = max(1, int(args.refresh_seconds))
    run_dashboard(service, user_id=args.user_id, refresh_seconds=refresh)
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


def _print_user(user: dict[str, Any]) -> None:
    channels = user.get("channels", [])
    migrated = sum(1 for row in channels if row.get("migrated_from"))
    print_panel(
        "User",
        [
            f"user_id: {user.get('user_id', '')}",
            f"display_name: {user.get('display_name', '')}",
            f"template: {user.get('source_template', '')}",
            f"clone_from: {user.get('clone_from', '')}",
            f"channel_strategy: {user.get('channel_strategy', '')}",
            f"channels: {len(channels)}",
            f"migrated_channels: {migrated}",
            f"agent_status: {user.get('agent', {}).get('status', '')}",
            f"agent_version: {user.get('agent', {}).get('version', '')}",
        ],
    )

    if channels:
        rows = []
        for channel in channels:
            rows.append(
                [
                    str(channel.get("kind", "")),
                    str(channel.get("name", "")),
                    str(channel.get("external_id", "")),
                    str(channel.get("migrated_from", "")),
                ]
            )
        print_table(["kind", "name", "external_id", "migrated_from"], rows)


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
