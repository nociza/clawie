"""Core prompt files and prompt sync (ClawieService mixin)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from clawie.providers import (
    get_provider,
)
from clawie.service_common import SetupError, AgentNotFoundError, now_iso, _default_core_prompt_content, _is_legacy_core_prompt_default
from clawie.safe_fs import owner_for_username, read_text_under, write_text_under


class PromptOpsMixin:

    def apply_staged_prompts(self, agent_id: str) -> dict[str, Any]:
        """Apply prompt files from state directly to an agent workspace.

        Older clawie releases used a world-writable /tmp handoff when the
        manager user could not write the agent workspace. That is intentionally
        disabled: core prompts are authority-bearing input, so applying them
        now requires direct write access as root, the agent user, or a manager
        user already granted workspace permissions.
        """
        self._require_setup()
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
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"agent '{agent_id}' has no linux_user home to write prompts to")
        before = self._read_core_prompts_from_home(provider, home)
        self._write_prompt_files_for_home(provider, home, agent.get("core_prompts", {}), linux_user)
        after = self._read_core_prompts_from_home(provider, home)
        applied = [
            name for name, content in after.items()
            if str(content) and str(before.get(name, "")) != str(content)
        ]
        return {"agent_id": agent_id, "applied": applied, "remaining": []}

    def list_agent_core_prompts(self, agent_id: str) -> list[dict[str, Any]]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        prompts = self._normalize_core_prompts(provider, payload.get("core_prompts", {}))
        rows: list[dict[str, Any]] = []
        for name in self._provider_core_prompt_names(provider):
            content = str(prompts.get(name, ""))
            rows.append(
                {
                    "name": name,
                    "chars": len(content),
                    "configured": bool(content.strip()),
                }
            )
        return rows

    def get_agent_core_prompt(self, agent_id: str, prompt_name: str) -> dict[str, str]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        name = self._canonical_core_prompt_name(provider, prompt_name)
        prompts = self._normalize_core_prompts(provider, payload.get("core_prompts", {}))
        return {"name": name, "content": str(prompts.get(name, ""))}

    def set_agent_core_prompt(
        self,
        agent_id: str,
        prompt_name: str,
        content: str,
        sync_to_disk: bool = True,
    ) -> dict[str, Any]:
        token = str(agent_id).strip()
        body = str(content)
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            name = self._canonical_core_prompt_name(provider, prompt_name)
            home = self._local_agent_home(provider)
            if not home:
                raise SetupError(f"could not resolve local home for provider '{provider}'")
            self._write_core_prompt_file(provider, home, name, body)
            return self._local_agent_view(provider)

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        name = self._canonical_core_prompt_name(provider, prompt_name)
        prompts = self._normalize_core_prompts(provider, agent.get("core_prompts", {}))
        prompts[name] = body
        agent["core_prompts"] = prompts
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.prompt_updated",
            f"Updated {name} for {token}",
            {"agent_id": token, "prompt": name, "chars": len(body)},
        )
        self.store.write_state(state)
        if sync_to_disk:
            self.write_agent_core_prompts_to_disk(token)
        return agent

    def clone_agent_prompts(
        self,
        from_agent: str,
        to_agent: str,
        apply_to_disk: bool = True,
    ) -> dict[str, Any]:
        source = self.get_dashboard_agent(from_agent)
        target = self.get_dashboard_agent(to_agent)
        source_info = source.get("agent", {})
        target_info = target.get("agent", {})
        source_provider = str(source_info.get("provider", "")).strip().lower()
        target_provider = str(target_info.get("provider", "")).strip().lower()
        if source_provider != target_provider:
            raise ValueError("source and target providers must match to clone core prompts")
        prompt_payload = self._normalize_core_prompts(source_provider, source.get("core_prompts", {}))
        for name, content in prompt_payload.items():
            self.set_agent_core_prompt(to_agent, name, content, sync_to_disk=False)
        if apply_to_disk:
            self.write_agent_core_prompts_to_disk(to_agent)
        return self.get_dashboard_agent(to_agent)

    def sync_agent_core_prompts_from_disk(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            return self._local_agent_view(provider)
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"agent '{token}' has no linux_user home to read prompts from")
        disk_prompts = self._read_core_prompts_from_home(provider, home)
        agent["core_prompts"] = self._normalize_core_prompts(provider, disk_prompts)
        self._seed_core_prompt_defaults(
            provider,
            agent["core_prompts"],
            agent_id=token,
            display_name=str(agent.get("display_name", "")),
        )
        self._seed_delegation_skill(agent["core_prompts"], self._normalize_plugins(info.get("plugins", {})))
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.prompt_synced",
            f"Synced core prompts from disk for {token}",
            {"agent_id": token, "source": str(home)},
        )
        self.store.write_state(state)
        return agent

    def write_agent_core_prompts_to_disk(self, agent_id: str) -> dict[str, Any]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        prompts = self._normalize_core_prompts(provider, payload.get("core_prompts", {}))
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            home = self._local_agent_home(provider)
            if not home:
                raise SetupError(f"could not resolve local home for provider '{provider}'")
            self._write_prompt_files_for_home(provider, home, prompts)
            return self._local_agent_view(provider)

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"agent '{token}' has no linux_user home to write prompts to")
        linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
        self._write_prompt_files_for_home(provider, home, prompts, linux_user)
        return agent

    @staticmethod
    def _staged_prompt_pickup_shell(provider: str, state_dir: str, workspace_dir: str) -> str:
        """No-op placeholder kept for generated units from older call sites."""
        return ":"

    @staticmethod
    def _provider_core_prompt_names(provider: str) -> tuple[str, ...]:
        try:
            names = get_provider(provider).core_prompt_files
        except ValueError:
            names = ()
        if names:
            return names
        return (
            "SOUL.md",
            "IDENTITY.md",
            "AGENTS.md",
            "TOOLS.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "BOOTSTRAP.md",
            "USER.md",
        )

    def _canonical_core_prompt_name(self, provider: str, prompt_name: str) -> str:
        token = str(prompt_name).strip().upper()
        if token and not token.endswith(".MD"):
            token = f"{token}.MD"
        for item in self._provider_core_prompt_names(provider):
            if item.upper() == token:
                return item
        raise ValueError(
            f"unknown core prompt '{prompt_name}'. supported: {', '.join(self._provider_core_prompt_names(provider))}"
        )

    def _normalize_core_prompts(self, provider: str, payload: dict[str, Any]) -> dict[str, str]:
        rows: dict[str, str] = {}
        data = payload if isinstance(payload, dict) else {}
        for name in self._provider_core_prompt_names(provider):
            value = data.get(name, "")
            rows[name] = str(value) if value is not None else ""
        return rows

    def _seed_core_prompt_defaults(
        self,
        provider: str,
        core_prompts: dict[str, str],
        agent_id: str = "",
        display_name: str = "",
    ) -> None:
        for name in self._provider_core_prompt_names(provider):
            existing = str(core_prompts.get(name, "") or "")
            if existing and _is_legacy_core_prompt_default(name, existing):
                content = _default_core_prompt_content(name, agent_id=agent_id, display_name=display_name)
                if content:
                    core_prompts[name] = content
                continue
            if existing:
                continue
            content = _default_core_prompt_content(name, agent_id=agent_id, display_name=display_name)
            if content:
                core_prompts[name] = content

    def _core_prompt_path(self, provider: str, home: Path, prompt_name: str) -> Path:
        spec = get_provider(provider)
        name = self._canonical_core_prompt_name(provider, prompt_name)
        return home / spec.state_dir / spec.workspace_dir / name

    def _read_core_prompts_from_home(self, provider: str, home: Path) -> dict[str, str]:
        rows: dict[str, str] = {}
        if not home.exists():
            return {name: "" for name in self._provider_core_prompt_names(provider)}
        for name in self._provider_core_prompt_names(provider):
            path = self._core_prompt_path(provider, home, name)
            try:
                rows[name] = read_text_under(home, path.relative_to(home), max_bytes=4 * 1024 * 1024)
            except FileNotFoundError:
                rows[name] = ""
        return rows

    def _write_core_prompt_file(
        self,
        provider: str,
        home: Path,
        prompt_name: str,
        content: str,
        linux_user: str = "",
    ) -> Path:
        path = self._core_prompt_path(provider, home, prompt_name)
        return write_text_under(
            home,
            path.relative_to(home),
            content,
            mode=0o600,
            directory_mode=0o700,
            owner=owner_for_username(linux_user) if os.geteuid() == 0 else None,
        )

    def _write_prompt_files_for_home(
        self,
        provider: str,
        home: Path,
        prompts: dict[str, str],
        linux_user: str = "",
    ) -> list[str]:
        written: list[str] = []
        for name, content in self._normalize_core_prompts(provider, prompts).items():
            try:
                target = self._write_core_prompt_file(provider, home, name, content, linux_user)
                written.append(str(target))
            except PermissionError:
                if linux_user:
                    raise SetupError(
                        "could not write core prompts directly to the agent workspace. "
                        "Run with sudo/root or through clawied; private agent homes "
                        "must not be opened to manager-group access and insecure staging is disabled."
                    )
                else:
                    raise
        return written

    def _stage_prompt_file(
        self, linux_user: str, provider: str, prompt_name: str, content: str,
    ) -> Path:
        raise SetupError("insecure /tmp prompt staging is disabled; write prompts directly instead")

    def _apply_staged_prompts_if_possible(
        self, provider: str, home: Path, linux_user: str,
    ) -> list[str]:
        return []
