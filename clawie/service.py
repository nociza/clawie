from __future__ import annotations

import copy
import crypt
import json
import os
import pwd
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clawie.provider_channels import dedupe_channels, get_channel_adapter
from clawie.providers import (
    credential_paths_for_providers,
    detect_installed_providers,
    get_provider,
    provider_names,
)
from clawie.store import StateStore


class SetupError(RuntimeError):
    pass


class AgentExistsError(RuntimeError):
    pass


class AgentNotFoundError(RuntimeError):
    pass


def now_iso() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return stamp.replace("+00:00", "Z")


def redact(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


class ZeroClawService:
    EVENT_LIMIT = 2000
    DEFAULT_AGENT_PLUGINS: dict[str, bool] = {
        "scheduler": True,
        "gateway": True,
        "memory": True,
        "web_search": True,
    }

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def setup(
        self,
        provider: str,
        api_key: str,
        subscription: str,
        workspace: str,
        api_url: str,
        auth_mode: str | None = None,
        spawn_password: str | None = None,
        clear_spawn_password: bool = False,
        install_runtime: bool = False,
    ) -> dict[str, Any]:
        provider = provider.strip().lower() or "zeroclaw"
        provider_spec = get_provider(provider)
        api_key_value = api_key.strip()
        mode = self._resolve_auth_mode(provider_spec.name, api_key_value, auth_mode)
        config = self.store.read_config()
        config["provider"] = provider_spec.name
        config["auth_mode"] = mode
        config["subscription"] = subscription.strip()
        config["workspace"] = workspace.strip()
        config["api_url"] = api_url.strip()
        credentials = self._normalized_provider_credentials(config)
        provider_creds = {"auth_mode": mode}
        if mode == "api_key":
            provider_creds["api_key"] = api_key_value
        credentials[provider_spec.name] = provider_creds
        config["provider_credentials"] = credentials
        config["api_key"] = provider_creds.get("api_key", "")
        if clear_spawn_password:
            config["spawn_password_hash"] = ""
        elif spawn_password is not None:
            config["spawn_password_hash"] = self._hash_password(spawn_password)
        if install_runtime:
            config["runtime_installed"] = True
        created = config.get("created_at") or now_iso()
        config["created_at"] = created
        config["updated_at"] = now_iso()
        self.store.write_config(config)

        state = self.store.read_state()
        self._event(
            state,
            "setup.initialized",
            "Clawie configuration initialized",
            {
                "provider": config["provider"],
                "workspace": config["workspace"],
                "subscription": config["subscription"],
                "auth_mode": config["auth_mode"],
                "spawn_password_configured": bool(config.get("spawn_password_hash")),
                "runtime_installed": bool(config.get("runtime_installed", False)),
            },
        )
        self.store.write_state(state)
        return config

    def setup_status(self) -> dict[str, Any]:
        config = self.store.read_config()
        provider = str(config.get("provider", "zeroclaw")).strip().lower() or "zeroclaw"
        provider_spec = get_provider(provider)
        credentials = self._provider_auth(provider)
        auth_mode = credentials.get("auth_mode", provider_spec.default_auth_mode)
        configured = self._is_provider_configured(provider, credentials)
        return {
            "configured": configured,
            "provider": provider,
            "auth_mode": auth_mode,
            "api_url": config.get("api_url", ""),
            "workspace": config.get("workspace", ""),
            "subscription": config.get("subscription", ""),
            "api_key": redact(str(credentials.get("api_key", ""))),
            "spawn_password_configured": bool(str(config.get("spawn_password_hash", "")).strip()),
            "runtime_installed": bool(config.get("runtime_installed", False)),
            "updated_at": config.get("updated_at", ""),
        }

    def create_agent(
        self,
        agent_id: str,
        display_name: str | None,
        template: str,
        clone_from: str | None,
        channel_strategy: str,
        channels: list[dict[str, str]] | None,
        agent_version: str,
        provider: str | None = None,
    ) -> dict[str, Any]:
        self._require_setup()

        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id is required")

        if channel_strategy not in {"new", "migrate"}:
            raise ValueError("channel_strategy must be one of: new, migrate")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        state["users"] = agents
        if agent_id in agents:
            raise AgentExistsError(f"agent already exists: {agent_id}")

        base_channels: list[dict[str, str]] = []
        source_template = template
        source_agent_defaults: dict[str, Any] = {}

        if clone_from:
            source = agents.get(clone_from)
            if not source:
                raise AgentNotFoundError(f"clone source agent not found: {clone_from}")
            base_channels = copy.deepcopy(source.get("channels", []))
            source_template = source.get("source_template") or template
            source_agent_defaults = copy.deepcopy(source.get("agent", {}))
        else:
            template_data = state["templates"].get(template)
            if not template_data:
                raise ValueError(f"template not found: {template}")
            base_channels = copy.deepcopy(template_data.get("channels", []))
            source_agent_defaults = copy.deepcopy(template_data.get("agent_defaults", {}))

        if channels:
            base_channels = copy.deepcopy(channels)

        if channel_strategy == "new":
            final_channels = self._mint_channels(agent_id, base_channels)
        else:
            if not clone_from:
                raise ValueError(
                    "channel strategy 'migrate' requires --clone-from to copy channels"
                )
            final_channels = copy.deepcopy(base_channels)
            for channel in final_channels:
                channel["migrated_from"] = clone_from
        for channel in final_channels:
            channel["enabled"] = bool(channel.get("enabled", True))

        config = self.store.read_config()
        default_provider = str(config.get("provider", "zeroclaw")).strip().lower() or "zeroclaw"
        if provider:
            provider_spec = get_provider(provider)
        elif clone_from:
            source = agents.get(clone_from, {})
            source_provider = str(source.get("agent", {}).get("provider", "")).strip().lower()
            provider_spec = get_provider(source_provider or default_provider)
        else:
            provider_spec = get_provider(default_provider)

        provider_auth = self._provider_auth(provider_spec.name)
        if not self._is_provider_configured(provider_spec.name, provider_auth):
            raise SetupError(f"provider '{provider_spec.name}' is not configured. Run 'clawie setup'.")

        raw_plugins = source_agent_defaults.get("plugins", self._default_plugins_for_provider(provider_spec.name))
        if not isinstance(raw_plugins, dict):
            raw_plugins = self._default_plugins_for_provider(provider_spec.name)
        plugins = self._normalize_plugins(raw_plugins)

        display = display_name.strip() if display_name else agent_id
        agent = {
            "status": "ready",
            "version": agent_version,
            "last_sync": now_iso(),
            "runtime": source_agent_defaults.get("runtime", provider_spec.runtime),
            "provider": provider_spec.name,
            "auth_mode": provider_auth.get("auth_mode", provider_spec.default_auth_mode),
            "autostart": bool(source_agent_defaults.get("autostart", True)),
            "heartbeat_seconds": int(source_agent_defaults.get("heartbeat_seconds", 30)),
            "pid": int(source_agent_defaults.get("pid", 0)),
            "plugins": plugins,
        }

        agent_state = {
            "agent_id": agent_id,
            "display_name": display,
            "created_at": now_iso(),
            "source_template": source_template,
            "clone_from": clone_from,
            "channel_strategy": channel_strategy,
            "channels": final_channels,
            "agent": agent,
        }
        agents[agent_id] = agent_state

        self._event(
            state,
            "agents.created",
            f"Provisioned agent {agent_id}",
            {
                "agent_id": agent_id,
                "channel_strategy": channel_strategy,
                "channel_count": len(final_channels),
                "clone_from": clone_from or "",
                "provider": provider_spec.name,
            },
        )
        self.store.write_state(state)
        return agent_state

    def list_agents(self) -> list[dict[str, Any]]:
        state = self.store.read_state()
        agents = list(state.setdefault("agents", state.get("users", {})).values())
        for agent in agents:
            self._hydrate_agent_controls(agent)
        return sorted(
            agents,
            key=lambda row: (row.get("created_at", ""), row.get("agent_id", row.get("user_id", ""))),
        )

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        return agent

    def toggle_agent_channel(self, agent_id: str, channel_index: int) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        channels = agent.get("channels", [])
        if not isinstance(channels, list) or channel_index < 0 or channel_index >= len(channels):
            raise ValueError("invalid channel selection")

        selected = channels[channel_index]
        selected["enabled"] = not bool(selected.get("enabled", True))
        agent_info = agent.setdefault("agent", {})
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.channel_toggled",
            f"Toggled channel {selected.get('name', '')} for {agent_id}",
            {
                "agent_id": agent_id,
                "channel_name": str(selected.get("name", "")),
                "enabled": bool(selected.get("enabled", True)),
            },
        )
        self.store.write_state(state)
        return agent

    def toggle_agent_plugin(self, agent_id: str, plugin: str) -> dict[str, Any]:
        self._require_setup()
        plugin_name = str(plugin).strip().lower()
        if not plugin_name:
            raise ValueError("plugin is required")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        plugins = agent_info.setdefault("plugins", {})
        current = bool(plugins.get(plugin_name, True))
        plugins[plugin_name] = not current
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.plugin_toggled",
            f"Toggled plugin {plugin_name} for {agent_id}",
            {
                "agent_id": agent_id,
                "plugin": plugin_name,
                "enabled": bool(plugins.get(plugin_name, False)),
            },
        )
        self.store.write_state(state)
        return agent

    def toggle_agent_autostart(self, agent_id: str) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        agent_info["autostart"] = not bool(agent_info.get("autostart", True))
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.autostart_toggled",
            f"Toggled autostart for {agent_id}",
            {
                "agent_id": agent_id,
                "autostart": bool(agent_info.get("autostart", True)),
            },
        )
        self.store.write_state(state)
        return agent

    def agent_service_action(self, agent_id: str, action: str) -> dict[str, Any]:
        self._require_setup()
        command = str(action).strip().lower()
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)

        agent_info = agent.setdefault("agent", {})
        provider = str(agent_info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{agent_id}' has no provider configured")
        linux_user = str(agent_info.get("linux_user", "")).strip()
        cmd = self._service_command(provider, command, linux_user)
        env = self._service_env(linux_user)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        output = (result.stdout or result.stderr or "").strip()

        if (
            result.returncode != 0
            and "failed to connect to bus" in output.lower()
            and linux_user
            and os.geteuid() == 0
        ):
            self._bootstrap_user_bus(linux_user)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()

        if result.returncode != 0 and "failed to connect to bus" in output.lower():
            fallback = self._fallback_service_action(
                provider=provider,
                action=command,
                linux_user=linux_user,
                executable=cmd[-3] if len(cmd) >= 3 else self._resolve_provider_executable(provider),
                agent_info=agent_info,
            )
            service_status = str(fallback.get("service_status", "unknown"))
            agent_info["service_status"] = service_status
            agent_info["service_mode"] = "fallback"
            agent_info["last_sync"] = now_iso()
            self._event(
                state,
                "agents.service_action",
                f"Service {command} for {agent_id} (fallback)",
                {
                    "agent_id": agent_id,
                    "provider": provider,
                    "linux_user": linux_user,
                    "action": command,
                    "service_status": service_status,
                    "mode": "fallback",
                },
            )
            self.store.write_state(state)
            return {
                "agent_id": agent_id,
                "provider": provider,
                "linux_user": linux_user,
                "action": command,
                "service_status": service_status,
                "output": str(fallback.get("output", "")),
            }

        if result.returncode != 0:
            raise SetupError(
                f"{provider} service {command} failed for {agent_id}: "
                + (output or f"exit {result.returncode}")
            )

        if command == "start":
            service_status = "running"
        elif command == "stop":
            service_status = "stopped"
        elif command == "restart":
            service_status = "running"
        else:
            service_status = self._infer_service_status(output)

        agent_info["service_status"] = service_status
        agent_info["service_mode"] = "systemd"
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.service_action",
            f"Service {command} for {agent_id}",
            {
                "agent_id": agent_id,
                "provider": provider,
                "linux_user": linux_user,
                "action": command,
                "service_status": service_status,
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": agent_id,
            "provider": provider,
            "linux_user": linux_user,
            "action": command,
            "service_status": service_status,
            "output": output,
        }

    def delete_agent(self, agent_id: str) -> None:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        if agent_id not in agents:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        del agents[agent_id]
        self._event(
            state,
            "agents.deleted",
            f"Deleted agent {agent_id}",
            {"agent_id": agent_id},
        )
        self.store.write_state(state)

    def purge_agent(self, agent_id: str) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
        user_removed = False
        home_removed = False
        if linux_user:
            if os.geteuid() != 0:
                raise SetupError(
                    "purge requires root privileges for spawned Linux users. Re-run with sudo/root."
                )
            home_path = Path("/home") / linux_user
            if self._linux_user_exists(linux_user):
                subprocess.run(["userdel", "-r", linux_user], check=True)
                user_removed = True
                home_removed = True
            elif home_path.exists():
                shutil.rmtree(home_path)
                home_removed = True

        del agents[agent_id]
        self._event(
            state,
            "agents.purged",
            f"Purged agent {agent_id}",
            {
                "agent_id": agent_id,
                "linux_user": linux_user,
                "linux_user_removed": user_removed,
                "home_removed": home_removed,
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": agent_id,
            "linux_user": linux_user,
            "linux_user_removed": user_removed,
            "home_removed": home_removed,
        }

    def migrate_channels(
        self,
        from_agent: str,
        to_agent: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()

        agents = state.setdefault("agents", state.get("users", {}))
        source = agents.get(from_agent)
        target = agents.get(to_agent)
        if not source:
            raise AgentNotFoundError(f"source agent not found: {from_agent}")
        if not target:
            raise AgentNotFoundError(f"target agent not found: {to_agent}")

        source_channels = copy.deepcopy(source.get("channels", []))
        for channel in source_channels:
            channel["migrated_from"] = from_agent

        if replace:
            target_channels = source_channels
        else:
            target_channels = copy.deepcopy(target.get("channels", []))
            existing = {(row.get("kind", ""), row.get("name", "")) for row in target_channels}
            for channel in source_channels:
                key = (channel.get("kind", ""), channel.get("name", ""))
                if key not in existing:
                    target_channels.append(channel)
                    existing.add(key)

        target["channels"] = target_channels
        target["channel_strategy"] = "migrate"
        target["agent"]["status"] = "syncing"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.migrated",
            f"Migrated channels from {from_agent} to {to_agent}",
            {
                "from_agent": from_agent,
                "to_agent": to_agent,
                "replace": replace,
                "channel_count": len(target_channels),
            },
        )
        self.store.write_state(state)
        return target

    def bootstrap_channels(
        self,
        agent_id: str,
        preset: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        presets = {
            "minimal": [{"kind": "chat", "name": "primary"}],
            "growth": [
                {"kind": "chat", "name": "support"},
                {"kind": "email", "name": "inbox"},
                {"kind": "social", "name": "community"},
            ],
            "enterprise": [
                {"kind": "chat", "name": "ops"},
                {"kind": "email", "name": "queue"},
                {"kind": "voice", "name": "contact-center"},
                {"kind": "ticketing", "name": "service-desk"},
            ],
        }
        if preset not in presets:
            raise ValueError("preset must be one of: minimal, growth, enterprise")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        target = agents.get(agent_id)
        if not target:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        generated = self._mint_channels(agent_id, presets[preset])
        if replace:
            target_channels = generated
        else:
            target_channels = copy.deepcopy(target.get("channels", []))
            existing = {(row.get("kind", ""), row.get("name", "")) for row in target_channels}
            for channel in generated:
                key = (channel.get("kind", ""), channel.get("name", ""))
                if key not in existing:
                    target_channels.append(channel)
                    existing.add(key)

        target["channels"] = target_channels
        target["agent"]["status"] = "ready"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.bootstrapped",
            f"Applied {preset} channel preset for {agent_id}",
            {
                "agent_id": agent_id,
                "preset": preset,
                "replace": replace,
                "channel_count": len(target_channels),
            },
        )
        self.store.write_state(state)
        return target

    def channel_inventory(self) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        rows: list[dict[str, Any]] = []
        assigned_keys: set[tuple[str, str]] = set()
        for aid, payload in sorted(agents.items()):
            self._hydrate_agent_controls(payload)
            provider = str(payload.get("agent", {}).get("provider", "")).strip().lower()
            for channel in payload.get("channels", []):
                if not isinstance(channel, dict):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                assigned_keys.add((kind, name))
                rows.append(
                    {
                        "source": "agent",
                        "owner_agent_id": str(aid),
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                    }
                )

        for channel in self._read_channel_pool():
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name or (kind, name) in assigned_keys:
                continue
            rows.append(
                {
                    "source": "pool",
                    "owner_agent_id": "@pool",
                    "provider": str(channel.get("provider", "")).strip().lower(),
                    "kind": kind,
                    "name": name,
                    "enabled": False,
                }
            )

        for item in self._local_channel_inventory():
            key = (str(item.get("kind", "")).strip().lower(), str(item.get("name", "")).strip())
            if key in assigned_keys:
                continue
            rows.append(item)

        kinds = {str(row.get("kind", "")) for row in rows if str(row.get("kind", "")).strip()}
        return {
            "generated_at": now_iso(),
            "rows": rows,
            "totals": {
                "channels": len(rows),
                "kinds": len(kinds),
                "assigned": sum(1 for row in rows if str(row.get("source", "")) == "agent"),
                "local": sum(1 for row in rows if str(row.get("source", "")) == "local"),
                "pool": sum(1 for row in rows if str(row.get("source", "")) == "pool"),
            },
        }

    def assign_channel_to_agent(
        self,
        source_agent_id: str,
        kind: str,
        name: str,
        target_agent_id: str,
    ) -> dict[str, Any]:
        self._require_setup()
        src = str(source_agent_id).strip()
        dst = str(target_agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")
        if not dst:
            raise ValueError("target_agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        target = agents.get(dst)
        if not target:
            raise AgentNotFoundError(f"target agent not found: {dst}")
        self._hydrate_agent_controls(target)
        target_channels = target.setdefault("channels", [])
        if not isinstance(target_channels, list):
            target_channels = []
            target["channels"] = target_channels

        self._remove_pool_channel(channel_kind, channel_name)
        if self._find_channel(target_channels, channel_kind, channel_name) is None:
            target_channels.append(
                {
                    "kind": channel_kind,
                    "name": channel_name,
                    "enabled": True,
                    "external_id": f"{dst}:{channel_kind}:{len(target_channels) + 1}",
                }
            )

        moved = False
        if src and not src.startswith("@local:") and src in agents and src != dst:
            source = agents[src]
            self._hydrate_agent_controls(source)
            source_channels = source.setdefault("channels", [])
            if isinstance(source_channels, list):
                found_idx = self._find_channel(source_channels, channel_kind, channel_name)
                if found_idx is not None:
                    source_channels.pop(found_idx)
                    moved = True
                    source.setdefault("agent", {})["last_sync"] = now_iso()

        target.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "channels.assigned",
            f"Assigned channel {channel_kind}:{channel_name} to {dst}",
            {
                "source_agent_id": src,
                "target_agent_id": dst,
                "kind": channel_kind,
                "name": channel_name,
                "moved": moved,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "target_agent_id": dst,
            "kind": channel_kind,
            "name": channel_name,
            "moved": moved,
        }

    def unassign_channel_from_agent(
        self,
        agent_id: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        self._require_setup()
        src = str(agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not src:
            raise ValueError("agent_id is required")
        if src.startswith("@local:"):
            raise ValueError("cannot unassign local-user channel")
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        source = agents.get(src)
        if not source:
            raise AgentNotFoundError(f"agent not found: {src}")
        self._hydrate_agent_controls(source)
        channels = source.setdefault("channels", [])
        if not isinstance(channels, list):
            channels = []
            source["channels"] = channels

        found_idx = self._find_channel(channels, channel_kind, channel_name)
        if found_idx is None:
            raise ValueError(f"channel not found on {src}: {channel_kind}:{channel_name}")
        removed = channels.pop(found_idx)
        source.setdefault("agent", {})["last_sync"] = now_iso()

        pool = self._read_channel_pool()
        if self._find_channel(pool, channel_kind, channel_name) is None:
            pool.append(
                {
                    "kind": channel_kind,
                    "name": channel_name,
                    "provider": str(source.get("agent", {}).get("provider", "")).strip().lower(),
                    "external_id": str(removed.get("external_id", "")),
                }
            )
            self._write_channel_pool(pool)

        self._event(
            state,
            "channels.unassigned",
            f"Unassigned channel {channel_kind}:{channel_name} from {src}",
            {
                "source_agent_id": src,
                "kind": channel_kind,
                "name": channel_name,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "kind": channel_kind,
            "name": channel_name,
            "status": "unassigned",
        }

    def connect_agent_channel(
        self,
        agent_id: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        self._require_setup()
        target = str(agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not target:
            raise ValueError("agent_id is required")
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")
        if target.startswith("@local:"):
            raise ValueError("connect is only supported for managed agents")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(target)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{target}' has no provider configured")
        linux_user = str(info.get("linux_user", "")).strip()
        self.assign_channel_to_agent("", channel_kind, channel_name, target)

        commands = self._channel_connect_commands(provider, channel_kind, channel_name, linux_user)
        last_error = ""
        chosen: list[str] = []
        for cmd in commands:
            chosen = cmd
            env = self._service_env(linux_user)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                state = self.store.read_state()
                agents = state.setdefault("agents", state.get("users", {}))
                refreshed = agents.get(target, {})
                refreshed_info = refreshed.setdefault("agent", {})
                refreshed_info["last_sync"] = now_iso()
                self._event(
                    state,
                    "channels.connected",
                    f"Connected channel {channel_kind}:{channel_name} for {target}",
                    {
                        "agent_id": target,
                        "provider": provider,
                        "kind": channel_kind,
                        "name": channel_name,
                        "command": " ".join(cmd),
                    },
                )
                self.store.write_state(state)
                return {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": cmd,
                    "output": output,
                    "status": "connected",
                }
            last_error = output or f"exit {result.returncode}"

        raise SetupError(
            f"channel connect failed for {target} ({provider}): {last_error}. "
            + ("attempted: " + " || ".join(" ".join(cmd) for cmd in commands) if commands else "")
        )

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        config = self.store.read_config()
        state = self.store.read_state()

        provider = str(config.get("provider", "zeroclaw"))
        provider_auth = self._provider_auth(provider)
        mode = provider_auth.get("auth_mode", get_provider(provider).default_auth_mode)
        if self._is_provider_configured(provider, provider_auth):
            checks.append(
                {
                    "status": "pass",
                    "message": f"Provider auth configured ({provider}/{mode})",
                }
            )
        else:
            checks.append(
                {
                    "status": "fail",
                    "message": "Provider credentials are missing. Run setup.",
                }
            )

        if config.get("workspace"):
            checks.append({"status": "pass", "message": "Workspace is configured"})
        else:
            checks.append({
                "status": "warn",
                "message": "Workspace is empty; default will be used.",
            })

        if state.get("templates"):
            checks.append({"status": "pass", "message": "At least one template exists"})
        else:
            checks.append({"status": "fail", "message": "No templates available"})
        checks.append(
            {
                "status": "pass",
                "message": f"Local database: {self.store.db_path}",
            }
        )

        agents = state.setdefault("agents", state.get("users", {}))
        if agents:
            checks.append({"status": "pass", "message": f"{len(agents)} agent(s) provisioned"})
        else:
            checks.append({"status": "warn", "message": "No agents provisioned yet"})

        no_channels = [aid for aid, row in agents.items() if not row.get("channels")]
        if no_channels:
            checks.append({
                "status": "warn",
                "message": "Agents without channels: " + ", ".join(no_channels),
            })

        overall = "healthy"
        if any(check["status"] == "fail" for check in checks):
            overall = "unhealthy"
        elif any(check["status"] == "warn" for check in checks):
            overall = "degraded"

        return {"status": overall, "checks": checks}

    def list_events(self, limit: int = 20) -> list[dict[str, Any]]:
        state = self.store.read_state()
        events = state.get("events", [])
        return list(reversed(events[-limit:]))

    def list_installed_claws(self, source_home: str | Path | None = None) -> list[dict[str, Any]]:
        if source_home:
            root = Path(source_home).expanduser()
        else:
            sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
            if os.geteuid() == 0 and sudo_user and sudo_user != "root":
                try:
                    root = Path(pwd.getpwnam(sudo_user).pw_dir)
                except KeyError:
                    root = Path.home()
            elif os.geteuid() == 0:
                discovered: list[dict[str, Any]] = []
                home_root = Path("/home")
                if home_root.exists():
                    for entry in sorted(home_root.iterdir()):
                        if not entry.is_dir():
                            continue
                        discovered.extend(detect_installed_providers(str(entry)))
                if discovered:
                    return discovered
                root = Path.home()
            else:
                root = Path.home()
        return detect_installed_providers(str(root))

    def get_dashboard_agent(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            return self._local_agent_view(provider)
        return self.get_agent(token)

    def local_claw_service_action(self, provider: str, action: str) -> dict[str, Any]:
        self._require_setup()
        name = str(provider).strip().lower()
        command = str(action).strip().lower()
        if not name:
            raise ValueError("provider is required")
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")

        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        local_info = local_state.setdefault(name, {})
        linux_user = self._local_linux_user_hint(name, self._local_target_user())
        if command == "status":
            unit_status = self._systemd_user_service_status(name, linux_user)
            if unit_status != "unknown":
                local_info["service_status"] = unit_status
                local_info["service_mode"] = "systemd"
                config["local_service_state"] = local_state
                self.store.write_config(config)
                return {
                    "provider": name,
                    "action": command,
                    "service_status": unit_status,
                    "service_mode": "systemd",
                    "output": f"systemctl user unit state: {unit_status}",
                }
        else:
            managed = self._systemd_user_service_manage(name, command, linux_user)
            if managed.get("ok", False):
                status = "running" if command in {"start", "restart"} else "stopped"
                local_info["service_status"] = status
                local_info["service_mode"] = "systemd"
                config["local_service_state"] = local_state
                self.store.write_config(config)
                return {
                    "provider": name,
                    "action": command,
                    "service_status": status,
                    "service_mode": "systemd",
                    "output": str(managed.get("output", "")),
                }
        try:
            probe = self._run_local_provider_command(name, command, linux_user)
            cmd = probe["command"]
            result = probe["result"]
            output = probe["output"]
        except Exception as exc:
            if command != "status":
                raise
            status = self._best_effort_local_status(local_info, linux_user)
            local_info["service_status"] = status
            local_info["service_mode"] = "fallback"
            config["local_service_state"] = local_state
            self.store.write_config(config)
            return {
                "provider": name,
                "action": command,
                "service_status": status,
                "service_mode": "fallback",
                "output": str(exc),
            }

        if result.returncode != 0 and "failed to connect to bus" in output.lower():
            fallback = self._fallback_service_action(
                provider=name,
                action=command,
                linux_user=linux_user,
                executable=cmd[0],
                agent_info=local_info,
            )
            local_info["service_status"] = str(fallback.get("service_status", "unknown"))
            local_info["service_mode"] = "fallback"
            config["local_service_state"] = local_state
            self.store.write_config(config)
            return {
                "provider": name,
                "action": command,
                "service_status": local_info["service_status"],
                "service_mode": "fallback",
                "output": str(fallback.get("output", "")),
            }

        if result.returncode != 0 and command == "status":
            status = self._best_effort_local_status(local_info, linux_user)
            local_info["service_status"] = status
            local_info["service_mode"] = "fallback"
            config["local_service_state"] = local_state
            self.store.write_config(config)
            return {
                "provider": name,
                "action": command,
                "service_status": status,
                "service_mode": "fallback",
                "output": output,
            }

        if result.returncode != 0:
            raise SetupError(
                f"{name} service {command} failed: " + (output or f"exit {result.returncode}")
            )

        if command == "start":
            status = "running"
            mode = "systemd"
        elif command == "stop":
            status = "stopped"
            mode = "systemd"
        elif command == "restart":
            status = "running"
            mode = "systemd"
        else:
            inferred = self._infer_service_status(output)
            if inferred == "unknown":
                status = self._best_effort_local_status(local_info, linux_user)
                mode = "fallback"
            else:
                status = inferred
                mode = "systemd"
        local_info["service_status"] = status
        local_info["service_mode"] = mode
        config["local_service_state"] = local_state
        self.store.write_config(config)
        return {
            "provider": name,
            "action": command,
            "service_status": status,
            "service_mode": local_info["service_mode"],
            "output": output,
        }

    def dashboard_snapshot(self, agent_id: str | None = None) -> dict[str, Any]:
        return self.performance_snapshot(agent_id=agent_id, refresh=True)

    def performance_snapshot(
        self,
        agent_id: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if refresh:
            self.collect_metrics(agent_id=agent_id)
        state = self.store.read_state()
        agents = list(state.setdefault("agents", state.get("users", {})).values())
        if agent_id:
            agents = [
                row
                for row in agents
                if row.get("agent_id", row.get("user_id", "")) == agent_id
            ]
        latest_metrics = self.store.latest_metrics(limit_per_user=1)

        rows: list[dict[str, Any]] = []
        channel_total = 0
        migrated_total = 0
        cpu_total = 0.0
        mem_total = 0.0
        for agent_state in sorted(
            agents,
            key=lambda row: row.get("agent_id", row.get("user_id", "")),
        ):
            self._hydrate_agent_controls(agent_state)
            channels = agent_state.get("channels", [])
            active_channels = sum(1 for channel in channels if bool(channel.get("enabled", True)))
            migrated_count = sum(1 for row in channels if row.get("migrated_from"))
            channel_total += len(channels)
            migrated_total += migrated_count
            current_id = str(agent_state.get("agent_id", agent_state.get("user_id", "")))
            metric = (latest_metrics.get(current_id, [{}]) or [{}])[0]
            cpu = float(metric.get("cpu_percent", 0.0))
            mem = float(metric.get("mem_percent", 0.0))
            rss = int(metric.get("rss_kb", 0))
            metric_status = str(metric.get("status", "")).strip()
            sampled_status = self._dashboard_status(metric_status, agent_state.get("agent", {}))
            cpu_total += cpu
            mem_total += mem
            rows.append(
                {
                    "agent_id": current_id,
                    "display_name": agent_state.get("display_name", ""),
                    "status": sampled_status,
                    "version": agent_state.get("agent", {}).get("version", ""),
                    "provider": agent_state.get("agent", {}).get("provider", ""),
                    "strategy": agent_state.get("channel_strategy", ""),
                    "channels": active_channels,
                    "channels_total": len(channels),
                    "migrated": migrated_count,
                    "last_sync": agent_state.get("agent", {}).get("last_sync", ""),
                    "pid": int(agent_state.get("agent", {}).get("pid") or 0),
                    "cpu_percent": cpu,
                    "mem_percent": mem,
                    "rss_kb": rss,
                }
            )

        for local_agent in self._local_dashboard_rows(refresh=refresh):
            if agent_id and local_agent["agent_id"] != agent_id:
                continue
            rows.append(local_agent)

        config = self.store.read_config()
        return {
            "generated_at": now_iso(),
            "workspace": config.get("workspace", ""),
            "provider": config.get("provider", "zeroclaw"),
            "totals": {
                "agents": len(rows),
                "channels": channel_total,
                "migrated_channels": migrated_total,
                "cpu_percent": round(cpu_total, 2),
                "mem_percent": round(mem_total, 2),
            },
            "rows": rows,
            "events": self.list_events(limit=8),
        }

    def collect_metrics(self, agent_id: str | None = None) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        sampled = 0
        for aid, agent_state in agents.items():
            if agent_id and aid != agent_id:
                continue
            agent = agent_state.setdefault("agent", {})
            pid = int(agent.get("pid") or 0)
            status = "offline"
            cpu_percent = 0.0
            mem_percent = 0.0
            rss_kb = 0
            if pid > 0:
                probe = self._probe_process(pid)
                if probe is not None:
                    cpu_percent = float(probe["cpu_percent"])
                    mem_percent = float(probe["mem_percent"])
                    rss_kb = int(probe["rss_kb"])
                    status = "running"
                else:
                    agent["pid"] = 0

            agent["status"] = status
            agent["last_sync"] = now_iso()
            self.store.write_metric(
                timestamp=now_iso(),
                user_id=aid,
                cpu_percent=cpu_percent,
                mem_percent=mem_percent,
                rss_kb=rss_kb,
                status=status,
            )
            sampled += 1
        self.store.write_state(state)
        return {"sampled": sampled}

    def spawn_linux_user(
        self,
        agent_id: str,
        linux_user: str | None = None,
        copy_configs: bool = True,
        source_home: str | Path | None = None,
        template: str = "baseline",
        agent_version: str = "1.0.0",
        provider: str | None = None,
        password: str | None = None,
        password_hash: str | None = None,
        use_global_password: bool = True,
    ) -> dict[str, Any]:
        self._require_setup()
        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        if agent_id in agents:
            raise AgentExistsError(f"agent already exists: {agent_id}")
        if template not in state.get("templates", {}):
            raise ValueError(f"template not found: {template}")

        target_user = (linux_user or agent_id).strip()
        self._validate_linux_username(target_user)
        if os.geteuid() != 0:
            raise SetupError(
                "spawn requires root privileges. Re-run with sudo/root to create Linux users."
            )

        target_home = Path("/home") / target_user
        if self._linux_user_exists(target_user):
            raise AgentExistsError(f"linux user already exists: {target_user}")

        subprocess.run(["useradd", "-m", "-s", "/bin/bash", target_user], check=True)
        password_source = self._apply_spawn_password(
            username=target_user,
            password=password,
            password_hash=password_hash,
            use_global_password=use_global_password,
        )

        if source_home:
            src_home = Path(source_home).expanduser()
        else:
            sudo_user = os.environ.get("SUDO_USER", "").strip()
            if sudo_user:
                src_home = Path("/home") / sudo_user
            else:
                src_home = Path.home()
        imported_channels = self._discover_channels_from_source_home(src_home, provider)
        copied = self._copy_user_configs(src_home, target_home, target_user, enabled=copy_configs)
        copied += self._copy_provider_credentials(
            source_home=src_home,
            target_home=target_home,
            username=target_user,
            requested_provider=provider,
            enabled=copy_configs,
        )
        agent_state = self.create_agent(
            agent_id=agent_id,
            display_name=agent_id,
            template=template,
            clone_from=None,
            channel_strategy="new",
            channels=imported_channels or None,
            agent_version=agent_version,
            provider=provider,
        )
        agent_state["agent"]["linux_user"] = target_user
        state = self.store.read_state()
        self._event(
            state,
            "agents.spawned",
            f"Spawned linux user {target_user} for {agent_id}",
            {
                "agent_id": agent_id,
                "linux_user": target_user,
                "copied": copied,
                "detected_providers": [
                    row["provider"] for row in self.list_installed_claws(source_home=src_home)
                ],
                "imported_channels": len(imported_channels),
                "password_source": password_source,
            },
        )
        agents = state.setdefault("agents", state.get("users", {}))
        agents[agent_id] = agent_state
        self.store.write_state(state)
        return {
            "agent": agent_state,
            "linux_user": target_user,
            "copied_paths": copied,
            "password_source": password_source,
        }

    def batch_create_agents(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        results = {"created": [], "errors": []}
        for entry in entries:
            agent_id = str(entry.get("agent_id", entry.get("user_id", ""))).strip()
            if not agent_id:
                results["errors"].append({
                    "agent_id": "",
                    "error": "entry missing agent_id",
                })
                continue
            try:
                agent_state = self.create_agent(
                    agent_id=agent_id,
                    display_name=entry.get("display_name"),
                    template=str(entry.get("template", "baseline")),
                    clone_from=entry.get("clone_from"),
                    channel_strategy=str(entry.get("channel_strategy", "new")),
                    channels=entry.get("channels"),
                    agent_version=str(entry.get("agent_version", "1.0.0")),
                    provider=entry.get("provider"),
                )
                results["created"].append(agent_state["agent_id"])
            except Exception as exc:  # noqa: BLE001
                results["errors"].append({"agent_id": agent_id, "error": str(exc)})
        return results

    def export_state(self, output_path: str | Path) -> Path:
        snapshot = {
            "exported_at": now_iso(),
            "config": self.store.read_config(),
            "state": self.store.read_state(),
        }
        target = Path(output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return target

    def import_state(self, input_path: str | Path, merge: bool = False) -> None:
        source = Path(input_path).expanduser()
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if not isinstance(payload, dict):
            raise ValueError("snapshot must be a JSON object")
        config = payload.get("config")
        state = payload.get("state")
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise ValueError("snapshot must include object fields: config, state")

        if merge:
            current_config = self.store.read_config()
            current_state = self.store.read_state()

            merged_config = copy.deepcopy(current_config)
            merged_config.update(config)

            merged_state = copy.deepcopy(current_state)
            merged_state.setdefault("templates", {})
            merged_state.setdefault("agents", merged_state.get("users", {}))
            merged_state.setdefault("events", [])
            merged_state["templates"].update(state.get("templates", {}))
            merged_state["agents"].update(state.get("agents", state.get("users", {})))
            merged_state["events"] = (
                merged_state["events"] + state.get("events", [])
            )[-self.EVENT_LIMIT :]

            self.store.write_config(merged_config)
            self.store.write_state(merged_state)
            return

        self.store.write_config(config)
        self.store.write_state(state)

    def _require_setup(self) -> None:
        config = self.store.read_config()
        provider = str(config.get("provider", "zeroclaw")).strip().lower() or "zeroclaw"
        credentials = self._provider_auth(provider)
        if not self._is_provider_configured(provider, credentials):
            raise SetupError("setup is incomplete. Run 'clawie setup'.")

    def _resolve_auth_mode(self, provider: str, api_key: str, auth_mode: str | None) -> str:
        spec = get_provider(provider)
        if auth_mode:
            mode = auth_mode.strip().lower()
            if not spec.supports_auth_mode(mode):
                allowed = ", ".join(spec.auth_modes)
                raise ValueError(f"auth mode for {provider} must be one of: {allowed}")
        elif api_key:
            mode = "api_key"
        else:
            mode = spec.default_auth_mode

        if mode == "api_key" and not api_key:
            raise ValueError("API key is required when --auth-mode api_key is selected")
        return mode

    def _normalized_provider_credentials(self, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        payload = config.get("provider_credentials", {})
        if not isinstance(payload, dict):
            payload = {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            normalized[str(key).strip().lower()] = dict(value)
        return normalized

    def _provider_auth(self, provider: str) -> dict[str, Any]:
        spec = get_provider(provider)
        config = self.store.read_config()
        credentials = self._normalized_provider_credentials(config)
        provider_auth = dict(credentials.get(spec.name, {}))
        if not provider_auth:
            if str(config.get("provider", "")).strip().lower() == spec.name:
                provider_auth["auth_mode"] = str(config.get("auth_mode") or spec.default_auth_mode)
                api_key = str(config.get("api_key", "")).strip()
                if api_key:
                    provider_auth["api_key"] = api_key
        return provider_auth

    def _is_provider_configured(self, provider: str, auth: dict[str, Any]) -> bool:
        spec = get_provider(provider)
        mode = str(auth.get("auth_mode", "")).strip().lower()
        if not mode:
            return False
        if not spec.supports_auth_mode(mode):
            return False
        if mode == "api_key":
            return bool(str(auth.get("api_key", "")).strip())
        if mode in {"linked", "none"}:
            return True
        return False

    def _mint_channels(
        self,
        agent_id: str,
        base_channels: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        items = base_channels or [{"kind": "chat", "name": "primary"}]
        minted: list[dict[str, str]] = []
        for idx, channel in enumerate(items, start=1):
            kind = str(channel.get("kind", "chat"))
            raw_name = str(channel.get("name", f"channel-{idx}"))
            if raw_name.startswith(f"{agent_id}-"):
                full_name = raw_name
            else:
                full_name = f"{agent_id}-{raw_name}"
            minted.append(
                {
                    "kind": kind,
                    "name": full_name,
                    "external_id": f"{agent_id}:{kind}:{idx}",
                }
            )
        return minted

    # Backward-compatible aliases.
    def create_user(self, **kwargs: Any) -> dict[str, Any]:
        return self.create_agent(
            agent_id=str(kwargs.get("user_id", kwargs.get("agent_id", ""))),
            display_name=kwargs.get("display_name"),
            template=str(kwargs.get("template", "baseline")),
            clone_from=kwargs.get("clone_from"),
            channel_strategy=str(kwargs.get("channel_strategy", "new")),
            channels=kwargs.get("channels"),
            agent_version=str(kwargs.get("agent_version", "1.0.0")),
            provider=kwargs.get("provider"),
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self.list_agents()

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self.get_agent(user_id)

    def delete_user(self, user_id: str) -> None:
        self.delete_agent(user_id)

    def batch_create_users(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        return self.batch_create_agents(entries)

    def _event(
        self,
        state: dict[str, Any],
        event_type: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        events = state.setdefault("events", [])
        events.append(
            {
                "timestamp": now_iso(),
                "type": event_type,
                "message": message,
                "context": context,
            }
        )
        if len(events) > self.EVENT_LIMIT:
            state["events"] = events[-self.EVENT_LIMIT :]

    def _probe_process(self, pid: int) -> dict[str, Any] | None:
        if pid <= 0:
            return None
        cmd = ["ps", "-p", str(pid), "-o", "%cpu=,%mem=,rss="]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        parts = result.stdout.strip().split()
        if len(parts) < 3:
            return None
        try:
            return {
                "cpu_percent": float(parts[0]),
                "mem_percent": float(parts[1]),
                "rss_kb": int(parts[2]),
            }
        except ValueError:
            return None

    @staticmethod
    def _validate_linux_username(username: str) -> None:
        if not username:
            raise ValueError("linux username is required")
        if len(username) > 32:
            raise ValueError("linux username must be <= 32 chars")
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if username[0] == "-" or any(ch not in allowed for ch in username):
            raise ValueError("linux username can only contain a-z, 0-9, _ and -")

    @staticmethod
    def _linux_user_exists(username: str) -> bool:
        result = subprocess.run(
            ["id", "-u", username],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _copy_user_configs(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        enabled: bool,
    ) -> list[str]:
        candidates = [
            ".bashrc",
            ".profile",
            ".gitconfig",
            ".config/clawie",
            ".clawie",
        ]
        return self._copy_selected_paths(
            source_home=source_home,
            target_home=target_home,
            username=username,
            relative_paths=candidates,
            enabled=enabled,
        )

    def _copy_provider_credentials(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        requested_provider: str | None,
        enabled: bool,
    ) -> list[str]:
        if not enabled:
            return []

        config = self.store.read_config()
        configured = list(self._normalized_provider_credentials(config).keys())
        configured.append(str(config.get("provider", "zeroclaw")))
        if requested_provider:
            configured.append(requested_provider)
        for row in self.list_installed_claws(source_home=source_home):
            configured.append(str(row.get("provider", "")))

        providers: list[str] = []
        seen: set[str] = set()
        valid = set(provider_names())
        for item in configured:
            name = str(item or "").strip().lower()
            if not name or name in seen:
                continue
            if name not in valid:
                continue
            providers.append(name)
            seen.add(name)

        candidates = credential_paths_for_providers(providers)
        return self._copy_selected_paths(
            source_home=source_home,
            target_home=target_home,
            username=username,
            relative_paths=candidates,
            enabled=True,
        )

    def _copy_selected_paths(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        relative_paths: list[str],
        enabled: bool,
    ) -> list[str]:
        if not enabled:
            return []
        copied: list[str] = []
        seen: set[str] = set()
        for rel in relative_paths:
            token = str(rel).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            src = source_home / token
            dst = target_home / token
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            subprocess.run(["chown", "-R", f"{username}:{username}", str(dst)], check=True)
            copied.append(str(dst))
        return copied

    @staticmethod
    def _find_channel(channels: list[dict[str, Any]], kind: str, name: str) -> int | None:
        for idx, channel in enumerate(channels):
            if not isinstance(channel, dict):
                continue
            row_kind = str(channel.get("kind", "")).strip().lower()
            row_name = str(channel.get("name", "")).strip()
            if row_kind == kind and row_name == name:
                return idx
        return None

    def _apply_spawn_password(
        self,
        username: str,
        password: str | None,
        password_hash: str | None,
        use_global_password: bool,
    ) -> str:
        raw_password = str(password or "").strip()
        raw_hash = str(password_hash or "").strip()
        if raw_password and raw_hash:
            raise ValueError("use either password or password_hash, not both")

        if raw_password:
            self._set_password_plaintext(username, raw_password)
            return "spawn-password"
        if raw_hash:
            self._set_password_hash(username, raw_hash)
            return "spawn-password-hash"

        if use_global_password:
            config = self.store.read_config()
            global_hash = str(config.get("spawn_password_hash", "")).strip()
            if global_hash:
                self._set_password_hash(username, global_hash)
                return "global-password-hash"
        return "none"

    @staticmethod
    def _set_password_plaintext(username: str, password: str) -> None:
        if not password:
            raise ValueError("password cannot be empty")
        subprocess.run(
            ["chpasswd"],
            input=f"{username}:{password}\n",
            text=True,
            check=True,
        )

    @staticmethod
    def _set_password_hash(username: str, password_hash: str) -> None:
        if not password_hash:
            raise ValueError("password_hash cannot be empty")
        subprocess.run(["usermod", "-p", password_hash, username], check=True)

    @staticmethod
    def _hash_password(password: str) -> str:
        if not password:
            raise ValueError("spawn password cannot be empty")
        return str(crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512)))

    def _service_command(self, provider: str, action: str, linux_user: str) -> list[str]:
        executable = self._resolve_provider_executable(provider)
        base = [executable, "service", action]
        if not linux_user:
            return base

        current_user = ""
        try:
            current_user = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            current_user = ""

        if linux_user == current_user:
            return base

        if os.geteuid() != 0:
            raise SetupError(
                "service control requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )
        return ["sudo", "-u", linux_user, "-H", "--", *base]

    def _channel_connect_commands(
        self,
        provider: str,
        kind: str,
        name: str,
        linux_user: str,
    ) -> list[list[str]]:
        executable = self._resolve_provider_executable(provider)
        adapter = get_channel_adapter(provider)
        commands = adapter.connect_commands(executable=executable, kind=kind, name=name)

        wrapped: list[list[str]] = []
        for raw in commands:
            if linux_user:
                wrapped.append(self._sudo_wrap(raw, linux_user))
            else:
                wrapped.append(raw)
        return wrapped

    def _sudo_wrap(self, base: list[str], linux_user: str) -> list[str]:
        current_user = ""
        try:
            current_user = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            current_user = ""
        if not linux_user or linux_user == current_user:
            return base
        if os.geteuid() != 0:
            raise SetupError(
                "channel connect requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )
        return ["sudo", "-u", linux_user, "-H", "--", *base]

    @staticmethod
    def _resolve_provider_executable(provider: str) -> str:
        resolved = shutil.which(provider)
        if resolved:
            return resolved
        fallback = f"/home/linuxbrew/.linuxbrew/bin/{provider}"
        if Path(fallback).exists():
            return fallback
        raise SetupError(
            f"provider executable '{provider}' was not found in PATH. Install or link it before using service controls."
        )

    def _service_env(self, linux_user: str) -> dict[str, str]:
        env = dict(os.environ)
        current_path = env.get("PATH", "")
        required_paths = ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        merged = [segment for segment in current_path.split(":") if segment]
        for segment in required_paths:
            if segment not in merged:
                merged.append(segment)
        env["PATH"] = ":".join(merged)

        if linux_user:
            try:
                record = pwd.getpwnam(linux_user)
                uid = int(record.pw_uid)
                env["HOME"] = record.pw_dir
                env["USER"] = linux_user
                env["LOGNAME"] = linux_user
                env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
            except KeyError:
                pass
        return env

    def _bootstrap_user_bus(self, linux_user: str) -> None:
        if not linux_user or os.geteuid() != 0:
            return
        try:
            uid = int(pwd.getpwnam(linux_user).pw_uid)
        except KeyError:
            return

        subprocess.run(["loginctl", "enable-linger", linux_user], check=False)
        subprocess.run(["systemctl", "start", f"user@{uid}.service"], check=False)

    def _fallback_service_action(
        self,
        provider: str,
        action: str,
        linux_user: str,
        executable: str,
        agent_info: dict[str, Any],
    ) -> dict[str, str]:
        _ = provider
        pid = int(agent_info.get("fallback_pid", 0) or 0)

        if action == "status":
            running = self._is_pid_running(pid, linux_user)
            return {
                "service_status": "running" if running else "stopped",
                "output": "fallback daemon " + ("running" if running else "stopped"),
            }

        if action == "stop":
            if pid and self._is_pid_running(pid, linux_user):
                self._kill_pid(pid, linux_user)
            agent_info["fallback_pid"] = 0
            return {"service_status": "stopped", "output": "fallback daemon stopped"}

        if action == "restart":
            if pid and self._is_pid_running(pid, linux_user):
                self._kill_pid(pid, linux_user)
            new_pid = self._start_fallback_daemon(executable, linux_user)
            agent_info["fallback_pid"] = new_pid
            return {"service_status": "running", "output": f"fallback daemon restarted pid={new_pid}"}

        # start
        if pid and self._is_pid_running(pid, linux_user):
            return {"service_status": "running", "output": f"fallback daemon already running pid={pid}"}
        new_pid = self._start_fallback_daemon(executable, linux_user)
        agent_info["fallback_pid"] = new_pid
        return {"service_status": "running", "output": f"fallback daemon started pid={new_pid}"}

    def _start_fallback_daemon(self, executable: str, linux_user: str) -> int:
        script = (
            'mkdir -p "$HOME/.zeroclaw"; '
            f'nohup "{executable}" daemon >>"$HOME/.zeroclaw/daemon.log" 2>&1 & echo $!'
        )
        cmd = self._user_shell_command(linux_user, script)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise SetupError("fallback daemon start failed: " + (message or f"exit {result.returncode}"))
        token = (result.stdout or "").strip().splitlines()
        if not token:
            raise SetupError("fallback daemon start failed: pid not reported")
        try:
            return int(token[-1].strip())
        except ValueError as exc:
            raise SetupError("fallback daemon start failed: invalid pid output") from exc

    def _kill_pid(self, pid: int, linux_user: str) -> None:
        if pid <= 0:
            return
        script = f"kill {pid}"
        cmd = self._user_shell_command(linux_user, script)
        subprocess.run(cmd, capture_output=True, text=True, check=False)

    def _is_pid_running(self, pid: int, linux_user: str) -> bool:
        if pid <= 0:
            return False
        script = f"kill -0 {pid}"
        cmd = self._user_shell_command(linux_user, script)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.returncode == 0

    @staticmethod
    def _normalized_local_service_state(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        payload = config.get("local_service_state", {})
        if not isinstance(payload, dict):
            payload = {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            normalized[str(key).strip().lower()] = dict(value)
        return normalized

    @staticmethod
    def _normalized_channel_pool(config: dict[str, Any]) -> list[dict[str, str]]:
        raw = config.get("channel_pool", [])
        if not isinstance(raw, list):
            return []
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "provider": str(item.get("provider", "")).strip().lower(),
                    "external_id": str(item.get("external_id", "")).strip(),
                }
            )
        return rows

    def _read_channel_pool(self) -> list[dict[str, str]]:
        config = self.store.read_config()
        return self._normalized_channel_pool(config)

    def _write_channel_pool(self, channels: list[dict[str, str]]) -> None:
        config = self.store.read_config()
        config["channel_pool"] = self._normalized_channel_pool({"channel_pool": channels})
        config["updated_at"] = now_iso()
        self.store.write_config(config)

    def _remove_pool_channel(self, kind: str, name: str) -> None:
        current = self._read_channel_pool()
        remaining = [
            row
            for row in current
            if not (
                str(row.get("kind", "")).strip().lower() == kind
                and str(row.get("name", "")).strip() == name
            )
        ]
        if len(remaining) != len(current):
            self._write_channel_pool(remaining)

    def _local_dashboard_rows(self, refresh: bool = False) -> list[dict[str, Any]]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        installed = self.list_installed_claws()
        user_hints: dict[str, str] = {}
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                user_hints[provider] = hint
        providers = [
            str(row.get("provider", "")).strip().lower()
            for row in installed
            if str(row.get("provider", "")).strip()
        ]
        if refresh and providers:
            local_state = self._refresh_local_service_statuses(providers, local_state, user_hints=user_hints)
            config = self.store.read_config()
        rows: list[dict[str, Any]] = []
        # Use the same home-resolution logic as list_installed_claws() so
        # `sudo clawie dashboard` still inspects the invoking user's claws.
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            local_info = local_state.get(provider, {})
            rows.append(
                {
                    "agent_id": f"@local:{provider}",
                    "display_name": "local-user",
                    "status": str(local_info.get("service_status", "unknown")),
                    "version": "local",
                    "provider": provider,
                    "strategy": "local-user",
                    "channels": 0,
                    "channels_total": 0,
                    "migrated": 0,
                    "last_sync": str(config.get("updated_at", "")),
                    "pid": int(local_info.get("fallback_pid", 0) or 0),
                    "cpu_percent": 0.0,
                    "mem_percent": 0.0,
                    "rss_kb": 0,
                    "local_user": True,
                }
            )
        return rows

    def _local_channel_inventory(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for claw in self.list_installed_claws():
            provider = str(claw.get("provider", "")).strip().lower()
            root = Path(str(claw.get("root", "")).strip())
            if not provider or not root:
                continue
            discovered = self._discover_channels_for_provider_root(provider, root)
            for channel in discovered:
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                rows.append(
                    {
                        "source": "local",
                        "owner_agent_id": f"@local:{provider}",
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                    }
                )
        return rows

    def _discover_channels_for_provider_root(self, provider: str, root: Path) -> list[dict[str, str]]:
        adapter = get_channel_adapter(provider)
        return adapter.discover_channels(root)

    def _refresh_local_service_statuses(
        self,
        providers: list[str],
        local_state: dict[str, dict[str, Any]],
        user_hints: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        config = self.store.read_config()
        default_user = self._local_target_user()
        dirty = False
        for provider in providers:
            info = local_state.setdefault(provider, {})
            hint_user = str((user_hints or {}).get(provider, "")).strip()
            cached_user = str(info.get("linux_user", "")).strip()
            linux_user = self._preferred_local_linux_user(
                default_user=default_user,
                hint_user=hint_user,
                cached_user=cached_user,
            )
            if linux_user and linux_user != str(info.get("linux_user", "")).strip():
                info["linux_user"] = linux_user
                dirty = True
            unit_status = self._systemd_user_service_status(provider, linux_user)
            if unit_status != "unknown":
                status = unit_status
                mode = "systemd"
                if status != str(info.get("service_status", "unknown")):
                    info["service_status"] = status
                    dirty = True
                if mode != str(info.get("service_mode", "unknown")):
                    info["service_mode"] = mode
                    dirty = True
                continue
            try:
                probe = self._run_local_provider_command(provider, "status", linux_user)
                cmd = probe["command"]
                result = probe["result"]
                output = probe["output"]
                lowered = output.lower()
                if result.returncode != 0 and "failed to connect to bus" in lowered:
                    fallback = self._fallback_service_action(
                        provider=provider,
                        action="status",
                        linux_user=linux_user,
                        executable=cmd[0],
                        agent_info=info,
                    )
                    status = str(fallback.get("service_status", "unknown"))
                    mode = "fallback"
                else:
                    inferred = self._infer_service_status(output)
                    if inferred == "unknown":
                        status = self._best_effort_local_status(info, linux_user)
                        mode = "fallback"
                    elif result.returncode != 0:
                        status = self._best_effort_local_status(info, linux_user)
                        mode = "fallback"
                    else:
                        status = inferred
                        mode = "systemd"
            except Exception:
                status = self._best_effort_local_status(info, linux_user)
                mode = "fallback"
            if status != str(info.get("service_status", "unknown")):
                info["service_status"] = status
                dirty = True
            if mode != str(info.get("service_mode", "unknown")):
                info["service_mode"] = mode
                dirty = True

        if dirty:
            config["local_service_state"] = local_state
            self.store.write_config(config)
        return local_state

    def _systemd_user_service_status(self, provider: str, linux_user: str) -> str:
        service = f"{provider}.service"
        candidates: list[str] = []
        for token in (
            str(linux_user).strip(),
            str(self._local_target_user()).strip(),
            str(self._local_linux_user_hint(provider, "")).strip(),
        ):
            if token and token not in candidates:
                candidates.append(token)
        home_root = Path("/home")
        if home_root.exists():
            for entry in sorted(home_root.iterdir(), key=lambda row: str(getattr(row, "name", ""))):
                if not entry.is_dir():
                    continue
                token = entry.name.strip()
                if token and token not in candidates:
                    candidates.append(token)

        saw_stopped = False
        for candidate in candidates:
            if candidate == "root":
                continue
            cmd = ["systemctl", "--machine", f"{candidate}@", "--user", "is-active", service]
            env = self._systemctl_env()
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            except Exception:
                continue
            parsed = self._parse_systemctl_status(result.stdout, result.stderr)
            if parsed == "running":
                return "running"
            if parsed == "stopped":
                saw_stopped = True

        fallback_env_user = candidates[0] if candidates else str(linux_user).strip()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(fallback_env_user),
            )
        except Exception:
            return "stopped" if saw_stopped else "unknown"
        parsed = self._parse_systemctl_status(result.stdout, result.stderr)
        if parsed == "running":
            return parsed
        if parsed == "stopped":
            return "stopped"
        return "stopped" if saw_stopped else "unknown"

    def _systemd_user_service_manage(self, provider: str, action: str, linux_user: str) -> dict[str, Any]:
        service = f"{provider}.service"
        candidates: list[str] = []
        for token in (
            str(linux_user).strip(),
            str(self._local_target_user()).strip(),
            str(self._local_linux_user_hint(provider, "")).strip(),
        ):
            if token and token not in candidates:
                candidates.append(token)
        home_root = Path("/home")
        if home_root.exists():
            for entry in sorted(home_root.iterdir(), key=lambda row: str(getattr(row, "name", ""))):
                if not entry.is_dir():
                    continue
                token = entry.name.strip()
                if token and token not in candidates:
                    candidates.append(token)

        last_output = ""
        for candidate in candidates:
            if candidate == "root":
                continue
            cmd = ["systemctl", "--machine", f"{candidate}@", "--user", action, service]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._systemctl_env(),
                )
            except Exception as exc:
                last_output = str(exc)
                continue
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": cmd}
            last_output = output or f"exit {result.returncode}"

        fallback_user = candidates[0] if candidates else str(linux_user).strip()
        try:
            result = subprocess.run(
                ["systemctl", "--user", action, service],
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(fallback_user),
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": ["systemctl", "--user", action, service]}
            last_output = output or f"exit {result.returncode}"
        except Exception as exc:
            last_output = str(exc)
        return {"ok": False, "output": last_output}

    @staticmethod
    def _systemctl_env() -> dict[str, str]:
        env = dict(os.environ)
        current_path = env.get("PATH", "")
        required_paths = ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        merged = [segment for segment in current_path.split(":") if segment]
        for segment in required_paths:
            if segment not in merged:
                merged.append(segment)
        env["PATH"] = ":".join(merged)
        return env

    @staticmethod
    def _parse_systemctl_status(stdout: str, stderr: str) -> str:
        text = (stdout or "").strip().lower()
        err = (stderr or "").strip().lower()
        token = text or err
        if not token:
            return "unknown"
        if "connect to bus" in token:
            return "unknown"
        if token in {"active", "activating", "reloading"} or token.startswith("active"):
            return "running"
        if token in {"inactive", "failed", "deactivating", "dead"} or token.startswith("inactive"):
            return "stopped"
        return "unknown"

    def _local_linux_user_hint(self, provider: str, fallback: str) -> str:
        fallback_user = str(fallback).strip()
        if fallback_user and fallback_user != "root":
            return fallback_user
        name = str(provider).strip().lower()
        for claw in self.list_installed_claws():
            if str(claw.get("provider", "")).strip().lower() != name:
                continue
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                return hint
        return fallback_user

    @staticmethod
    def _preferred_local_linux_user(
        default_user: str,
        hint_user: str,
        cached_user: str,
    ) -> str:
        default_token = str(default_user).strip()
        hint_token = str(hint_user).strip()
        cached_token = str(cached_user).strip()
        # Always prefer the current invoking user (e.g. SUDO_USER) over stale cache.
        for candidate in (default_token, hint_token, cached_token):
            if candidate and candidate != "root":
                return candidate
        return default_token or hint_token or cached_token

    @staticmethod
    def _linux_user_from_provider_root(root: Path) -> str:
        parts = root.parts
        if len(parts) >= 3 and parts[1] == "home":
            return str(parts[2]).strip()
        if len(parts) >= 2 and parts[1] == "root":
            return "root"
        return ""

    def _run_local_provider_command(
        self,
        provider: str,
        action: str,
        linux_user: str,
    ) -> dict[str, Any]:
        attempts: list[tuple[list[str], dict[str, str]]] = []
        if os.geteuid() == 0 and linux_user and linux_user != "root":
            attempts.append(
                (self._service_command(provider, action, linux_user=linux_user), self._service_env(linux_user))
            )
        attempts.append((self._service_command(provider, action, linux_user=""), self._service_env(linux_user)))

        last: dict[str, Any] | None = None
        for idx, (cmd, env) in enumerate(attempts):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            except Exception:
                if idx + 1 < len(attempts):
                    continue
                raise

            output = (result.stdout or result.stderr or "").strip()
            last = {"command": cmd, "result": result, "output": output}

            retry_allowed = idx + 1 < len(attempts)
            unparseable_status = action == "status" and self._infer_service_status(output) == "unknown"
            failed = result.returncode != 0
            if retry_allowed and (failed or unparseable_status):
                continue
            return last

        if last is not None:
            return last
        raise SetupError(f"{provider} service {action} failed before process launch")

    def _best_effort_local_status(self, info: dict[str, Any], linux_user: str) -> str:
        status = self._normalize_status_text(str(info.get("service_status", "unknown")))
        if status != "unknown":
            return status
        pid = int(info.get("fallback_pid", 0) or 0)
        if pid > 0:
            return "running" if self._is_pid_running(pid, linux_user) else "stopped"
        return "stopped"

    @staticmethod
    def _local_target_user() -> str:
        if os.geteuid() == 0:
            sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
            if sudo_user and sudo_user != "root":
                return sudo_user
        try:
            return str(pwd.getpwuid(os.geteuid()).pw_name)
        except KeyError:
            return ""

    def _local_agent_view(self, provider: str) -> dict[str, Any]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        local_state = self._refresh_local_service_statuses([provider], local_state)
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        info = dict(local_state.get(provider, {}))
        return {
            "agent_id": f"@local:{provider}",
            "display_name": "local-user",
            "source_template": "local-user",
            "clone_from": "",
            "channel_strategy": "local-user",
            "channels": [],
            "agent": {
                "provider": provider,
                "auth_mode": str(self._provider_auth(provider).get("auth_mode", "")),
                "autostart": False,
                "heartbeat_seconds": 0,
                "status": str(info.get("service_status", "unknown")),
                "service_status": str(info.get("service_status", "unknown")),
                "service_mode": str(info.get("service_mode", "unknown")),
                "fallback_pid": int(info.get("fallback_pid", 0) or 0),
                "version": "local",
                "plugins": self._default_plugins_for_provider(provider),
                "local_user": True,
            },
        }

    def _user_shell_command(self, linux_user: str, script: str) -> list[str]:
        if not linux_user:
            return ["bash", "-lc", script]

        current_user = ""
        try:
            current_user = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            current_user = ""
        if linux_user == current_user:
            return ["bash", "-lc", script]
        if os.geteuid() != 0:
            raise SetupError(
                "service control requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )
        return ["sudo", "-u", linux_user, "-H", "--", "bash", "-lc", script]

    @staticmethod
    def _infer_service_status(output: str) -> str:
        text = str(output).strip().lower()
        if "running" in text or "active" in text or "started" in text:
            return "running"
        if "stopped" in text or "inactive" in text or "dead" in text:
            return "stopped"
        return "unknown"

    @staticmethod
    def _normalize_status_text(value: str) -> str:
        token = str(value).strip().lower()
        if token in {"running", "active", "started"}:
            return "running"
        if token in {"stopped", "inactive", "dead", "offline"}:
            return "stopped"
        if token in {"ready", "syncing"}:
            return token
        return "unknown"

    def _dashboard_status(self, metric_status: str, agent_info: dict[str, Any]) -> str:
        service_status = self._normalize_status_text(str(agent_info.get("service_status", "")))
        if service_status != "unknown":
            return service_status
        measured = self._normalize_status_text(metric_status)
        if measured != "unknown":
            return measured
        return self._normalize_status_text(str(agent_info.get("status", "unknown")))

    def _default_plugins_for_provider(self, provider: str) -> dict[str, bool]:
        _ = provider
        return copy.deepcopy(self.DEFAULT_AGENT_PLUGINS)

    def _normalize_plugins(self, plugins: dict[str, Any]) -> dict[str, bool]:
        merged = self._default_plugins_for_provider("")
        for key, value in plugins.items():
            token = str(key).strip().lower()
            if not token:
                continue
            merged[token] = bool(value)
        return merged

    def _hydrate_agent_controls(self, agent_state: dict[str, Any]) -> None:
        channels = agent_state.get("channels", [])
        if isinstance(channels, list):
            for channel in channels:
                if isinstance(channel, dict):
                    channel["enabled"] = bool(channel.get("enabled", True))
        agent = agent_state.setdefault("agent", {})
        raw_plugins = agent.get("plugins", self._default_plugins_for_provider(str(agent.get("provider", ""))))
        if not isinstance(raw_plugins, dict):
            raw_plugins = self._default_plugins_for_provider(str(agent.get("provider", "")))
        agent["plugins"] = self._normalize_plugins(raw_plugins)

    def _discover_channels_from_source_home(
        self,
        source_home: Path,
        requested_provider: str | None,
    ) -> list[dict[str, str]]:
        providers: list[str] = []
        if requested_provider:
            providers.append(str(requested_provider).strip().lower())
        config = self.store.read_config()
        providers.append(str(config.get("provider", "zeroclaw")).strip().lower())

        channels: list[dict[str, str]] = []
        for provider in providers:
            try:
                state_dir = get_provider(provider).state_dir
            except ValueError:
                continue
            adapter = get_channel_adapter(provider)
            channels.extend(adapter.discover_channels(source_home / state_dir))
        return dedupe_channels(channels)
