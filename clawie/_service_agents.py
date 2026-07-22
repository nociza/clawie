"""Agent lifecycle and provider switching (ClawieService mixin)."""
from __future__ import annotations

import copy
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from clawie.addon_integration import inject_addon_tools_snippet, remove_addon_tools_snippet
from clawie.provider_channels import is_openclaw_channel_placeholder
from clawie.providers import (
    get_provider,
    provider_names,
)
from clawie.service_common import (
    SetupError,
    AgentExistsError,
    AgentNotFoundError,
    now_iso,
    _default_core_prompt_content,
)
from clawie.safe_fs import read_text_under, remove_under

# Agent IDs become file names, backup paths, channel prefixes, and
# default Linux usernames; keep them path- and shell-safe.
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MANAGED_USER_MARKER = ".clawie-managed-user.json"


class AgentOpsMixin:
    _CONTROL_AGENTS_MARKER = "<!-- clawie-control-boot-begin -->"
    _CONTROL_AGENTS_MARKER_END = "<!-- clawie-control-boot-end -->"
    _CONTROL_AGENTS_SNIPPET = (
        "<!-- clawie-control-boot-begin -->\n"
        "6. If your manifest role is `control`, read the Clawie control RPC section in "
        "`TOOLS.md` before taking fleet actions. Use daemon-backed control commands; "
        "do not edit state files directly or approve your own pending nonce.\n"
        "<!-- clawie-control-boot-end -->"
    )
    _CONTROL_TOOLS_SNIPPET = (
        "## Clawie Control RPC\n\n"
        "This workspace is the fleet control agent. Its runtime receives a request-only "
        "Unix socket through `CLAWIE_CONTROL_SOCKET`; it cannot confirm pending actions "
        "or call generic daemon service methods.\n\n"
        "- Autonomous read/safe-heal actions go through `clawie control request <verb> "
        "--args-json '<json-object>' --json`.\n"
        "- Destructive and outward actions return `pending_confirmation` with a nonce. "
        "Show the full JSON to an allowlisted local OS operator and wait for them to run "
        "`clawie control confirm <verb> --nonce <nonce> "
        "--args-json '<same-json-object>' --json`.\n"
        "- Never approve your own nonce, never bypass `clawied`, and never edit "
        "`state.json` or SQLite files directly.\n"
        "- For `open_pr`, request a pull request from an existing pushed branch only; "
        "do not push branches or merge changes from the control workspace.\n"
    )

    @staticmethod
    def _validate_agent_id(agent_id: str) -> str:
        token = str(agent_id).strip()
        if not token:
            raise ValueError("agent_id is required")
        if token.startswith("@local:"):
            raise ValueError("agent_id must not use the reserved '@local:' prefix")
        if ".." in token or not _AGENT_ID_PATTERN.fullmatch(token):
            raise ValueError(
                "agent_id must start with a letter or digit and contain only "
                "letters, digits, '.', '_' or '-' (max 64 chars)"
            )
        return token

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
        core_prompts: dict[str, str] | None = None,
        plugin_overrides: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        self._require_setup()

        agent_id = self._validate_agent_id(agent_id)

        if channel_strategy not in {"new", "migrate"}:
            raise ValueError("channel_strategy must be one of: new, migrate")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        if agent_id in agents:
            raise AgentExistsError(f"agent already exists: {agent_id}")

        base_channels: list[dict[str, str]] = []
        source_template = template
        source_agent_defaults: dict[str, Any] = {}
        source_addons: dict[str, Any] = {}

        if clone_from:
            source = agents.get(clone_from)
            if not source:
                raise AgentNotFoundError(f"clone source agent not found: {clone_from}")
            base_channels = copy.deepcopy(source.get("channels", []))
            source_template = source.get("source_template") or template
            source_agent_defaults = copy.deepcopy(source.get("agent", {}))
            source_addons = copy.deepcopy(source.get("addons", {}))
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
            if not clone_from and not channels:
                raise ValueError(
                    "channel strategy 'migrate' requires --clone-from or explicit channels"
                )
            final_channels = copy.deepcopy(base_channels)
            for channel in final_channels:
                channel["migrated_from"] = clone_from or "local-source"
        for channel in final_channels:
            channel["enabled"] = bool(channel.get("enabled", True))
        transfer_from_clone = bool(clone_from and channel_strategy == "migrate")
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=agent_id,
            channels=final_channels,
            allow_owners={str(clone_from)} if transfer_from_clone else set(),
        )

        config = self.store.read_config()
        default_provider = str(config.get("provider", "openclaw")).strip().lower() or "openclaw"
        if provider:
            provider_spec = get_provider(provider)
        elif clone_from:
            source = agents.get(clone_from, {})
            source_provider = str(source.get("agent", {}).get("provider", "")).strip().lower()
            provider_spec = get_provider(source_provider or default_provider)
        else:
            provider_spec = get_provider(default_provider)

        provider_auth = self._preferred_agent_provider_auth(
            provider_spec.name,
            agent=None,
            current_auth_mode="",
            allow_defaults=True,
        )

        raw_plugins = source_agent_defaults.get("plugins", self._default_plugins_for_provider(provider_spec.name))
        if not isinstance(raw_plugins, dict):
            raw_plugins = self._default_plugins_for_provider(provider_spec.name)
        plugins = self._normalize_plugins(raw_plugins)
        if plugin_overrides:
            for key, value in plugin_overrides.items():
                plugins[str(key).strip().lower()] = bool(value)
        runtime = provider_spec.runtime
        if clone_from:
            runtime = str(source_agent_defaults.get("runtime", provider_spec.runtime)).strip() or provider_spec.runtime

        display = display_name.strip() if display_name else agent_id
        agent = {
            # This record is a desired control-plane definition.  Runtime
            # readiness is established separately by ``runtime create`` and
            # live service probes, so do not claim it is ready here.
            "status": "configured",
            "version": agent_version,
            "last_sync": now_iso(),
            "runtime": runtime,
            "provider": provider_spec.name,
            "auth_mode": provider_auth.get("auth_mode", provider_spec.default_auth_mode),
            "autostart": bool(source_agent_defaults.get("autostart", True)),
            "heartbeat_seconds": int(source_agent_defaults.get("heartbeat_seconds", 30)),
            "pid": int(source_agent_defaults.get("pid", 0)),
            "plugins": plugins,
            "role": "worker",
            "model_tier": "balanced",
        }
        if clone_from and not core_prompts:
            core_prompts = copy.deepcopy(agents.get(clone_from, {}).get("core_prompts", {}))
        normalized_prompts = self._normalize_core_prompts(provider_spec.name, core_prompts or {})
        self._seed_core_prompt_defaults(
            provider_spec.name,
            normalized_prompts,
            agent_id=agent_id,
            display_name=display,
        )
        self._seed_delegation_skill(normalized_prompts, plugins)

        agent_state = {
            "agent_id": agent_id,
            "display_name": display,
            "created_at": now_iso(),
            "source_template": source_template,
            "clone_from": clone_from,
            "channel_strategy": channel_strategy,
            "channels": final_channels,
            "core_prompts": normalized_prompts,
            "credential_sync": self._normalize_credential_sync_state({}, default_when_missing=True),
            "addons": self._normalize_agent_addons(source_addons),
            "agent": agent,
        }
        self._sync_control_role_workspace(agent_state)
        agents[agent_id] = agent_state
        moved_from_clone = 0
        if transfer_from_clone and clone_from:
            source = agents.get(clone_from)
            if source:
                moved_from_clone = self._remove_channel_keys_from_agent(
                    source=source,
                    keys=self._channel_keys(final_channels),
                )
                if moved_from_clone:
                    source.setdefault("agent", {})["last_sync"] = now_iso()

        self._event(
            state,
            "agents.created",
            f"Created agent definition {agent_id}",
            {
                "agent_id": agent_id,
                "channel_strategy": channel_strategy,
                "channel_count": len(final_channels),
                "clone_from": clone_from or "",
                "provider": provider_spec.name,
                "moved_from_clone": moved_from_clone,
            },
        )
        self.store.write_state(state)
        return agent_state

    def list_agents(self) -> list[dict[str, Any]]:
        self._refresh_managed_agent_provider_alignments()
        state = self.store.read_state()
        agents = list(state.setdefault("agents", {}).values())
        for agent in agents:
            self._hydrate_agent_controls(agent)
        return sorted(
            agents,
            key=lambda row: (row.get("created_at", ""), row.get("agent_id", row.get("user_id", ""))),
        )

    def configured_provider_names(self) -> list[str]:
        config = self.store.read_config()
        ordered: list[str] = []
        seen: set[str] = set()
        for item in [config.get("provider", "")] + list(self._normalized_provider_credentials(config).keys()):
            token = str(item or "").strip().lower()
            if not token or token in seen:
                continue
            try:
                get_provider(token)
            except ValueError:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        return agent

    def rename_agent(self, old_agent_id: str, new_agent_id: str) -> dict[str, Any]:
        """Rename a logical agent identity while preserving its Linux sandbox."""
        self._require_setup()
        old_id = self._validate_agent_id(old_agent_id)
        new_id = self._validate_agent_id(new_agent_id)
        if old_id == new_id:
            raise ValueError("old and new agent IDs must differ")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        source = agents.get(old_id)
        if not isinstance(source, dict):
            raise AgentNotFoundError(f"agent not found: {old_id}")
        if new_id in agents:
            raise AgentExistsError(f"agent already exists: {new_id}")

        renamed = copy.deepcopy(source)
        old_display = str(renamed.get("display_name", "")).strip()
        new_display = new_id if not old_display or old_display == old_id else old_display
        renamed["agent_id"] = new_id
        renamed["display_name"] = new_display
        renamed["renamed_at"] = now_iso()
        history = [
            str(item).strip()
            for item in renamed.get("identity_history", [])
            if str(item).strip()
        ]
        if old_id not in history:
            history.append(old_id)
        renamed["identity_history"] = list(dict.fromkeys(history))

        info = renamed.setdefault("agent", {})
        previous_ids = [
            str(item).strip()
            for item in info.get("previous_agent_ids", [])
            if str(item).strip()
        ]
        previous_ids.extend(renamed["identity_history"])
        info["previous_agent_ids"] = list(dict.fromkeys(previous_ids))
        info["last_sync"] = now_iso()

        channels = renamed.get("channels", [])
        if isinstance(channels, list):
            for row in channels:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name", ""))
                if name.startswith(f"{old_id}-"):
                    row["name"] = f"{new_id}-{name[len(old_id) + 1:]}"
                external_id = str(row.get("external_id", ""))
                if external_id.startswith(f"{old_id}:"):
                    row["external_id"] = f"{new_id}:{external_id[len(old_id) + 1:]}"

        prompts = renamed.get("core_prompts", {})
        if isinstance(prompts, dict):
            for prompt_name, content in list(prompts.items()):
                expected = _default_core_prompt_content(prompt_name, old_id, old_display or old_id)
                if str(content) == expected:
                    prompts[prompt_name] = _default_core_prompt_content(
                        prompt_name, new_id, new_display
                    )

        published = self._published_workspace()
        alias_added = False
        if (published.root / "WORKSPACE.json").is_file() and published.catalog_path.is_file():
            published.add_agent_alias(old_id, new_id)
            alias_added = True
        try:
            self.store.rename_agent_record(
                old_id,
                new_id,
                renamed,
                timestamp=str(renamed["renamed_at"]),
            )
        except Exception:
            if alias_added:
                try:
                    published.remove_agent_alias(old_id, new_id)
                except Exception:
                    pass
            raise

        warnings: list[str] = []
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(renamed)
        provider = str(info.get("provider", "")).strip().lower()
        if home is not None and linux_user:
            try:
                self._require_linux_user_access(linux_user, "agent rename prompt sync")
                self._write_prompt_files_for_home(
                    provider,
                    home,
                    renamed.get("core_prompts", {}),
                    linux_user,
                )
            except Exception as exc:  # Identity is already durable; report safe remediation.
                warnings.append(f"prompt sync failed: {exc}")
        if alias_added and home is not None:
            try:
                self.workspace_mount(agent_id=new_id)
            except Exception as exc:
                warnings.append(f"published-workspace remount failed: {exc}")

        safe_agent = copy.deepcopy(self.get_agent(new_id))
        safe_agent.setdefault("agent", {}).pop("gateway_token", None)
        return {
            "old_agent_id": old_id,
            "new_agent_id": new_id,
            "linux_user": linux_user,
            "linux_user_renamed": False,
            "published_workspace_alias": alias_added,
            "warnings": warnings,
            "agent": safe_agent,
        }

    def rotate_agent_gateway_token(self, agent_id: str) -> dict[str, Any]:
        """Rotate one OpenClaw gateway token with config/service rollback."""
        self._require_setup()
        target = self._validate_agent_id(agent_id)
        state_before = self.store.read_state()
        source = state_before.setdefault("agents", {}).get(target)
        if not isinstance(source, dict):
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(source)
        info = source.setdefault("agent", {})
        if str(info.get("provider", "")).strip().lower() != "openclaw":
            raise SetupError("gateway token rotation is only supported for OpenClaw agents")
        linux_user = str(info.get("linux_user", "")).strip()
        self._require_linux_user_access(linux_user, "gateway token rotation")
        home = self._agent_linux_home(source)
        if home is None:
            raise SetupError(f"could not resolve the managed home for agent '{target}'")
        gateway_port = int(info.get("gateway_port", 0) or 0)
        old_token = str(info.get("gateway_token", "")).strip()
        if gateway_port <= 0 or not old_token:
            raise SetupError(f"agent '{target}' has no configured gateway token to rotate")

        try:
            config_before = self._read_agent_text_file(
                home,
                ".openclaw/openclaw.json",
                max_bytes=16 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            raise SetupError(f"agent '{target}' has no OpenClaw config to rotate") from exc
        from clawie.adapters import OpenclawAdapter, deep_merge

        new_token = OpenclawAdapter.new_gateway_token()
        detached = copy.deepcopy(source)
        detached_info = detached.setdefault("agent", {})
        detached_info["gateway_token"] = new_token
        was_running = self._provider_process_live("openclaw", linux_user)
        service_touched = False
        failure_stage = "validating the existing OpenClaw config"
        try:
            config = json.loads(config_before)
            if not isinstance(config, dict):
                raise SetupError("OpenClaw config root must be a JSON object")
            config = deep_merge(
                config,
                OpenclawAdapter().gateway_config_patch(port=gateway_port, token=new_token),
            )
            failure_stage = "writing the candidate OpenClaw gateway config"
            self._write_agent_json_file(
                home,
                ".openclaw/openclaw.json",
                config,
                linux_user,
            )
            service_touched = True
            failure_stage = "restarting the OpenClaw service"
            service_result = self._run_managed_provider_service_action(
                provider="openclaw",
                action="restart" if was_running else "start",
                linux_user=linux_user,
                agent_info=detached_info,
            )
            if str(service_result.get("service_status", "")).strip().lower() != "running":
                raise SetupError("OpenClaw did not report a running service after token rotation")
            failure_stage = "waiting for OpenClaw postflight readiness"
            self._assert_provider_postflight_ready(
                provider="openclaw",
                linux_user=linux_user,
                home=home,
                auth_mode=str(detached_info.get("auth_mode", "")),
            )

            failure_stage = "committing the rotated credential to Clawie state"
            state = self.store.read_state()
            persisted = state.setdefault("agents", {}).get(target)
            if not isinstance(persisted, dict):
                raise AgentNotFoundError(f"agent not found: {target}")
            persisted_info = persisted.setdefault("agent", {})
            if str(persisted_info.get("gateway_token", "")).strip() != old_token:
                raise SetupError("gateway token changed concurrently; the rotation was not committed")
            persisted_info["gateway_token"] = new_token
            persisted_info["service_status"] = "running"
            persisted_info["service_mode"] = str(service_result.get("service_mode", "unknown"))
            persisted_info["last_sync"] = now_iso()
            self._event(
                state,
                "agents.gateway_token_rotated",
                f"Rotated private gateway token for {target}",
                {"agent_id": target, "linux_user": linux_user},
            )
            self.store.write_state(state)
        except BaseException as exc:
            rollback_errors: list[str] = []
            try:
                self._write_agent_text_file(
                    home,
                    ".openclaw/openclaw.json",
                    config_before,
                    linux_user,
                    mode=0o600,
                )
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(f"config restore failed: {rollback_exc}")
            if service_touched:
                try:
                    self._run_managed_provider_service_action(
                        provider="openclaw",
                        action="restart" if was_running else "stop",
                        linux_user=linux_user,
                        agent_info=info,
                    )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(f"service restore failed: {rollback_exc}")
            if rollback_errors:
                raise SetupError(
                    "gateway token rotation failed and automatic rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from None
            raise SetupError(
                f"gateway token rotation failed while {failure_stage} ({type(exc).__name__}); "
                "prior config, service, and Clawie state were restored"
            ) from None
        finally:
            new_token = ""

        return {
            "agent_id": target,
            "linux_user": linux_user,
            "rotated": True,
            "service_status": "running",
        }

    def set_agent_provider(self, agent_id: str, provider: str) -> dict[str, Any]:
        return self.switch_agent_provider(agent_id, provider)["agent"]

    def switch_agent_provider(self, agent_id: str, provider: str) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token:
            raise ValueError("agent_id is required")
        if token.startswith("@local:"):
            raise ValueError("provider switching is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(token)

        target_provider = str(provider).strip().lower()
        if not target_provider:
            raise ValueError("provider is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)

        info = agent.setdefault("agent", {})
        current_provider = str(info.get("provider", "")).strip().lower()
        target_spec = get_provider(target_provider)
        changed = current_provider != target_spec.name
        linux_user = str(info.get("linux_user", "")).strip()
        stop_result: dict[str, Any] = {}
        stopped_results: list[dict[str, Any]] = []
        start_result: dict[str, Any] = {}
        reconnected_channels: list[dict[str, str]] = []
        old_running = False
        target_running_before = False
        old_service_state = {
            "service_status": str(info.get("service_status", "unknown")),
            "service_mode": str(info.get("service_mode", "unknown")),
            "fallback_pid": int(info.get("fallback_pid", 0) or 0),
        }
        new_service_state = {"service_status": "unknown", "service_mode": "unknown", "fallback_pid": 0}

        try:
            if linux_user:
                # Permission is the most fundamental precondition: check it before
                # auth/runtime preparation so callers get an actionable error
                # instead of a misleading auth or install failure.
                self._require_linux_user_access(linux_user, "provider switching")
            auth_prepare = self._prepare_linked_auth_for_provider_switch(provider=target_spec.name, agent=agent)
            provider_auth = self._preferred_agent_provider_auth(
                target_spec.name,
                agent=agent,
                # The stored auth mode belongs to the current provider; it only
                # carries over when reconciling the same provider. A switch to a
                # different provider must derive its own auth mode.
                current_auth_mode=str(info.get("auth_mode", "")) if not changed else "",
                allow_defaults=True,
            )
            home = self._agent_linux_home(agent)
            prompts = self._normalize_core_prompts(target_spec.name, agent.get("core_prompts", {}))
            effective_channels = self._effective_agent_channels(agent) if linux_user else []
            live_channel_payloads = self._discover_live_channel_payloads(agent) if linux_user else {}

            if not changed and not linux_user:
                auth = self.agent_auth_status(token)
                return {
                    "agent": agent,
                    "changed": False,
                    "from_provider": current_provider,
                    "to_provider": target_spec.name,
                    "service": {},
                    "stopped_service": {},
                    "stopped_services": [],
                    "reconnected_channels": [],
                    "auth": auth,
                    "auth_prepare": auth_prepare,
                }

            if linux_user:
                self.ensure_provider_runtime(target_spec.name)
                target_running_before = self._managed_provider_is_running(
                    provider=target_spec.name,
                    linux_user=linux_user,
                    agent_info=new_service_state,
                )
                if changed and current_provider:
                    self._resolve_provider_executable(current_provider)
                if home:
                    self._write_prompt_files_for_home(target_spec.name, home, prompts, linux_user)
            if linux_user and changed and current_provider:
                old_running = self._managed_provider_is_running(
                    provider=current_provider,
                    linux_user=linux_user,
                    agent_info=old_service_state,
                )
                fallback_pid = int(str(old_service_state.get("fallback_pid", 0) or 0))
                if old_running or fallback_pid > 0:
                    stop_result = self._run_managed_provider_service_action(
                        provider=current_provider,
                        action="stop",
                        linux_user=linux_user,
                        agent_info=old_service_state,
                    )

            if linux_user:
                self._prepare_agent_provider_home(
                    provider=target_spec.name,
                    agent=agent,
                    linux_user=linux_user,
                    home=home,
                    channels=effective_channels,
                    live_payloads=live_channel_payloads,
                )
                start_result = self._run_managed_provider_service_action(
                    provider=target_spec.name,
                    action="restart" if target_running_before else "start",
                    linux_user=linux_user,
                    agent_info=new_service_state,
                )
                reconnected_channels = self._reconnect_agent_channels(
                    provider=target_spec.name,
                    linux_user=linux_user,
                    channels=effective_channels,
                )
                for other_provider in provider_names():
                    if other_provider == target_spec.name:
                        continue
                    try:
                        self._resolve_provider_executable(other_provider)
                    except SetupError:
                        continue
                    other_state = {"service_status": "unknown", "service_mode": "unknown", "fallback_pid": 0}
                    if self._managed_provider_is_running(
                        provider=other_provider,
                        linux_user=linux_user,
                        agent_info=other_state,
                    ):
                        stopped = self._run_managed_provider_service_action(
                            provider=other_provider,
                            action="stop",
                            linux_user=linux_user,
                            agent_info=other_state,
                        )
                        stopped_results.append(stopped)
                live_after_switch = self._live_provider_names_for_user(linux_user)
                if target_spec.name not in live_after_switch:
                    raise SetupError(
                        f"provider switch to {target_spec.name} did not produce a live {target_spec.name} runtime"
                    )
                other_live = [item for item in live_after_switch if item != target_spec.name]
                if other_live:
                    raise SetupError(
                        f"provider switch to {target_spec.name} left other runtimes active: {', '.join(other_live)}"
                    )
                self._assert_provider_postflight_ready(
                    provider=target_spec.name,
                    linux_user=linux_user,
                    home=home,
                    auth_mode=str(provider_auth.get("auth_mode", target_spec.default_auth_mode)),
                )
        except Exception as exc:
            self._set_agent_provider_issue(
                agent,
                status="error",
                kind="switch_failed",
                issue=f"provider switch to {target_spec.name} failed: {exc}",
                remediation=self._provider_switch_remediation(
                    agent_id=token,
                    target_provider=target_spec.name,
                    linux_user=linux_user,
                    error=str(exc),
                ),
                requested_provider=target_spec.name,
            )
            self._event(
                state,
                "agents.provider_switch_failed",
                f"Provider switch failed for {token}",
                {
                    "agent_id": token,
                    "from_provider": current_provider,
                    "to_provider": target_spec.name,
                    "linux_user": linux_user,
                    "error": str(exc),
                },
            )
            self.store.write_state(state)
            if changed and linux_user and str(start_result.get("service_status", "")) == "running":
                try:
                    self._run_managed_provider_service_action(
                        provider=target_spec.name,
                        action="stop",
                        linux_user=linux_user,
                        agent_info=new_service_state,
                    )
                except Exception:
                    pass
            if changed and linux_user and current_provider and old_running:
                try:
                    self._run_managed_provider_service_action(
                        provider=current_provider,
                        action="start",
                        linux_user=linux_user,
                        agent_info=old_service_state,
                    )
                except Exception:
                    pass
            raise

        info["provider"] = target_spec.name
        info["runtime"] = target_spec.runtime
        info["auth_mode"] = str(provider_auth.get("auth_mode", target_spec.default_auth_mode))
        info["service_status"] = str(start_result.get("service_status", "unknown")) if linux_user else "unknown"
        info["service_mode"] = str(start_result.get("service_mode", "unknown")) if linux_user else "unknown"
        info["pid"] = 0
        if "fallback_pid" in info or int(start_result.get("fallback_pid", 0) or 0) > 0:
            info["fallback_pid"] = int(start_result.get("fallback_pid", 0) or 0)
        info["last_sync"] = now_iso()
        if effective_channels:
            self._persist_effective_agent_channels(agent, effective_channels)
        agent["core_prompts"] = prompts
        self._clear_agent_provider_issue(agent)
        if not stop_result and stopped_results:
            stop_result = stopped_results[-1]
        self._event(
            state,
            "agents.provider_changed" if changed else "agents.provider_reconciled",
            f"Changed provider for {token}" if changed else f"Reconciled provider runtime for {token}",
            {
                "agent_id": token,
                "from_provider": current_provider,
                "to_provider": target_spec.name,
                "linux_user": linux_user,
                "service_status": str(start_result.get("service_status", "unknown")),
                "service_mode": str(start_result.get("service_mode", "unknown")),
                "reconnected_channels": len(reconnected_channels),
                "stopped_provider_count": len(stopped_results or ([stop_result] if stop_result else [])),
            },
        )
        self.store.write_state(state)
        auth = self.agent_auth_status(token)
        return {
            "agent": agent,
            "changed": changed,
            "from_provider": current_provider,
            "to_provider": target_spec.name,
            "service": start_result,
            "stopped_service": stop_result,
            "stopped_services": stopped_results,
            "reconnected_channels": reconnected_channels,
            "auth": auth,
            "auth_prepare": auth_prepare,
        }

    def toggle_agent_plugin(self, agent_id: str, plugin: str) -> dict[str, Any]:
        self._require_setup()
        plugin_name = str(plugin).strip().lower()
        if not plugin_name:
            raise ValueError("plugin is required")
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
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
        agents = state.setdefault("agents", {})
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

    def _allocate_gateway_port(self, base: int = 18789) -> int:
        """Pick the next free per-agent gateway port, starting at *base* (18789).

        One openclaw gateway runs per agent Linux user, so each managed agent
        needs a distinct loopback port (mirrors display VNC port allocation).
        State roots are intentionally independent, so the persisted assignments
        in this store are not enough: also consult the host's live listeners to
        avoid colliding with an agent managed by another Clawie state root.
        """
        port = int(base)
        if not 1024 <= port <= 65535:
            raise ValueError("gateway port base must be between 1024 and 65535")
        state = self.store.read_state()
        agents = state.get("agents", {})
        used: set[int] = set()
        for row in agents.values():
            if not isinstance(row, dict):
                continue
            port = int(row.get("agent", {}).get("gateway_port", 0) or 0)
            if port:
                used.add(port)
        port = int(base)
        while port <= 65535 and (port in used or not self._gateway_port_available(port)):
            port += 1
        if port > 65535:
            raise SetupError("no free loopback gateway port is available")
        return port

    @staticmethod
    def _gateway_port_available(port: int) -> bool:
        """Return whether both loopback families can accept a gateway bind."""
        probes: list[tuple[int, tuple[Any, ...]]] = [
            (socket.AF_INET, ("127.0.0.1", int(port))),
        ]
        if socket.has_ipv6:
            probes.append((socket.AF_INET6, ("::1", int(port), 0, 0)))
        for family, address in probes:
            try:
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    probe.bind(address)
            except OSError:
                return False
        return True

    def _prepare_agent_provider_home(
        self,
        *,
        provider: str,
        agent: dict[str, Any],
        linux_user: str,
        home: Path | None,
        channels: list[dict[str, Any]],
        live_payloads: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        if not home:
            return
        name = str(provider).strip().lower()
        if name not in {"picoclaw", "openclaw"}:
            return
        spec = get_provider(name)
        if spec.verified_delivery:
            # This method is the shared config-write choke point used by
            # spawning, provider switching, channel changes, and manifest
            # reconciliation.  Probe immediately before touching a runtime's
            # schema so an untested upgrade degrades to read-only.
            executable = self._resolve_provider_executable(name)
            self._verify_installed_runtime_version(name, executable)

        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        use_shared_auth = "provider-auth" in set(sync.get("bundles", []))
        if name == "picoclaw":
            self._ensure_picoclaw_native_auth(home=home, linux_user=linux_user, use_shared_auth=use_shared_auth)
        if use_shared_auth:
            self._ensure_shared_provider_auth_links(target_home=home, username=linux_user)
        if name == "openclaw":
            self._ensure_openclaw_agent_auth_link(home=home, linux_user=linux_user)

        auth = self._preferred_agent_provider_auth(
            name,
            agent=agent,
            current_auth_mode=str(agent.get("agent", {}).get("auth_mode", "")),
            allow_defaults=True,
        )
        auth_mode = str(auth.get("auth_mode", spec.default_auth_mode)).strip().lower()
        api_key = str(auth.get("api_key", "")).strip()
        if name == "picoclaw":
            self._ensure_picoclaw_home_prepared(
                home=home,
                linux_user=linux_user,
                channels=channels,
                live_payloads=live_payloads,
                auth_mode=auth_mode,
                api_key=api_key,
            )
            return
        gw = agent.setdefault("agent", {})
        from clawie.adapters import OpenclawAdapter

        gateway_port = int(gw.get("gateway_port") or 0) or self._allocate_gateway_port()
        gateway_token = str(gw.get("gateway_token") or "") or OpenclawAdapter.new_gateway_token()
        gw["gateway_port"] = gateway_port
        gw["gateway_token"] = gateway_token
        self._ensure_openclaw_home_prepared(
            home=home,
            linux_user=linux_user,
            agent_id=str(agent.get("agent_id", "")),
            channels=channels,
            live_payloads=live_payloads,
            auth_mode=auth_mode,
            api_key=api_key,
            gateway_port=gateway_port,
            gateway_token=gateway_token,
        )

    def _ensure_picoclaw_home_prepared(
        self,
        *,
        home: Path,
        linux_user: str,
        channels: list[dict[str, Any]],
        live_payloads: dict[tuple[str, str], dict[str, Any]],
        auth_mode: str,
        api_key: str,
    ) -> None:
        self._ensure_agent_directory(home, ".picoclaw", linux_user)
        workspace = self._ensure_agent_directory(home, ".picoclaw/workspace", linux_user)
        config = self._read_agent_json_file(home, ".picoclaw/config.json")

        agents_cfg = config.get("agents", {})
        if not isinstance(agents_cfg, dict):
            agents_cfg = {}
        defaults = agents_cfg.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
        defaults["workspace"] = str(workspace)
        defaults["restrict_to_workspace"] = bool(defaults.get("restrict_to_workspace", True))
        defaults["provider"] = "openai"
        defaults["model_name"] = "gpt-5.2"
        defaults["model"] = "gpt-5.2"
        agents_cfg["defaults"] = defaults
        config["agents"] = agents_cfg

        providers_cfg = config.get("providers", {})
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}
        openai_cfg = providers_cfg.get("openai", {})
        if not isinstance(openai_cfg, dict):
            openai_cfg = {}
        openai_cfg.setdefault("api_base", "https://api.openai.com/v1")
        if auth_mode == "linked":
            openai_cfg["auth_method"] = "oauth"
            openai_cfg.pop("api_key", None)
        elif auth_mode == "api_key":
            if not api_key:
                raise SetupError("picoclaw API-key mode requires an API key before the runtime can start")
            openai_cfg["api_key"] = api_key
            openai_cfg.pop("auth_method", None)
        providers_cfg["openai"] = openai_cfg
        config["providers"] = providers_cfg

        model_list = config.get("model_list", [])
        if not isinstance(model_list, list):
            model_list = []
        model_entry: dict[str, Any] | None = None
        for item in model_list:
            if not isinstance(item, dict):
                continue
            model_name = str(item.get("model_name", "")).strip()
            model_ref = str(item.get("model", "")).strip()
            if model_name == "gpt-5.2" or model_ref == "openai/gpt-5.2":
                model_entry = item
                break
        if model_entry is None:
            for item in model_list:
                if isinstance(item, dict) and str(item.get("model", "")).startswith("openai/"):
                    model_entry = item
                    break
        if model_entry is None:
            model_entry = {}
            model_list.append(model_entry)
        model_entry["model_name"] = "gpt-5.2"
        model_entry["model"] = "openai/gpt-5.2"
        model_entry.setdefault("api_base", "https://api.openai.com/v1")
        if auth_mode == "linked":
            model_entry["auth_method"] = "oauth"
            model_entry.pop("api_key", None)
        elif auth_mode == "api_key":
            model_entry["api_key"] = api_key
            model_entry.pop("auth_method", None)
        config["model_list"] = model_list

        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            channels_cfg = {}
        login_env = self._login_shell_env(linux_user)
        payload_by_kind: dict[str, dict[str, Any]] = {}
        for payload in live_payloads.values():
            kind = str(payload.get("kind", "")).strip().lower()
            if kind and kind not in payload_by_kind:
                payload_by_kind[kind] = payload

        for channel in channels:
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or kind == "cli":
                continue
            payload = live_payloads.get((kind, name)) or payload_by_kind.get(kind, {})
            settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
            if not isinstance(settings, dict):
                settings = {}
            settings = self._resolve_shell_placeholders(settings, login_env)
            if kind != "telegram":
                continue
            telegram_cfg = channels_cfg.get("telegram", {})
            if not isinstance(telegram_cfg, dict):
                telegram_cfg = {}
            token = (
                str(settings.get("token", "")).strip()
                or str(settings.get("bot_token", "")).strip()
                or str(telegram_cfg.get("token", "")).strip()
            )
            if not token:
                raise SetupError(
                    "picoclaw telegram bootstrap could not find a bot token; sync live channels or re-link Telegram first"
                )
            if self._looks_like_unresolved_secret(token):
                raise SetupError(
                    "picoclaw telegram bootstrap found an unresolved token placeholder; "
                    "export the bot token in the target user's login shell or re-link Telegram first"
                )
            if not self._looks_like_telegram_bot_token(token):
                raise SetupError(
                    "picoclaw telegram bootstrap found an invalid Telegram bot token in live channel settings; "
                    "re-link Telegram or update the target user's Telegram token"
                )
            telegram_cfg["enabled"] = True
            telegram_cfg["token"] = token
            if name:
                telegram_cfg["name"] = name
            base_url = str(settings.get("base_url", telegram_cfg.get("base_url", ""))).strip()
            if base_url:
                telegram_cfg["base_url"] = base_url
            proxy = str(settings.get("proxy", telegram_cfg.get("proxy", ""))).strip()
            if proxy:
                telegram_cfg["proxy"] = proxy
            allow_from = self._coerce_string_list(settings.get("allow_from", telegram_cfg.get("allow_from", [])))
            telegram_cfg["allow_from"] = allow_from
            group_trigger = settings.get("group_trigger", telegram_cfg.get("group_trigger", {}))
            if isinstance(group_trigger, dict) and group_trigger:
                normalized_trigger: dict[str, Any] = {}
                if "mention_only" in group_trigger:
                    normalized_trigger["mention_only"] = bool(group_trigger.get("mention_only"))
                prefixes = self._coerce_string_list(group_trigger.get("prefixes", []))
                if prefixes:
                    normalized_trigger["prefixes"] = prefixes
                if normalized_trigger:
                    telegram_cfg["group_trigger"] = normalized_trigger
            channels_cfg["telegram"] = telegram_cfg

        config["channels"] = channels_cfg
        has_enabled_channel = any(
            isinstance(value, dict) and bool(value.get("enabled", False))
            for value in channels_cfg.values()
        )
        if not has_enabled_channel:
            raise SetupError("picoclaw requires at least one enabled provider channel before the gateway can run")

        self._write_agent_json_file(home, ".picoclaw/config.json", config, linux_user)

    def _ensure_openclaw_home_prepared(
        self,
        *,
        home: Path,
        linux_user: str,
        channels: list[dict[str, Any]],
        live_payloads: dict[tuple[str, str], dict[str, Any]],
        auth_mode: str,
        api_key: str,
        gateway_port: int | None = None,
        gateway_token: str = "",
        agent_id: str = "",
    ) -> None:
        self._ensure_agent_directory(home, ".openclaw", linux_user)
        workspace = self._ensure_agent_directory(home, ".openclaw/workspace", linux_user)
        config = self._read_agent_json_file(home, ".openclaw/openclaw.json")

        gateway_cfg = config.get("gateway", {})
        if not isinstance(gateway_cfg, dict):
            gateway_cfg = {}
        gateway_cfg["mode"] = "local"
        config["gateway"] = gateway_cfg
        if gateway_port and gateway_token:
            # Make this agent's gateway addressable for delegation: loopback
            # bind, a deterministic per-agent port, and token auth.
            from clawie.adapters import OpenclawAdapter, deep_merge

            config = deep_merge(
                config,
                OpenclawAdapter().gateway_config_patch(port=int(gateway_port), token=gateway_token),
            )

        agents_cfg = config.get("agents", {})
        if not isinstance(agents_cfg, dict):
            agents_cfg = {}
        defaults = agents_cfg.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
        defaults["workspace"] = str(workspace)
        heartbeat = defaults.get("heartbeat", {})
        if not isinstance(heartbeat, dict):
            heartbeat = {}
        heartbeat.setdefault("every", "0m")
        heartbeat.setdefault("directPolicy", "block")
        heartbeat.setdefault("lightContext", True)
        heartbeat.setdefault("ackMaxChars", 300)
        defaults["heartbeat"] = heartbeat

        desired_model = ""
        if auth_mode in {"linked", "api_key"}:
            # Keep home provisioning on the same source-pinned model contract
            # as delegated delivery.  `openai-codex/*` is a legacy route that
            # `openclaw doctor --fix` rewrites (see clawie.adapters).
            from clawie.adapters import OpenclawAdapter

            desired_model = OpenclawAdapter.DEFAULT_MODEL
        if auth_mode == "api_key":
            if not api_key:
                raise SetupError("openclaw API-key mode requires an API key before the runtime can start")

        current_model = defaults.get("model")
        if desired_model:
            if isinstance(current_model, dict):
                current_model["primary"] = desired_model
                defaults["model"] = current_model
            else:
                defaults["model"] = desired_model
        agents_cfg["defaults"] = defaults
        config["agents"] = agents_cfg

        if auth_mode == "linked":
            auth_cfg = config.get("auth", {})
            if isinstance(auth_cfg, dict):
                profiles = auth_cfg.get("profiles", {})
                if isinstance(profiles, dict):
                    profiles.pop("openai-codex:default", None)
                    auth_cfg["profiles"] = profiles
                order = auth_cfg.get("order", {})
                if isinstance(order, dict):
                    order.pop("openai-codex", None)
                    auth_cfg["order"] = order
                config["auth"] = auth_cfg
        elif auth_mode == "api_key":
            models_cfg = config.get("models", {})
            if not isinstance(models_cfg, dict):
                models_cfg = {}
            providers_cfg = models_cfg.get("providers", {})
            if not isinstance(providers_cfg, dict):
                providers_cfg = {}
            openai_cfg = providers_cfg.get("openai", {})
            if not isinstance(openai_cfg, dict):
                openai_cfg = {}
            openai_cfg["apiKey"] = api_key
            providers_cfg["openai"] = openai_cfg
            models_cfg["providers"] = providers_cfg
            config["models"] = models_cfg

        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            channels_cfg = {}
        channel_defaults = channels_cfg.get("defaults", {})
        if not isinstance(channel_defaults, dict):
            channel_defaults = {}
        heartbeat_visibility = channel_defaults.get("heartbeat", {})
        if not isinstance(heartbeat_visibility, dict):
            heartbeat_visibility = {}
        heartbeat_visibility.setdefault("showOk", False)
        heartbeat_visibility.setdefault("showAlerts", False)
        heartbeat_visibility.setdefault("useIndicator", False)
        channel_defaults["heartbeat"] = heartbeat_visibility
        channels_cfg["defaults"] = channel_defaults

        if "telegram" in channels_cfg:
            existing_telegram_cfg = channels_cfg.get("telegram")
            if is_openclaw_channel_placeholder(existing_telegram_cfg):
                # Remove the inert placeholder written by older Clawie
                # releases so OpenClaw cannot rediscover it as a live channel.
                channels_cfg.pop("telegram", None)
            elif isinstance(existing_telegram_cfg, dict):
                self._set_openclaw_telegram_streaming_off(existing_telegram_cfg)
                channels_cfg["telegram"] = existing_telegram_cfg
        login_env = self._login_shell_env(linux_user)
        payload_by_kind: dict[str, dict[str, Any]] = {}
        for payload in live_payloads.values():
            kind = str(payload.get("kind", "")).strip().lower()
            if kind and kind not in payload_by_kind:
                payload_by_kind[kind] = payload

        for channel in channels:
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or kind == "cli":
                continue
            payload = live_payloads.get((kind, name)) or payload_by_kind.get(kind, {})
            settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
            if not isinstance(settings, dict):
                settings = {}
            settings = self._resolve_shell_placeholders(settings, login_env)
            if kind != "telegram":
                continue

            telegram_cfg = channels_cfg.get("telegram", {})
            if not isinstance(telegram_cfg, dict):
                telegram_cfg = {}
            self._set_openclaw_telegram_streaming_off(telegram_cfg)
            token_file = (
                str(settings.get("tokenFile", "")).strip()
                or str(settings.get("token_file", "")).strip()
                or str(telegram_cfg.get("tokenFile", "")).strip()
                or str(telegram_cfg.get("token_file", "")).strip()
            )
            token = (
                str(settings.get("botToken", "")).strip()
                or str(settings.get("bot_token", "")).strip()
                or str(settings.get("token", "")).strip()
                or str(telegram_cfg.get("botToken", "")).strip()
            )
            if not token_file and not token:
                raise SetupError(
                    "openclaw telegram bootstrap could not find a bot token or token file; "
                    "sync live channels or re-link Telegram first"
                )
            if token_file:
                if self._looks_like_unresolved_secret(token_file):
                    raise SetupError(
                        "openclaw telegram bootstrap found an unresolved token-file placeholder; "
                        "export the path in the target user's login shell or re-link Telegram first"
                    )
                if token_file.startswith("~/"):
                    token_path = home / token_file[2:]
                else:
                    token_path = Path(token_file)
                    if not token_path.is_absolute():
                        token_path = home / ".openclaw" / token_path
                try:
                    token_stat = token_path.lstat()
                except OSError as exc:
                    raise SetupError(
                        "openclaw telegram token file is missing or inaccessible; "
                        "re-link Telegram or repair the token-file path"
                    ) from exc
                if not stat.S_ISREG(token_stat.st_mode) or not 0 < token_stat.st_size <= 4096:
                    raise SetupError(
                        "openclaw telegram token file must be a non-empty regular file no larger than 4096 bytes"
                    )
                if stat.S_IMODE(token_stat.st_mode) & 0o077:
                    raise SetupError(
                        "openclaw telegram token file must be private (mode 0600 or stricter)"
                    )
                if os.geteuid() == 0 and linux_user:
                    try:
                        expected_uid = int(pwd.getpwnam(linux_user).pw_uid)
                    except KeyError as exc:
                        raise SetupError(
                            f"openclaw telegram token-file owner does not exist: {linux_user}"
                        ) from exc
                    if int(token_stat.st_uid) != expected_uid:
                        raise SetupError(
                            "openclaw telegram token file must be owned by the managed Linux user"
                        )
                # OpenClaw gives tokenFile precedence over botToken and rejects
                # symlinks itself. Preserve the private file reference instead
                # of copying its secret into openclaw.json.
                telegram_cfg["tokenFile"] = token_file
                telegram_cfg.pop("token_file", None)
                telegram_cfg.pop("botToken", None)
            else:
                if self._looks_like_unresolved_secret(token):
                    raise SetupError(
                        "openclaw telegram bootstrap found an unresolved token placeholder; "
                        "export the bot token in the target user's login shell or re-link Telegram first"
                    )
                if not self._looks_like_telegram_bot_token(token):
                    raise SetupError(
                        "openclaw telegram bootstrap found an invalid Telegram bot token in live channel settings; "
                        "re-link Telegram or update the target user's Telegram token"
                    )
                telegram_cfg["botToken"] = token
            telegram_cfg["enabled"] = True
            allow_from = self._coerce_string_list(
                settings.get("allowFrom", settings.get("allow_from", telegram_cfg.get("allowFrom", [])))
            )
            existing_allow_from = self._coerce_string_list(telegram_cfg.get("allowFrom", []))
            effective_allow_from = allow_from or existing_allow_from
            explicit_dm_policy = str(
                settings.get("dmPolicy", settings.get("dm_policy", telegram_cfg.get("dmPolicy", "")))
            ).strip().lower()
            if effective_allow_from:
                telegram_cfg["allowFrom"] = effective_allow_from
                telegram_cfg["dmPolicy"] = "open" if "*" in set(effective_allow_from) else "allowlist"
            elif explicit_dm_policy in {"open", "allowlist", "pairing", "disabled"}:
                telegram_cfg["dmPolicy"] = explicit_dm_policy
            else:
                # Keep the runtime's secure one-owner default. The first DM
                # receives a pairing code that the host operator approves;
                # never make a newly managed bot public merely to make it
                # appear reachable.
                telegram_cfg.pop("allowFrom", None)
                telegram_cfg["dmPolicy"] = "pairing"
            proxy = str(settings.get("proxy", telegram_cfg.get("proxy", ""))).strip()
            if proxy:
                telegram_cfg["proxy"] = proxy
            webhook_url = str(
                settings.get("webhookUrl", settings.get("webhook_url", telegram_cfg.get("webhookUrl", "")))
            ).strip()
            if webhook_url:
                telegram_cfg["webhookUrl"] = webhook_url
            webhook_secret = str(
                settings.get("webhookSecret", settings.get("webhook_secret", telegram_cfg.get("webhookSecret", "")))
            ).strip()
            if webhook_secret:
                telegram_cfg["webhookSecret"] = webhook_secret
            group_trigger = settings.get("group_trigger", settings.get("groupTrigger", {}))
            if isinstance(group_trigger, dict) and "mention_only" in group_trigger:
                groups = telegram_cfg.get("groups", {})
                if not isinstance(groups, dict):
                    groups = {}
                default_group = groups.get("*", {})
                if not isinstance(default_group, dict):
                    default_group = {}
                default_group["requireMention"] = bool(group_trigger.get("mention_only"))
                groups["*"] = default_group
                telegram_cfg["groups"] = groups
            channels_cfg["telegram"] = telegram_cfg

        config["channels"] = channels_cfg
        self._write_agent_json_file(home, ".openclaw/openclaw.json", config, linux_user)
        if auth_mode == "linked":
            self._ensure_openclaw_agent_auth_link(home=home, linux_user=linux_user)
        if agent_id:
            self._ensure_published_workspace_mount(
                agent_id=agent_id,
                workspace=workspace,
                linux_user=linux_user,
            )

    @staticmethod
    def _set_openclaw_telegram_streaming_off(telegram_cfg: dict[str, Any]) -> None:
        """Write the pinned OpenClaw canonical streaming schema.

        OpenClaw 2026.7.1 rejects the historical scalar ``streaming`` value
        and related top-level preview keys. Preserve supported nested tuning,
        force preview delivery off, and remove every legacy key called out by
        the pinned runtime's schema validator.
        """
        streaming = telegram_cfg.get("streaming", {})
        canonical = dict(streaming) if isinstance(streaming, dict) else {}
        canonical["mode"] = "off"
        telegram_cfg["streaming"] = canonical
        for key in (
            "streamMode",
            "chunkMode",
            "blockStreaming",
            "draftChunk",
            "blockStreamingCoalesce",
        ):
            telegram_cfg.pop(key, None)

    def _remove_picoclaw_channel_from_home(
        self,
        *,
        home: Path,
        linux_user: str,
        kind: str,
    ) -> None:
        config = self._read_agent_json_file(home, ".picoclaw/config.json")
        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return
        token = str(kind).strip().lower()
        if token in channels_cfg:
            channels_cfg.pop(token, None)
            config["channels"] = channels_cfg
            self._write_agent_json_file(home, ".picoclaw/config.json", config, linux_user)

    def _remove_openclaw_channel_from_home(
        self,
        *,
        home: Path,
        linux_user: str,
        kind: str,
    ) -> None:
        config = self._read_agent_json_file(home, ".openclaw/openclaw.json")
        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return
        token = str(kind).strip().lower()
        if token in channels_cfg:
            channels_cfg.pop(token, None)
            config["channels"] = channels_cfg
            self._write_agent_json_file(home, ".openclaw/openclaw.json", config, linux_user)

    def ensure_agent_permissions(self, agent_id: str, manager_user: str = "") -> dict[str, Any]:
        """Repair an agent home to the private production permission profile.

        The manager-user argument remains for API compatibility but never
        grants group access. Cross-user writes run as root or through the
        authenticated ``clawied`` service.
        """
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("ensure_agent_permissions requires root. Re-run with sudo.")
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        provider = str(agent_info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{agent_id}' has no provider configured")
        linux_user = str(agent_info.get("linux_user", "")).strip()
        if not linux_user:
            raise SetupError(f"agent '{agent_id}' has no linux_user")
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"cannot resolve home directory for {linux_user}")

        _ = manager_user
        warnings = self._ensure_workspace_accessible(provider, home, linux_user)
        if warnings:
            raise SetupError("could not repair private permissions: " + "; ".join(warnings))

        return {
            "agent_id": agent_id,
            "linux_user": linux_user,
            "manager": "root-or-clawied",
            "changes": [
                f"{home}: 0700 and owned by {linux_user}",
                "provider directories: 0700",
                "provider files: 0600",
            ],
        }

    def delete_agent(self, agent_id: str) -> None:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        info = agent.get("agent", {}) if isinstance(agent, dict) else {}
        linux_user = str(info.get("linux_user", "") or "").strip()
        if linux_user:
            raise SetupError(
                f"agent '{agent_id}' still has Linux runtime user '{linux_user}'; "
                f"use 'clawie agent purge {agent_id}' to remove it safely"
            )
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
        agents = state.setdefault("agents", {})
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        info = agent.get("agent", {}) if isinstance(agent.get("agent"), dict) else {}
        linux_user = str(info.get("linux_user", "")).strip()
        user_removed = False
        home_removed = False
        runtime_stopped = False
        ssh_cleanup_error = ""
        if linux_user:
            if os.geteuid() != 0:
                raise SetupError(
                    "purge requires root privileges for spawned Linux users. Re-run with sudo/root."
                )
            user_exists = self._linux_user_exists(linux_user)
            home_path = self._verified_managed_user_home_for_purge(
                agent_id=agent_id,
                linux_user=linux_user,
                info=info,
                require_marker=user_exists,
            )
            home_exists = home_path is not None and home_path.exists()
            if home_exists and not user_exists:
                self._verified_managed_user_home_for_purge(
                    agent_id=agent_id,
                    linux_user=linux_user,
                    info=info,
                    require_marker=True,
                )
                raise SetupError(
                    f"linux user {linux_user} is missing; refusing recursive deletion of orphan home "
                    f"{home_path}. Inspect it and remove it explicitly before purging the record."
                )
            if user_exists:
                runtime_stopped = self._stop_managed_runtime_for_purge(
                    agent_id=agent_id,
                    linux_user=linux_user,
                    info=info,
                )
                subprocess.run(["userdel", "-r", linux_user], check=True)
                user_removed = True
                if self._linux_user_exists(linux_user):
                    raise SetupError(
                        f"userdel reported success but Linux user {linux_user} still exists; "
                        "the agent record was preserved"
                    )
                home_removed = bool(home_path is not None and not home_path.exists())
                if home_path is not None and not home_removed:
                    raise SetupError(
                        f"userdel removed Linux user {linux_user} but managed home {home_path} "
                        "still exists; the agent record was preserved for manual recovery"
                    )
                try:
                    self._remove_ssh_login_denial(linux_user)
                except Exception as exc:  # noqa: BLE001 - user deletion already succeeded.
                    ssh_cleanup_error = str(exc)
            else:
                recorded_uid = int(info.get("linux_uid", 0) or 0)
                if recorded_uid <= 0:
                    raise SetupError(
                        f"linux user {linux_user} is missing and the legacy agent record has no "
                        "recorded uid; refusing to forget it because a process may still own the "
                        "deleted account uid"
                    )
                provider = str(info.get("provider", "") or "").strip().lower()
                if self._managed_provider_process_live_for_purge(
                    provider,
                    linux_user,
                    recorded_uid,
                ):
                    raise SetupError(
                        f"refusing to purge agent '{agent_id}': its {provider} runtime is still "
                        f"running under uid {recorded_uid}"
                    )
                runtime_stopped = True

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
                "runtime_stopped": runtime_stopped,
                "ssh_cleanup_error": ssh_cleanup_error,
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": agent_id,
            "linux_user": linux_user,
            "linux_user_removed": user_removed,
            "home_removed": home_removed,
            "runtime_stopped": runtime_stopped,
            "ssh_cleanup_error": ssh_cleanup_error,
        }

    def _stop_managed_runtime_for_purge(
        self,
        *,
        agent_id: str,
        linux_user: str,
        info: dict[str, Any],
    ) -> bool:
        """Stop and disable an agent runtime before deleting its OS account.

        Purge is deliberately fail-closed: losing the state record while a
        service can still restart under the managed UID would orphan a live
        runtime.  The fallback path handles a removed provider executable by
        disabling an existing user unit and terminating matching processes.
        """
        provider = str(info.get("provider", "") or "").strip().lower()
        if not provider:
            raise SetupError(
                f"agent '{agent_id}' has no provider metadata; refusing to purge its Linux runtime"
            )

        stop_error = ""
        try:
            result = self._run_managed_provider_service_action(
                provider=provider,
                action="stop",
                linux_user=linux_user,
                agent_info=info,
            )
            if str(result.get("service_status", "")).strip().lower() == "running":
                raise SetupError("provider service still reports running after stop")
        except Exception as exc:  # noqa: BLE001 - the safe fallback is intentional.
            stop_error = str(exc)
            self._force_stop_provider_processes(provider, linux_user)

        unit = f"{provider}.service"
        self._run_systemd_user_command(linux_user, ["disable", "--now", unit])
        managed_home = self._linux_home_for_user(linux_user)
        if managed_home is not None:
            try:
                remove_under(
                    managed_home,
                    Path(".config") / "systemd" / "user" / unit,
                )
            except (OSError, ValueError) as exc:
                raise SetupError(
                    f"refusing to purge agent '{agent_id}': could not remove generated service "
                    f"unit beneath {managed_home}: {exc}"
                ) from exc
        recorded_uid = int(info.get("linux_uid", 0) or 0)
        if recorded_uid <= 0:
            try:
                recorded_uid = int(pwd.getpwnam(linux_user).pw_uid)
            except KeyError as exc:
                raise SetupError(
                    f"cannot verify runtime processes for missing user {linux_user}"
                ) from exc
        if self._managed_provider_process_live_for_purge(
            provider,
            linux_user,
            recorded_uid,
        ):
            detail = f": {stop_error}" if stop_error else ""
            raise SetupError(
                f"refusing to purge agent '{agent_id}': its {provider} runtime is still running{detail}"
            )
        loginctl = shutil.which("loginctl")
        if loginctl:
            linger = subprocess.run(
                [loginctl, "disable-linger", linux_user],
                capture_output=True,
                text=True,
                check=False,
            )
            if linger.returncode != 0:
                detail = (linger.stderr or linger.stdout or "").strip()
                raise SetupError(
                    f"refusing to purge agent '{agent_id}': failed to disable systemd lingering "
                    f"for {linux_user}: {detail or f'exit {linger.returncode}'}"
                )
            terminate = subprocess.run(
                [loginctl, "terminate-user", linux_user],
                capture_output=True,
                text=True,
                check=False,
            )
            if terminate.returncode != 0 and self._linux_uid_has_processes_for_purge(recorded_uid):
                systemctl = shutil.which("systemctl")
                if systemctl:
                    subprocess.run(
                        [systemctl, "stop", f"user@{recorded_uid}.service"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
            deadline = time.monotonic() + 5.0
            while self._linux_uid_has_processes_for_purge(recorded_uid):
                if time.monotonic() >= deadline:
                    detail = (terminate.stderr or terminate.stdout or "").strip()
                    raise SetupError(
                        f"refusing to purge agent '{agent_id}': processes remain for managed "
                        f"uid {recorded_uid} after terminating {linux_user}"
                        + (f": {detail}" if detail else "")
                    )
                time.sleep(0.1)
        return True

    @staticmethod
    def _linux_uid_has_processes_for_purge(linux_uid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-eo", "uid="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SetupError(f"could not verify managed user processes: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SetupError(
                "could not verify managed user processes: "
                + (detail or f"ps exited {result.returncode}")
            )
        target = int(linux_uid)
        for line in result.stdout.splitlines():
            try:
                if int(line.strip()) == target:
                    return True
            except ValueError:
                continue
        return False

    def _managed_provider_process_live_for_purge(
        self,
        provider: str,
        linux_user: str,
        linux_uid: int,
    ) -> bool:
        """Reliably inspect provider processes for a possibly deleted account."""
        try:
            result = subprocess.run(
                ["ps", "-eo", "uid=,user=,args="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SetupError(f"could not verify managed runtime processes: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SetupError(
                "could not verify managed runtime processes: "
                + (detail or f"ps exited {result.returncode}")
            )
        wanted_provider = str(provider).strip().lower()
        wanted_user = str(linux_user).strip()
        wanted_uid = int(linux_uid)
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            uid_text, user_text, args = parts
            try:
                process_uid = int(uid_text)
            except ValueError:
                continue
            if process_uid != wanted_uid and user_text != wanted_user:
                continue
            if self._provider_from_process_args(args) == wanted_provider:
                return True
        return False

    def _verified_managed_user_home_for_purge(
        self,
        *,
        agent_id: str,
        linux_user: str,
        info: dict[str, Any],
        require_marker: bool,
    ) -> Path | None:
        stored_home_text = str(info.get("linux_home", "") or "").strip()
        stored_home = Path(stored_home_text) if stored_home_text else None
        passwd_home = self._linux_home_for_user(linux_user)
        if stored_home is not None and passwd_home is not None:
            try:
                if stored_home.resolve() != passwd_home.resolve():
                    raise SetupError(
                        f"refusing purge: stored home {stored_home} does not match passwd home {passwd_home}"
                    )
            except OSError as exc:
                raise SetupError(f"could not validate managed user home: {exc}") from exc
        recorded_uid = int(info.get("linux_uid", 0) or 0)
        if recorded_uid > 0:
            try:
                current_uid = int(pwd.getpwnam(linux_user).pw_uid)
            except KeyError:
                current_uid = 0
            if current_uid and current_uid != recorded_uid:
                raise SetupError(
                    f"refusing purge: Linux user {linux_user} now has uid {current_uid}, "
                    f"but the managed agent record owns uid {recorded_uid}"
                )
        home = passwd_home or stored_home or (Path("/home") / linux_user)
        if not require_marker and not home.exists():
            return home
        if not bool(info.get("linux_user_managed", False)):
            raise SetupError(
                f"refusing to purge Linux user {linux_user}: agent record has no managed-user ownership proof"
            )
        try:
            home_st = home.lstat()
        except FileNotFoundError as exc:
            raise SetupError(f"refusing purge: managed user home is missing: {home}") from exc
        if stat.S_ISLNK(home_st.st_mode) or not stat.S_ISDIR(home_st.st_mode):
            raise SetupError(f"refusing purge: managed user home is not a real directory: {home}")
        try:
            marker = json.loads(read_text_under(home, _MANAGED_USER_MARKER, max_bytes=16 * 1024))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SetupError(f"refusing purge: managed-user marker is missing or invalid: {exc}") from exc
        expected = {
            "format_version": 1,
            "linux_user": linux_user,
            "operation_id": str(info.get("managed_user_operation_id", "") or ""),
            "state_root": str(self.store.root.resolve()),
        }
        if recorded_uid > 0:
            expected["linux_uid"] = recorded_uid
        valid_agent_ids = {agent_id}
        valid_agent_ids.update(
            str(item).strip()
            for item in info.get("previous_agent_ids", [])
            if str(item).strip()
        )
        if (
            not isinstance(marker, dict)
            or str(marker.get("agent_id", "")) not in valid_agent_ids
            or any(marker.get(key) != value for key, value in expected.items())
        ):
            raise SetupError("refusing purge: managed-user marker does not match the agent record")
        return home

    def batch_create_agents(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        results: dict[str, list[dict[str, Any]]] = {"created": [], "errors": []}
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
                    core_prompts=entry.get("core_prompts"),
                )
                results["created"].append(agent_state["agent_id"])
            except Exception as exc:  # noqa: BLE001
                results["errors"].append({"agent_id": agent_id, "error": str(exc)})
        return results

    def _refresh_managed_agent_provider_alignment(
        self,
        agent_id: str,
        *,
        daemon_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            return
        self._refresh_managed_agent_provider_alignments(agent_id=token, daemon_map=daemon_map)

    def _refresh_managed_agent_provider_alignments(
        self,
        *,
        agent_id: str | None = None,
        daemon_map: dict[str, list[dict[str, Any]]] | None = None,
        state: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if daemon_map is None:
            daemon_map = self._running_provider_daemons_by_user()
        if state is None:
            state = self.store.read_state()
        agents = state.setdefault("agents", {})
        dirty = False
        for token, agent in agents.items():
            if agent_id and token != agent_id:
                continue
            self._hydrate_agent_controls(agent)
            if self._apply_live_provider_alignment(
                state=state,
                agent_id=token,
                agent=agent,
                daemon_map=daemon_map,
            ):
                dirty = True
        if dirty and persist:
            self.store.write_state(state)
        return state

    def _apply_live_provider_alignment(
        self,
        *,
        state: dict[str, Any],
        agent_id: str,
        agent: dict[str, Any],
        daemon_map: dict[str, list[dict[str, Any]]],
    ) -> bool:
        info = agent.setdefault("agent", {})
        if bool(info.get("local_user", False)):
            return False
        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            return self._clear_runtime_provider_issue(agent)

        current_provider = str(info.get("provider", "")).strip().lower()
        live_entries = list(daemon_map.get(linux_user, []))
        live_providers: list[str] = []
        for entry in live_entries:
            provider = str(entry.get("provider", "")).strip().lower()
            if provider and provider not in live_providers:
                live_providers.append(provider)

        if not live_providers:
            return self._clear_runtime_provider_issue(agent)

        effective_provider = current_provider if current_provider in live_providers else live_providers[0]
        changed = False
        if effective_provider and effective_provider != current_provider:
            info["provider"] = effective_provider
            info["runtime"] = get_provider(effective_provider).runtime
            auth_mode = str(info.get("auth_mode", "")).strip().lower()
            spec = get_provider(effective_provider)
            if not spec.supports_auth_mode(auth_mode):
                info["auth_mode"] = spec.default_auth_mode
            changed = True

        if len(live_providers) > 1:
            changed = self._set_agent_provider_issue(
                agent,
                status="warning",
                kind="runtime_conflict",
                issue=f"multiple provider daemons detected: {', '.join(live_providers)}; using {effective_provider}",
                remediation=(
                    f"Run 'sudo clawie agent provider set {agent_id} {effective_provider}' to stop the extra runtimes."
                ),
                requested_provider="",
            ) or changed
            if changed:
                self._event(
                    state,
                    "agents.provider_runtime_conflict",
                    f"Detected multiple runtimes for {agent_id}",
                    {
                        "agent_id": agent_id,
                        "linux_user": linux_user,
                        "live_providers": list(live_providers),
                        "effective_provider": effective_provider,
                    },
                )
            return changed

        live_provider = live_providers[0]
        if live_provider != current_provider:
            changed = self._set_agent_provider_issue(
                agent,
                status="warning",
                kind="runtime_drift",
                issue=(
                    f"live runtime was {live_provider}; Clawie aligned state away from {current_provider or 'unknown'}"
                ),
                remediation=(
                    f"Run 'sudo clawie agent provider set {agent_id} {current_provider}' if you still want to switch."
                    if current_provider
                    else ""
                ),
                requested_provider=current_provider,
            ) or changed
            self._event(
                state,
                "agents.provider_aligned_to_runtime",
                f"Aligned {agent_id} to live runtime {live_provider}",
                {
                    "agent_id": agent_id,
                    "linux_user": linux_user,
                    "previous_provider": current_provider,
                    "live_provider": live_provider,
                },
            )
            return True

        return self._clear_runtime_provider_issue(agent) or changed

    @staticmethod
    def _clear_agent_provider_issue(agent: dict[str, Any]) -> None:
        info = agent.setdefault("agent", {})
        info["provider_status"] = "ok"
        for key in ("provider_issue_kind", "provider_issue", "provider_remediation", "provider_requested"):
            info.pop(key, None)

    def _clear_runtime_provider_issue(self, agent: dict[str, Any]) -> bool:
        info = agent.setdefault("agent", {})
        if str(info.get("provider_issue_kind", "")) != "runtime_conflict":
            return False
        self._clear_agent_provider_issue(agent)
        return True

    @staticmethod
    def _set_agent_provider_issue(
        agent: dict[str, Any],
        *,
        status: str,
        kind: str,
        issue: str,
        remediation: str,
        requested_provider: str,
    ) -> bool:
        info = agent.setdefault("agent", {})
        next_values = {
            "provider_status": str(status or "warning"),
            "provider_issue_kind": str(kind or "").strip(),
            "provider_issue": str(issue or "").strip(),
            "provider_remediation": str(remediation or "").strip(),
            "provider_requested": str(requested_provider or "").strip().lower(),
        }
        current_values = {
            key: str(info.get(key, "") if key != "provider_status" else info.get(key, "ok"))
            for key in next_values
        }
        if current_values == next_values:
            return False
        info.update(next_values)
        return True

    def _provider_switch_remediation(
        self,
        *,
        agent_id: str,
        target_provider: str,
        linux_user: str,
        error: str,
    ) -> str:
        message = str(error).strip().lower()
        if "requires root" in message or "sudo" in message:
            return f"Re-run 'sudo clawie agent provider set {agent_id} {target_provider}'."
        if "executable" in message or "not found" in message or "install" in message:
            return f"Install or link '{target_provider}', then run 'sudo clawie agent provider set {agent_id} {target_provider}'."
        if linux_user:
            return (
                f"Check the {target_provider} service for {linux_user}, then run "
                f"'sudo clawie agent provider set {agent_id} {target_provider}' again."
            )
        return f"Retry 'clawie agent provider set {agent_id} {target_provider}' after fixing the provider runtime."

    def _local_agent_view(self, provider: str) -> dict[str, Any]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        local_state = self._refresh_local_service_statuses([provider], local_state)
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        info = dict(local_state.get(provider, {}))
        home = self._local_agent_home(provider)
        prompts = self._normalize_core_prompts(provider, {})
        if home:
            prompts = self._normalize_core_prompts(provider, self._read_core_prompts_from_home(provider, home))
        self._seed_core_prompt_defaults(provider, prompts, agent_id=f"@local:{provider}", display_name="local-user")
        self._seed_delegation_skill(prompts, self._default_plugins_for_provider(provider))
        return {
            "agent_id": f"@local:{provider}",
            "display_name": "local-user",
            "source_template": "local-user",
            "clone_from": "",
            "channel_strategy": "local-user",
            "channels": [],
            "core_prompts": prompts,
            "credential_sync": self._normalize_credential_sync_state({}, default_when_missing=False),
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

    @classmethod
    def _inject_marked_prompt_snippet(
        cls,
        content: str,
        begin: str,
        end: str,
        snippet: str,
    ) -> str:
        body = str(content or "")
        block = str(snippet).rstrip()
        pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
        if pattern.search(body):
            return pattern.sub(block, body)
        sep = "\n\n" if body.strip() else ""
        return body.rstrip() + sep + block + "\n"

    @classmethod
    def _remove_marked_prompt_snippet(cls, content: str, begin: str, end: str) -> str:
        body = str(content or "")
        if not body.strip():
            return ""
        pattern = re.compile(
            r"\n*" + re.escape(begin) + r".*?" + re.escape(end) + r"\n*",
            re.DOTALL,
        )
        return pattern.sub("\n", body).rstrip() + "\n"

    def _sync_control_role_workspace(self, agent_state: dict[str, Any]) -> None:
        agent = agent_state.setdefault("agent", {})
        role = str(agent.get("role", "worker")).strip().lower() or "worker"
        if role not in {"worker", "control"}:
            role = "worker"
        agent["role"] = role
        prompts = agent_state.setdefault("core_prompts", {})
        if not isinstance(prompts, dict):
            prompts = {}
            agent_state["core_prompts"] = prompts
        tools_md = str(prompts.get("TOOLS.md", "") or "")
        agents_md = str(prompts.get("AGENTS.md", "") or "")
        if role == "control":
            prompts["TOOLS.md"] = inject_addon_tools_snippet(
                tools_md,
                "control",
                self._CONTROL_TOOLS_SNIPPET.rstrip(),
            )
            prompts["AGENTS.md"] = self._inject_marked_prompt_snippet(
                agents_md,
                self._CONTROL_AGENTS_MARKER,
                self._CONTROL_AGENTS_MARKER_END,
                self._CONTROL_AGENTS_SNIPPET,
            )
            return
        prompts["TOOLS.md"] = remove_addon_tools_snippet(tools_md, "control")
        prompts["AGENTS.md"] = self._remove_marked_prompt_snippet(
            agents_md,
            self._CONTROL_AGENTS_MARKER,
            self._CONTROL_AGENTS_MARKER_END,
        )

    def _hydrate_agent_controls(self, agent_state: dict[str, Any]) -> None:
        channels = agent_state.get("channels", [])
        if isinstance(channels, list):
            for channel in channels:
                if isinstance(channel, dict):
                    channel["enabled"] = bool(channel.get("enabled", True))
        agent = agent_state.setdefault("agent", {})
        provider = str(agent.get("provider", "")).strip().lower()
        raw_plugins = agent.get("plugins", self._default_plugins_for_provider(str(agent.get("provider", ""))))
        if not isinstance(raw_plugins, dict):
            raw_plugins = self._default_plugins_for_provider(str(agent.get("provider", "")))
        agent["plugins"] = self._normalize_plugins(raw_plugins)
        if "model_tier" not in agent:
            agent["model_tier"] = "balanced"
        agent_state["core_prompts"] = self._normalize_core_prompts(provider, agent_state.get("core_prompts", {}))
        self._seed_core_prompt_defaults(
            provider,
            agent_state["core_prompts"],
            agent_id=str(agent_state.get("agent_id", "")),
            display_name=str(agent_state.get("display_name", "")),
        )
        agent_state["credential_sync"] = self._normalize_credential_sync_state(
            agent_state.get("credential_sync"),
            default_when_missing=True,
        )
        agent_state["addons"] = self._normalize_agent_addons(agent_state.get("addons"))
        self._seed_delegation_skill(agent_state["core_prompts"], agent["plugins"])
        self._sync_control_role_workspace(agent_state)

    def set_agent_model_tier(self, agent_id: str, tier: str = "") -> str:
        """Set or cycle the model tier for *agent_id*. Returns the new tier."""
        from clawie.delegation import VALID_TIER_NAMES

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent_state = agents.get(agent_id)
        if not agent_state:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        agent = agent_state.setdefault("agent", {})
        current = str(agent.get("model_tier", "balanced"))

        if tier:
            if tier not in VALID_TIER_NAMES:
                raise ValueError(
                    f"unknown tier {tier!r}; valid: {', '.join(VALID_TIER_NAMES)}"
                )
            new_tier = tier
        else:
            # Cycle: fast -> balanced -> power -> fast
            idx = list(VALID_TIER_NAMES).index(current) if current in VALID_TIER_NAMES else 0
            new_tier = VALID_TIER_NAMES[(idx + 1) % len(VALID_TIER_NAMES)]

        if new_tier == current:
            # An explicit request for the already-selected tier is a no-op, not
            # a state transition. Older records may lack the normalized field,
            # so persist it once without emitting a misleading change event.
            if agent.get("model_tier") != new_tier:
                agent["model_tier"] = new_tier
                self.store.write_state(state)
            return new_tier

        agent["model_tier"] = new_tier
        self._event(
            state,
            "agent.model_tier.changed",
            f"Agent {agent_id} model tier changed to {new_tier}",
            {"agent_id": agent_id, "old_tier": current, "new_tier": new_tier},
        )
        self.store.write_state(state)
        return new_tier

    def start_agent_repl(
        self,
        agent_id: str,
        handler: Any = None,
        model_tier: str = "",
        executor_agent_id: str = "",
    ) -> None:
        from clawie.delegation import AgentREPL, DEFAULT_TIER

        agent_id = self._validate_agent_id(agent_id)
        tier = model_tier or DEFAULT_TIER
        executor = str(executor_agent_id).strip()
        if handler is None:
            if not executor:
                raise SetupError("delegation REPL requires a managed executor agent")
            handler = self._gateway_task_handler(executor)

        repl = AgentREPL(agent_id, handler=handler, model_tier=tier)
        import signal

        def _shutdown(*_a: Any) -> None:
            repl.stop()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
        repl.start()

    def stop_session_agent(self, parent_id: str, child_id: str) -> None:
        stopped = False
        if parent_id in self._session_managers:
            before = {
                str(item.get("agent_id", ""))
                for item in self._session_managers[parent_id].list_agents()
            }
            self._session_managers[parent_id].stop_agent(child_id)
            stopped = child_id in before

        record = self.store.read_session_agent(parent_id, child_id)
        if record:
            pid = int(record.get("pid", 0) or 0)
            self._shutdown_session_process(parent_id, child_id, pid)
            self.store.delete_session_agent(parent_id, child_id)
            if self.store.read_session_agents(parent_id):
                self._persist_session_tree(parent_id)
            else:
                self._mark_delegation_tree_status(parent_id, child_id, "completed")
            stopped = True

        if not stopped:
            raise ValueError(f"session agent not found: {child_id}")

        state = self.store.read_state()
        self._event(
            state,
            "session.agent.stopped",
            f"Session agent {child_id} stopped under {parent_id}",
            {"parent": parent_id, "child": child_id},
        )
        self.store.write_state(state)

    def stop_all_session_agents(self, parent_id: str) -> None:
        stopped_children: list[str] = []
        if parent_id in self._session_managers:
            stopped_children.extend(
                str(item.get("agent_id", ""))
                for item in self._session_managers[parent_id].list_agents()
                if str(item.get("agent_id", ""))
            )
            self._session_managers[parent_id].stop_all()
            del self._session_managers[parent_id]
        for record in self.store.read_session_agents(parent_id):
            child_id = str(record.get("child_agent_id", ""))
            if not child_id:
                continue
            self._shutdown_session_process(parent_id, child_id, int(record.get("pid", 0) or 0))
            self.store.delete_session_agent(parent_id, child_id)
            stopped_children.append(child_id)
        if self.store.read_session_agents(parent_id):
            self._persist_session_tree(parent_id)
        else:
            for child_id in stopped_children:
                self._mark_delegation_tree_status(parent_id, child_id, "completed")

    def list_session_agents(self, parent_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        if parent_id in self._session_managers:
            for item in self._session_managers[parent_id].list_agents():
                agent_id = str(item.get("agent_id", ""))
                if agent_id:
                    seen.add(agent_id)
                rows.append(item)
        for record in self.store.read_session_agents(parent_id):
            child_id = str(record.get("child_agent_id", ""))
            if child_id in seen:
                continue
            rows.append(self._session_record_with_liveness(record))
        return rows
