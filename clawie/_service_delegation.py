"""Recursive delegation, session agents, and maintenance automation (ZeroClawService mixin)."""
from __future__ import annotations

import os
import shutil
from typing import Any
from clawie.service_common import SetupError


class DelegationOpsMixin:

    @classmethod
    def _seed_delegation_skill(
        cls,
        core_prompts: dict[str, str],
        plugins: dict[str, bool],
    ) -> None:
        if not plugins.get("delegation", False):
            core_prompts.pop("DELEGATION.md", None)
            return
        if not core_prompts.get("DELEGATION.md"):
            try:
                from clawie.delegation import DELEGATION_SKILL_CONTENT

                core_prompts["DELEGATION.md"] = DELEGATION_SKILL_CONTENT
            except ImportError:
                pass
        # Ensure AGENTS.md tells the bot to read DELEGATION.md on startup.
        agents_md = core_prompts.get("AGENTS.md", "")
        if agents_md and cls._DELEGATION_AGENTS_MARKER not in agents_md:
            # Insert after the "4. **If in MAIN SESSION**" line or after
            # the last numbered step in the "Every Session" section.
            insertion_point = agents_md.find("\n\nDon't ask permission")
            if insertion_point == -1:
                insertion_point = agents_md.find("\n\n## Memory")
            if insertion_point != -1:
                core_prompts["AGENTS.md"] = (
                    agents_md[:insertion_point]
                    + "\n" + cls._DELEGATION_AGENTS_SNIPPET + "\n"
                    + agents_md[insertion_point:]
                )
            else:
                # Fallback: append to end
                core_prompts["AGENTS.md"] = agents_md + "\n\n" + cls._DELEGATION_AGENTS_SNIPPET + "\n"

    # ── Delegation methods ────────────────────────────────────────────────

    def delegate_task(
        self,
        parent_id: str,
        child_id: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 300.0,
        model_tier: str = "",
    ) -> dict[str, Any]:
        from clawie.delegation import DelegationCoordinator, DelegationBus, DelegationTree, DEFAULT_TIER

        tier = model_tier or DEFAULT_TIER
        task_id = str(__import__("uuid").uuid4().hex)
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload or {},
            depth=0,
            timeout_seconds=timeout,
            model_tier=tier,
        )
        bus = DelegationBus(parent_id)
        tree = DelegationTree()
        coordinator = DelegationCoordinator(parent_id, bus, tree, model_tier=tier)
        try:
            result = coordinator.delegate(child_id, payload or {}, timeout=timeout)
        except Exception as exc:
            self.store.write_delegation_task(
                task_id=task_id,
                parent_agent_id=parent_id,
                child_agent_id=child_id,
                payload=payload or {},
                depth=0,
                timeout_seconds=timeout,
                status="failed",
                error=str(exc),
                model_tier=tier,
            )
            state = self.store.read_state()
            self._event(
                state,
                "delegation.failed",
                f"Delegation {parent_id}->{child_id} failed: {exc}",
                {"task_id": task_id, "parent": parent_id, "child": child_id},
            )
            self.store.write_state(state)
            return {"task_id": task_id, "status": "failed", "error": str(exc)}
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload or {},
            depth=0,
            timeout_seconds=timeout,
            status="completed",
            result=result,
            model_tier=tier,
        )
        tree_data = tree.to_dict()
        self.store.write_delegation_tree(parent_id, tree_data)
        state = self.store.read_state()
        self._event(
            state,
            "delegation.completed",
            f"Delegation {parent_id}->{child_id} completed",
            {"task_id": task_id, "parent": parent_id, "child": child_id},
        )
        self.store.write_state(state)
        return {"task_id": task_id, "status": "completed", "result": result}

    def delegation_tree(self, root_agent_id: str) -> dict[str, Any]:
        return self.store.read_delegation_tree(root_agent_id) or {}

    def delegation_tree_lines(self, root_agent_id: str) -> list[str]:
        from clawie.delegation import render_tree_ascii

        tree_data = self.store.read_delegation_tree(root_agent_id) or {}
        if not tree_data:
            return []
        return render_tree_ascii(tree_data, root_agent_id)

    def delegation_tasks(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.store.read_delegation_tasks(
            parent_agent_id=agent_id,
            status=status,
            limit=limit,
        )

    def cleanup_delegation(self) -> dict[str, Any]:
        from clawie.delegation import cleanup_stale_sockets, list_active_agents

        removed = cleanup_stale_sockets()
        active = list_active_agents()
        return {"removed_sockets": removed, "active_agents": active}

    def active_delegation_agents(self) -> list[dict[str, Any]]:
        """Read-only list of agents with live delegation sockets.

        Unlike :meth:`cleanup_delegation`, this never removes stale sockets, so
        it is safe to call from read-only callers such as ``clawie status``.
        """
        from clawie.delegation import list_active_agents

        return list_active_agents()

    # ── Maintenance cron ──────────────────────────────────────────────────

    def maintenance_enable(self, *, interval_hours: int = 4) -> dict[str, Any]:
        """Install a system cron job that periodically syncs agent credentials."""
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("maintenance enable requires root. Re-run with sudo.")
        clawie_bin = shutil.which("clawie") or "/usr/local/bin/clawie"
        hour_spec = f"*/{interval_hours}" if interval_hours < 24 else "0"
        cron_content = (
            "# Managed by clawie -- do not edit manually\n"
            "SHELL=/bin/bash\n"
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ":/home/linuxbrew/.linuxbrew/bin\n"
            f"0 {hour_spec} * * * root {clawie_bin} maintenance run"
            f" >> {self.MAINTENANCE_LOG_FILE} 2>&1\n"
        )
        self.MAINTENANCE_CRON_FILE.write_text(cron_content, encoding="utf-8")
        os.chmod(str(self.MAINTENANCE_CRON_FILE), 0o644)
        config = self.store.read_config()
        config["maintenance_cron_enabled"] = True
        config["maintenance_cron_interval_hours"] = interval_hours
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(state, "maintenance.enabled", f"Maintenance cron enabled (every {interval_hours}h)", {
            "interval_hours": interval_hours, "cron_file": str(self.MAINTENANCE_CRON_FILE),
        })
        self.store.write_state(state)
        return {"enabled": True, "cron_file": str(self.MAINTENANCE_CRON_FILE),
                "interval_hours": interval_hours, "clawie_binary": clawie_bin}

    def maintenance_disable(self) -> dict[str, Any]:
        """Remove the maintenance cron job."""
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("maintenance disable requires root. Re-run with sudo.")
        removed = self.MAINTENANCE_CRON_FILE.exists()
        self.MAINTENANCE_CRON_FILE.unlink(missing_ok=True)
        config = self.store.read_config()
        config["maintenance_cron_enabled"] = False
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(state, "maintenance.disabled", "Maintenance cron disabled", {})
        self.store.write_state(state)
        return {"enabled": False, "removed": removed}

    def maintenance_run(self) -> dict[str, Any]:
        """Run maintenance tasks: sync credentials and apply staged prompts for all managed agents."""
        self._require_setup()

        # First, refresh the shared auth store from the freshest source (codex).
        # This converts codex OAuth tokens into openclaw/picoclaw auth-profiles
        # so agents get a live token instead of a stale copy.
        src_home = self._default_source_home()
        auth_refresh = "skipped"
        for source_type in ("codex", "claude"):
            try:
                self.import_shared_auth("openclaw", source=source_type, source_home=str(src_home))
                auth_refresh = f"ok ({source_type} from {src_home})"
                break
            except Exception:
                continue

        state = self.store.read_state()
        agents = state.get("agents", state.get("users", {}))
        results: dict[str, dict[str, str]] = {}
        errors = 0
        skipped = 0

        for agent_id, agent in agents.items():
            if not isinstance(agent, dict):
                continue
            info = agent.get("agent", {})
            linux_user = str(info.get("linux_user", "")).strip()
            if not linux_user:
                skipped += 1
                continue
            sync_cfg = agent.get("credential_sync", {})
            bundles = sync_cfg.get("bundles", []) if isinstance(sync_cfg, dict) else []

            entry: dict[str, str] = {}

            # Credential sync
            if bundles:
                try:
                    self.sync_agent_credentials(agent_id)
                    entry["credentials"] = "ok"
                except Exception as exc:
                    entry["credentials"] = f"error: {exc}"
                    errors += 1
            else:
                entry["credentials"] = "skipped (no bundles)"

            # Apply staged prompts
            try:
                applied = self.apply_staged_prompts(agent_id)
                count = len(applied.get("applied", []))
                entry["prompts"] = f"ok ({count} applied)" if count else "ok (none staged)"
            except Exception as exc:
                entry["prompts"] = f"error: {exc}"
                errors += 1

            results[agent_id] = entry

        self._event(state, "maintenance.run", f"Maintenance run: {len(results)} agents, {errors} errors", {
            "agents_processed": len(results), "skipped": skipped, "errors": errors,
        })
        self.store.write_state(state)
        return {
            "auth_refresh": auth_refresh,
            "agents_processed": len(results),
            "agents_skipped": skipped,
            "errors": errors,
            "results": results,
        }

    def _get_session_manager(self, parent_id: str) -> Any:
        if parent_id not in self._session_managers:
            from clawie.delegation import SessionAgentManager

            self._session_managers[parent_id] = SessionAgentManager(parent_id)
        return self._session_managers[parent_id]

    def session_tree_lines(self, parent_id: str) -> list[str]:
        if parent_id not in self._session_managers:
            return []
        return self._session_managers[parent_id].tree_lines()
