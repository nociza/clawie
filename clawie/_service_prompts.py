"""Core prompt files and prompt sync (ZeroClawService mixin)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from clawie.providers import (
    get_provider,
)
from clawie.service_common import SetupError, AgentNotFoundError, now_iso, _default_core_prompt_content, _is_legacy_core_prompt_default


class PromptOpsMixin:

    def apply_staged_prompts(self, agent_id: str) -> dict[str, Any]:
        """Apply staged prompt files to an agent workspace.

        Runs the pickup shell snippet as the agent's linux_user so it has
        write access to the workspace.  Requires root when linux_user differs
        from the current user.
        """
        self._require_setup()
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
        spec = get_provider(provider)
        pickup = self._staged_prompt_pickup_shell(provider, spec.state_dir, spec.workspace_dir)
        cmd = self._user_shell_command(linux_user, pickup)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            raise SetupError(f"apply staged prompts failed: {output or f'exit {result.returncode}'}")
        # Check what was applied by looking at what's no longer in staging
        stage_dir = self._prompt_stage_dir(linux_user) if linux_user else None
        remaining: list[str] = []
        if stage_dir and stage_dir.is_dir():
            remaining = [f.name for f in stage_dir.iterdir() if f.name.startswith(f"{provider}--")]
        # Also write from DB to disk (may succeed now that workspace exists)
        home = self._agent_linux_home(agent)
        if home:
            self._write_prompt_files_for_home(provider, home, agent.get("core_prompts", {}), linux_user)
        applied = [
            name.split("--", 1)[1] for name in
            [f.name for f in (stage_dir.iterdir() if stage_dir and stage_dir.is_dir() else [])]
            if name.startswith(f"{provider}--")
        ]
        # If staging dir is now empty or doesn't exist, all were applied
        prompts = agent.get("core_prompts", {})
        prompt_names = [k for k in prompts if prompts[k]]
        if not remaining:
            applied = prompt_names
        return {"agent_id": agent_id, "applied": applied, "remaining": remaining}

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
        agents = state.setdefault("agents", state.get("users", {}))
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
        agents = state.setdefault("agents", state.get("users", {}))
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
        agents = state.setdefault("agents", state.get("users", {}))
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
        """Shell snippet that copies staged prompt files into the workspace.

        Runs as the target user so it has write access to $HOME.
        Staged files live at /tmp/clawie-prompt-stage/$USER/<provider>--<name>.
        """
        return (
            f'STAGE_DIR="/tmp/clawie-prompt-stage/$USER"; '
            f'WS="$HOME/{state_dir}/{workspace_dir}"; '
            f'if [ -d "$STAGE_DIR" ]; then '
            f'  mkdir -p "$WS"; '
            f'  for f in "$STAGE_DIR"/{provider}--*; do '
            f'    [ -f "$f" ] || continue; '
            f'    name="${{f##*--}}"; '
            f'    cp "$f" "$WS/$name" && rm -f "$f" 2>/dev/null; '
            f'  done; '
            f'fi'
        )

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
        for name in self._provider_core_prompt_names(provider):
            path = self._core_prompt_path(provider, home, name)
            if path.exists():
                rows[name] = path.read_text(encoding="utf-8")
            else:
                rows[name] = ""
        return rows

    @classmethod
    def _prompt_stage_dir(cls, linux_user: str) -> Path:
        return cls._PROMPT_STAGE_ROOT / linux_user

    def _write_core_prompt_file(self, provider: str, home: Path, prompt_name: str, content: str) -> Path:
        path = self._core_prompt_path(provider, home, prompt_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        try:
            os.chmod(str(path), 0o664)
        except OSError:
            pass
        return path

    def _write_prompt_files_for_home(
        self,
        provider: str,
        home: Path,
        prompts: dict[str, str],
        linux_user: str = "",
    ) -> list[str]:
        written: list[str] = []
        staged: list[str] = []
        for name, content in self._normalize_core_prompts(provider, prompts).items():
            try:
                target = self._write_core_prompt_file(provider, home, name, content)
                if linux_user and os.geteuid() == 0:
                    subprocess.run(
                        ["chown", f"{linux_user}:{linux_user}", str(target)],
                        check=False,
                        capture_output=True,
                    )
                written.append(str(target))
            except PermissionError:
                if linux_user:
                    self._stage_prompt_file(linux_user, provider, name, content)
                    staged.append(name)
                else:
                    raise
        if staged:
            self._apply_staged_prompts_if_possible(provider, home, linux_user)
        return written

    def _stage_prompt_file(
        self, linux_user: str, provider: str, prompt_name: str, content: str,
    ) -> Path:
        root = self._PROMPT_STAGE_ROOT
        root.mkdir(parents=True, exist_ok=True, mode=0o1777)
        try:
            os.chmod(str(root), 0o1777)
        except OSError:
            pass
        stage = self._prompt_stage_dir(linux_user)
        # Per-user dir is 777 (no sticky) so the target user can delete files
        # written by the manager user after copying them to the workspace.
        stage.mkdir(parents=True, exist_ok=True, mode=0o777)
        try:
            os.chmod(str(stage), 0o777)
        except OSError:
            pass
        target = stage / f"{provider}--{prompt_name}"
        target.write_text(str(content), encoding="utf-8")
        os.chmod(str(target), 0o666)
        return target

    def _apply_staged_prompts_if_possible(
        self, provider: str, home: Path, linux_user: str,
    ) -> list[str]:
        stage = self._prompt_stage_dir(linux_user)
        if not stage.is_dir():
            return []
        applied: list[str] = []
        prefix = f"{provider}--"
        for entry in sorted(stage.iterdir()):
            if not entry.name.startswith(prefix):
                continue
            prompt_name = entry.name[len(prefix):]
            content = entry.read_text(encoding="utf-8")
            try:
                target = self._write_core_prompt_file(provider, home, prompt_name, content)
                if linux_user and os.geteuid() == 0:
                    subprocess.run(
                        ["chown", f"{linux_user}:{linux_user}", str(target)],
                        check=False,
                        capture_output=True,
                    )
                applied.append(prompt_name)
                entry.unlink(missing_ok=True)
            except PermissionError:
                continue
        if applied and not any(stage.iterdir()):
            stage.rmdir()
        return applied
