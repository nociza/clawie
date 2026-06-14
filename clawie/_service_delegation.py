"""Recursive delegation, session agents, and maintenance automation (ClawieService mixin)."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from typing import Any, Callable
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
        """Run maintenance tasks: sync credentials and write configured prompts for all managed agents."""
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

            # Write configured prompts directly into the agent workspace.
            try:
                applied = self.apply_staged_prompts(agent_id)
                count = len(applied.get("applied", []))
                entry["prompts"] = f"ok ({count} applied)" if count else "ok (no changes)"
            except Exception as exc:
                entry["prompts"] = f"error: {exc}"
                errors += 1

            results[agent_id] = entry

        # Knowledge backup: keep the git backup repo current on every
        # maintenance pass so it stays continuously maintained.
        backup_summary = "disabled"
        if bool(self.store.read_config().get("backup_enabled", False)):
            try:
                outcome = self.backup_run()
                if outcome.get("changed"):
                    backup_summary = f"ok (commit {str(outcome.get('commit', ''))[:10]})"
                else:
                    backup_summary = "ok (no changes)"
                if outcome.get("push_error"):
                    backup_summary += f"; push failed: {outcome['push_error']}"
            except Exception as exc:
                backup_summary = f"error: {exc}"
                errors += 1

        # Re-read state before recording the summary event: the per-agent
        # operations above write state themselves, and writing the stale
        # snapshot read at the top would clobber their updates.
        state = self.store.read_state()
        self._event(state, "maintenance.run", f"Maintenance run: {len(results)} agents, {errors} errors", {
            "agents_processed": len(results), "skipped": skipped, "errors": errors,
            "backup": backup_summary,
        })
        self.store.write_state(state)
        return {
            "auth_refresh": auth_refresh,
            "agents_processed": len(results),
            "agents_skipped": skipped,
            "errors": errors,
            "backup": backup_summary,
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

    # ── Gateway delivery bridge ───────────────────────────────────────────
    #
    # The real delegation path: deliver a task to a managed agent through its
    # live gateway and return the reply. Unlike the legacy Unix-socket REPL
    # (which only echoes), this resolves the agent's provider adapter and runs
    # the adapter's delivery command as the agent's Linux user.

    def deliver_to_agent(
        self,
        agent_id: str,
        message: str,
        *,
        tier: str = "balanced",
        timeout: float = 300.0,
        run: Callable[[list[str]], str] | None = None,
    ) -> dict[str, Any]:
        """Deliver one task to *agent_id* via its gateway; return the parsed reply.

        *run* is the injection point for tests (it takes the adapter's argv and
        returns stdout). In production it defaults to a runner that executes the
        command in the agent user's service environment.
        """
        from clawie.adapters import Task, get_adapter

        agent = self.get_agent(agent_id)
        info = agent.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        linux_user = str(info.get("linux_user", "")).strip()
        adapter = get_adapter(provider)

        task = Task(task_id=uuid.uuid4().hex, message=str(message), tier=str(tier or "balanced"))
        cmd = adapter.deliver_command(agent_id, task, timeout=timeout)
        runner = run or self._default_deliver_runner(linux_user)
        stdout = runner(cmd)
        reply = adapter.parse_reply(stdout)

        result = {
            "agent_id": agent_id,
            "task_id": task.task_id,
            "ok": reply.ok,
            "output": reply.output,
            "error": reply.error,
            "usage": reply.usage,
            "delivery_status": reply.delivery_status,
        }
        state = self.store.read_state()
        self._event(
            state,
            "delegation.delivered" if reply.ok else "delegation.delivery_failed",
            f"Delivered task to {agent_id}" if reply.ok else f"Delivery to {agent_id} failed",
            {"agent_id": agent_id, "task_id": task.task_id, "ok": reply.ok, "tier": task.tier},
        )
        self.store.write_state(state)
        return result

    def _default_deliver_runner(self, linux_user: str) -> Callable[[list[str]], str]:
        """Run an adapter delivery command in the agent user's service env."""

        def _run(cmd: list[str]) -> str:
            executable = self._resolve_provider_executable("openclaw")
            argv = [executable, *list(cmd)[1:]]
            wrapped = self._wrap_user_command(argv, linux_user, purpose="agent delegation")
            result = subprocess.run(
                wrapped,
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(linux_user),
            )
            if result.returncode != 0 and not str(result.stdout).strip():
                detail = (result.stderr or "").strip() or f"exit {result.returncode}"
                raise SetupError(f"openclaw agent delivery failed: {detail}")
            return result.stdout

        return _run
