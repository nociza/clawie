from __future__ import annotations

import argparse
import errno
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from clawie import __version__
from clawie.default_names import choose_default_agent_name
from clawie.providers import get_provider, provider_names
from clawie.service import (
    SetupError,
    AgentExistsError,
    AgentNotFoundError,
    STATUS_SECTIONS,
    ClawieService,
)
from clawie.store import StateStore
from clawie.ui import (
    print_error,
    print_info,
    print_panel,
    print_success,
    print_table,
    print_warning,
    set_color_enabled,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawie",
        description="Clawie control plane for config, agents, runtimes, delegation, and status",
    )
    parser.add_argument(
        "--config-dir",
        help="Override state directory (default: ~/.clawie)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output (also honored through NO_COLOR)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the installed clawie version and exit",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{status,config,agent,channel,runtime,auth,addon,delegation,workspace,maintenance,control,clawied,production,health,event,backup,dashboard}",
    )

    _build_config_parser(subparsers)
    _build_agent_parser(subparsers)
    _build_channel_parser(subparsers)
    _build_runtime_parser(subparsers)
    _build_auth_parser(subparsers)
    _build_addon_parser(subparsers)
    _build_delegation_parser(subparsers)
    _build_workspace_parser(subparsers)
    _build_maintenance_parser(subparsers)
    _build_control_parser(subparsers)
    _build_clawied_parser(subparsers)
    _build_production_parser(subparsers)

    status = subparsers.add_parser(
        "status",
        help="Show a unified status overview across the whole fleet",
    )
    status.add_argument(
        "section",
        nargs="?",
        choices=STATUS_SECTIONS,
        metavar="SECTION",
        help="Limit to one section: " + ", ".join(STATUS_SECTIONS),
    )
    status.add_argument("--agent", default="", help="Focus a single agent")
    status.add_argument("--json", action="store_true", help="Emit the snapshot as JSON")
    status.add_argument(
        "--watch", action="store_true", help="Live view; refresh until Ctrl-C"
    )
    status.add_argument(
        "--interval", type=int, default=2, help="Watch refresh interval in seconds"
    )
    status.add_argument(
        "--refresh", action="store_true", help="Sample live CPU/memory metrics"
    )
    status.set_defaults(func=cmd_status)

    dashboard = subparsers.add_parser(
        "dashboard",
        help="(deprecated) alias for `status --watch`",
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
    dashboard.set_defaults(func=cmd_dashboard)

    health = subparsers.add_parser(
        "health",
        help="Run health checks",
    )
    health.add_argument(
        "--host-validate",
        action="store_true",
        help="Run Linux/root multi-user host isolation validation",
    )
    health.add_argument("--json", action="store_true", help="Emit the report as JSON")
    health.set_defaults(func=cmd_health)

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
        help="Git-backed knowledge backup, plus config/state snapshots",
    )
    backup_sub = backup.add_subparsers(
        dest="backup_command",
        required=True,
        metavar="{init,run,status,restore,export,import}",
    )

    backup_init = backup_sub.add_parser(
        "init",
        help="Create the backup git repo and enable continuous backup",
    )
    _add_positional_argument(
        backup_init,
        "path",
        metavar="PATH",
        help_text="Backup repo directory (default: <config-dir>/backup)",
    )
    backup_init.add_argument(
        "--remote",
        help="Git remote URL to push backups to (set as 'origin')",
    )
    backup_init.add_argument(
        "--no-auto",
        action="store_true",
        help="Register the repo but do not enable automatic backups",
    )
    backup_push_policy = backup_init.add_mutually_exclusive_group()
    backup_push_policy.add_argument(
        "--auto-push",
        dest="auto_push",
        action="store_true",
        default=None,
        help="Opt in to pushing new maintenance commits to the configured remote",
    )
    backup_push_policy.add_argument(
        "--no-auto-push",
        dest="auto_push",
        action="store_false",
        help="Disable automatic remote pushes while retaining local automatic backups",
    )
    backup_init.set_defaults(func=cmd_backup_init)

    backup_run = backup_sub.add_parser(
        "run",
        help="Snapshot fleet knowledge into the backup repo and commit",
    )
    backup_run.add_argument("--message", default="", help="Custom commit message")
    push_group = backup_run.add_mutually_exclusive_group()
    push_group.add_argument(
        "--push",
        dest="push",
        action="store_true",
        default=None,
        help="Push to the configured remote after committing",
    )
    push_group.add_argument(
        "--no-push",
        dest="push",
        action="store_false",
        help="Skip a configured automatic push for this run",
    )
    backup_run.set_defaults(func=cmd_backup_run)

    backup_status = backup_sub.add_parser(
        "status",
        help="Show backup repo state and last run",
    )
    backup_status.set_defaults(func=cmd_backup_status)

    backup_restore = backup_sub.add_parser(
        "restore",
        help="Restore agent prompts and workspace knowledge from the backup repo",
    )
    backup_restore.add_argument("--agent", default="", help="Only restore one agent")
    backup_restore.add_argument(
        "--no-workspace",
        action="store_true",
        help="Restore core prompts only; skip workspace knowledge files",
    )
    backup_restore.add_argument(
        "--no-apply-to-disk",
        action="store_true",
        help="Only update state; do not write prompt files to agent homes",
    )
    backup_restore.set_defaults(func=cmd_backup_restore)

    backup_export = backup_sub.add_parser(
        "export",
        help="Write a full-fidelity snapshot file (includes credentials)",
    )
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
    backup_import.add_argument(
        "--merge",
        action="store_true",
        help="Merge into current state instead of replacing it",
    )
    backup_import.add_argument(
        "--yes",
        action="store_true",
        help="Confirm replacement without an interactive prompt (not required with --merge)",
    )
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
    config_set.set_defaults(func=cmd_config_set)

    config_show = config_sub.add_parser(
        "show",
        help="Show current configuration status",
    )
    config_show.set_defaults(func=cmd_config_show)


def _build_auth_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    auth = subparsers.add_parser(
        "auth",
        help="Manage shared provider auth for all agents",
    )
    auth_sub = auth.add_subparsers(
        dest="shared_auth_command",
        required=True,
        metavar="{show,login,import,port,apply}",
    )

    auth_show = auth_sub.add_parser(
        "show",
        help="Show shared auth status for one or more providers",
    )
    _add_positional_argument(
        auth_show,
        "provider",
        metavar="PROVIDER",
        help_text="Provider name",
    )
    auth_show.set_defaults(func=cmd_shared_auth_show)

    auth_login = auth_sub.add_parser(
        "login",
        help="Run linked login against the shared auth store",
    )
    _add_positional_argument(
        auth_login,
        "provider",
        metavar="PROVIDER",
        help_text="Provider name",
    )
    auth_login.set_defaults(func=cmd_shared_auth_login)

    auth_import = auth_sub.add_parser(
        "import",
        help="Import existing Codex, Claude, or provider auth into the shared auth store",
    )
    _add_positional_argument(
        auth_import,
        "provider",
        metavar="PROVIDER",
        help_text="Provider name",
    )
    auth_import.add_argument(
        "--from",
        dest="source",
        required=True,
        choices=["provider", "codex", "claude"],
        help="Auth source to import",
    )
    auth_import.add_argument(
        "--source-home",
        help="Home directory to import from (default: current user home)",
    )
    auth_import.set_defaults(func=cmd_shared_auth_import)

    auth_port = auth_sub.add_parser(
        "port",
        help="Port shared auth sessions from one claw provider to another",
    )
    auth_port.add_argument(
        "--from",
        dest="from_provider",
        required=True,
        choices=provider_names(),
        help="Source provider to read shared auth from",
    )
    auth_port.add_argument(
        "--to",
        dest="to_provider",
        required=True,
        choices=provider_names(),
        help="Target provider to write shared auth into",
    )
    auth_port.set_defaults(func=cmd_shared_auth_port)

    auth_apply = auth_sub.add_parser(
        "apply",
        help="Link the shared auth store into one agent or all eligible agents",
    )
    _add_positional_argument(
        auth_apply,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Only apply to one agent",
    )
    auth_apply.set_defaults(func=cmd_shared_auth_apply)


