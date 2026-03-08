from __future__ import annotations

import argparse
import json
import subprocess
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
        description="Clawie control plane for config, agents, runtimes, and dashboard operations",
    )
    parser.add_argument(
        "--config-dir",
        help="Override state directory (default: ~/.clawie)",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{config,agent,channel,runtime,dashboard,health,event,backup}",
    )

    _build_config_parser(subparsers)
    _build_agent_parser(subparsers)
    _build_channel_parser(subparsers)
    _build_runtime_parser(subparsers)

    dashboard = subparsers.add_parser(
        "dashboard",
        help="Open the interactive dashboard",
    )
    _add_positional_argument(
        dashboard,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Only show one agent",
    )
    dashboard.add_argument(
        "--refresh",
        dest="refresh_seconds",
        type=int,
        default=2,
        help="Refresh interval in seconds",
    )
    dashboard.set_defaults(func=cmd_monitor)

    health = subparsers.add_parser(
        "health",
        help="Run health checks",
    )
    health.set_defaults(func=cmd_doctor)

    event = subparsers.add_parser(
        "event",
        help="Inspect recorded events",
    )
    event_sub = event.add_subparsers(
        dest="event_command",
        required=True,
        metavar="{list}",
    )
    event_list = event_sub.add_parser("list", help="List recent events")
    event_list.add_argument("--limit", type=int, default=20)
    event_list.set_defaults(func=cmd_events_list)

    backup = subparsers.add_parser(
        "backup",
        help="Export and import config/state snapshots",
    )
    backup_sub = backup.add_subparsers(
        dest="backup_command",
        required=True,
        metavar="{export,import}",
    )

    backup_export = backup_sub.add_parser("export", help="Write a snapshot to disk")
    _add_positional_argument(
        backup_export,
        "output",
        metavar="PATH",
        help_text="Snapshot output file",
    )
    backup_export.set_defaults(func=cmd_state_export)

    backup_import = backup_sub.add_parser("import", help="Load a snapshot from disk")
    _add_positional_argument(
        backup_import,
        "input",
        metavar="PATH",
        help_text="Snapshot file to import",
    )
    backup_import.add_argument("--merge", action="store_true")
    backup_import.set_defaults(func=cmd_state_import)

    return parser


def _build_config_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config = subparsers.add_parser(
        "config",
        help="View and update Clawie configuration",
    )
    config_sub = config.add_subparsers(
        dest="config_command",
        required=True,
        metavar="{set,show}",
    )

    config_set = config_sub.add_parser(
        "set",
        help="Write provider, auth, and workspace settings",
    )
    _add_setup_arguments(config_set)
    config_set.set_defaults(func=cmd_setup)

    config_show = config_sub.add_parser(
        "show",
        help="Show current configuration status",
    )
    config_show.set_defaults(func=cmd_config_show)


