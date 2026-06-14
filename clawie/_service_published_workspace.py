"""Published workspace operations (ClawieService mixin)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from clawie.providers import get_provider
from clawie.published_workspace import PublishedWorkspace, PublishedWorkspaceError
from clawie.service_common import AgentNotFoundError, SetupError


class PublishedWorkspaceOpsMixin:
    """Service methods for explicit cross-agent artifact publication."""

    def _published_workspace_root(self) -> Path:
        env = os.environ.get("CLAWIE_PUBLISHED_WORKSPACE_DIR", "").strip()
        if env:
            return Path(env).expanduser()
        config = self.store.read_config()
        configured = str(config.get("published_workspace_root", "")).strip()
        if configured:
            return Path(configured).expanduser()
        return self.store.root / "published-workspace"

    def _published_workspace(self) -> PublishedWorkspace:
        return PublishedWorkspace(self._published_workspace_root())

    def workspace_status(self) -> dict[str, Any]:
        workspace = self._published_workspace()
        root = workspace.root
        initialized = (root / "WORKSPACE.json").is_file() and workspace.catalog_path.is_file()
        publications: list[dict[str, Any]] = []
        if initialized:
            publications = workspace.list_publications()
        return {
            "root": str(root),
            "initialized": initialized,
            "publications": len(publications),
            "views": str(root / "views"),
        }

    def workspace_publish(
        self,
        source_path: str | Path,
        *,
        agent_id: str = "",
        visible_to: list[str] | None = None,
        title: str = "",
    ) -> dict[str, Any]:
        publisher_id = self._resolve_workspace_agent_id(agent_id)
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        publisher = agents.get(publisher_id)
        if not isinstance(publisher, dict):
            raise AgentNotFoundError(f"agent not found: {publisher_id}")
        self._hydrate_agent_controls(publisher)
        workspace_path = self._agent_provider_workspace_path(publisher)
        if workspace_path is None:
            raise SetupError(f"agent '{publisher_id}' has no manageable provider workspace")
        linux_user = str(publisher.get("agent", {}).get("linux_user", "")).strip()
        if linux_user:
            self._require_linux_user_access(linux_user, "workspace publish")

        viewers = self._resolve_workspace_viewers(visible_to or [])
        result = self._published_workspace().publish(
            source_path=Path(source_path),
            publisher_agent_id=publisher_id,
            visible_to=viewers,
            title=title,
            source_workspace=workspace_path,
        )
        mount_result = self.workspace_mount(
            agent_id="",
            all_agents=False,
            agents=result.get("visible_to", []),
            skip_inaccessible=True,
        )
        state = self.store.read_state()
        self._event(
            state,
            "workspace.published",
            f"Published workspace artifact {result.get('publication_id', '')}",
            {
                "publication_id": result.get("publication_id", ""),
                "publisher_agent_id": publisher_id,
                "visible_to": result.get("visible_to", []),
                "title": result.get("title", ""),
                "path": result.get("path", ""),
            },
        )
        self.store.write_state(state)
        result["mounts"] = mount_result
        return result

    def workspace_list(
        self,
        *,
        agent_id: str = "",
        publisher_agent_id: str = "",
    ) -> list[dict[str, Any]]:
        viewer = ""
        if str(agent_id or "").strip():
            viewer = self._resolve_workspace_agent_id(agent_id)
        else:
            viewer = self._infer_workspace_agent_id(raise_on_ambiguous=False) or ""
        if publisher_agent_id:
            self._require_agent_exists(str(publisher_agent_id))
        return self._published_workspace().list_publications(
            viewer_agent_id=viewer,
            publisher_agent_id=str(publisher_agent_id or "").strip(),
        )

    def workspace_show(
        self,
        publication_id: str,
        *,
        agent_id: str = "",
    ) -> dict[str, Any]:
        viewer = ""
        if str(agent_id or "").strip():
            viewer = self._resolve_workspace_agent_id(agent_id)
        result = self._published_workspace().show(publication_id, viewer_agent_id=viewer)
        if viewer and viewer not in set(result.get("visible_to", [])):
            raise SetupError(f"agent '{viewer}' cannot view publication {publication_id}")
        return result

    def workspace_verify(self, publication_id: str = "") -> dict[str, Any]:
        return self._published_workspace().verify(publication_id)

    def workspace_mount(
        self,
        *,
        agent_id: str = "",
        all_agents: bool = False,
        agents: list[str] | None = None,
        skip_inaccessible: bool = False,
    ) -> dict[str, Any]:
        if all_agents and agent_id:
            raise ValueError("use either --agent or --all, not both")
        state = self.store.read_state()
        known = state.setdefault("agents", {})
        if agents is not None:
            targets = [self._resolve_workspace_agent_id(item) for item in agents]
        elif all_agents:
            targets = sorted(str(key) for key in known)
        else:
            targets = [self._resolve_workspace_agent_id(agent_id)]

        mounted: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for target_id in targets:
            agent = known.get(target_id)
            if not isinstance(agent, dict):
                raise AgentNotFoundError(f"agent not found: {target_id}")
            self._hydrate_agent_controls(agent)
            home = self._agent_linux_home(agent)
            linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
            workspace_path = self._agent_provider_workspace_path(agent)
            if home is None or workspace_path is None:
                skipped.append({"agent_id": target_id, "reason": "agent has no linux home"})
                continue
            if linux_user and not self._can_manage_linux_user(linux_user):
                reason = "requires root or the target agent linux user"
                if skip_inaccessible or all_agents or agents is not None:
                    skipped.append({"agent_id": target_id, "reason": reason})
                    continue
                raise SetupError(f"workspace mount for {target_id} {reason}")
            try:
                mount = self._ensure_published_workspace_mount(
                    agent_id=target_id,
                    workspace=workspace_path,
                    linux_user=linux_user,
                )
            except PublishedWorkspaceError as exc:
                if skip_inaccessible or all_agents or agents is not None:
                    skipped.append({"agent_id": target_id, "reason": str(exc)})
                    continue
                raise
            mounted.append(mount)
        return {
            "root": str(self._published_workspace_root()),
            "mounted": mounted,
            "skipped": skipped,
        }

    def _ensure_published_workspace_mount(
        self,
        *,
        agent_id: str,
        workspace: Path,
        linux_user: str,
    ) -> dict[str, str]:
        token = str(agent_id or "").strip()
        if not token:
            return {}
        published = self._published_workspace()
        view = Path(published.rebuild_view(token)["view_path"]).resolve(strict=True)
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "published"
        if target.is_symlink():
            try:
                if target.resolve(strict=True) == view:
                    self._chown_path(target, linux_user)
                    return {
                        "agent_id": token,
                        "workspace": str(workspace),
                        "target": str(target),
                        "view": str(view),
                        "status": "mounted",
                    }
            except OSError:
                pass
            target.unlink()
        elif target.exists():
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            else:
                raise PublishedWorkspaceError(
                    f"cannot mount published workspace because path already exists: {target}"
                )
        os.symlink(view, target, target_is_directory=True)
        self._chown_path(target, linux_user)
        return {
            "agent_id": token,
            "workspace": str(workspace),
            "target": str(target),
            "view": str(view),
            "status": "mounted",
        }

    def _agent_provider_workspace_path(self, agent: dict[str, Any]) -> Path | None:
        info = agent.get("agent", {})
        if not isinstance(info, dict):
            return None
        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            return None
        provider = str(info.get("provider", "openclaw")).strip().lower() or "openclaw"
        try:
            spec = get_provider(provider)
        except ValueError:
            return None
        home = self._agent_linux_home(agent)
        if home is None:
            return None
        return home / spec.state_dir / spec.workspace_dir

    def _resolve_workspace_agent_id(self, agent_id: str) -> str:
        token = str(agent_id or "").strip()
        if token:
            self._require_agent_exists(token)
            return token
        inferred = self._infer_workspace_agent_id(raise_on_ambiguous=True)
        if inferred:
            return inferred
        raise ValueError("agent_id is required when clawie cannot infer it from the current Linux user")

    def _infer_workspace_agent_id(self, *, raise_on_ambiguous: bool) -> str | None:
        current = self._current_linux_user()
        if not current:
            return None
        state = self.store.read_state()
        matches: list[str] = []
        for agent_id, agent in state.setdefault("agents", {}).items():
            if not isinstance(agent, dict):
                continue
            linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
            if linux_user == current:
                matches.append(str(agent_id))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and raise_on_ambiguous:
            raise ValueError(
                "current Linux user maps to multiple agents; pass --agent explicitly"
            )
        return None

    def _resolve_workspace_viewers(self, visible_to: list[str]) -> list[str]:
        rows: list[str] = []
        seen: set[str] = set()
        for item in visible_to:
            for token in str(item or "").split(","):
                agent_id = token.strip()
                if not agent_id or agent_id in seen:
                    continue
                self._require_agent_exists(agent_id)
                seen.add(agent_id)
                rows.append(agent_id)
        return rows

    def _require_agent_exists(self, agent_id: str) -> None:
        state = self.store.read_state()
        if str(agent_id or "").strip() not in state.setdefault("agents", {}):
            raise AgentNotFoundError(f"agent not found: {agent_id}")

