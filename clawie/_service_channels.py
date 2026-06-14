"""Channel discovery, assignment, sync, and connect flows (ClawieService mixin)."""
from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path
from typing import Any
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]  # Python 3.10 fallback
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]
from clawie.provider_channels import dedupe_channels, get_channel_adapter
from clawie.providers import (
    get_provider,
    provider_names,
)
from clawie.service_common import SetupError, AgentNotFoundError, now_iso


class ChannelOpsMixin:

    def toggle_agent_channel(self, agent_id: str, channel_index: int) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
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

    def _reconnect_agent_channels(
        self,
        *,
        provider: str,
        linux_user: str,
        channels: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        reconnectable: list[dict[str, str]] = []
        if str(provider).strip().lower() in {"picoclaw", "openclaw"}:
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                if not bool(channel.get("enabled", True)):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name or kind == "cli" or kind != "telegram":
                    continue
                reconnectable.append({"kind": kind, "name": name})
            return reconnectable

        commands: list[list[str]] = []
        seen_commands: set[tuple[str, ...]] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            if not bool(channel.get("enabled", True)):
                continue
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name or kind == "cli":
                continue
            reconnectable.append({"kind": kind, "name": name})
            for cmd in self._channel_connect_commands(provider, kind, name, linux_user):
                key = tuple(cmd)
                if key in seen_commands:
                    continue
                seen_commands.add(key)
                commands.append(cmd)

        env = self._service_env(linux_user)
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                raise SetupError(
                    f"channel reconnect failed for {provider}: {output or f'exit {result.returncode}'}"
                )
        return reconnectable

    def _effective_agent_channels(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        view = self._attach_agent_channel_view(copy.deepcopy(payload))
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for channel in view.get("channels", []):
            if not isinstance(channel, dict):
                continue
            if not bool(channel.get("enabled", True)):
                continue
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
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
                    "enabled": True,
                    "external_id": str(channel.get("external_id", "")).strip(),
                    "discovered_provider": str(channel.get("discovered_provider", "")).strip().lower(),
                }
            )
        return rows

    def _persist_effective_agent_channels(
        self,
        payload: dict[str, Any],
        channels: list[dict[str, Any]],
    ) -> None:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        existing_rows = payload.get("channels", [])
        existing_map: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing_rows, list):
            for row in existing_rows:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key[0] and key[1]:
                    existing_map[key] = dict(row)

        persisted: list[dict[str, Any]] = []
        for idx, channel in enumerate(channels, start=1):
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            row = dict(existing_map.get(key, {}))
            row["kind"] = kind
            row["name"] = name
            row["enabled"] = bool(channel.get("enabled", True))
            external_id = str(channel.get("external_id", row.get("external_id", ""))).strip()
            if external_id:
                row["external_id"] = external_id
            elif agent_id:
                row["external_id"] = f"{agent_id}:{kind}:{idx}"
            row.pop("channel_source", None)
            row.pop("discovered_provider", None)
            persisted.append(row)
        payload["channels"] = persisted

    def _provider_channel_payloads_for_home(
        self,
        provider: str,
        root: Path,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        name = str(provider).strip().lower()
        if name == "zeroclaw":
            return self._read_zeroclaw_channel_payloads(root)
        if name == "picoclaw":
            return self._read_picoclaw_channel_payloads(root)
        if name == "openclaw":
            return self._read_openclaw_channel_payloads(root)
        return {}

    def _read_zeroclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "config.toml"
        if tomllib is None or not config_path.exists():
            return {}
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        channels_cfg = payload.get("channels_config", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        if bool(channels_cfg.get("cli")):
            rows[("cli", "local")] = {
                "kind": "cli",
                "name": "local",
                "provider": "zeroclaw",
                "settings": {"enabled": True},
            }
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "cli" or not kind or not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue
            name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "zeroclaw",
                "settings": dict(value),
            }
        return rows

    def _read_picoclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "config.json"
        payload = self._read_json_file(config_path)
        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if not kind:
                continue
            if isinstance(value, dict):
                enabled = bool(value.get("enabled", True))
                name = str(value.get("name", kind)).strip().lower() or kind
                settings = dict(value)
            else:
                enabled = bool(value)
                name = kind
                settings = {"enabled": enabled}
            if not enabled:
                continue
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "picoclaw",
                "settings": settings,
            }
        return rows

    def _read_openclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "openclaw.json"
        payload = self._read_json_file(config_path)
        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "defaults":
                continue
            if not kind or not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue

            settings = dict(value)
            settings.pop("accounts", None)
            name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "openclaw",
                "settings": settings,
            }

            accounts = value.get("accounts", {})
            if not isinstance(accounts, dict):
                continue
            for account_id, account_value in accounts.items():
                if not isinstance(account_value, dict):
                    continue
                if not bool(account_value.get("enabled", value.get("enabled", True))):
                    continue
                account_name = (
                    str(account_value.get("name", account_id)).strip().lower().replace(" ", "-")
                    or str(account_id).strip().lower()
                    or kind
                )
                account_settings = dict(settings)
                account_settings.update(account_value)
                rows[(kind, account_name)] = {
                    "kind": kind,
                    "name": account_name,
                    "provider": "openclaw",
                    "settings": account_settings,
                }
        return rows

    def _can_read_provider_channel_roots(self, home: Path, providers: list[str]) -> bool:
        for item in providers:
            token = str(item).strip().lower()
            if not token:
                continue
            try:
                root = home / get_provider(token).state_dir
            except ValueError:
                continue
            if root.exists() and os.access(root, os.R_OK | os.X_OK):
                return True
        return False

    def _discover_live_channel_payloads(self, payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        info = payload.get("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        is_local = bool(info.get("local_user", False))
        provider = str(info.get("provider", "")).strip().lower()
        home = self._local_agent_home(provider) if is_local else self._agent_linux_home(payload)
        if not home:
            return {}
        if linux_user and not is_local and not self._can_manage_linux_user(linux_user):
            if not self._can_read_provider_channel_roots(home, [provider, *provider_names()]):
                return {}

        ordered: list[str] = []
        seen_providers: set[str] = set()
        for item in [provider] + provider_names():
            token = str(item).strip().lower()
            if not token or token in seen_providers:
                continue
            seen_providers.add(token)
            ordered.append(token)

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for name in ordered:
            root = home / get_provider(name).state_dir
            for key, value in self._provider_channel_payloads_for_home(name, root).items():
                rows.setdefault(key, value)
        return rows

    @staticmethod
    def _looks_like_unresolved_secret(value: str) -> bool:
        token = str(value).strip()
        if not token:
            return False
        if re.search(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*", token):
            return True
        lowered = token.lower()
        if lowered.startswith("env:") or lowered.startswith("secret:"):
            return True
        return "{{" in token and "}}" in token

    @staticmethod
    def _looks_like_telegram_bot_token(value: str) -> bool:
        token = str(value).strip()
        return bool(re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token))

    def migrate_channels(
        self,
        from_agent: str,
        to_agent: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        if from_agent == to_agent:
            raise ValueError("from_agent and to_agent must differ")
        state = self.store.read_state()

        agents = state.setdefault("agents", {})
        source = agents.get(from_agent)
        target = agents.get(to_agent)
        if not source:
            raise AgentNotFoundError(f"source agent not found: {from_agent}")
        if not target:
            raise AgentNotFoundError(f"target agent not found: {to_agent}")

        source_channels = copy.deepcopy(source.get("channels", []))
        for channel in source_channels:
            channel["migrated_from"] = from_agent
        source_keys = self._channel_keys(source_channels)
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=to_agent,
            channels=source_channels,
            allow_owners={from_agent, to_agent},
        )

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
        moved_from_source = self._remove_channel_keys_from_agent(source=source, keys=source_keys)
        if moved_from_source:
            source.setdefault("agent", {})["last_sync"] = now_iso()
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
                "moved_from_source": moved_from_source,
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
        agents = state.setdefault("agents", {})
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
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=agent_id,
            channels=target_channels,
        )

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
        agents = state.setdefault("agents", {})
        rows: list[dict[str, Any]] = []
        assigned_keys: set[tuple[str, str]] = set()
        for aid, payload in sorted(agents.items()):
            view = self._attach_agent_channel_view(copy.deepcopy(payload))
            provider = str(view.get("agent", {}).get("provider", "")).strip().lower()
            for channel in view.get("channels", []):
                if not isinstance(channel, dict):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                assigned_keys.add((kind, name))
                rows.append(
                    {
                        "source": str(channel.get("channel_source", "agent")) or "agent",
                        "owner_agent_id": str(aid),
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                        "discovered_provider": str(channel.get("discovered_provider", "")),
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
                "assigned": sum(
                    1 for row in rows if str(row.get("owner_agent_id", "")).strip() not in {"", "@pool"}
                ),
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
        agents = state.setdefault("agents", {})
        target = agents.get(dst)
        if not target:
            raise AgentNotFoundError(f"target agent not found: {dst}")
        self._hydrate_agent_controls(target)
        target_channels = target.setdefault("channels", [])
        if not isinstance(target_channels, list):
            target_channels = []
            target["channels"] = target_channels

        moved_from_agents = self._remove_channel_from_other_agents(
            agents=agents,
            kind=channel_kind,
            name=channel_name,
            keep_agent_id=dst,
        )
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

        moved = bool(moved_from_agents)

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
                "moved_from_agent_ids": moved_from_agents,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "target_agent_id": dst,
            "kind": channel_kind,
            "name": channel_name,
            "moved": moved,
            "moved_from_agent_ids": moved_from_agents,
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
        agents = state.setdefault("agents", {})
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

        provider = str(source.get("agent", {}).get("provider", "")).strip().lower()
        linux_user = str(source.get("agent", {}).get("linux_user", "")).strip()
        home = self._agent_linux_home(source)
        if provider in {"picoclaw", "openclaw"} and linux_user and home:
            self._prepare_agent_provider_home(
                provider=provider,
                agent=source,
                linux_user=linux_user,
                home=home,
                channels=self._effective_agent_channels(source),
                live_payloads=self._discover_live_channel_payloads(source),
            )
            if provider == "picoclaw":
                self._remove_picoclaw_channel_from_home(home=home, linux_user=linux_user, kind=channel_kind)
            else:
                self._remove_openclaw_channel_from_home(home=home, linux_user=linux_user, kind=channel_kind)
            if self._provider_process_live(provider, linux_user):
                result = self._run_managed_provider_service_action(
                    provider=provider,
                    action="restart",
                    linux_user=linux_user,
                    agent_info=source.setdefault("agent", {}),
                )
                source["agent"]["service_status"] = str(result.get("service_status", "unknown"))
                source["agent"]["service_mode"] = str(result.get("service_mode", "unknown"))

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
        self._refresh_managed_agent_provider_alignment(target)

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(target)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{target}' has no provider configured")
        linux_user = str(info.get("linux_user", "")).strip()
        existing_channels = agent.get("channels", [])
        already_assigned = (
            isinstance(existing_channels, list)
            and self._find_channel(existing_channels, channel_kind, channel_name) is not None
        )
        if not already_assigned:
            self.assign_channel_to_agent("", channel_kind, channel_name, target)
            state = self.store.read_state()
            agents = state.setdefault("agents", {})
            agent = agents.get(target)
            if not agent:
                raise AgentNotFoundError(f"agent not found: {target}")
            self._hydrate_agent_controls(agent)
            info = agent.setdefault("agent", {})
            provider = str(info.get("provider", "")).strip().lower()
            linux_user = str(info.get("linux_user", "")).strip()

        if provider == "picoclaw":
            home = self._agent_linux_home(agent)
            effective_channels = self._effective_agent_channels(agent)
            live_payloads = self._discover_live_channel_payloads(agent)
            if home:
                self._write_prompt_files_for_home(
                    provider, home, agent.get("core_prompts", {}), linux_user,
                )
            self._prepare_agent_provider_home(
                provider=provider,
                agent=agent,
                linux_user=linux_user,
                home=home,
                channels=effective_channels,
                live_payloads=live_payloads,
            )
            if self._provider_process_live(provider, linux_user):
                result = self._run_managed_provider_service_action(
                    provider=provider,
                    action="restart",
                    linux_user=linux_user,
                    agent_info=info,
                )
                info["service_status"] = str(result.get("service_status", "unknown"))
                info["service_mode"] = str(result.get("service_mode", "unknown"))
            info["last_sync"] = now_iso()
            self._event(
                state,
                "channels.connected",
                f"Connected channel {channel_kind}:{channel_name} for {target}",
                {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": "config-write",
                },
            )
            self.store.write_state(state)
            return {
                "agent_id": target,
                "provider": provider,
                "kind": channel_kind,
                "name": channel_name,
                "command": [],
                "output": "configured provider channel",
                "status": "connected",
            }
        if provider == "openclaw":
            home = self._agent_linux_home(agent)
            effective_channels = self._effective_agent_channels(agent)
            live_payloads = self._discover_live_channel_payloads(agent)
            if channel_kind == "telegram" and (
                live_payloads.get((channel_kind, channel_name))
                or any(str(key[0]).strip().lower() == channel_kind for key in live_payloads)
            ):
                if home:
                    self._write_prompt_files_for_home(
                        provider, home, agent.get("core_prompts", {}), linux_user,
                    )
                self._prepare_agent_provider_home(
                    provider=provider,
                    agent=agent,
                    linux_user=linux_user,
                    home=home,
                    channels=effective_channels,
                    live_payloads=live_payloads,
                )
                if self._provider_process_live(provider, linux_user):
                    result = self._run_managed_provider_service_action(
                        provider=provider,
                        action="restart",
                        linux_user=linux_user,
                        agent_info=info,
                    )
                    info["service_status"] = str(result.get("service_status", "unknown"))
                    info["service_mode"] = str(result.get("service_mode", "unknown"))
                info["last_sync"] = now_iso()
                self._event(
                    state,
                    "channels.connected",
                    f"Connected channel {channel_kind}:{channel_name} for {target}",
                    {
                        "agent_id": target,
                        "provider": provider,
                        "kind": channel_kind,
                        "name": channel_name,
                        "command": "config-write",
                    },
                )
                self.store.write_state(state)
                return {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": [],
                    "output": "configured provider channel",
                    "status": "connected",
                }

        commands = self._channel_connect_commands(provider, channel_kind, channel_name, linux_user)
        last_error = ""
        env = self._service_env(linux_user)
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                state = self.store.read_state()
                agents = state.setdefault("agents", {})
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

        if not already_assigned:
            try:
                self.unassign_channel_from_agent(target, channel_kind, channel_name)
            except Exception:
                pass

        raise SetupError(
            f"channel connect failed for {target} ({provider}): {last_error}. "
            + ("attempted: " + " || ".join(" ".join(cmd) for cmd in commands) if commands else "")
        )

    def sync_agent_channels_from_provider(self, agent_id: str, *, replace: bool = True) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            raise ValueError("channel sync is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(token)
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        discovery = self._discover_agent_channels(agent)
        if str(discovery.get("source", "")) == "permission":
            raise SetupError(str(discovery.get("detail", "live channel discovery requires root")))
        discovered = discovery.get("channels", [])
        if not isinstance(discovered, list) or not discovered:
            raise SetupError(str(discovery.get("detail", "no live channels discovered")))

        existing = agent.get("channels", [])
        existing_map: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing, list):
            for row in existing:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key[0] and key[1]:
                    existing_map[key] = dict(row)

        synced: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for channel in discovered:
            key = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if key in seen or not key[0] or not key[1]:
                continue
            seen.add(key)
            row = dict(existing_map.get(key, {}))
            row["kind"] = key[0]
            row["name"] = key[1]
            row["enabled"] = bool(channel.get("enabled", row.get("enabled", True)))
            row["external_id"] = str(row.get("external_id", f"{token}:{key[0]}:{len(synced) + 1}"))
            synced.append(row)

        if not replace and isinstance(existing, list):
            for row in existing:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key in seen or not key[0] or not key[1]:
                    continue
                synced.append(dict(row))

        agent["channels"] = synced
        agent.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "channels.synced_from_provider",
            f"Synced live channels for {token}",
            {
                "agent_id": token,
                "replace": bool(replace),
                "channel_count": len(synced),
                "discovered_provider": list(discovery.get("providers", [])),
            },
        )
        self.store.write_state(state)
        return self.get_dashboard_agent(token)

    def _mint_channels(
        self,
        agent_id: str,
        base_channels: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        items = list(base_channels or [])
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

    @staticmethod
    def _channel_key(kind: str, name: str) -> tuple[str, str]:
        return (str(kind).strip().lower(), str(name).strip())

    def _channel_keys(self, channels: list[dict[str, Any]]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            kind, name = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if not kind or not name:
                continue
            keys.add((kind, name))
        return keys

    def _assert_channels_unclaimed(
        self,
        agents: dict[str, Any],
        owner_agent_id: str,
        channels: list[dict[str, Any]],
        allow_owners: set[str] | None = None,
    ) -> None:
        keys = self._channel_keys(channels)
        if not keys:
            return
        allowed = {str(owner_agent_id).strip()}
        if allow_owners:
            for item in allow_owners:
                token = str(item).strip()
                if token:
                    allowed.add(token)
        conflicts: list[str] = []
        for aid, payload in sorted(agents.items()):
            token = str(aid).strip()
            if token in allowed:
                continue
            rows = payload.get("channels", [])
            if not isinstance(rows, list):
                continue
            claimed = [
                f"{kind}:{name}" for (kind, name) in keys if self._find_channel(rows, kind, name) is not None
            ]
            if claimed:
                conflicts.append(f"{token} owns {', '.join(claimed)}")
        if conflicts:
            raise ValueError("channel already assigned to another agent: " + "; ".join(conflicts))

    def _remove_channel_keys_from_agent(
        self,
        source: dict[str, Any],
        keys: set[tuple[str, str]],
    ) -> int:
        if not keys:
            return 0
        channels = source.setdefault("channels", [])
        if not isinstance(channels, list):
            source["channels"] = []
            return 0
        kept: list[Any] = []
        removed = 0
        for channel in channels:
            if not isinstance(channel, dict):
                kept.append(channel)
                continue
            kind, name = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if (kind, name) in keys:
                removed += 1
                continue
            kept.append(channel)
        source["channels"] = kept
        return removed

    def _remove_channel_from_other_agents(
        self,
        agents: dict[str, Any],
        kind: str,
        name: str,
        keep_agent_id: str,
    ) -> list[str]:
        keep = str(keep_agent_id).strip()
        moved_from: list[str] = []
        for aid, payload in agents.items():
            token = str(aid).strip()
            if token == keep:
                continue
            rows = payload.setdefault("channels", [])
            if not isinstance(rows, list):
                continue
            removed_any = False
            while True:
                found_idx = self._find_channel(rows, kind, name)
                if found_idx is None:
                    break
                rows.pop(found_idx)
                removed_any = True
            if removed_any:
                moved_from.append(token)
                payload.setdefault("agent", {})["last_sync"] = now_iso()
        return moved_from

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
            wrapped.append(self._wrap_user_command(raw, linux_user, purpose="channel connect"))
        return wrapped

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

    def _discover_agent_channels(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        linux_user = str(info.get("linux_user", "")).strip()
        is_local = bool(info.get("local_user", False))
        if is_local:
            home = self._local_agent_home(provider)
        else:
            home = self._agent_linux_home(payload)
        if not home:
            return {"source": "none", "detail": "agent home is not available", "channels": [], "providers": []}
        if linux_user and not is_local and not self._can_manage_linux_user(linux_user):
            if not self._can_read_provider_channel_roots(home, [provider, *provider_names()]):
                return {
                    "source": "permission",
                    "detail": "live channel discovery requires root for managed agents owned by another Linux user",
                    "channels": [],
                    "providers": [],
                }

        ordered: list[str] = []
        seen_providers: set[str] = set()
        candidate_providers: list[str] = []
        if linux_user and not is_local:
            candidate_providers.extend(self._live_provider_names_for_user(linux_user))
        if not candidate_providers:
            candidate_providers = [provider, *provider_names()]
        for item in candidate_providers:
            token = str(item or "").strip().lower()
            if not token or token in seen_providers:
                continue
            seen_providers.add(token)
            ordered.append(token)

        discovered: list[dict[str, Any]] = []
        found_providers: list[str] = []
        seen_channels: set[tuple[str, str]] = set()
        for name in ordered:
            root = home / get_provider(name).state_dir
            channels = self._discover_channels_for_provider_root(name, root)
            provider_had_rows = False
            for channel in channels:
                key = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
                if key in seen_channels or not key[0] or not key[1]:
                    continue
                seen_channels.add(key)
                provider_had_rows = True
                discovered.append(
                    {
                        "kind": key[0],
                        "name": key[1],
                        "enabled": bool(channel.get("enabled", True)),
                        "discovered_provider": name,
                    }
                )
            if provider_had_rows:
                found_providers.append(name)

        if discovered:
            return {
                "source": "provider",
                "detail": "live channels discovered",
                "channels": discovered,
                "providers": found_providers,
            }
        return {
            "source": "none",
            "detail": "no live channels discovered",
            "channels": [],
            "providers": [],
        }

    def _attach_agent_channel_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.setdefault("agent", {})
        stored = payload.get("channels", [])
        stored_rows = [dict(row) for row in stored if isinstance(row, dict)] if isinstance(stored, list) else []
        discovery = self._discover_agent_channels(payload)
        live_rows = discovery.get("channels", [])
        live_map = {
            self._channel_key(row.get("kind", ""), row.get("name", "")): dict(row)
            for row in live_rows
            if isinstance(row, dict)
        }

        merged: list[dict[str, Any]] = []
        appended: set[tuple[str, str]] = set()
        for row in stored_rows:
            key = self._channel_key(row.get("kind", ""), row.get("name", ""))
            if not key[0] or not key[1]:
                continue
            live = live_map.get(key)
            if live:
                row["channel_source"] = "live"
                row["discovered_provider"] = str(live.get("discovered_provider", ""))
            elif str(discovery.get("source", "")) == "provider":
                row["channel_source"] = "stale"
            else:
                row["channel_source"] = "state"
            merged.append(row)
            appended.add(key)

        for row in live_rows:
            if not isinstance(row, dict):
                continue
            key = self._channel_key(row.get("kind", ""), row.get("name", ""))
            if key in appended or not key[0] or not key[1]:
                continue
            merged.append(
                {
                    "kind": key[0],
                    "name": key[1],
                    "enabled": bool(row.get("enabled", True)),
                    "external_id": "",
                    "channel_source": "discovered",
                    "discovered_provider": str(row.get("discovered_provider", "")),
                }
            )
            appended.add(key)

        def _sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
            source = str(row.get("channel_source", "state"))
            order = {"live": 0, "discovered": 1, "state": 2, "stale": 3}
            return (order.get(source, 9), str(row.get("kind", "")), str(row.get("name", "")))

        payload["channels"] = sorted(merged, key=_sort_key)
        info["channel_status_source"] = str(discovery.get("source", "state"))
        info["channel_status_detail"] = str(discovery.get("detail", ""))
        info["live_channel_count"] = sum(
            1 for row in payload["channels"] if str(row.get("channel_source", "")) in {"live", "discovered"}
        )
        info["stale_channel_count"] = sum(
            1 for row in payload["channels"] if str(row.get("channel_source", "")) == "stale"
        )
        return payload

    def _discover_channels_from_source_home(
        self,
        source_home: Path,
        requested_provider: str | None,
    ) -> list[dict[str, str]]:
        providers: list[str] = []
        if requested_provider:
            providers.append(str(requested_provider).strip().lower())
        config = self.store.read_config()
        providers.append(str(config.get("provider", "openclaw")).strip().lower())

        channels: list[dict[str, str]] = []
        for provider in providers:
            try:
                state_dir = get_provider(provider).state_dir
            except ValueError:
                continue
            adapter = get_channel_adapter(provider)
            channels.extend(adapter.discover_channels(source_home / state_dir))
        return dedupe_channels(channels)