def _build_agent_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    agent = subparsers.add_parser(
        "agent",
        help="Manage agents, prompts, and credential access",
    )
    agent_sub = agent.add_subparsers(
        dest="agent_command",
        required=True,
        metavar="{create,clone,prompt,credentials,auth,provider,list,show,delete,purge,create-batch}",
    )

    create = agent_sub.add_parser("create", help="Create a new agent")
    _add_positional_argument(
        create,
        "agent_id",
        metavar="AGENT_ID",
        help_text="New agent ID",
    )
    create.add_argument("--display-name", help="Display name")
    create.add_argument("--template", default="baseline", help="Template name")
    create.add_argument(
        "--clone-from",
        help="Clone channels/defaults from another agent",
    )
    create.add_argument(
        "--channel-strategy",
        choices=["new", "migrate"],
        default="new",
        help="Use new channel names or migrated channel names",
    )
    create.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="KIND:NAME",
        help="Add a channel definition (repeatable)",
    )
    create.add_argument(
        "--channels-file",
        help="JSON file with channel definitions: [{\"kind\": ..., \"name\": ...}]",
    )
    create.add_argument("--agent-version", default="1.0.0", help="Agent version")
    create.add_argument(
        "--provider",
        choices=provider_names(),
        help="Override provider for this agent",
    )
    create.set_defaults(func=cmd_agents_create)

    clone = agent_sub.add_parser("clone", help="Clone an existing agent into a new one")
    _add_positional_argument(
        clone,
        "from_agent",
        metavar="SOURCE_AGENT",
        help_text="Source agent ID",
    )
    _add_positional_argument(
        clone,
        "agent_id",
        metavar="TARGET_AGENT",
        help_text="New agent ID",
    )
    clone.add_argument("--display-name", help="Display name")
    clone.add_argument(
        "--channel-strategy",
        choices=["new", "migrate"],
        default="migrate",
        help="Keep copied channel names or mint new ones",
    )
    clone.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="KIND:NAME",
        help="Override cloned channels with explicit definitions (repeatable)",
    )
    clone.add_argument(
        "--channels-file",
        help="JSON file with channel definitions to override clone channels",
    )
    clone.add_argument("--agent-version", default="1.0.0", help="Agent version")
    clone.add_argument(
        "--provider",
        choices=provider_names(),
        help="Override provider for this cloned agent",
    )
    clone.set_defaults(func=cmd_agents_clone)

    prompt = agent_sub.add_parser(
        "prompt",
        help="Manage core prompt files",
    )
    prompt_sub = prompt.add_subparsers(dest="agent_prompt_command", required=True, metavar="{copy}")
    prompt_copy = prompt_sub.add_parser(
        "copy",
        help="Copy core prompt files from one agent to another",
    )
    _add_positional_argument(
        prompt_copy,
        "from_agent",
        metavar="SOURCE_AGENT",
        help_text="Source agent ID",
    )
    _add_positional_argument(
        prompt_copy,
        "to_agent",
        metavar="TARGET_AGENT",
        help_text="Target agent ID",
    )
    prompt_copy.add_argument(
        "--no-apply-to-disk",
        action="store_true",
        help="Only update state; do not write prompt files to the target Linux home",
    )
    prompt_copy.set_defaults(func=cmd_agents_clone_prompts)

    credentials = agent_sub.add_parser(
        "credentials",
        help="Manage credential bundle policy and sync",
    )
    credentials_sub = credentials.add_subparsers(
        dest="agent_credentials_command",
        required=True,
        metavar="{list,show,set,sync,revoke}",
    )

    credentials_list = credentials_sub.add_parser(
        "list",
        help="List available credential bundles",
    )
    credentials_list.set_defaults(func=cmd_agents_credentials_bundles)

    credentials_show = credentials_sub.add_parser(
        "show",
        help="Show selected credential bundles for an agent",
    )
    _add_positional_argument(
        credentials_show,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    credentials_show.set_defaults(func=cmd_agents_credentials_show)

    credentials_set = credentials_sub.add_parser(
        "set",
        help="Set selected credential bundles for an agent",
    )
    _add_positional_argument(
        credentials_set,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    credentials_set.add_argument(
        "bundles",
        nargs="*",
        metavar="BUNDLE",
        help="Credential bundle IDs",
    )
    credentials_set.add_argument(
        "--bundle",
        action="append",
        default=[],
        metavar="BUNDLE",
        help="Credential bundle ID (repeatable)",
    )
    credentials_set.add_argument(
        "--include-defaults",
        action="store_true",
        help="Start from default bundles, then add explicit selections",
    )
    credentials_set.set_defaults(func=cmd_agents_credentials_set)

    credentials_sync = credentials_sub.add_parser(
        "sync",
        help="Sync selected credentials into the agent Linux home",
    )
    _add_positional_argument(
        credentials_sync,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    credentials_sync.add_argument(
        "bundles",
        nargs="*",
        metavar="BUNDLE",
        help="Override bundle IDs for this sync run",
    )
    credentials_sync.add_argument(
        "--bundle",
        action="append",
        default=[],
        metavar="BUNDLE",
        help="Override bundles for this sync run only (repeatable)",
    )
    credentials_sync.add_argument(
        "--include-defaults",
        action="store_true",
        help="When using --bundle, include default bundles too",
    )
    credentials_sync.add_argument(
        "--source-home",
        help="Source home directory to copy credentials from",
    )
    credentials_sync.set_defaults(func=cmd_agents_credentials_sync)

    credentials_revoke = credentials_sub.add_parser(
        "revoke",
        help="Remove credential access from an agent Linux home",
    )
    _add_positional_argument(
        credentials_revoke,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    credentials_revoke.add_argument(
        "bundles",
        nargs="*",
        metavar="BUNDLE",
        help="Bundle IDs to revoke (default: all selected bundles)",
    )
    credentials_revoke.add_argument(
        "--bundle",
        action="append",
        default=[],
        metavar="BUNDLE",
        help="Only revoke these bundles (repeatable)",
    )
    credentials_revoke.set_defaults(func=cmd_agents_credentials_revoke)

    auth = agent_sub.add_parser(
        "auth",
        help="Inspect and refresh linked login sessions",
    )
    auth_sub = auth.add_subparsers(
        dest="agent_auth_command",
        required=True,
        metavar="{show,login}",
    )

    auth_show = auth_sub.add_parser(
        "show",
        help="Show auth session status for an agent or local claw",
    )
    _add_positional_argument(
        auth_show,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID or @local:PROVIDER",
    )
    auth_show.set_defaults(func=cmd_agents_auth_show)

    auth_login = auth_sub.add_parser(
        "login",
        help="Refresh or re-run linked login for an agent or local claw",
    )
    _add_positional_argument(
        auth_login,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID or @local:PROVIDER",
    )
    auth_login.set_defaults(func=cmd_agents_auth_login)

    provider = agent_sub.add_parser(
        "provider",
        help="Inspect and change an agent provider",
    )
    provider_sub = provider.add_subparsers(
        dest="agent_provider_command",
        required=True,
        metavar="{set}",
    )

    provider_set = provider_sub.add_parser(
        "set",
        help="Change the provider for an existing managed agent",
    )
    _add_positional_argument(
        provider_set,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    _add_positional_argument(
        provider_set,
        "provider",
        metavar="PROVIDER",
        help_text="Target provider name",
    )
    provider_set.set_defaults(func=cmd_agents_provider_set)

    agent_list = agent_sub.add_parser("list", help="List agents")
    agent_list.set_defaults(func=cmd_agents_list)

    agent_show = agent_sub.add_parser("show", help="Show one agent")
    _add_positional_argument(
        agent_show,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID to inspect",
    )
    agent_show.set_defaults(func=cmd_agents_show)

    agent_delete = agent_sub.add_parser("delete", help="Delete an agent record")
    _add_positional_argument(
        agent_delete,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID to delete",
    )
    agent_delete.set_defaults(func=cmd_agents_delete)

    agent_purge = agent_sub.add_parser(
        "purge",
        help="Delete an agent and remove its Linux runtime",
    )
    _add_positional_argument(
        agent_purge,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID to purge",
    )
    agent_purge.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt",
    )
    agent_purge.set_defaults(func=cmd_purge)

    create_batch = agent_sub.add_parser(
        "create-batch",
        help="Create many agents from a JSON file",
    )
    _add_positional_argument(
        create_batch,
        "file",
        metavar="FILE",
        help_text="Input JSON file",
    )
    create_batch.set_defaults(func=cmd_agents_batch_create)


def _build_channel_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    channel = subparsers.add_parser(
        "channel",
        help="Manage agent channels",
    )
    channel_sub = channel.add_subparsers(
        dest="channel_command",
        required=True,
        metavar="{apply,move}",
    )

    apply_preset = channel_sub.add_parser(
        "apply",
        help="Apply a channel preset to an agent",
    )
    _add_positional_argument(
        apply_preset,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    apply_preset.add_argument(
        "--preset",
        choices=["minimal", "growth", "enterprise"],
        default="growth",
    )
    apply_preset.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing channels instead of merging",
    )
    apply_preset.set_defaults(func=cmd_channels_bootstrap)

    move = channel_sub.add_parser(
        "move",
        help="Move channels from one agent to another",
    )
    _add_positional_argument(
        move,
        "from_agent",
        metavar="SOURCE_AGENT",
        help_text="Source agent ID",
    )
    _add_positional_argument(
        move,
        "to_agent",
        metavar="TARGET_AGENT",
        help_text="Target agent ID",
    )
    move.add_argument(
        "--replace",
        action="store_true",
        help="Replace destination channels instead of merging",
    )
    move.set_defaults(func=cmd_channels_migrate)


def _build_runtime_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    runtime = subparsers.add_parser(
        "runtime",
        help="Manage isolated Linux runtimes",
    )
    runtime_sub = runtime.add_subparsers(
        dest="runtime_command",
        required=True,
        metavar="{create,detect,status,login}",
    )

    create = runtime_sub.add_parser(
        "create",
        help="Create a Linux runtime and matching agent",
    )
    _add_positional_argument(
        create,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID to create",
    )
    create.add_argument(
        "--user",
        dest="linux_user",
        help="Linux username (defaults to agent ID)",
    )
    create.add_argument("--template", default="baseline", help="Template name")
    create.add_argument("--agent-version", default="1.0.0", help="Agent version")
    create.add_argument(
        "--provider",
        choices=provider_names(),
        help="Provider for the spawned agent",
    )
    create.add_argument(
        "--source-home",
        help="Source home directory to copy configs from",
    )
    create.add_argument(
        "--password",
        help="Set a plaintext Linux password for this runtime only",
    )
    create.add_argument(
        "--password-hash",
        help="Set a pre-hashed Linux password for this runtime only",
    )
    create.add_argument(
        "--no-global-password",
        action="store_true",
        help="Do not apply the global default spawn password",
    )
    create.add_argument(
        "--skip-config-copy",
        action="store_true",
        help="Do not copy current user config files to the new Linux user",
    )
    create.add_argument(
        "--from-agent",
        dest="clone_from_agent",
        help="Clone state from an existing agent",
    )
    create.add_argument(
        "--credential-bundle",
        action="append",
        default=[],
        metavar="BUNDLE",
        help="Credential bundle to sync on create (repeatable)",
    )
    create.add_argument(
        "--no-default-credentials",
        action="store_true",
        help="Do not include default credential bundles when syncing on create",
    )
    create.set_defaults(func=cmd_spawn)

    detect = runtime_sub.add_parser(
        "detect",
        help="Detect installed runtimes in a home directory",
    )
    detect.add_argument(
        "--source-home",
        help="Home directory to inspect (default: current user home)",
    )
    detect.set_defaults(func=cmd_claws_detect)

    status = runtime_sub.add_parser(
        "status",
        help="Show local runtime service and auth status",
    )
    status.set_defaults(func=cmd_runtime_status)

    login = runtime_sub.add_parser(
        "login",
        help="Refresh or re-run linked login for a local runtime",
    )
    _add_positional_argument(
        login,
        "provider",
        metavar="PROVIDER",
        help_text="Installed local provider name",
    )
    login.set_defaults(func=cmd_runtime_login)


def _add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=provider_names(),
        default="picoclaw",
        help="Agent provider",
    )
    parser.add_argument("--api-key", help="Provider API key (if using api_key auth)")
    parser.add_argument(
        "--auth-mode",
        choices=["linked", "api_key", "none"],
        help="Provider auth mode (default is provider-specific)",
    )
    parser.add_argument("--subscription", default="starter", help="Plan name")
    parser.add_argument("--workspace", default="default", help="Workspace slug")
    parser.add_argument(
        "--spawn-password",
        help="Set a global default Linux password for future spawned users",
    )
    parser.add_argument(
        "--clear-spawn-password",
        action="store_true",
        help="Clear the global default spawn password",
    )
    parser.add_argument(
        "--api-url",
        default=str(DEFAULT_CONFIG["api_url"]),
        help="API base URL",
    )
    parser.add_argument(
        "--install-runtime",
        action="store_true",
        help="Record local runtime as installed",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for values interactively",
    )


def _add_positional_argument(
    parser: argparse.ArgumentParser,
    name: str,
    *,
    metavar: str,
    help_text: str,
) -> None:
    parser.add_argument(name, nargs="?", metavar=metavar, help=help_text)


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
    provider = str(args.provider).strip().lower() or "picoclaw"
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

    print_success("Clawie config updated")
    print_panel(
        "Config",
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


def cmd_config_show(args: argparse.Namespace, service: ZeroClawService) -> int:
    _ = args
    return _print_setup_status(service)


def _print_setup_status(service: ZeroClawService) -> int:
    status = service.setup_status()
    print_panel(
        "Config",
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
        print_warning("Config is incomplete. Run `clawie config set`.")
        return 1
    return 0


def cmd_agents_create(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    channels = _resolve_channels(args.channel, args.channels_file)
    agent = service.create_agent(
        agent_id=agent_id,
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
    from_agent = _resolve_required_value(args.from_agent, field_name="from_agent")
    agent_id = _resolve_agent_id(args.agent_id)
    channels = _resolve_channels(args.channel, args.channels_file)
    agent = service.create_agent(
        agent_id=agent_id,
        display_name=args.display_name,
        template="baseline",
        clone_from=from_agent,
        channel_strategy=args.channel_strategy,
        channels=channels,
        agent_version=args.agent_version,
        provider=args.provider,
    )
    print_success(f"Cloned agent config from {from_agent} to {agent['agent_id']}")
    _print_agent(agent)
    return 0


def cmd_agents_clone_prompts(args: argparse.Namespace, service: ZeroClawService) -> int:
    from_agent = _resolve_required_value(args.from_agent, field_name="from_agent")
    to_agent = _resolve_required_value(args.to_agent, field_name="to_agent")
    updated = service.clone_agent_prompts(
        from_agent=from_agent,
        to_agent=to_agent,
        apply_to_disk=not bool(args.no_apply_to_disk),
    )
    print_success(f"Cloned core prompts {from_agent} -> {to_agent}")
    _print_agent(updated)
    return 0


def cmd_agents_credentials_bundles(args: argparse.Namespace, service: ZeroClawService) -> int:
    _ = args
    rows: list[list[str]] = []
    for item in service.credential_bundle_options():
        rows.append(
            [
                str(item.get("id", "")),
                str(item.get("label", "")),
                str(bool(item.get("default", False))),
            ]
        )
    print_table(["bundle", "description", "default"], rows)
    return 0


def cmd_agents_credentials_show(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = service.get_agent_credential_sync(agent_id)
    selected = ", ".join(str(item) for item in payload.get("selected_bundles", [])) or "<none>"
    print_panel(
        "Credential Sync",
        [
            f"agent_id: {payload.get('agent_id', '')}",
            f"linux_user: {payload.get('linux_user', '')}",
            f"selected: {selected}",
            f"last_synced_at: {payload.get('last_synced_at', '')}",
            f"last_source_home: {payload.get('last_source_home', '')}",
            f"last_revoked_at: {payload.get('last_revoked_at', '')}",
        ],
    )
    rows = []
    for item in payload.get("bundles", []):
        rows.append(
            [
                str(item.get("id", "")),
                str(item.get("label", "")),
                str(bool(item.get("default", False))),
                str(bool(item.get("selected", False))),
            ]
        )
    if rows:
        print_table(["bundle", "description", "default", "selected"], rows)
    return 0


def cmd_agents_credentials_set(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    bundles = _resolve_bundles(getattr(args, "bundles", []), args.bundle)
    agent = service.set_agent_credential_bundles(
        agent_id,
        bundles=bundles,
        include_defaults=bool(args.include_defaults),
    )
    selected = ", ".join(agent.get("credential_sync", {}).get("bundles", [])) or "<none>"
    print_success(f"Updated credential bundles for {agent_id}")
    print_info(f"Selected bundles: {selected}")
    return 0


def cmd_agents_credentials_sync(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    override_bundles = _resolve_bundles(getattr(args, "bundles", []), args.bundle)
    result = service.sync_agent_credentials(
        agent_id,
        source_home=args.source_home,
        bundles=override_bundles or None,
        include_defaults=bool(args.include_defaults),
    )
    print_success(f"Synced credentials for {agent_id}")
    print_info(f"Source home: {result.get('source_home', '')}")
    print_info("Bundles: " + (", ".join(result.get("bundles", [])) or "<none>"))
    copied = result.get("copied_paths", [])
    if copied:
        print_info("Copied credential paths:")
        for path in copied:
            print(f"- {path}")
    return 0


def cmd_agents_credentials_revoke(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    bundles = _resolve_bundles(getattr(args, "bundles", []), args.bundle)
    result = service.revoke_agent_credentials(
        agent_id,
        bundles=bundles or None,
    )
    print_success(f"Revoked credential access for {agent_id}")
    print_info("Revoked bundles: " + (", ".join(result.get("bundles", [])) or "<none>"))
    print_info("Remaining bundles: " + (", ".join(result.get("remaining_bundles", [])) or "<none>"))
    removed = result.get("removed_paths", [])
    if removed:
        print_info("Removed credential paths:")
        for path in removed:
            print(f"- {path}")
    return 0


def cmd_agents_auth_show(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = service.agent_auth_status(agent_id)
    _print_auth_status(payload, title="Agent Auth")
    return 0


def cmd_agents_auth_login(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = service.agent_auth_login(agent_id)
    action = str(payload.get("action_performed", "login"))
    if action == "status":
        print_info(f"Linked login already ready for {agent_id}")
    elif action == "refresh":
        print_success(f"Refreshed linked login for {agent_id}")
    else:
        print_success(f"Completed linked login for {agent_id}")
    _print_auth_status(payload, title="Agent Auth")
    return 0


def cmd_agents_provider_set(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    provider = _resolve_required_value(args.provider, field_name="provider")
    agent = service.set_agent_provider(agent_id, provider)
    print_success(f"Changed provider for {agent_id} to {agent.get('agent', {}).get('provider', '')}")
    _print_agent(service.get_dashboard_agent(agent_id))
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

def cmd_agents_show(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    agent = service.get_dashboard_agent(agent_id)
    _print_agent(agent)
    return 0


def cmd_agents_delete(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    service.delete_agent(agent_id)
    print_success(f"Deleted agent {agent_id}")
    return 0


def cmd_agents_batch_create(args: argparse.Namespace, service: ZeroClawService) -> int:
    source = _resolve_required_value(args.file, field_name="file")
    payload = _read_json_file(source)
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
    agent_id = _resolve_agent_id(args.agent_id)
    agent = service.bootstrap_channels(
        agent_id=agent_id,
        preset=args.preset,
        replace=args.replace,
    )
    print_success(
        f"Applied {args.preset} preset for {agent_id} ({len(agent.get('channels', []))} channels)"
    )
    return 0


def cmd_channels_migrate(args: argparse.Namespace, service: ZeroClawService) -> int:
    from_agent = _resolve_required_value(args.from_agent, field_name="from_agent")
    to_agent = _resolve_required_value(args.to_agent, field_name="to_agent")
    agent = service.migrate_channels(
        from_agent=from_agent,
        to_agent=to_agent,
        replace=args.replace,
    )
    print_success(
        f"Migrated channels {from_agent} -> {to_agent} ({len(agent.get('channels', []))} channels)"
    )
    return 0


def cmd_spawn(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    result = service.spawn_linux_user(
        agent_id=agent_id,
        linux_user=args.linux_user,
        copy_configs=not bool(args.skip_config_copy),
        source_home=args.source_home,
        template=args.template,
        agent_version=args.agent_version,
        provider=args.provider,
        password=args.password,
        password_hash=args.password_hash,
        use_global_password=not bool(args.no_global_password),
        clone_from_agent=args.clone_from_agent,
        credential_bundles=list(args.credential_bundle or []),
        include_default_credentials=not bool(args.no_default_credentials),
    )
    print_success(
        f"Spawned linux user {result['linux_user']} and provisioned {result['agent']['agent_id']}"
    )
    print_info("Credential bundles: " + (", ".join(result.get("credential_bundles", [])) or "<none>"))
    source = str(result.get("password_source", "none"))
    print_info(f"Password source: {source}")
    password_value = str(result.get("password_value", ""))
    if password_value:
        print_info(f"Password: {password_value}")
    else:
        print_info("Password: <not shown>")
    if bool(result.get("ssh_login_disabled", False)):
        print_info("SSH login: disabled for spawned Linux user")
    copied = result.get("copied_paths", [])
    if copied:
        print_info("Copied config paths:")
        for path in copied:
            print(f"- {path}")
    _print_agent(result["agent"])
    return 0


def cmd_purge(args: argparse.Namespace, service: ZeroClawService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
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
    run_dashboard(service, agent_id=args.agent_id or None, refresh_seconds=refresh)
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


def cmd_runtime_status(args: argparse.Namespace, service: ZeroClawService) -> int:
    _ = args
    rows = service.list_local_runtime_statuses(refresh=True)
    if not rows:
        print_info("No local runtimes detected.")
        return 0

    table: list[list[str]] = []
    for row in rows:
        table.append(
            [
                str(row.get("provider", "")),
                str(row.get("linux_user", "")),
                str(row.get("service_status", "unknown")),
                str(row.get("service_mode", "")),
                str(row.get("auth_mode", "")),
                str(row.get("auth_status", "unknown")),
                str(row.get("auth_profile", "")),
                str(row.get("expires_at", "")),
                str(row.get("root", "")),
            ]
        )
    print_table(
        ["provider", "linux_user", "service", "service_mode", "auth_mode", "auth", "profile", "expires_at", "root"],
        table,
    )
    return 0


def cmd_runtime_login(args: argparse.Namespace, service: ZeroClawService) -> int:
    provider = _resolve_required_value(args.provider, field_name="provider")
    payload = service.local_claw_auth_login(provider)
    action = str(payload.get("action_performed", "login"))
    if action == "status":
        print_info(f"Linked login already ready for {provider}")
    elif action == "refresh":
        print_success(f"Refreshed linked login for {provider}")
    else:
        print_success(f"Completed linked login for {provider}")
    _print_auth_status(payload, title="Runtime Auth")
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
    target = service.export_state(_resolve_required_value(args.output, field_name="output"))
    print_success(f"State exported to {target}")
    return 0


def cmd_state_import(args: argparse.Namespace, service: ZeroClawService) -> int:
    source = _resolve_required_value(args.input, field_name="input")
    service.import_state(source, merge=bool(args.merge))
    if args.merge:
        print_success(f"State merged from {source}")
    else:
        print_success(f"State imported from {source}")
    return 0


def _print_agent(agent: dict[str, Any]) -> None:
    channels = agent.get("channels", [])
    migrated = sum(1 for row in channels if row.get("migrated_from"))
    credential_sync = agent.get("credential_sync", {})
    selected_bundles = ", ".join(str(item) for item in credential_sync.get("bundles", [])) or "<none>"
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
            f"auth_status: {agent.get('agent', {}).get('auth_status', 'unknown')}",
            f"auth_profile: {agent.get('agent', {}).get('auth_profile', '')}",
            f"auth_account: {agent.get('agent', {}).get('auth_account', '')}",
            f"auth_expires_at: {agent.get('agent', {}).get('auth_expires_at', '')}",
            f"core_prompts: {len(agent.get('core_prompts', {}))}",
            f"credential_bundles: {selected_bundles}",
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


def _print_auth_status(payload: dict[str, Any], *, title: str) -> None:
    print_panel(
        title,
        [
            f"agent_id: {payload.get('agent_id', '')}",
            f"provider: {payload.get('provider', '')}",
            f"linux_user: {payload.get('linux_user', '')}",
            f"home: {payload.get('home', '')}",
            f"auth_mode: {payload.get('auth_mode', '')}",
            f"auth_status: {payload.get('auth_status', 'unknown')}",
            f"auth_profile: {payload.get('auth_profile', '')}",
            f"account: {payload.get('account', '')}",
            f"expires_at: {payload.get('expires_at', '')}",
            f"last_refresh: {payload.get('last_refresh', '')}",
            f"source: {payload.get('source', '')}",
            f"detail: {payload.get('detail', '')}",
            f"login_required: {payload.get('login_required', False)}",
        ],
    )


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


def _resolve_required_value(
    value: str | None,
    *,
    field_name: str,
) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError(f"{field_name} is required")
    return token


def _resolve_agent_id(agent_id: str | None) -> str:
    return _resolve_required_value(agent_id, field_name="agent_id")


def _resolve_bundles(positional: list[str] | None, flags: list[str] | None) -> list[str]:
    bundles: list[str] = []
    for source in (positional or [], flags or []):
        for item in source:
            token = str(item).strip()
            if token:
                bundles.append(token)
    return bundles


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