def _build_addon_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    addon = subparsers.add_parser(
        "addon",
        help="Install and manage shared addon tools",
    )
    addon_sub = addon.add_subparsers(
        dest="addon_command",
        required=True,
        metavar="{list,show,install,auth}",
    )

    addon_list = addon_sub.add_parser("list", help="List available addons")
    addon_list.set_defaults(func=cmd_addons_list)

    addon_show = addon_sub.add_parser("show", help="Show one addon")
    _add_positional_argument(
        addon_show,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_show.set_defaults(func=cmd_addons_show)

    addon_install = addon_sub.add_parser("install", help="Install one addon CLI")
    _add_positional_argument(
        addon_install,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_install.set_defaults(func=cmd_addons_install)

    addon_auth = addon_sub.add_parser("auth", help="Manage shared addon credentials")
    addon_auth_sub = addon_auth.add_subparsers(
        dest="addon_auth_command",
        required=True,
        metavar="{show,login,import}",
    )

    addon_auth_show = addon_auth_sub.add_parser("show", help="Show shared addon auth status")
    _add_positional_argument(
        addon_auth_show,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_auth_show.set_defaults(func=cmd_addon_auth_show)

    addon_auth_login = addon_auth_sub.add_parser("login", help="Run shared addon login")
    _add_positional_argument(
        addon_auth_login,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_auth_login.set_defaults(func=cmd_addon_auth_login)

    addon_auth_import = addon_auth_sub.add_parser("import", help="Import addon credentials from a user or agent")
    _add_positional_argument(
        addon_auth_import,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_auth_import.add_argument("--source-home", help="Home directory to import from")
    addon_auth_import.add_argument("--from-agent", help="Managed agent ID to import from")
    addon_auth_import.set_defaults(func=cmd_addon_auth_import)


def _build_agent_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    agent = subparsers.add_parser(
        "agent",
        help="Manage agents, prompts, and credential access",
    )
    agent_sub = agent.add_subparsers(
        dest="agent_command",
        required=True,
        metavar="{create,clone,prompt,credentials,addon,auth,provider,service,list,show,delete,purge,create-batch}",
    )

    create = agent_sub.add_parser(
        "create",
        help="Create an agent definition only (no Linux runtime)",
    )
    _add_positional_argument(
        create,
        "agent_id",
        metavar="AGENT_ID",
        help_text="New agent ID (defaults to a random built-in name)",
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
    create.add_argument(
        "--no-delegation",
        action="store_true",
        help="Disable the delegation skill for this agent",
    )
    create.add_argument(
        "--model-tier",
        choices=["fast", "balanced", "power"],
        help="Default model tier for this agent",
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
    clone.add_argument(
        "--no-delegation",
        action="store_true",
        help="Disable the delegation skill for this cloned agent",
    )
    clone.add_argument(
        "--model-tier",
        choices=["fast", "balanced", "power"],
        help="Default model tier for this cloned agent",
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
        help="Start from configured default bundles (currently empty), then add explicit selections",
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
        help="When using --bundle, include configured default bundles too (currently empty)",
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

    addon = agent_sub.add_parser(
        "addon",
        help="Attach shared addon tools to an agent",
    )
    addon_sub = addon.add_subparsers(
        dest="agent_addon_command",
        required=True,
        metavar="{show,enable,disable,apply}",
    )

    addon_show = addon_sub.add_parser("show", help="Show addon access for an agent")
    _add_positional_argument(
        addon_show,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    addon_show.set_defaults(func=cmd_agents_addons_show)

    addon_enable = addon_sub.add_parser("enable", help="Enable one addon for an agent")
    _add_positional_argument(
        addon_enable,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    _add_positional_argument(
        addon_enable,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_enable.add_argument("--source-home", help="Import addon credentials from this home before enabling")
    addon_enable.add_argument("--from-agent", help="Import addon credentials from this managed agent before enabling")
    addon_enable.add_argument(
        "--login-if-missing",
        action="store_true",
        help="Run shared addon login automatically if credentials are missing",
    )
    addon_enable.set_defaults(func=cmd_agents_addons_enable)

    addon_disable = addon_sub.add_parser("disable", help="Disable one addon for an agent")
    _add_positional_argument(
        addon_disable,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    _add_positional_argument(
        addon_disable,
        "addon",
        metavar="ADDON",
        help_text="Addon ID",
    )
    addon_disable.set_defaults(func=cmd_agents_addons_disable)

    addon_apply = addon_sub.add_parser("apply", help="Apply enabled addons into the agent home")
    _add_positional_argument(
        addon_apply,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    _add_positional_argument(
        addon_apply,
        "addon",
        metavar="ADDON",
        help_text="Only apply one addon",
    )
    addon_apply.set_defaults(func=cmd_agents_addons_apply)

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

    service = agent_sub.add_parser(
        "service",
        help="Control a managed agent runtime service",
    )
    service_sub = service.add_subparsers(
        dest="agent_service_command",
        required=True,
        metavar="{start,stop,restart,status,apply-prompts}",
    )
    for action in ("start", "stop", "restart", "status"):
        action_parser = service_sub.add_parser(
            action,
            help=f"{action.title()} a managed agent runtime",
        )
        _add_positional_argument(
            action_parser,
            "agent_id",
            metavar="AGENT_ID",
            help_text="Agent ID",
        )
        action_parser.set_defaults(func=cmd_agents_service)

    apply_prompts_parser = service_sub.add_parser(
        "apply-prompts",
        help="Write configured prompt files to an agent workspace (may require sudo)",
    )
    _add_positional_argument(
        apply_prompts_parser,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    apply_prompts_parser.set_defaults(func=cmd_agents_apply_prompts)

    fix_perms = agent_sub.add_parser(
        "fix-permissions",
        help="Restore private ownership and permission modes for an agent home",
    )
    _add_positional_argument(
        fix_perms,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID",
    )
    fix_perms.add_argument(
        "--manager",
        default="",
        help=argparse.SUPPRESS,
    )
    fix_perms.set_defaults(func=cmd_agents_fix_permissions)

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
    agent_delete.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt",
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
    agent_purge.set_defaults(func=cmd_agent_purge)

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
    apply_preset.set_defaults(func=cmd_channel_apply)

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
    move.set_defaults(func=cmd_channel_move)


def _build_runtime_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    runtime = subparsers.add_parser(
        "runtime",
        help="Manage isolated Linux runtimes",
    )
    runtime_sub = runtime.add_subparsers(
        dest="runtime_command",
        required=True,
        metavar="{create,detect,install,status,version,login,service}",
    )

    create = runtime_sub.add_parser(
        "create",
        help="Create a Linux runtime and matching agent",
    )
    _add_positional_argument(
        create,
        "agent_id",
        metavar="AGENT_ID",
        help_text="Agent ID to create (defaults to a random built-in name)",
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
        help="Compatibility flag; default credential bundles are empty",
    )
    create.add_argument(
        "--no-delegation",
        action="store_true",
        help="Disable the delegation skill for the spawned agent",
    )
    create.set_defaults(func=cmd_runtime_create)

    detect = runtime_sub.add_parser(
        "detect",
        help="Detect installed runtimes in a home directory",
    )
    detect.add_argument(
        "--source-home",
        help="Home directory to inspect (default: current user home)",
    )
    detect.set_defaults(func=cmd_runtime_detect)

    install = runtime_sub.add_parser(
        "install",
        help="Install a provider runtime on this host",
    )
    _add_positional_argument(
        install,
        "provider",
        metavar="PROVIDER",
        help_text="Provider runtime to install",
    )
    install.set_defaults(func=cmd_runtime_install)

    status = runtime_sub.add_parser(
        "status",
        help="Show local runtime service and auth status",
    )
    status.set_defaults(func=cmd_runtime_status)

    version = runtime_sub.add_parser(
        "version",
        help="Show the installed openclaw version and whether it is supported",
    )
    version.set_defaults(func=cmd_runtime_version)

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

    service = runtime_sub.add_parser(
        "service",
        help="Control a local runtime service",
    )
    service_sub = service.add_subparsers(
        dest="runtime_service_command",
        required=True,
        metavar="{start,stop,restart,status}",
    )
    for action in ("start", "stop", "restart", "status"):
        action_parser = service_sub.add_parser(
            action,
            help=f"{action.title()} a local runtime service",
        )
        _add_positional_argument(
            action_parser,
            "provider",
            metavar="PROVIDER",
            help_text="Installed local provider name",
        )
        action_parser.set_defaults(func=cmd_runtime_service)


def _build_delegation_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    delegation = subparsers.add_parser("delegation", help="Recursive agent delegation")
    delegation_sub = delegation.add_subparsers(
        dest="delegation_command",
        required=True,
        metavar="{submit,deliver,repl,tree,tasks,spawn-session,stop-session,session-agents,cleanup,status}",
    )

    deleg_submit = delegation_sub.add_parser("submit", help="Delegate a task")
    deleg_submit.add_argument("--parent", required=True, help="Parent agent ID")
    deleg_submit.add_argument("--child", required=True, help="Child agent ID")
    deleg_submit.add_argument("--payload", default="{}", help="JSON payload")
    deleg_submit.add_argument("--timeout", type=float, default=300.0, help="Timeout seconds")
    deleg_submit.add_argument("--tier", choices=["fast", "balanced", "power"], help="Model tier")
    deleg_submit.add_argument(
        "--parent-task",
        default="",
        help="Active task ID that assigned work to --parent (for explicit recursive lineage)",
    )
    deleg_submit.set_defaults(func=cmd_delegation_submit)

    deleg_deliver = delegation_sub.add_parser(
        "deliver", help="Deliver a task to an agent's gateway and print the reply"
    )
    deleg_deliver.add_argument("--agent", required=True, help="Target agent ID")
    deleg_deliver.add_argument("--message", required=True, help="Task message")
    deleg_deliver.add_argument(
        "--tier", choices=["fast", "balanced", "power"], default="balanced", help="Model tier"
    )
    deleg_deliver.add_argument("--timeout", type=float, default=300.0, help="Timeout seconds")
    deleg_deliver.add_argument("--json", action="store_true", help="Emit the reply as JSON")
    deleg_deliver.set_defaults(func=cmd_delegation_deliver)

    deleg_repl = delegation_sub.add_parser("repl", help="Start agent REPL (blocks)")
    deleg_repl.add_argument("--agent-id", required=True, help="Agent ID")
    deleg_repl.add_argument(
        "--executor-agent",
        required=True,
        help="Managed agent whose live gateway executes session tasks",
    )
    deleg_repl.add_argument("--tier", choices=["fast", "balanced", "power"], help="Model tier")
    deleg_repl.set_defaults(func=cmd_delegation_repl)

    deleg_tree = delegation_sub.add_parser("tree", help="Print delegation tree")
    deleg_tree.add_argument("--agent-id", required=True, help="Root agent ID")
    deleg_tree.set_defaults(func=cmd_delegation_tree)

    deleg_tasks = delegation_sub.add_parser("tasks", help="List delegation tasks")
    deleg_tasks.add_argument("--agent-id", help="Filter by parent agent ID")
    deleg_tasks.add_argument("--status", help="Filter by status")
    deleg_tasks.add_argument("--limit", type=int, default=20)
    deleg_tasks.set_defaults(func=cmd_delegation_tasks)

    deleg_spawn = delegation_sub.add_parser(
        "spawn-session", help="Spawn a lightweight session sub-agent (no root needed)"
    )
    deleg_spawn.add_argument("--parent", required=True, help="Parent agent ID")
    deleg_spawn.add_argument("--child", required=True, help="Child agent ID to spawn")
    deleg_spawn.add_argument("--timeout", type=float, default=300.0, help="Handler timeout")
    deleg_spawn.add_argument("--tier", choices=["fast", "balanced", "power"], help="Model tier")
    deleg_spawn.set_defaults(func=cmd_delegation_spawn_session)

    deleg_stop = delegation_sub.add_parser(
        "stop-session", help="Stop a session sub-agent"
    )
    deleg_stop.add_argument("--parent", required=True, help="Parent agent ID")
    deleg_stop.add_argument("--child", required=True, help="Child agent ID to stop")
    deleg_stop.set_defaults(func=cmd_delegation_stop_session)

    deleg_session_list = delegation_sub.add_parser(
        "session-agents", help="List session sub-agents"
    )
    deleg_session_list.add_argument("--parent", required=True, help="Parent agent ID")
    deleg_session_list.set_defaults(func=cmd_delegation_session_agents)

    deleg_cleanup = delegation_sub.add_parser("cleanup", help="Remove stale sockets")
    deleg_cleanup.set_defaults(func=cmd_delegation_cleanup)

    deleg_status = delegation_sub.add_parser("status", help="Show active REPL agents")
    deleg_status.set_defaults(func=cmd_delegation_status)


def _build_workspace_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    workspace = subparsers.add_parser(
        "workspace",
        help="Publish and inspect shared read-only agent artifacts",
    )
    workspace_sub = workspace.add_subparsers(
        dest="workspace_command",
        required=True,
        metavar="{status,publish,list,show,mount,verify}",
    )

    status = workspace_sub.add_parser("status", help="Show published workspace status")
    status.set_defaults(func=cmd_workspace_status)

    publish = workspace_sub.add_parser(
        "publish",
        help="Publish a file or directory from an agent workspace",
    )
    _add_positional_argument(
        publish,
        "path",
        metavar="PATH",
        help_text="File or directory to publish",
    )
    publish.add_argument("--agent", default="", help="Publishing agent ID (default: infer from Linux user)")
    publish.add_argument(
        "--to",
        action="append",
        default=[],
        metavar="AGENT[,AGENT...]",
        help="Viewer agent ID or comma-separated agent IDs (repeatable)",
    )
    publish.add_argument("--title", default="", help="Human-readable title")
    publish.add_argument("--json", action="store_true", help="Emit publication metadata as JSON")
    publish.set_defaults(func=cmd_workspace_publish)

    list_cmd = workspace_sub.add_parser("list", help="List visible publications")
    list_cmd.add_argument("--agent", default="", help="Viewer agent ID (default: infer when possible)")
    list_cmd.add_argument("--publisher", default="", help="Filter by publisher agent ID")
    list_cmd.add_argument("--json", action="store_true", help="Emit publications as JSON")
    list_cmd.set_defaults(func=cmd_workspace_list)

    show = workspace_sub.add_parser("show", help="Show one publication")
    _add_positional_argument(
        show,
        "publication_id",
        metavar="PUB_ID",
        help_text="Publication ID",
    )
    show.add_argument("--agent", default="", help="Viewer agent ID for access check")
    show.add_argument("--json", action="store_true", help="Emit publication metadata as JSON")
    show.set_defaults(func=cmd_workspace_show)

    mount = workspace_sub.add_parser(
        "mount",
        help="Mount generated published views into agent workspaces",
    )
    mount_target = mount.add_mutually_exclusive_group()
    mount_target.add_argument("--agent", default="", help="Agent ID to mount (default: infer from Linux user)")
    mount_target.add_argument("--all", action="store_true", help="Mount all manageable agents")
    mount.set_defaults(func=cmd_workspace_mount)

    verify = workspace_sub.add_parser("verify", help="Verify publication hashes")
    _add_positional_argument(
        verify,
        "publication_id",
        metavar="PUB_ID",
        help_text="Publication ID (default: all)",
    )
    verify.add_argument("--json", action="store_true", help="Emit verification result as JSON")
    verify.set_defaults(func=cmd_workspace_verify)


def _add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=provider_names(),
        default=None,
        help="Agent provider (omit to keep the current provider)",
    )
    parser.add_argument("--api-key", help="Provider API key (if using api_key auth)")
    parser.add_argument(
        "--auth-mode",
        choices=["linked", "api_key", "none"],
        help="Provider auth mode (default is provider-specific)",
    )
    parser.add_argument("--subscription", default=None, help="Plan name")
    parser.add_argument("--workspace", default=None, help="Workspace slug")
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
        help="Optional API base URL for direct API-key integrations",
    )
    parser.add_argument(
        "--install-runtime",
        action="store_true",
        help="Record local runtime as installed",
    )
    parser.add_argument(
        "--control-github-repo",
        help="GitHub repo for confirmed control-agent escalation (owner/name)",
    )
    parser.add_argument(
        "--control-github-token-path",
        help="Path to a 0600 GitHub token file for control-agent escalation",
    )
    parser.add_argument(
        "--control-operator",
        action="append",
        dest="control_operators",
        help="Allowlisted local OS username or uid:<number> for control confirmations",
    )
    parser.add_argument(
        "--control-issue-label",
        action="append",
        dest="control_issue_labels",
        help="Label to apply to control-agent GitHub issues",
    )
    parser.add_argument(
        "--control-github-rate-limit-seconds",
        type=int,
        help="Minimum seconds between new non-duplicate control-agent GitHub issues or PRs",
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
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if not effective_argv:
        parser.print_help()
        return 0
    args = parser.parse_args(effective_argv)
    set_color_enabled(False if bool(args.no_color) else None)
    service = ClawieService(StateStore(config_dir=args.config_dir))

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
        if bool(getattr(args, "json", False)):
            print(
                json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}),
                file=sys.stderr,
            )
        else:
            print_error(str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        # EOFError covers interactive prompts running without stdin
        # (piped/cron); treat it like a user abort instead of a traceback.
        if bool(getattr(args, "json", False)):
            print(json.dumps({"ok": False, "error": "interrupted"}), file=sys.stderr)
        else:
            print_warning("Interrupted")
        return 130


def cmd_config_set(args: argparse.Namespace, service: ClawieService) -> int:
    provider = str(args.provider or "").strip().lower() or None
    current = service.setup_status()
    effective_provider = provider or str(current.get("provider", "openclaw") or "openclaw")
    provider_spec = get_provider(effective_provider)
    api_key = str(args.api_key).strip() if args.api_key is not None else None
    auth_mode = str(args.auth_mode or "").strip().lower() or None
    spawn_password = args.spawn_password
    subscription = str(args.subscription).strip() if args.subscription is not None else None
    workspace = str(args.workspace).strip() if args.workspace is not None else None
    api_url = str(args.api_url).strip() if args.api_url is not None else None

    if args.interactive:
        print_info("Interactive setup mode")
        provider = _prompt_with_default(
            f"Provider ({'/'.join(provider_names())})",
            effective_provider,
        ).lower()
        if provider not in set(provider_names()):
            raise ValueError("provider must be one of: " + ", ".join(provider_names()))
        provider_spec = get_provider(provider)
        current_auth_mode = str(current.get("auth_mode", "") or "").strip().lower()
        auth_mode = auth_mode or (
            current_auth_mode if provider == effective_provider else provider_spec.default_auth_mode
        )
        auth_mode = _prompt_with_default(
            f"Auth mode ({'/'.join(provider_spec.auth_modes)})",
            auth_mode,
        ).lower()
        stored_config = service.store.read_config()
        stored_credentials = stored_config.get("provider_credentials", {})
        target_credentials = (
            stored_credentials.get(provider, {}) if isinstance(stored_credentials, dict) else {}
        )
        target_has_api_key = bool(
            isinstance(target_credentials, dict)
            and str(target_credentials.get("api_key", "") or "").strip()
        ) or bool(
            provider == effective_provider
            and str(stored_config.get("api_key", "") or "").strip()
        )
        if auth_mode == "api_key" and api_key is None and not target_has_api_key:
            api_key = _prompt_required(f"{provider} API key")
        subscription = _prompt_with_default(
            "Subscription", subscription or str(current.get("subscription", "starter") or "starter")
        )
        workspace = _prompt_with_default(
            "Workspace", workspace or str(current.get("workspace", "default") or "default")
        )
        api_url = _prompt_with_default(
            "API URL",
            api_url if api_url is not None else str(current.get("api_url", provider_spec.default_api_url) or ""),
        )

    config = _clawied_service_call_or_unavailable(
        service,
        "setup",
        {
            "provider": provider,
            "api_key": api_key,
            "auth_mode": auth_mode,
            "spawn_password": spawn_password,
            "clear_spawn_password": bool(args.clear_spawn_password),
            "subscription": subscription,
            "workspace": workspace,
            "api_url": api_url,
            "install_runtime": bool(args.install_runtime),
        },
    )
    if config is _CLAWIED_UNAVAILABLE:
        config = service.setup(
            provider=provider,
            api_key=api_key,
            auth_mode=auth_mode,
            spawn_password=spawn_password,
            clear_spawn_password=bool(args.clear_spawn_password),
            subscription=subscription,
            workspace=workspace,
            api_url=api_url,
            install_runtime=bool(args.install_runtime),
        )
    control_kwargs = {
        "github_repo": getattr(args, "control_github_repo", None),
        "github_token_path": getattr(args, "control_github_token_path", None),
        "operators": getattr(args, "control_operators", None),
        "issue_labels": getattr(args, "control_issue_labels", None),
        "rate_limit_seconds": getattr(args, "control_github_rate_limit_seconds", None),
    }
    control_kwargs = {key: value for key, value in control_kwargs.items() if value is not None}
    control_settings: dict[str, Any] = {}
    if control_kwargs:
        result = _clawied_service_call_or_unavailable(
            service,
            "configure_control_escalation",
            control_kwargs,
        )
        if result is _CLAWIED_UNAVAILABLE:
            result = service.configure_control_escalation(**control_kwargs)
        control_settings = result
    status = service.setup_status()

    print_success("Clawie config updated")
    print_panel(
        "Config",
        [
            f"provider: {config.get('provider', '')}",
            f"workspace: {config.get('workspace', '')}",
            f"subscription: {config.get('subscription', '')}",
            f"api_url: {config.get('api_url', '') or '<not set>'}",
            f"auth_mode: {config.get('auth_mode', '')}",
            f"spawn_password_default: {'set' if status.get('spawn_password_configured') else 'not set'}",
            f"runtime_installed: {status.get('runtime_installed', False)}",
            f"api_key: {status.get('api_key', '') or '<not set>'}",
        ],
    )
    if control_settings:
        print_panel(
            "Control",
            [
                f"github_repo: {control_settings.get('github_repo', '') or '<not set>'}",
                f"github_token_path: {control_settings.get('github_token_path', '') or '<not set>'}",
                f"operator_allowlist: {', '.join(control_settings.get('operator_allowlist', [])) or '<none>'}",
                f"issue_labels: {', '.join(control_settings.get('issue_labels', [])) or '<none>'}",
                f"rate_limit_seconds: {control_settings.get('rate_limit_seconds', 0)}",
            ],
        )
    return 0


def cmd_config_show(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    return _print_setup_status(service)


def _print_setup_status(service: ClawieService) -> int:
    status = service.setup_status()
    print_panel(
        "Config",
        [
            f"configured: {status.get('configured', False)}",
            f"provider: {status.get('provider', '')}",
            f"workspace: {status.get('workspace', '')}",
            f"subscription: {status.get('subscription', '')}",
            f"api_url: {status.get('api_url', '') or '<not set>'}",
            f"auth_mode: {status.get('auth_mode', '')}",
            f"api_key: {status.get('api_key', '') or '<not set>'}",
            f"spawn_password_default: {'set' if status.get('spawn_password_configured') else 'not set'}",
            f"runtime_installed: {status.get('runtime_installed', False)}",
            f"updated_at: {status.get('updated_at', '')}",
        ],
    )
    if not status.get("configured"):
        print_warning("Config is incomplete. Run `clawie config set`.")
        return 1
    return 0


def cmd_shared_auth_show(args: argparse.Namespace, service: ClawieService) -> int:
    provider = str(args.provider or "").strip().lower()
    if provider:
        payload = service.shared_auth_status(provider)
        _print_auth_status(payload, title="Shared Auth")
        agents = payload.get("shared_agents", [])
        print_info("Linked agents: " + (", ".join(str(item) for item in agents) or "<none>"))
        return 0

    rows: list[list[str]] = []
    for payload in service.list_shared_auth_statuses():
        rows.append(
            [
                str(payload.get("provider", "")),
                str(payload.get("auth_status", "unknown")),
                str(payload.get("auth_profile", "")),
                str(payload.get("shared_scope", "")),
                str(len(payload.get("shared_agents", []))),
                str(payload.get("home", "")),
            ]
        )
    print_table(["provider", "auth", "profile", "scope", "agents", "home"], rows)
    return 0


def cmd_shared_auth_login(args: argparse.Namespace, service: ClawieService) -> int:
    provider = _resolve_required_value(args.provider, field_name="provider")
    payload = _clawied_service_call_or_unavailable(
        service,
        "shared_auth_login",
        {"provider": provider},
    )
    if payload is _CLAWIED_UNAVAILABLE:
        payload = service.shared_auth_login(provider)
    action = str(payload.get("action_performed", "login"))
    if action == "status":
        print_success(f"Shared auth already ready for {provider}")
    elif action == "refresh":
        print_success(f"Refreshed shared auth for {provider}")
    else:
        print_success(f"Completed shared auth login for {provider}")
    _print_auth_status(payload, title="Shared Auth")
    agents = payload.get("shared_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in agents) or "<none>"))
    _print_restart_required_agents(args, payload.get("restart_required_agents", []))
    return 0


def cmd_shared_auth_import(args: argparse.Namespace, service: ClawieService) -> int:
    provider = _resolve_required_value(args.provider, field_name="provider")
    result = _clawied_service_call_or_unavailable(
        service,
        "import_shared_auth",
        {
            "provider": provider,
            "source": str(args.source),
            "source_home": _resolve_optional_path_arg(args.source_home),
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.import_shared_auth(
            provider,
            source=str(args.source),
            source_home=args.source_home,
        )
    print_success(f"Imported {result.get('source', '')} auth into shared store for {provider}")
    print_info(f"Shared home: {result.get('home', '')}")
    updated_paths = result.get("updated_paths", [])
    if updated_paths:
        print_info("Updated shared auth paths:")
        for path in updated_paths:
            print(f"- {path}")
    updated_agents = result.get("updated_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in updated_agents) or "<none>"))
    skipped_agents = result.get("skipped_agents", [])
    if skipped_agents:
        print_warning("Skipped agents: " + ", ".join(str(item) for item in skipped_agents))
    _print_restart_required_agents(args, result.get("restart_required_agents", []))
    auth = result.get("auth", {})
    if auth:
        _print_auth_status(auth, title="Shared Auth")
    return 0


def cmd_shared_auth_port(args: argparse.Namespace, service: ClawieService) -> int:
    result = _clawied_service_call_or_unavailable(
        service,
        "port_shared_auth",
        {"from_provider": args.from_provider, "to_provider": args.to_provider},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.port_shared_auth(args.from_provider, args.to_provider)
    profiles = result.get("profiles", [])
    print_success(
        f"Ported {len(profiles)} auth profile(s) from "
        f"{result.get('from_provider', '')} to {result.get('to_provider', '')}"
    )
    print_info("Profiles: " + (", ".join(str(item) for item in profiles) or "<none>"))
    updated_paths = result.get("updated_paths", [])
    if updated_paths:
        print_info("Updated shared auth paths:")
        for path in updated_paths:
            print(f"- {path}")
    updated_agents = result.get("updated_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in updated_agents) or "<none>"))
    skipped_agents = result.get("skipped_agents", [])
    if skipped_agents:
        print_warning("Skipped agents: " + ", ".join(str(item) for item in skipped_agents))
    _print_restart_required_agents(args, result.get("restart_required_agents", []))
    auth = result.get("auth", {})
    if auth:
        _print_auth_status(auth, title="Shared Auth")
    return 0


def cmd_shared_auth_apply(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = str(args.agent_id or "").strip() or None
    result = _clawied_service_call_or_unavailable(
        service,
        "apply_shared_auth_links",
        {"agent_id": agent_id},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.apply_shared_auth_links(agent_id=agent_id)
    target = agent_id or "eligible agents"
    print_success(f"Applied shared auth copies for {target}")
    print_info(f"Shared home: {result.get('home', '')}")
    updated_agents = result.get("updated_agents", [])
    print_info("Updated agents: " + (", ".join(str(item) for item in updated_agents) or "<none>"))
    skipped_agents = result.get("skipped_agents", [])
    if skipped_agents:
        print_warning("Skipped agents: " + ", ".join(str(item) for item in skipped_agents))
    _print_restart_required_agents(args, result.get("restart_required_agents", []))
    return 0


def _print_restart_required_agents(args: argparse.Namespace, agents: Any) -> None:
    if not isinstance(agents, list):
        return
    tokens = [str(item).strip() for item in agents if str(item).strip()]
    if not tokens:
        return
    print_warning("Restart required for agents using updated shared auth: " + ", ".join(tokens))
    config_dir = Path(args.config_dir).expanduser() if getattr(args, "config_dir", None) else Path.home() / ".clawie"
    executable = shutil.which("clawie") or "clawie"
    restart_base = "sudo " + shlex.quote(executable) + " --config-dir " + shlex.quote(str(config_dir))
    if len(tokens) == 1:
        print_info("Run: " + restart_base + " agent service restart " + shlex.quote(tokens[0]))
        return
    print_info(
        "Run one restart per agent, for example: "
        + restart_base
        + " agent service restart "
        + shlex.quote(tokens[0])
    )


def cmd_addons_list(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    rows: list[list[str]] = []
    for addon in service.list_addons():
        rows.append(
            [
                str(addon.get("addon", "")),
                str(bool(addon.get("installed", False))),
                str(addon.get("auth_status", "unknown")),
                str(len(addon.get("linked_agents", []))),
                str(addon.get("config_dir", "")),
                str(addon.get("description", "")),
            ]
        )
    print_table(["addon", "installed", "auth", "agents", "config_dir", "description"], rows)
    return 0


def cmd_addons_show(args: argparse.Namespace, service: ClawieService) -> int:
    addon = _resolve_required_value(args.addon, field_name="addon")
    payload = service.get_addon_status(addon)
    _print_addon_status(payload, title="Addon")
    agents = payload.get("linked_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in agents) or "<none>"))
    active_displays = payload.get("active_displays", [])
    if active_displays:
        rows = []
        for d in active_displays:
            rows.append([
                str(d.get("agent_id", "")),
                str(d.get("display_number", "")),
                str(d.get("vnc_port", "")),
                str(d.get("novnc_port", "")),
                str(d.get("resolution", "")),
            ])
        print_table(["agent", "display", "vnc_port", "novnc_port", "resolution"], rows)
    return 0


def cmd_addons_install(args: argparse.Namespace, service: ClawieService) -> int:
    addon = _resolve_required_value(args.addon, field_name="addon")
    payload = _clawied_service_call_or_unavailable(
        service,
        "install_addon",
        {"addon": addon},
    )
    if payload is _CLAWIED_UNAVAILABLE:
        payload = service.install_addon(addon)
    print_success(f"Installed addon {payload.get('addon', addon)}")
    _print_addon_status(service.get_addon_status(addon), title="Addon")
    return 0


def cmd_addon_auth_show(args: argparse.Namespace, service: ClawieService) -> int:
    addon = _resolve_required_value(args.addon, field_name="addon")
    payload = service.shared_addon_auth_status(addon)
    _print_addon_auth_status(payload, title="Shared Addon Auth")
    agents = payload.get("linked_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in agents) or "<none>"))
    return 0


def cmd_addon_auth_login(args: argparse.Namespace, service: ClawieService) -> int:
    addon = _resolve_required_value(args.addon, field_name="addon")
    payload = _clawied_service_call_or_unavailable(
        service,
        "shared_addon_auth_login",
        {"addon": addon},
    )
    if payload is _CLAWIED_UNAVAILABLE:
        payload = service.shared_addon_auth_login(addon)
    action = str(payload.get("action_performed", "login"))
    if action == "status":
        print_success(f"Shared addon auth already ready for {addon}")
    else:
        print_success(f"Completed shared addon auth login for {addon}")
    _print_addon_auth_status(payload, title="Shared Addon Auth")
    agents = payload.get("linked_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in agents) or "<none>"))
    return 0


def cmd_addon_auth_import(args: argparse.Namespace, service: ClawieService) -> int:
    addon = _resolve_required_value(args.addon, field_name="addon")
    result = _clawied_service_call_or_unavailable(
        service,
        "import_shared_addon_auth",
        {
            "addon": addon,
            "source_home": _resolve_optional_path_arg(args.source_home),
            "source_agent": args.from_agent,
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.import_shared_addon_auth(
            addon,
            source_home=args.source_home,
            source_agent=args.from_agent,
        )
    print_success(f"Imported shared addon auth for {addon}")
    print_info(f"Config dir: {result.get('config_dir', '')}")
    updated_paths = result.get("updated_paths", [])
    if updated_paths:
        print_info("Updated paths:")
        for path in updated_paths:
            print(f"- {path}")
    updated_agents = result.get("updated_agents", [])
    print_info("Linked agents: " + (", ".join(str(item) for item in updated_agents) or "<none>"))
    skipped_agents = result.get("skipped_agents", [])
    if skipped_agents:
        print_warning("Skipped agents: " + ", ".join(str(item) for item in skipped_agents))
    auth = result.get("auth", {})
    if auth:
        _print_addon_auth_status(auth, title="Shared Addon Auth")
    return 0


def cmd_agents_create(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_new_agent_id(args.agent_id, service)
    channels = _resolve_channels(args.channel, args.channels_file)
    plugin_overrides = {}
    if getattr(args, "no_delegation", False):
        plugin_overrides["delegation"] = False
    agent = _clawied_service_call_or_unavailable(
        service,
        "create_agent",
        {
            "agent_id": agent_id,
            "display_name": args.display_name,
            "template": args.template,
            "clone_from": args.clone_from,
            "channel_strategy": args.channel_strategy,
            "channels": channels,
            "agent_version": args.agent_version,
            "provider": args.provider,
            "plugin_overrides": plugin_overrides or None,
        },
    )
    if agent is _CLAWIED_UNAVAILABLE:
        agent = service.create_agent(
            agent_id=agent_id,
            display_name=args.display_name,
            template=args.template,
            clone_from=args.clone_from,
            channel_strategy=args.channel_strategy,
            channels=channels,
            agent_version=args.agent_version,
            provider=args.provider,
            plugin_overrides=plugin_overrides or None,
        )
    tier = getattr(args, "model_tier", None)
    if tier:
        model_tier = _clawied_service_call_or_unavailable(
            service,
            "set_agent_model_tier",
            {"agent_id": agent_id, "tier": tier},
        )
        if model_tier is _CLAWIED_UNAVAILABLE:
            model_tier = service.set_agent_model_tier(agent_id, tier)
        agent.setdefault("agent", {})["model_tier"] = model_tier
    print_success(f"Created agent definition {agent['agent_id']}")
    print_info("Definition only: no Linux user or provider service was created or started.")
    _print_agent(agent)
    return 0


def cmd_agents_clone(args: argparse.Namespace, service: ClawieService) -> int:
    from_agent = _resolve_required_value(args.from_agent, field_name="from_agent")
    agent_id = _resolve_agent_id(args.agent_id)
    channels = _resolve_channels(args.channel, args.channels_file)
    plugin_overrides = {}
    if getattr(args, "no_delegation", False):
        plugin_overrides["delegation"] = False
    agent = _clawied_service_call_or_unavailable(
        service,
        "create_agent",
        {
            "agent_id": agent_id,
            "display_name": args.display_name,
            "template": "baseline",
            "clone_from": from_agent,
            "channel_strategy": args.channel_strategy,
            "channels": channels,
            "agent_version": args.agent_version,
            "provider": args.provider,
            "plugin_overrides": plugin_overrides or None,
        },
    )
    if agent is _CLAWIED_UNAVAILABLE:
        agent = service.create_agent(
            agent_id=agent_id,
            display_name=args.display_name,
            template="baseline",
            clone_from=from_agent,
            channel_strategy=args.channel_strategy,
            channels=channels,
            agent_version=args.agent_version,
            provider=args.provider,
            plugin_overrides=plugin_overrides or None,
        )
    tier = getattr(args, "model_tier", None)
    if tier:
        model_tier = _clawied_service_call_or_unavailable(
            service,
            "set_agent_model_tier",
            {"agent_id": agent_id, "tier": tier},
        )
        if model_tier is _CLAWIED_UNAVAILABLE:
            model_tier = service.set_agent_model_tier(agent_id, tier)
        agent.setdefault("agent", {})["model_tier"] = model_tier
    print_success(f"Cloned agent config from {from_agent} to {agent['agent_id']}")
    _print_agent(agent)
    return 0


def cmd_agents_clone_prompts(args: argparse.Namespace, service: ClawieService) -> int:
    from_agent = _resolve_required_value(args.from_agent, field_name="from_agent")
    to_agent = _resolve_required_value(args.to_agent, field_name="to_agent")
    updated = _clawied_service_call_or_unavailable(
        service,
        "clone_agent_prompts",
        {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "apply_to_disk": not bool(args.no_apply_to_disk),
        },
    )
    if updated is _CLAWIED_UNAVAILABLE:
        updated = service.clone_agent_prompts(
            from_agent=from_agent,
            to_agent=to_agent,
            apply_to_disk=not bool(args.no_apply_to_disk),
        )
    print_success(f"Cloned core prompts {from_agent} -> {to_agent}")
    _print_agent(updated)
    return 0


def cmd_agents_credentials_bundles(args: argparse.Namespace, service: ClawieService) -> int:
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


def cmd_agents_credentials_show(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = service.get_agent_credential_sync(agent_id)
    selected = ", ".join(str(item) for item in payload.get("selected_bundles", [])) or "<none>"
    print_panel(
        "Credential Sync",
        [
            f"agent_id: {payload.get('agent_id', '')}",
            f"linux_user: {payload.get('linux_user', '')}",
            f"selected: {selected}",
            f"shared_provider_auth: {payload.get('shared_provider_auth', False)}",
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


def cmd_agents_credentials_set(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    bundles = _resolve_bundles(getattr(args, "bundles", []), args.bundle)
    agent = _clawied_service_call_or_unavailable(
        service,
        "set_agent_credential_bundles",
        {
            "agent_id": agent_id,
            "bundles": bundles,
            "include_defaults": bool(args.include_defaults),
        },
    )
    if agent is _CLAWIED_UNAVAILABLE:
        agent = service.set_agent_credential_bundles(
            agent_id,
            bundles=bundles,
            include_defaults=bool(args.include_defaults),
        )
    selected = ", ".join(agent.get("credential_sync", {}).get("bundles", [])) or "<none>"
    print_success(f"Updated credential bundles for {agent_id}")
    print_info(f"Selected bundles: {selected}")
    return 0


def cmd_agents_credentials_sync(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    override_bundles = _resolve_bundles(getattr(args, "bundles", []), args.bundle)
    result = _clawied_service_call_or_unavailable(
        service,
        "sync_agent_credentials",
        {
            "agent_id": agent_id,
            "source_home": _resolve_optional_path_arg(args.source_home),
            "bundles": override_bundles or None,
            "include_defaults": bool(args.include_defaults),
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
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


def cmd_agents_credentials_revoke(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    bundles = _resolve_bundles(getattr(args, "bundles", []), args.bundle)
    result = _clawied_service_call_or_unavailable(
        service,
        "revoke_agent_credentials",
        {"agent_id": agent_id, "bundles": bundles or None},
    )
    if result is _CLAWIED_UNAVAILABLE:
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


def cmd_agents_addons_show(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = service.get_agent_addons(agent_id)
    _print_agent_addons(payload)
    try:
        ds = service.agent_display_status(agent_id)
        if ds.get("enabled"):
            status = ds.get("status", "unknown")
            print_panel(
                "Display",
                [
                    f"display: :{ds.get('display_number', '')}",
                    f"resolution: {ds.get('resolution', '')}",
                    f"vnc_port: {ds.get('vnc_port', '')}",
                    f"novnc_port: {ds.get('novnc_port', '')}",
                    f"novnc_url: http://<server-ip>:{ds.get('novnc_port', '')}/vnc.html",
                    f"status: {status}",
                ],
            )
            services = ds.get("services", {})
            if services:
                svc_rows = [[str(k), str(v)] for k, v in services.items()]
                print_table(["service", "status"], svc_rows)
    except Exception:
        pass
    return 0


def cmd_agents_addons_enable(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.addons import is_service_addon

    agent_id = _resolve_agent_id(args.agent_id)
    addon = _resolve_required_value(args.addon, field_name="addon")

    if is_service_addon(addon):
        result = _clawied_service_call_or_unavailable(
            service,
            "enable_agent_addon",
            {"agent_id": agent_id, "addon": addon},
        )
        if result is _CLAWIED_UNAVAILABLE:
            result = service.enable_agent_addon(agent_id, addon)
        if result.get("already_enabled"):
            print_info(f"Display already enabled for {agent_id}")
        else:
            print_success(f"Enabled display for {agent_id}")
        display_num = result.get("display_number", "")
        print_info(f"Display: :{display_num}")
        print_info(f"Resolution: {result.get('resolution', '')}")
        print_info(f"VNC port: {result.get('vnc_port', '')}")
        print_info(f"noVNC port: {result.get('novnc_port', '')}")
        print_info(f"noVNC URL: http://<server-ip>:{result.get('novnc_port', '')}/vnc.html")
        services = result.get("services", [])
        if services:
            print_info("Services: " + ", ".join(str(s) for s in services))
        _print_agent(result.get("agent", {}))
        return 0

    login_if_missing = bool(args.login_if_missing)
    if not login_if_missing and sys.stdin.isatty():
        try:
            status = service.shared_addon_auth_status(addon)
        except Exception:
            status = {}
        if str(status.get("auth_status", "")).strip().lower() != "ready":
            login_if_missing = _prompt_yes_no(
                f"No shared {addon} credentials are ready. Run shared login now?",
                default=False,
            )
    result = _clawied_service_call_or_unavailable(
        service,
        "enable_agent_addon",
        {
            "agent_id": agent_id,
            "addon": addon,
            "source_home": _resolve_optional_path_arg(args.source_home),
            "source_agent": args.from_agent,
            "login_if_missing": login_if_missing,
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.enable_agent_addon(
            agent_id,
            addon,
            source_home=args.source_home,
            source_agent=args.from_agent,
            login_if_missing=login_if_missing,
        )
    print_success(f"Enabled addon {result.get('addon', addon)} for {agent_id}")
    linked = result.get("linked_paths", [])
    if linked:
        print_info("Linked paths:")
        for path in linked:
            print(f"- {path}")
    if result.get("pending", False):
        print_warning("Addon is enabled in state but not yet applied because the agent has no manageable Linux home")
    _print_agent(result.get("agent", {}))
    return 0


def cmd_agents_addons_disable(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.addons import is_service_addon

    agent_id = _resolve_agent_id(args.agent_id)
    addon = _resolve_required_value(args.addon, field_name="addon")
    result = _clawied_service_call_or_unavailable(
        service,
        "disable_agent_addon",
        {"agent_id": agent_id, "addon": addon},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.disable_agent_addon(agent_id, addon)

    if is_service_addon(addon):
        print_success(f"Disabled display :{result.get('display_number', '')} for {agent_id}")
        stopped = result.get("stopped", [])
        if stopped:
            print_info("Stopped services: " + ", ".join(str(s) for s in stopped))
        removed_units = result.get("removed_units", [])
        if removed_units:
            print_info("Removed unit files: " + ", ".join(str(u) for u in removed_units))
        _print_agent(result.get("agent", {}))
        return 0

    print_success(f"Disabled addon {result.get('addon', addon)} for {agent_id}")
    removed = result.get("removed_paths", [])
    if removed:
        print_info("Removed paths:")
        for path in removed:
            print(f"- {path}")
    _print_agent(result.get("agent", {}))
    return 0


def cmd_agents_addons_apply(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    addons = [str(args.addon).strip()] if str(args.addon or "").strip() else None
    result = _clawied_service_call_or_unavailable(
        service,
        "apply_agent_addons",
        {"agent_id": agent_id, "addons": addons},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.apply_agent_addons(agent_id, addons=addons)
    print_success(f"Applied addons for {agent_id}")
    print_info("Addons: " + ", ".join(str(item) for item in result.get("addons", [])))
    linked = result.get("linked_paths", [])
    if linked:
        print_info("Linked paths:")
        for path in linked:
            print(f"- {path}")
    _print_agent(result.get("agent", {}))
    return 0


def cmd_agents_auth_show(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = service.agent_auth_status(agent_id)
    _print_auth_status(payload, title="Agent Auth")
    return 0


def cmd_agents_auth_login(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    payload = _clawied_service_call_or_unavailable(
        service,
        "agent_auth_login",
        {"agent_id": agent_id},
    )
    if payload is _CLAWIED_UNAVAILABLE:
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


def cmd_agents_service(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    action = _resolve_required_value(args.agent_service_command, field_name="action")
    result = _clawied_service_call_or_unavailable(
        service,
        "agent_service_action",
        {"agent_id": agent_id, "action": action},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.agent_service_action(agent_id, action)
    verb = {"start": "Started", "stop": "Stopped", "restart": "Restarted", "status": "Status"}.get(action, action)
    if action == "status":
        print_info(
            f"{agent_id}: {result.get('service_status', 'unknown')} "
            f"({result.get('service_mode', 'unknown')})"
        )
    else:
        print_success(f"{verb} service for {agent_id}")
    print_info("Provider: " + str(result.get("provider", "")))
    print_info("Linux user: " + str(result.get("linux_user", "")))
    print_info("Service mode: " + str(result.get("service_mode", "unknown")))
    output = str(result.get("output", "")).strip()
    if output:
        print_info("Output: " + output)
    return 0


def cmd_agents_apply_prompts(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    result = _clawied_service_call_or_unavailable(
        service,
        "apply_staged_prompts",
        {"agent_id": agent_id},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.apply_staged_prompts(agent_id)
    applied = result.get("applied", [])
    if applied:
        print_success(f"Applied {len(applied)} prompt(s) for {agent_id}: {', '.join(applied)}")
        # Restart the service so the gateway picks up updated workspace files.
        try:
            restart = _clawied_service_call_or_unavailable(
                service,
                "agent_service_action",
                {"agent_id": agent_id, "action": "restart"},
            )
            if restart is _CLAWIED_UNAVAILABLE:
                restart = service.agent_service_action(agent_id, "restart")
            print_success(f"Restarted service for {agent_id} ({restart.get('service_status', 'unknown')})")
        except Exception as exc:
            print_info(f"Could not restart service: {exc}")
            print_info("Run 'clawie agent service restart " + agent_id + "' manually.")
    else:
        print_info(f"No prompt changes to apply for {agent_id}")
    return 0


def cmd_agents_fix_permissions(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    manager = getattr(args, "manager", "") or ""
    result = _clawied_service_call_or_unavailable(
        service,
        "ensure_agent_permissions",
        {"agent_id": agent_id, "manager_user": manager},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.ensure_agent_permissions(agent_id, manager_user=manager)
    changes = result.get("changes", [])
    if changes:
        print_success(
            f"Private permissions restored for {agent_id} "
            f"(agent_user={result.get('linux_user', '?')})"
        )
        for change in changes:
            print_info(f"  {change}")
    else:
        print_info(f"No permission changes needed for {agent_id}")
    print_info("Cross-user changes must run with sudo/root or through clawied.")
    return 0


def cmd_agents_provider_set(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    provider = _resolve_required_value(args.provider, field_name="provider")
    result = _clawied_service_call_or_unavailable(
        service,
        "switch_agent_provider",
        {"agent_id": agent_id, "provider": provider},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.switch_agent_provider(agent_id, provider)
    agent = result["agent"]
    if bool(result.get("changed", True)):
        print_success(f"Changed provider for {agent_id} to {agent.get('agent', {}).get('provider', '')}")
    else:
        print_info(f"{agent_id} already uses {agent.get('agent', {}).get('provider', '')}")
    stopped = result.get("stopped_service", {})
    if stopped:
        print_info(
            f"Stopped {result.get('from_provider', '')}: "
            f"{stopped.get('service_status', 'unknown')} ({stopped.get('service_mode', 'unknown')})"
        )
    service_result = result.get("service", {})
    if service_result:
        action = str(service_result.get("action", "start")).strip().lower() or "start"
        verb = {"start": "Started", "restart": "Restarted", "stop": "Stopped", "status": "Status"}.get(action, "Started")
        print_info(
            f"{verb} {result.get('to_provider', '')}: "
            f"{service_result.get('service_status', 'unknown')} ({service_result.get('service_mode', 'unknown')})"
        )
    auth_prepare = result.get("auth_prepare", {})
    if bool(auth_prepare.get("prepared", False)):
        source = str(auth_prepare.get("source", "")).strip()
        source_home = str(auth_prepare.get("source_home", "")).strip()
        if source == "shared":
            print_info("Prepared shared auth: refreshed/login completed in shared store")
        elif source and source_home:
            print_info(f"Prepared shared auth: imported {source} session from {source_home}")
        elif source:
            print_info(f"Prepared shared auth: imported {source} session")
    channels = result.get("reconnected_channels", [])
    if channels:
        tokens = [f"{row.get('kind', '')}:{row.get('name', '')}" for row in channels]
        print_info("Reconnected channels: " + ", ".join(token for token in tokens if token))
    auth = result.get("auth", {})
    if auth:
        print_info(
            f"Auth after switch: {auth.get('auth_status', 'unknown')}"
            + (f" ({auth.get('detail', '')})" if str(auth.get("detail", "")).strip() else "")
        )
    _print_agent(service.get_dashboard_agent(agent_id))
    return 0


def cmd_agents_list(args: argparse.Namespace, service: ClawieService) -> int:
    agents = service.list_agents()
    if not agents:
        print_info("No agent definitions yet.")
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

def cmd_agents_show(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    agent = service.get_dashboard_agent(agent_id)
    _print_agent(agent)
    return 0


def cmd_agents_delete(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    if not bool(getattr(args, "yes", False)):
        print_warning(
            f"This will permanently delete agent definition '{agent_id}', including its prompts and channels."
        )
        try:
            confirmation = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            raise ValueError(
                "delete cancelled: no confirmation input available "
                "(use --yes for non-interactive runs)"
            ) from None
        if confirmation not in {"y", "yes"}:
            raise ValueError("delete cancelled by user")
    result = _clawied_service_call_or_unavailable(
        service,
        "delete_agent",
        {"agent_id": agent_id},
    )
    if result is _CLAWIED_UNAVAILABLE:
        service.delete_agent(agent_id)
    print_success(f"Deleted agent {agent_id}")
    return 0


def cmd_agents_batch_create(args: argparse.Namespace, service: ClawieService) -> int:
    source = _resolve_required_value(args.file, field_name="file")
    payload = _read_json_file(source)
    if not isinstance(payload, list):
        raise ValueError("batch file must be a JSON array")

    entries: list[dict[str, Any]] = []
    for idx, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"batch entry at index {idx} must be an object")
        entries.append(row)

    result = _clawied_service_call_or_unavailable(
        service,
        "batch_create_agents",
        {"entries": entries},
    )
    if result is _CLAWIED_UNAVAILABLE:
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


def cmd_channel_apply(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    agent = _clawied_service_call_or_unavailable(
        service,
        "bootstrap_channels",
        {"agent_id": agent_id, "preset": args.preset, "replace": bool(args.replace)},
    )
    if agent is _CLAWIED_UNAVAILABLE:
        agent = service.bootstrap_channels(
            agent_id=agent_id,
            preset=args.preset,
            replace=args.replace,
        )
    print_success(
        f"Applied {args.preset} preset for {agent_id} ({len(agent.get('channels', []))} channels)"
    )
    return 0


def cmd_channel_move(args: argparse.Namespace, service: ClawieService) -> int:
    from_agent = _resolve_required_value(args.from_agent, field_name="from_agent")
    to_agent = _resolve_required_value(args.to_agent, field_name="to_agent")
    agent = _clawied_service_call_or_unavailable(
        service,
        "migrate_channels",
        {"from_agent": from_agent, "to_agent": to_agent, "replace": bool(args.replace)},
    )
    if agent is _CLAWIED_UNAVAILABLE:
        agent = service.migrate_channels(
            from_agent=from_agent,
            to_agent=to_agent,
            replace=args.replace,
        )
    print_success(
        f"Migrated channels {from_agent} -> {to_agent} ({len(agent.get('channels', []))} channels)"
    )
    return 0


def cmd_runtime_create(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_new_agent_id(args.agent_id, service)
    linux_user = args.linux_user
    if not str(linux_user or "").strip() and not str(args.agent_id or "").strip():
        linux_user = agent_id.lower()
    plugin_overrides = {}
    if getattr(args, "no_delegation", False):
        plugin_overrides["delegation"] = False
    result = _clawied_service_call_or_unavailable(
        service,
        "spawn_linux_user",
        {
            "agent_id": agent_id,
            "linux_user": linux_user,
            "copy_configs": not bool(args.skip_config_copy),
            "source_home": _resolve_optional_path_arg(args.source_home),
            "template": args.template,
            "agent_version": args.agent_version,
            "provider": args.provider,
            "password": args.password,
            "password_hash": args.password_hash,
            "use_global_password": not bool(args.no_global_password),
            "clone_from_agent": args.clone_from_agent,
            "credential_bundles": list(args.credential_bundle or []),
            "include_default_credentials": not bool(args.no_default_credentials),
            "plugin_overrides": plugin_overrides or None,
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.spawn_linux_user(
            agent_id=agent_id,
            linux_user=linux_user,
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
            plugin_overrides=plugin_overrides or None,
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


def cmd_agent_purge(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = _resolve_agent_id(args.agent_id)
    if not args.yes:
        print_warning(f"This will permanently purge agent '{agent_id}' and its Linux user profile.")
        try:
            confirmation = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            raise ValueError(
                "purge cancelled: no confirmation input available (use --yes for non-interactive runs)"
            ) from None
        if confirmation not in {"y", "yes"}:
            raise ValueError("purge cancelled by user")
    result = _clawied_service_call_or_unavailable(
        service,
        "purge_agent",
        {"agent_id": agent_id},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.purge_agent(agent_id)
    print_success(f"Purged agent {result.get('agent_id', '')}")
    linux_user = str(result.get("linux_user", "")).strip()
    if linux_user:
        print_info(
            f"linux_user={linux_user} removed={bool(result.get('linux_user_removed', False))} "
            f"home_removed={bool(result.get('home_removed', False))} "
            f"runtime_stopped={bool(result.get('runtime_stopped', False))}"
        )
    return 0


def cmd_status(args: argparse.Namespace, service: ClawieService) -> int:
    sections = [args.section] if getattr(args, "section", None) else None
    agent_id = (getattr(args, "agent", "") or "").strip() or None
    as_json = bool(getattr(args, "json", False))
    watch = bool(getattr(args, "watch", False))
    interval = max(1, int(getattr(args, "interval", 2) or 2))
    refresh = bool(getattr(args, "refresh", False)) or watch

    def render_once() -> dict[str, Any]:
        snapshot = service.status_snapshot(
            agent_id=agent_id, sections=sections, refresh=refresh
        )
        if as_json:
            print(json.dumps(snapshot, indent=2, default=str))
        else:
            _print_status(snapshot)
        return snapshot

    # --watch gives a simple, non-curses live view (clear + reprint). When the
    # output is not a TTY (piped/redirected) we fall back to a single snapshot.
    if watch and not as_json and sys.stdout.isatty():
        try:
            while True:
                sys.stdout.write("\033[2J\033[H")  # clear screen, cursor home
                sys.stdout.flush()
                snapshot = render_once()
                exit_code = _status_snapshot_exit_code(snapshot)
                if exit_code:
                    return exit_code
                print()
                print_info(f"watching every {interval}s — press Ctrl-C to exit")
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
        return 0

    snapshot = render_once()
    return _status_snapshot_exit_code(snapshot)


def cmd_dashboard(args: argparse.Namespace, service: ClawieService) -> int:
    """Deprecated alias for ``clawie status --watch``."""
    print_warning("`clawie dashboard` is deprecated; use `clawie status --watch`.")
    args.section = None
    args.agent = getattr(args, "agent_id", "") or ""
    args.json = False
    args.refresh = True
    args.interval = max(1, int(getattr(args, "refresh_seconds", 2) or 2))
    # Live view on a TTY, single snapshot when piped/redirected.
    args.watch = sys.stdout.isatty()
    return cmd_status(args, service)


# ── status rendering ──────────────────────────────────────────────────────

def _status_section_error(payload: Any) -> str | None:
    if isinstance(payload, dict) and "error" in payload and len(payload) == 1:
        return str(payload["error"])
    return None


def _status_snapshot_exit_code(snapshot: dict[str, Any]) -> int:
    for payload in snapshot.values():
        error = _status_section_error(payload)
        if error is not None and _fatal_status_error(error):
            return 1
    health = snapshot.get("health")
    if isinstance(health, dict) and _status_section_error(health) is None:
        health_status = str(health.get("status", "unknown")).strip().lower()
        if health_status not in {"healthy", "degraded", "passed"}:
            return 1
    return 0


def _fatal_status_error(error: str) -> bool:
    fatal_prefixes = (
        "refusing to change permissions on non-clawie state directory:",
        "clawie state root must not be a symlink:",
        "clawie state root must be a real directory:",
        "clawie state permissions are not private;",
        "cannot safely lock clawie state root",
        "clawie state lock is not a directory:",
        "clawie database must not be a symlink:",
        "clawie database must be a regular non-symlink file:",
        "clawie database sidecar must be a regular file:",
        "clawie database sidecar permissions are not private:",
        "read-only status cannot inspect an uncheckpointed clawie WAL;",
        "timed out waiting for the clawie database lock",
    )
    return error.startswith(fatal_prefixes)


def _print_status(snapshot: dict[str, Any]) -> None:
    renderers = {
        "setup": _print_status_setup,
        "health": _print_status_health,
        "agents": _print_status_agents,
        "runtimes": _print_status_runtimes,
        "auth": _print_status_auth,
        "delegation": _print_status_delegation,
        "maintenance": _print_status_maintenance,
        "backup": _print_status_backup,
        "events": _print_status_events,
    }
    for name in STATUS_SECTIONS:
        if name in snapshot:
            renderers[name](snapshot[name])


def _print_status_setup(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"setup: {err}")
        return
    print_panel(
        "Setup",
        [
            f"configured: {payload.get('configured', False)}",
            f"provider: {payload.get('provider', '')}",
            f"auth_mode: {payload.get('auth_mode', '')}",
            f"workspace: {payload.get('workspace', '')}",
            f"subscription: {payload.get('subscription', '')}",
            f"runtime_installed: {payload.get('runtime_installed', False)}",
            f"updated_at: {payload.get('updated_at', '')}",
        ],
    )


def _print_status_health(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"health: {err}")
        return
    print_panel("Health", [f"overall: {payload.get('status', 'unknown')}"])
    rows = [
        [str(check.get("status", "")), str(check.get("message", ""))]
        for check in payload.get("checks", [])
    ]
    if rows:
        print_table(["status", "check"], rows)


def _print_status_agents(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"agents: {err}")
        return
    totals = payload.get("totals", {})
    print_panel(
        "Agents",
        [
            f"agents: {totals.get('agents', 0)}   channels: {totals.get('channels', 0)}",
            f"cpu: {totals.get('cpu_percent', 0)}%   mem: {totals.get('mem_percent', 0)}%",
            f"workspace: {payload.get('workspace', '')}   provider: {payload.get('provider', '')}",
        ],
    )
    rows = payload.get("rows", [])
    if not rows:
        print_info("No agent definitions yet.")
        return
    table = [
        [
            str(row.get("agent_id", "")),
            str(row.get("status", "")),
            str(row.get("provider", "")),
            str(row.get("model_tier", "")),
            f"{row.get('channels', 0)}/{row.get('channels_total', 0)}",
            str(row.get("cpu_percent", 0)),
            str(row.get("mem_percent", 0)),
            str(row.get("version", "")),
        ]
        for row in rows
    ]
    print_table(
        ["agent_id", "status", "provider", "tier", "channels", "cpu%", "mem%", "version"],
        table,
    )
    for row in rows:
        if str(row.get("provider_status", "ok")) != "ok" and row.get("provider_issue"):
            print_warning(f"{row.get('agent_id', '')}: {row.get('provider_issue', '')}")


def _print_status_runtimes(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"runtimes: {err}")
        return
    if not payload:
        print_info("No local runtimes detected.")
        return
    table = [
        [
            str(row.get("provider", "")),
            str(row.get("linux_user", "")),
            str(row.get("service_status", "unknown")),
            str(row.get("auth_status", "unknown")),
            str(row.get("expires_at", "")),
        ]
        for row in payload
    ]
    print_table(["provider", "linux_user", "service", "auth", "expires_at"], table)


def _print_status_auth(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"auth: {err}")
        return
    if not payload:
        print_info("No provider auth configured.")
        return
    table = [
        [
            str(row.get("provider", "")),
            str(row.get("auth_status", "unknown")),
            str(row.get("auth_mode", "")),
            str(row.get("expires_at", "")),
            "yes" if row.get("login_required") else "no",
        ]
        for row in payload
    ]
    print_table(["provider", "auth", "mode", "expires_at", "login_required"], table)


def _print_status_delegation(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"delegation: {err}")
        return
    active = payload.get("active_agents", [])
    tasks = payload.get("tasks", [])
    print_panel(
        "Delegation",
        [f"active agents: {len(active)}", f"recent tasks: {len(tasks)}"],
    )
    if active:
        print_table(
            ["agent_id", "alive", "socket"],
            [
                [
                    str(row.get("agent_id", "")),
                    "yes" if row.get("alive") else "no",
                    str(row.get("socket", "")),
                ]
                for row in active
            ],
        )
    if tasks:
        print_table(
            ["task", "parent", "child", "status", "tier"],
            [
                [
                    str(row.get("task_id", ""))[:8],
                    str(row.get("parent_agent_id", "")),
                    str(row.get("child_agent_id", "")),
                    str(row.get("status", "")),
                    str(row.get("model_tier", "")),
                ]
                for row in tasks
            ],
        )


def _print_status_maintenance(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"maintenance: {err}")
        return
    print_panel(
        "Maintenance",
        [
            f"enabled: {payload.get('enabled', False)}",
            f"interval_hours: {payload.get('interval_hours', '')}",
            f"cron_file_exists: {payload.get('cron_file_exists', False)}",
            f"cron_file: {payload.get('cron_file', '')}",
        ],
    )


def _print_status_backup(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"backup: {err}")
        return
    _print_backup_status_panel(payload)


def _print_backup_status_panel(payload: dict[str, Any]) -> None:
    print_panel(
        "Backup",
        [
            f"enabled: {payload.get('enabled', False)}",
            f"repo: {payload.get('repo', '')}",
            f"initialized: {payload.get('initialized', False)}",
            f"remote: {payload.get('remote', '') or '<none>'}",
            f"auto_push: {payload.get('auto_push', False)}",
            f"head: {payload.get('head', '') or '<no commits>'}",
            f"commits: {payload.get('commit_count', 0)}",
            f"dirty: {payload.get('dirty', False)}",
            f"interrupted_transaction: {payload.get('interrupted_transaction', False)}",
            f"last_status: {payload.get('last_status', 'never')}",
            f"last_attempt_at: {payload.get('last_attempt_at', '')}",
            f"last_run_at: {payload.get('last_run_at', '')}",
            f"last_error: {payload.get('last_error', '') or '<none>'}",
            f"validation_error: {payload.get('validation_error', '') or '<none>'}",
        ],
    )


def _print_status_events(payload: Any) -> None:
    err = _status_section_error(payload)
    if err:
        print_warning(f"events: {err}")
        return
    if not payload:
        print_info("No events recorded.")
        return
    table = [
        [
            str(row.get("timestamp", ""))[:19],
            str(row.get("type", "")),
            str(row.get("message", "")),
        ]
        for row in payload
    ]
    print_table(["timestamp", "type", "message"], table)


def cmd_runtime_detect(args: argparse.Namespace, service: ClawieService) -> int:
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


def cmd_runtime_install(args: argparse.Namespace, service: ClawieService) -> int:
    provider = _resolve_required_value(args.provider, field_name="provider")
    result = _clawied_service_call_or_unavailable(
        service,
        "install_provider_runtime",
        {"provider": provider},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.install_provider_runtime(provider)
    if bool(result.get("already_present", False)):
        print_info(f"Runtime already available for {provider}")
    else:
        print_success(f"Installed runtime for {provider}")
    print_panel(
        "Runtime Install",
        [
            f"provider: {result.get('provider', '')}",
            f"method: {result.get('method', '')}",
            f"package: {result.get('package', '')}",
            f"executable: {result.get('executable', '')}",
        ],
    )
    output = str(result.get("output", "")).strip()
    if output:
        print_info("Output: " + output)
    return 0


def cmd_runtime_version(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    gate = service.openclaw_version_gate()
    print_panel(
        "openclaw version",
        [
            f"version: {gate.get('version', '') or 'unknown'}",
            f"supported: {gate.get('supported', False)}",
            f"message: {gate.get('message', '')}",
        ],
    )
    if gate.get("degraded"):
        print_warning("openclaw version is unsupported; config writes degrade to read-only.")
        return 1
    print_success("openclaw version is supported.")
    return 0


def cmd_runtime_status(args: argparse.Namespace, service: ClawieService) -> int:
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


def cmd_runtime_login(args: argparse.Namespace, service: ClawieService) -> int:
    provider = _resolve_required_value(args.provider, field_name="provider")
    payload = _clawied_service_call_or_unavailable(
        service,
        "local_claw_auth_login",
        {"provider": provider},
    )
    if payload is _CLAWIED_UNAVAILABLE:
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


def cmd_runtime_service(args: argparse.Namespace, service: ClawieService) -> int:
    provider = _resolve_required_value(args.provider, field_name="provider")
    action = _resolve_required_value(args.runtime_service_command, field_name="action")
    result = _clawied_service_call_or_unavailable(
        service,
        "local_claw_service_action",
        {"provider": provider, "action": action},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.local_claw_service_action(provider, action)
    verb = {"start": "Started", "stop": "Stopped", "restart": "Restarted", "status": "Status"}.get(action, action)
    if action == "status":
        print_info(
            f"{provider}: {result.get('service_status', 'unknown')} "
            f"({result.get('service_mode', 'unknown')})"
        )
    else:
        print_success(f"{verb} runtime service for {provider}")
    output = str(result.get("output", "")).strip()
    if output:
        print_info("Output: " + output)
    return 0


def cmd_health(args: argparse.Namespace, service: ClawieService) -> int:
    host_validate = bool(getattr(args, "host_validate", False))
    report = service.host_validation_report() if host_validate else service.doctor()
    status = str(report.get("status", "unknown"))

    if bool(getattr(args, "json", False)):
        print(json.dumps(report, indent=2, default=str))
        if host_validate and status != "passed":
            return 2 if status == "skipped" else 1
        return 0 if status in {"healthy", "degraded", "passed"} else 1

    print_panel(
        "Host Validation" if host_validate else "Doctor",
        [f"overall: {status}"],
    )

    rows = []
    for check in report.get("checks", []):
        rows.append([str(check.get("status", "")), str(check.get("message", ""))])
    if rows:
        print_table(["status", "check"], rows)

    if host_validate:
        if status == "passed":
            print_success("Host validation passed")
            return 0
        if status == "skipped":
            print_warning("Host validation skipped")
            return 2
        print_error("Host validation did not pass")
        return 1

    if status == "healthy":
        print_success("All critical checks passed")
        return 0
    if status == "degraded":
        print_warning("Non-critical warnings found")
        return 0
    print_error("Critical setup issues found")
    return 1


def cmd_events_list(args: argparse.Namespace, service: ClawieService) -> int:
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


def cmd_workspace_status(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    payload = service.workspace_status()
    print_panel(
        "Published Workspace",
        [
            f"root: {payload.get('root', '')}",
            f"initialized: {payload.get('initialized', False)}",
            f"publications: {payload.get('publications', 0)}",
            f"views: {payload.get('views', '')}",
        ],
    )
    return 0


def cmd_workspace_publish(args: argparse.Namespace, service: ClawieService) -> int:
    source = _resolve_required_value(args.path, field_name="path")
    result = service.workspace_publish(
        _resolve_path_arg(source),
        agent_id=str(args.agent or ""),
        visible_to=_parse_workspace_viewers(args.to),
        title=str(args.title or ""),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    print_success(f"Published {result.get('publication_id', '')}")
    print_panel(
        "Publication",
        [
            f"publisher: {result.get('publisher_agent_id', '')}",
            f"title: {result.get('title', '')}",
            f"visible_to: {', '.join(str(item) for item in result.get('visible_to', []))}",
            f"files: {result.get('file_count', 0)}",
            f"path: {result.get('path', '')}",
        ],
    )
    mounts = result.get("mounts", {})
    mounted = mounts.get("mounted", []) if isinstance(mounts, dict) else []
    skipped = mounts.get("skipped", []) if isinstance(mounts, dict) else []
    for row in mounted:
        print_info(f"Mounted for {row.get('agent_id', '')}: {row.get('target', '')}")
    for row in skipped:
        print_warning(f"Mount skipped for {row.get('agent_id', '')}: {row.get('reason', '')}")
    return 0


def cmd_workspace_list(args: argparse.Namespace, service: ClawieService) -> int:
    rows = service.workspace_list(
        agent_id=str(args.agent or ""),
        publisher_agent_id=str(args.publisher or ""),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(rows, indent=2, sort_keys=True, default=str))
        return 0
    if not rows:
        print_info("No publications found.")
        return 0
    table: list[list[str]] = []
    for row in rows:
        table.append(
            [
                str(row.get("publication_id", "")),
                str(row.get("publisher_agent_id", "")),
                str(row.get("title", "")),
                str(row.get("created_at", "")),
                str(row.get("file_count", 0)),
                str(row.get("view_path", row.get("path", ""))),
            ]
        )
    print_table(["publication", "publisher", "title", "created", "files", "path"], table)
    return 0


def cmd_workspace_show(args: argparse.Namespace, service: ClawieService) -> int:
    publication_id = _resolve_required_value(args.publication_id, field_name="publication_id")
    result = service.workspace_show(publication_id, agent_id=str(args.agent or ""))
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    print_panel(
        "Publication",
        [
            f"publication_id: {result.get('publication_id', '')}",
            f"publisher: {result.get('publisher_agent_id', '')}",
            f"title: {result.get('title', '')}",
            f"mode: {result.get('mode', '')}",
            f"created_at: {result.get('created_at', '')}",
            f"visible_to: {', '.join(str(item) for item in result.get('visible_to', []))}",
            f"path: {result.get('path', '')}",
        ],
    )
    manifest = result.get("manifest", {})
    files = manifest.get("files", []) if isinstance(manifest, dict) else []
    table = [
        [str(item.get("path", "")), str(item.get("size", 0)), str(item.get("sha256", ""))[:12]]
        for item in files
        if isinstance(item, dict)
    ]
    if table:
        print_table(["path", "bytes", "sha256"], table)
    return 0


def cmd_workspace_mount(args: argparse.Namespace, service: ClawieService) -> int:
    result = service.workspace_mount(
        agent_id=str(args.agent or ""),
        all_agents=bool(args.all),
    )
    mounted = result.get("mounted", [])
    skipped = result.get("skipped", [])
    if mounted:
        print_success(f"Mounted published workspace for {len(mounted)} agent(s)")
        rows = [
            [str(row.get("agent_id", "")), str(row.get("target", "")), str(row.get("view", ""))]
            for row in mounted
        ]
        print_table(["agent", "target", "view"], rows)
    else:
        print_info("No published workspace mounts changed.")
    for row in skipped:
        print_warning(f"Skipped {row.get('agent_id', '')}: {row.get('reason', '')}")
    return 0


def cmd_workspace_verify(args: argparse.Namespace, service: ClawieService) -> int:
    publication_id = str(args.publication_id or "").strip()
    result = service.workspace_verify(publication_id)
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result.get("status") == "ok" else 1
    print_panel(
        "Published Workspace Verify",
        [
            f"status: {result.get('status', '')}",
            f"publications: {result.get('publications', 0)}",
            f"files: {result.get('files', 0)}",
        ],
    )
    failures = result.get("failures", [])
    if failures:
        rows = [
            [str(row.get("publication_id", "")), str(row.get("path", "")), str(row.get("reason", ""))]
            for row in failures
        ]
        print_table(["publication", "path", "reason"], rows)
        return 1
    print_success("Published workspace hashes verified")
    return 0


def cmd_backup_init(args: argparse.Namespace, service: ClawieService) -> int:
    repo_path = _resolve_optional_path_arg(args.path)
    result = _clawied_service_call_or_unavailable(
        service,
        "backup_init",
        {
            "repo_path": repo_path,
            "remote": args.remote,
            "enable": not bool(args.no_auto),
            "auto_push": args.auto_push,
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.backup_init(
            repo_path=repo_path,
            remote=args.remote,
            enable=not bool(args.no_auto),
            auto_push=args.auto_push,
        )
    if result.get("created"):
        print_success(f"Created backup repo at {result.get('repo', '')}")
    else:
        print_success(f"Backup repo ready at {result.get('repo', '')}")
    print_info("Remote: " + (str(result.get("remote", "")) or "<none>"))
    if result.get("enabled"):
        print_info("Automatic backup: enabled (runs on every maintenance pass)")
        status = service.maintenance_status()
        if not status.get("enabled"):
            print_warning(
                "Maintenance cron is not enabled; run 'sudo clawie maintenance enable' "
                "so backups run automatically, or run 'clawie backup run' manually."
            )
    else:
        print_info("Automatic backup: disabled (run 'clawie backup run' manually)")
    print_info(f"Automatic remote push: {'enabled' if result.get('auto_push') else 'disabled'}")
    return 0


def cmd_backup_run(args: argparse.Namespace, service: ClawieService) -> int:
    result = _clawied_service_call_or_unavailable(
        service,
        "backup_run",
        {"message": str(args.message or ""), "push": args.push},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.backup_run(message=str(args.message or ""), push=args.push)
    status = str(result.get("status", "completed") or "completed")
    if status != "completed":
        if result.get("incomplete"):
            print_warning(
                "Backup collection was incomplete; the previous complete snapshot was preserved."
            )
        else:
            print_warning("Backup committed locally, but remote durability was not achieved.")
    elif result.get("changed"):
        print_success(f"Backup committed {str(result.get('commit', ''))[:10]} in {result.get('repo', '')}")
    else:
        print_info(f"Backup up to date (no changes) in {result.get('repo', '')}")
    agents = result.get("agents", [])
    print_info(f"Backed up {len(agents)} agent(s), {result.get('files', 0)} file(s)")
    if result.get("pushed"):
        print_info("Pushed to remote origin")
    push_error = str(result.get("push_error", "")).strip()
    if push_error:
        print_warning(f"Push failed: {push_error}")
    skipped = result.get("skipped", [])
    for row in skipped:
        print_warning(f"{row.get('agent_id', '')}: {row.get('reason', '')}")
    error = str(result.get("error", "") or "").strip()
    if error and not push_error:
        print_warning(error)
    return 0 if status == "completed" else 1


def cmd_backup_status(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    payload = service.backup_status()
    _print_backup_status_panel(payload)
    if not payload.get("git_available"):
        print_warning("git is not installed; backup commands will fail until it is.")
        return 1
    if not payload.get("initialized"):
        print_info("Backup repo not initialized. Run 'clawie backup init'.")
        return 1
    if payload.get("validation_error"):
        print_warning(str(payload["validation_error"]))
        return 1
    return 0


def cmd_backup_restore(args: argparse.Namespace, service: ClawieService) -> int:
    agent_id = (str(args.agent or "").strip()) or None
    result = _clawied_service_call_or_unavailable(
        service,
        "backup_restore",
        {
            "agent_id": agent_id,
            "apply_to_disk": not bool(args.no_apply_to_disk),
            "include_workspace": not bool(args.no_workspace),
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.backup_restore(
            agent_id=agent_id,
            apply_to_disk=not bool(args.no_apply_to_disk),
            include_workspace=not bool(args.no_workspace),
        )
    restored = result.get("restored", {})
    print_success(f"Restored {len(restored)} agent(s) from {result.get('repo', '')}")
    for token, counts in sorted(restored.items()):
        print_info(
            f"{token}: {counts.get('prompts', 0)} prompt(s), "
            f"{counts.get('workspace_files', 0)} workspace file(s)"
        )
    for row in result.get("skipped", []):
        print_warning(f"{row.get('agent_id', '')}: {row.get('reason', '')}")
    return 0


def cmd_state_export(args: argparse.Namespace, service: ClawieService) -> int:
    target = service.export_state(_resolve_required_value(args.output, field_name="output"))
    print_success(f"State exported to {target}")
    print_warning("Snapshot contains unredacted credentials; keep the file private.")
    return 0


def cmd_state_import(args: argparse.Namespace, service: ClawieService) -> int:
    source = _resolve_required_value(args.input, field_name="input")
    if not args.merge and not args.yes:
        print_warning("This will replace the current Clawie configuration and fleet state.")
        try:
            confirmation = input("Proceed? [y/N]: ").strip().lower()
        except EOFError:
            raise ValueError(
                "import cancelled: no confirmation input available (use --yes for non-interactive runs)"
            ) from None
        if confirmation not in {"y", "yes"}:
            raise ValueError("import cancelled by user")
    result = _clawied_service_call_or_unavailable(
        service,
        "import_state",
        {"input_path": _resolve_path_arg(source), "merge": bool(args.merge)},
    )
    if result is _CLAWIED_UNAVAILABLE:
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
    enabled_addons = ", ".join(
        sorted(
            str(name)
            for name, payload in agent.get("addons", {}).items()
            if isinstance(payload, dict) and bool(payload.get("enabled", False))
        )
    ) or "<none>"
    print_panel(
        "Agent",
        [
            f"agent_id: {agent.get('agent_id', agent.get('user_id', ''))}",
            f"display_name: {agent.get('display_name', '')}",
            f"template: {agent.get('source_template', '')}",
            f"clone_from: {agent.get('clone_from', '') or '<none>'}",
            f"channel_strategy: {agent.get('channel_strategy', '')}",
            f"channels: {len(channels)}",
            f"migrated_channels: {migrated}",
            f"provider: {agent.get('agent', {}).get('provider', '')}",
            f"provider_status: {agent.get('agent', {}).get('provider_status', 'ok')}",
            f"provider_issue: {agent.get('agent', {}).get('provider_issue', '')}",
            f"provider_remediation: {agent.get('agent', {}).get('provider_remediation', '')}",
            f"model_tier: {agent.get('agent', {}).get('model_tier', 'balanced')}",
            f"auth_mode: {agent.get('agent', {}).get('auth_mode', '')}",
            f"auth_status: {agent.get('agent', {}).get('auth_status', 'unknown')}",
            f"auth_profile: {agent.get('agent', {}).get('auth_profile', '')}",
            f"auth_account: {agent.get('agent', {}).get('auth_account', '')}",
            f"auth_expires_at: {agent.get('agent', {}).get('auth_expires_at', '')}",
            f"channel_status: {agent.get('agent', {}).get('channel_status_source', 'state')}",
            f"channel_detail: {agent.get('agent', {}).get('channel_status_detail', '')}",
            f"core_prompts: {len(agent.get('core_prompts', {}))}",
            f"credential_bundles: {selected_bundles}",
            f"addons: {enabled_addons}",
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
                    str(channel.get("channel_source", "")),
                    str(channel.get("discovered_provider", "")),
                    str(channel.get("external_id", "")),
                    str(channel.get("migrated_from", "")),
                ]
            )
        print_table(
            ["kind", "name", "enabled", "source", "provider", "external_id", "migrated_from"],
            rows,
        )

    plugins = agent.get("agent", {}).get("plugins", {})
    if plugins:
        plugin_rows = [[str(key), str(bool(value))] for key, value in sorted(plugins.items())]
        print()
        print_table(["plugin", "enabled"], plugin_rows)

    addons = agent.get("addons", {})
    addon_rows = []
    if isinstance(addons, dict):
        for key, payload in sorted(addons.items()):
            if not isinstance(payload, dict):
                continue
            extra = ""
            if key == "display" and payload.get("display_number"):
                extra = f" :{payload['display_number']} vnc={payload.get('vnc_port', '')} novnc={payload.get('novnc_port', '')}"
            addon_rows.append(
                [
                    str(key),
                    str(bool(payload.get("enabled", False))),
                    str(payload.get("credential_mode", "")),
                    str(payload.get("last_applied_at", "")) + extra,
                ]
            )
    if addon_rows:
        print()
        print_table(["addon", "enabled", "mode", "last_applied_at"], addon_rows)


def _print_addon_status(payload: dict[str, Any], *, title: str) -> None:
    lines = [
        f"addon: {payload.get('addon', '')}",
        f"label: {payload.get('label', '')}",
        f"description: {payload.get('description', '')}",
        f"installed: {payload.get('installed', False)}",
        f"executable: {payload.get('executable', '')}",
        f"install_method: {payload.get('install_method', '')}",
        f"install_package: {payload.get('install_package', '')}",
        f"auth_status: {payload.get('auth_status', 'unknown')}",
        f"auth_detail: {payload.get('auth_detail', '')}",
        f"config_dir: {payload.get('config_dir', '')}",
        f"shared_scope: {payload.get('shared_scope', '')}",
    ]
    print_panel(title, lines)


def _print_auth_status(payload: dict[str, Any], *, title: str) -> None:
    lines = [
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
    ]
    if "shared_scope" in payload:
        lines.append(f"shared_scope: {payload.get('shared_scope', '')}")
    if "shared_provider_auth" in payload:
        lines.append(f"shared_provider_auth: {payload.get('shared_provider_auth', False)}")
    print_panel(title, lines)


def _print_addon_auth_status(payload: dict[str, Any], *, title: str) -> None:
    lines = [
        f"addon: {payload.get('addon', '')}",
        f"label: {payload.get('label', '')}",
        f"config_dir: {payload.get('config_dir', '')}",
        f"auth_status: {payload.get('auth_status', 'unknown')}",
        f"source: {payload.get('source', '')}",
        f"detail: {payload.get('detail', '')}",
        f"login_required: {payload.get('login_required', False)}",
        f"client_secret_present: {payload.get('client_secret_present', False)}",
        f"credentials_path: {payload.get('credentials_path', '')}",
        f"shared_scope: {payload.get('shared_scope', '')}",
    ]
    print_panel(title, lines)


def _print_agent_addons(payload: dict[str, Any]) -> None:
    print_panel(
        "Agent Addons",
        [
            f"agent_id: {payload.get('agent_id', '')}",
            f"linux_user: {payload.get('linux_user', '')}",
            f"home: {payload.get('home', '')}",
        ],
    )
    rows = []
    for item in payload.get("addons", []):
        access = str(item.get("access_status", "ok"))
        if access == "ok":
            access = ""
        rows.append(
            [
                str(item.get("addon", "")),
                str(bool(item.get("enabled", False))),
                str(bool(item.get("installed", False))),
                str(item.get("auth_status", "unknown")),
                access,
                str(bool(item.get("applied", False))),
                str(item.get("last_applied_at", "")),
            ]
        )
    if rows:
        print_table(["addon", "enabled", "installed", "auth", "access", "applied", "last_applied_at"], rows)


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


def _parse_workspace_viewers(values: list[str] | None) -> list[str]:
    rows: list[str] = []
    for value in values or []:
        for item in str(value or "").split(","):
            token = item.strip()
            if token:
                rows.append(token)
    return rows


def _read_json_file(path: str) -> Any:
    source = Path(path).expanduser()
    with source.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_path_arg(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve())


def _resolve_optional_path_arg(value: str | Path | None) -> str | None:
    token = str(value or "").strip()
    if not token:
        return None
    return _resolve_path_arg(token)


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


def _resolve_new_agent_id(agent_id: str | None, service: ClawieService) -> str:
    token = str(agent_id or "").strip()
    if token:
        return token
    state = service.store.read_state()
    return choose_default_agent_name(state.get("agents", {}).keys())


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


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{label} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


# ── Delegation handlers ─────────────────────────────────────────────────────


def cmd_delegation_submit(args: argparse.Namespace, service: ClawieService) -> int:
    import json as _json

    payload = _json.loads(args.payload)
    if not isinstance(payload, dict):
        raise ValueError("--payload must be a JSON object")
    delegation_socket = str(os.environ.get("CLAWIE_DELEGATION_SOCKET", "")).strip()
    if delegation_socket:
        from clawie.daemon import request_ipc_socket

        # The daemon derives parent identity from the authenticated peer/socket
        # binding. Do not forward the user-supplied --parent value.
        result = request_ipc_socket(
            delegation_socket,
            "delegation_request",
            {
                "child_id": args.child,
                "payload": payload,
                "timeout": args.timeout,
                "model_tier": getattr(args, "tier", "") or "",
                "parent_task_id": getattr(args, "parent_task", "") or "",
            },
            timeout=max(10.0, float(args.timeout) + 10.0),
        )
    else:
        result = _clawied_service_call_or_unavailable(
            service,
            "delegate_task",
            {
                "parent_id": args.parent,
                "child_id": args.child,
                "payload": payload,
                "timeout": args.timeout,
                "model_tier": getattr(args, "tier", "") or "",
                "parent_task_id": getattr(args, "parent_task", "") or "",
            },
        )
        if result is _CLAWIED_UNAVAILABLE:
            result = service.delegate_task(
                parent_id=args.parent,
                child_id=args.child,
                payload=payload,
                timeout=args.timeout,
                model_tier=getattr(args, "tier", "") or "",
                parent_task_id=getattr(args, "parent_task", "") or "",
            )
    print_panel(
        "Delegation Result",
        [
            f"task_id: {result.get('task_id', '')}",
            f"status: {result.get('status', '')}",
            f"tier: {result.get('model_tier', '')}",
            f"depth: {result.get('depth', '')}",
            *([f"error: {result.get('error', '')}"] if result.get("error") else []),
            f"result: {_json.dumps(result.get('result', {}), indent=2)[:200]}",
        ],
    )
    return 0 if result.get("status") != "failed" else 1


def cmd_delegation_deliver(args: argparse.Namespace, service: ClawieService) -> int:
    result = _clawied_service_call_or_unavailable(
        service,
        "deliver_to_agent",
        {
            "agent_id": args.agent,
            "message": args.message,
            "tier": getattr(args, "tier", "balanced") or "balanced",
            "timeout": args.timeout,
        },
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.deliver_to_agent(
            args.agent,
            args.message,
            tier=getattr(args, "tier", "balanced") or "balanced",
            timeout=args.timeout,
        )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    if result.get("ok"):
        print_success(f"Delivered to {args.agent}")
        output = str(result.get("output", "")).strip()
        if output:
            print(output)
        return 0
    print_error(f"Delivery to {args.agent} failed: {result.get('error', '')}")
    return 1


def cmd_delegation_repl(args: argparse.Namespace, service: ClawieService) -> int:
    service.start_agent_repl(
        args.agent_id,
        model_tier=getattr(args, "tier", "") or "",
        executor_agent_id=args.executor_agent,
    )
    return 0


def cmd_delegation_tree(args: argparse.Namespace, service: ClawieService) -> int:
    lines = service.delegation_tree_lines(args.agent_id)
    if not lines:
        print_info("No delegation tree found.")
        return 0
    print_panel("Delegation Tree", lines)
    return 0


def cmd_delegation_tasks(args: argparse.Namespace, service: ClawieService) -> int:
    tasks = service.delegation_tasks(
        agent_id=getattr(args, "agent_id", None),
        status=getattr(args, "status", None),
        limit=args.limit,
    )
    if not tasks:
        print_info("No delegation tasks found.")
        return 0
    for t in tasks:
        print(
            f"  {t['task_id'][:12]}  {t['parent_agent_id']:12} -> {t['child_agent_id']:15}"
            f"  status={t['status']:10} depth={t['depth']}"
        )
    return 0


def cmd_delegation_spawn_session(args: argparse.Namespace, service: ClawieService) -> int:
    info = _clawied_service_call_or_unavailable(
        service,
        "spawn_session_agent",
        {
            "parent_id": args.parent,
            "child_id": args.child,
            "timeout": getattr(args, "timeout", 300.0),
            "model_tier": getattr(args, "tier", "") or "",
            "detached": True,
        },
    )
    if info is _CLAWIED_UNAVAILABLE:
        info = service.spawn_session_agent(
            parent_id=args.parent,
            child_id=args.child,
            timeout=getattr(args, "timeout", 300.0),
            model_tier=getattr(args, "tier", "") or "",
            detached=True,
        )
    print_success(f"Session agent {info['agent_id']} spawned under {args.parent}")
    print_panel(
        "Session Agent",
        [
            f"agent_id: {info.get('agent_id', '')}",
            f"parent: {info.get('parent_id', '')}",
            f"status: {info.get('status', '')}",
            f"depth: {info.get('depth', '')}",
            f"model_tier: {info.get('model_tier', '')}",
            *([f"pid: {info.get('pid', '')}"] if info.get("pid") else []),
            *([f"socket: {info.get('socket', '')}"] if info.get("socket") else []),
            *([f"log: {info.get('log', '')}"] if info.get("log") else []),
        ],
    )
    return 0


def cmd_delegation_stop_session(args: argparse.Namespace, service: ClawieService) -> int:
    result = _clawied_service_call_or_unavailable(
        service,
        "stop_session_agent",
        {"parent_id": args.parent, "child_id": args.child},
    )
    if result is _CLAWIED_UNAVAILABLE:
        service.stop_session_agent(parent_id=args.parent, child_id=args.child)
    print_success(f"Stopped session agent {args.child}")
    return 0


def cmd_delegation_session_agents(args: argparse.Namespace, service: ClawieService) -> int:
    agents = service.list_session_agents(parent_id=args.parent)
    if not agents:
        print_info("No session agents found.")
        return 0
    for a in agents:
        print(
            f"  {a['agent_id']:20} status={a['status']:10} "
            f"running={a['running']}  depth={a['depth']}"
            f"  pid={a.get('pid', 0)}"
        )
    tree_lines = service.session_tree_lines(args.parent)
    if tree_lines:
        print()
        print_panel("Session Tree", tree_lines)
    return 0


def cmd_delegation_cleanup(args: argparse.Namespace, service: ClawieService) -> int:
    result = _clawied_service_call_or_unavailable(
        service,
        "cleanup_delegation",
        {},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.cleanup_delegation()
    removed = result.get("removed_sockets", [])
    active = result.get("active_agents", [])
    if removed:
        print_info(f"Removed {len(removed)} stale socket(s).")
    if active:
        for a in active:
            print(f"  {a['agent_id']:20} alive={a['alive']}  age={a['age_seconds']}s")
    else:
        print_info("No active REPL agents.")
    return 0


def cmd_delegation_status(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.delegation import list_active_agents

    active = list_active_agents()
    if not active:
        print_info("No active REPL agents.")
        return 0
    for a in active:
        print(
            f"  {a['agent_id']:20} alive={a['alive']}  "
            f"age={a['age_seconds']}s  {a['socket']}"
        )
    return 0


def _build_clawied_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    clawied = subparsers.add_parser(
        "clawied",
        help="Run or inspect the local manifest reconcile daemon",
    )
    clawied_sub = clawied.add_subparsers(
        dest="clawied_command",
        required=True,
        metavar="{reconcile,run,status,stop}",
    )

    reconcile = clawied_sub.add_parser(
        "reconcile",
        help="Reconcile one manifest or all manifests once",
    )
    reconcile.add_argument("--manifest", help="Path to an agent manifest JSON file")
    reconcile.add_argument("--agent", help="Reconcile the stored manifest for one agent")
    reconcile.add_argument("--dry-run", action="store_true", help="Plan without applying changes")
    reconcile.add_argument("--json", action="store_true", help="Emit JSON")
    reconcile.set_defaults(func=cmd_clawied_reconcile)

    run = clawied_sub.add_parser(
        "run",
        help="Run the foreground clawied reconcile loop",
    )
    run.add_argument("--once", action="store_true", help="Run one reconcile cycle and exit")
    run.add_argument("--interval", type=float, default=60.0, help="Seconds between reconcile cycles")
    run.add_argument("--dry-run", action="store_true", help="Plan each cycle without applying changes")
    run.add_argument("--json", action="store_true", help="Emit JSON for --once or final status")
    run.set_defaults(func=cmd_clawied_run)

    status = clawied_sub.add_parser(
        "status",
        help="Show clawied pid and last reconcile status",
    )
    status.add_argument("--json", action="store_true", help="Emit JSON")
    status.set_defaults(func=cmd_clawied_status)

    stop = clawied_sub.add_parser(
        "stop",
        help="Send SIGTERM to the running clawied process",
    )
    stop.add_argument("--json", action="store_true", help="Emit JSON")
    stop.set_defaults(func=cmd_clawied_stop)


def cmd_clawied_reconcile(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.daemon import Clawied

    manifest = str(getattr(args, "manifest", "") or "").strip()
    agent = str(getattr(args, "agent", "") or "").strip()
    if manifest and agent:
        raise ValueError("use either --manifest or --agent, not both")
    daemon = Clawied(service)
    payload = {"manifest": manifest, "agent": agent, "dry_run": bool(args.dry_run)}
    result: Any | None = _clawied_ipc_request_or_none(daemon, "reconcile", payload)
    if result is None:
        if manifest:
            result = service.reconcile_agent_manifest(Path(manifest), dry_run=bool(args.dry_run))
        elif agent:
            result = service.reconcile_agent_manifest(service.agent_manifest_path(agent), dry_run=bool(args.dry_run))
        else:
            result = service.reconcile_all_manifests(dry_run=bool(args.dry_run))

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        _print_clawied_reconcile_result(result, dry_run=bool(args.dry_run))
    return _clawied_result_exit_code(result, dry_run=bool(args.dry_run))


def cmd_clawied_run(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.daemon import Clawied

    daemon = Clawied(service, interval_seconds=float(getattr(args, "interval", 60.0)))
    dry_run = bool(getattr(args, "dry_run", False))
    if bool(getattr(args, "once", False)):
        result = _clawied_ipc_request_or_none(daemon, "run_once", {"dry_run": dry_run})
        if result is None:
            result = daemon.run_forever(dry_run=dry_run, max_cycles=1)
    else:
        print_info(f"Starting clawied foreground loop (interval={daemon.interval_seconds:g}s)")
        result = daemon.run_forever(dry_run=dry_run)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    elif bool(getattr(args, "once", False)):
        last = result.get("last", {})
        print_success(f"clawied cycle complete: {last.get('status', result.get('status', 'ok'))}")
        print_info(f"manifests: {last.get('manifests', 0)}  errors: {last.get('errors', 0)}")
    return 1 if _clawied_status_has_errors(result) else 0


def cmd_clawied_status(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.daemon import Clawied

    daemon = Clawied(service)
    result = _clawied_ipc_request_or_none(daemon, "status", {})
    if result is None:
        result = daemon.status()
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        running = "running" if result.get("running") else "stopped"
        print_panel(
            "clawied",
            [
                f"status: {running}",
                f"pid: {result.get('pid', 0)}",
                f"pid_file: {result.get('pid_file', '')}",
                f"status_file: {result.get('status_file', '')}",
            ],
        )
    return 0


def cmd_clawied_stop(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.daemon import Clawied

    daemon = Clawied(service)
    result = _clawied_ipc_request_or_none(daemon, "stop", {})
    if result is None:
        result = daemon.stop()
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    elif result.get("stopped"):
        print_success(f"Sent SIGTERM to clawied pid {result.get('pid', 0)}")
    else:
        print_info("clawied is not running")
    return 0


def _print_clawied_reconcile_result(result: Any, *, dry_run: bool) -> None:
    rows = _clawied_result_rows(result)
    if not rows:
        print_info("No agent manifests found.")
        return
    for row in rows:
        prefix = "planned" if dry_run else "reconciled"
        print_panel(
            f"{prefix}: {row.get('agent_id', '')}",
            [
                f"converged: {bool(row.get('converged', False))}",
                f"actions: {len(row.get('actions', []))}",
                f"applied: {len(row.get('applied', []))}",
                f"remaining: {len(row.get('remaining', []))}",
                f"errors: {len(row.get('errors', []))}",
            ],
        )
        for action in row.get("remaining" if dry_run else "applied", []):
            print(f"  {action.get('kind', '')}: {json.dumps(action.get('detail', {}), sort_keys=True)}")
        for err in row.get("errors", []):
            print_error(f"  {err.get('kind', '')}: {err.get('error', '')}")


def _clawied_result_exit_code(result: Any, *, dry_run: bool) -> int:
    if dry_run:
        return 0
    rows = _clawied_result_rows(result)
    return 1 if any(_clawied_status_has_errors(row) for row in rows) else 0


def _clawied_result_rows(result: Any) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        return list(result["results"])
    return [result]


_CLAWIED_UNAVAILABLE = object()


def _clawied_service_call_or_unavailable(
    service: ClawieService,
    method: str,
    kwargs: dict[str, Any],
) -> Any:
    from clawie.daemon import Clawied

    daemon = Clawied(service)
    try:
        result = daemon.request(
            "service_call",
            {"method": method, "kwargs": kwargs},
            timeout=300.0,
        )
    except OSError as exc:
        if _clawied_is_genuinely_unavailable(daemon, exc):
            return _CLAWIED_UNAVAILABLE
        raise SetupError(f"clawied IPC failed closed: {exc}") from exc
    return result.get("result")


def _clawied_ipc_request_or_none(daemon: Any, command: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return daemon.request(command, payload)
    except OSError as exc:
        if _clawied_is_genuinely_unavailable(daemon, exc):
            return None
        raise SetupError(f"clawied IPC failed closed: {exc}") from exc


def _clawied_is_genuinely_unavailable(daemon: Any, exc: OSError) -> bool:
    """Allow direct-mode fallback only when no daemon endpoint exists."""
    if not isinstance(exc, (FileNotFoundError, ConnectionRefusedError)) and getattr(
        exc, "errno", None
    ) not in {errno.ENOENT, errno.ECONNREFUSED}:
        return False
    socket_path = Path(getattr(daemon, "socket_path", ""))
    try:
        socket_path.lstat()
    except (FileNotFoundError, OSError):
        return True
    return False


def _clawied_status_has_errors(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    errors = result.get("errors", 0)
    if isinstance(errors, list):
        if errors:
            return True
    elif int(errors or 0) > 0:
        return True
    if result.get("status") == "error":
        return True
    if result.get("last"):
        return _clawied_status_has_errors(result.get("last"))
    unconverged = int(result.get("unconverged", 0) or 0)
    if unconverged > 0 and not bool(result.get("dry_run", False)):
        return True
    if result.get("converged") is False and result.get("dry_run") is False:
        return True
    return bool(result.get("errors"))


def _build_control_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    control = subparsers.add_parser(
        "control",
        help="Manage control-agent runtime helpers",
    )
    control_sub = control.add_subparsers(
        dest="control_command",
        required=True,
        metavar="{request,confirm,watchdog}",
    )
    request = control_sub.add_parser(
        "request",
        help="Submit a control RPC request to clawied",
    )
    _add_positional_argument(
        request,
        "verb",
        metavar="VERB",
        help_text="Control verb, such as status, reconcile, open_issue, or open_pr",
    )
    request.add_argument(
        "--args-json",
        default="{}",
        help="JSON object passed as verb arguments",
    )
    request.add_argument("--json", action="store_true", help="Emit JSON")
    request.set_defaults(func=cmd_control_request)

    confirm = control_sub.add_parser(
        "confirm",
        help="Confirm a pending destructive/outward control RPC request",
    )
    _add_positional_argument(
        confirm,
        "verb",
        metavar="VERB",
        help_text="Control verb being confirmed",
    )
    confirm.add_argument(
        "--nonce",
        required=True,
        help="Nonce returned by `clawie control request`",
    )
    confirm.add_argument(
        "--confirmer",
        help="Deprecated and ignored; confirmer identity comes from the Unix peer credential",
    )
    confirm.add_argument(
        "--args-json",
        default="{}",
        help="Same JSON object used for the original request",
    )
    confirm.add_argument("--json", action="store_true", help="Emit JSON")
    confirm.set_defaults(func=cmd_control_confirm)

    watchdog = control_sub.add_parser(
        "watchdog",
        help="Manage the systemd watchdog for clawied/control RPC",
    )
    watchdog_sub = watchdog.add_subparsers(
        dest="control_watchdog_command",
        required=True,
        metavar="{install,status,verify,remove}",
    )
    install = watchdog_sub.add_parser(
        "install",
        help="Install and optionally start the systemd watchdog (requires sudo)",
    )
    install.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between clawied reconcile cycles (default: 60)",
    )
    install.add_argument(
        "--notify-command",
        default="",
        help="Optional shell command run by a systemd OnFailure alert unit",
    )
    install.add_argument(
        "--no-start",
        action="store_true",
        help="Write unit files but do not enable/start the watchdog",
    )
    install.set_defaults(func=cmd_control_watchdog_install)

    status = watchdog_sub.add_parser("status", help="Show watchdog unit status")
    status.set_defaults(func=cmd_control_watchdog_status)

    verify = watchdog_sub.add_parser(
        "verify",
        help="Verify watchdog unit state and optionally exercise systemd restart",
    )
    verify.add_argument(
        "--exercise-restart",
        action="store_true",
        help="Kill the watchdog process and wait for systemd to restart it",
    )
    verify.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Seconds to wait for restart proof when --exercise-restart is set",
    )
    verify.add_argument("--json", action="store_true", help="Emit JSON")
    verify.set_defaults(func=cmd_control_watchdog_verify)

    remove = watchdog_sub.add_parser(
        "remove",
        help="Disable and remove the watchdog unit (requires sudo)",
    )
    remove.set_defaults(func=cmd_control_watchdog_remove)


def _build_production_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    production = subparsers.add_parser(
        "production",
        help="Verify production-readiness proof gates",
    )
    production_sub = production.add_subparsers(
        dest="production_command",
        required=True,
        metavar="{verify}",
    )
    verify = production_sub.add_parser(
        "verify",
        help="Run aggregate production-readiness checks",
    )
    verify.add_argument(
        "--exercise-watchdog-restart",
        action="store_true",
        help="Required for production pass: kill the watchdog process and wait for systemd to restart it",
    )
    verify.add_argument(
        "--watchdog-timeout",
        type=int,
        default=30,
        help="Seconds to wait for watchdog restart proof",
    )
    verify.add_argument(
        "--all-provider-contracts",
        action="store_true",
        help="Check every verified production delivery provider contract, not just configured providers",
    )
    verify.add_argument(
        "--exercise-runtime-delivery",
        action="store_true",
        help="Required for production pass: execute a challenge-response task through each runtime gateway",
    )
    verify.add_argument("--json", action="store_true", help="Emit JSON")
    verify.set_defaults(func=cmd_production_verify)


def cmd_control_request(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.daemon import Clawied

    verb = str(getattr(args, "verb", "") or "").strip()
    if not verb:
        raise ValueError("control verb is required")
    payload = {
        "verb": verb,
        "args": _parse_json_object_arg(str(getattr(args, "args_json", "{}") or "{}"), "--args-json"),
    }
    result = _clawied_ipc_request_or_none(Clawied(service), "control_request", payload)
    if result is None:
        raise SetupError("clawied is not running; start `clawie clawied run` before using control RPC")
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        _print_control_rpc_result("Control Request", result)
    return 2 if str(result.get("decision", "")) == "deny" else 0


def cmd_control_confirm(args: argparse.Namespace, service: ClawieService) -> int:
    from clawie.daemon import Clawied

    verb = str(getattr(args, "verb", "") or "").strip()
    if not verb:
        raise ValueError("control verb is required")
    payload = {
        "verb": verb,
        "nonce": str(getattr(args, "nonce", "") or "").strip(),
        "args": _parse_json_object_arg(str(getattr(args, "args_json", "{}") or "{}"), "--args-json"),
    }
    result = _clawied_ipc_request_or_none(Clawied(service), "control_confirm", payload)
    if result is None:
        raise SetupError("clawied is not running; start `clawie clawied run` before using control RPC")
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        _print_control_rpc_result("Control Confirm", result)
    return 2 if str(result.get("decision", "")) == "deny" else 0


def _parse_json_object_arg(raw: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _print_control_rpc_result(title: str, result: dict[str, Any]) -> None:
    lines = [
        f"decision: {result.get('decision', '')}",
        f"tier: {result.get('tier', '')}",
        f"allowed: {bool(result.get('allowed', False))}",
    ]
    if result.get("nonce"):
        lines.append(f"nonce: {result.get('nonce', '')}")
    if result.get("reason"):
        lines.append(f"reason: {result.get('reason', '')}")
    if "result" in result:
        lines.append(f"result: {json.dumps(result.get('result'), sort_keys=True, default=str)}")
    print_panel(title, lines)


def cmd_control_watchdog_install(args: argparse.Namespace, service: ClawieService) -> int:
    result = service.control_watchdog_install(
        interval_seconds=int(getattr(args, "interval", 60) or 60),
        notify_command=str(getattr(args, "notify_command", "") or ""),
        start=not bool(getattr(args, "no_start", False)),
    )
    print_success("Control watchdog installed")
    print_panel(
        "Watchdog",
        [
            f"unit_file: {result.get('unit_file', '')}",
            f"alert_unit_file: {result.get('alert_unit_file', '') or '<none>'}",
            f"interval_seconds: {result.get('interval_seconds', 0)}",
            f"enabled: {result.get('enabled', False)}",
            f"started: {result.get('started', False)}",
        ],
    )
    return 0


def cmd_control_watchdog_status(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    result = service.control_watchdog_status()
    print_panel(
        "Watchdog",
        [
            f"enabled: {result.get('enabled', False)}",
            f"unit_file_exists: {result.get('unit_file_exists', False)}",
            f"unit_file: {result.get('unit_file', '')}",
            f"alert_unit_file_exists: {result.get('alert_unit_file_exists', False)}",
            f"active: {result.get('active', 'unknown')}",
            f"systemd_enabled: {result.get('systemd_enabled', 'unknown')}",
            f"interval_seconds: {result.get('interval_seconds', 0)}",
            f"notify_command_configured: {result.get('notify_command_configured', False)}",
        ],
    )
    return 0


def cmd_control_watchdog_verify(args: argparse.Namespace, service: ClawieService) -> int:
    result = service.control_watchdog_verify(
        exercise_restart=bool(getattr(args, "exercise_restart", False)),
        timeout_seconds=int(getattr(args, "timeout", 30) or 30),
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        lines = [
            f"status: {result.get('status', 'unknown')}",
            f"restart_exercised: {result.get('restart_exercised', False)}",
            f"unit_file: {result.get('unit_file', '')}",
        ]
        for row in result.get("checks", []):
            if not isinstance(row, dict):
                continue
            lines.append(f"{row.get('status', '')}: {row.get('message', '')}")
        print_panel("Watchdog Verify", lines)
    return 0 if result.get("status") == "passed" else 1


def cmd_control_watchdog_remove(args: argparse.Namespace, service: ClawieService) -> int:
    _ = args
    result = service.control_watchdog_remove()
    if result.get("removed"):
        print_success("Control watchdog removed")
        print_info("Removed: " + ", ".join(str(item) for item in result.get("removed", [])))
    else:
        print_info("Control watchdog was not installed")
    return 0


def cmd_production_verify(args: argparse.Namespace, service: ClawieService) -> int:
    result = service.production_readiness_report(
        exercise_watchdog_restart=bool(getattr(args, "exercise_watchdog_restart", False)),
        watchdog_timeout_seconds=int(getattr(args, "watchdog_timeout", 30) or 30),
        all_provider_contracts=bool(getattr(args, "all_provider_contracts", False)),
        exercise_runtime_delivery=bool(getattr(args, "exercise_runtime_delivery", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        lines = [
            f"status: {result.get('status', 'unknown')}",
            f"exercise_watchdog_restart: {result.get('exercise_watchdog_restart', False)}",
            f"all_provider_contracts: {result.get('all_provider_contracts', False)}",
            f"exercise_runtime_delivery: {result.get('exercise_runtime_delivery', False)}",
        ]
        for row in result.get("checks", []):
            if not isinstance(row, dict):
                continue
            lines.append(f"{row.get('status', '')}: {row.get('name', '')} - {row.get('message', '')}")
        print_panel("Production Readiness", lines)
    return 0 if result.get("status") == "passed" else 1


def _build_maintenance_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    maint = subparsers.add_parser(
        "maintenance",
        help="Manage periodic maintenance cron jobs (credential sync, prompt sync)",
    )
    maint_sub = maint.add_subparsers(
        dest="maintenance_command",
        required=True,
        metavar="{enable,disable,status,run}",
    )

    maint_enable = maint_sub.add_parser(
        "enable",
        help="Install the maintenance cron job (requires sudo)",
    )
    maint_enable.add_argument(
        "--interval", type=int, default=4,
        help="Hours between runs (default: 4)",
    )
    maint_enable.set_defaults(func=cmd_maintenance_enable)

    maint_disable = maint_sub.add_parser(
        "disable",
        help="Remove the maintenance cron job (requires sudo)",
    )
    maint_disable.set_defaults(func=cmd_maintenance_disable)

    maint_status = maint_sub.add_parser(
        "status",
        help="Show maintenance cron job status",
    )
    maint_status.set_defaults(func=cmd_maintenance_status)

    maint_run = maint_sub.add_parser(
        "run",
        help="Run maintenance tasks now (sync credentials, write configured prompts)",
    )
    maint_run.set_defaults(func=cmd_maintenance_run)


def cmd_maintenance_enable(args: argparse.Namespace, service: ClawieService) -> int:
    interval = getattr(args, "interval", 4)
    result = _clawied_service_call_or_unavailable(
        service,
        "maintenance_enable",
        {"interval_hours": interval},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.maintenance_enable(interval_hours=interval)
    print_success(f"Maintenance cron enabled (every {result['interval_hours']}h)")
    print_info(f"Cron file: {result['cron_file']}")
    print_info(f"Binary: {result['clawie_binary']}")
    print_info(f"Log: {ClawieService.MAINTENANCE_LOG_FILE}")
    return 0


def cmd_maintenance_disable(args: argparse.Namespace, service: ClawieService) -> int:
    result = _clawied_service_call_or_unavailable(
        service,
        "maintenance_disable",
        {},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.maintenance_disable()
    if result["removed"]:
        print_success("Maintenance cron job removed")
    else:
        print_info("No maintenance cron job was installed")
    return 0


def cmd_maintenance_status(args: argparse.Namespace, service: ClawieService) -> int:
    result = service.maintenance_status()
    if result["enabled"] and result["cron_file_exists"]:
        print_success(f"Maintenance cron is active (every {result['interval_hours']}h)")
        print_info(f"Cron file: {result['cron_file']}")
    elif result["enabled"]:
        print_info("Config says enabled but cron file is missing — re-run 'maintenance enable'")
    else:
        print_info("Maintenance cron is not enabled")
    return 0


def cmd_maintenance_run(args: argparse.Namespace, service: ClawieService) -> int:
    import datetime

    print(f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}] clawie maintenance run")
    result = _clawied_service_call_or_unavailable(
        service,
        "maintenance_run",
        {},
    )
    if result is _CLAWIED_UNAVAILABLE:
        result = service.maintenance_run()
    print(f"  Auth refresh: {result.get('auth_refresh', 'n/a')}")
    for agent_id, entry in result.get("results", {}).items():
        creds = entry.get("credentials", "")
        prompts = entry.get("prompts", "")
        status = "ok" if "error" not in creds and "error" not in prompts else "FAIL"
        print(f"  {agent_id}: credentials={creds}  prompts={prompts}  [{status}]")
    print(f"  Backup: {result.get('backup', 'disabled')}")
    total = result["agents_processed"]
    errs = result["errors"]
    print(f"  Total: {total} agents, {result['agents_skipped']} skipped, {errs} errors")
    return 1 if errs > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
