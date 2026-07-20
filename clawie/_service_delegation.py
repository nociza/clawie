"""Recursive delegation, session agents, and maintenance automation (ClawieService mixin)."""
from __future__ import annotations

import json
import math
import os
import signal
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from clawie.service_common import AgentNotFoundError, SetupError, now_iso


class DelegationOpsMixin:

    def _delegation_depth_limit(self, agent_id: str) -> int:
        from clawie.delegation import MAX_RECURSION_DEPTH

        try:
            agent = self.get_agent(agent_id)
        except AgentNotFoundError:
            return MAX_RECURSION_DEPTH
        info = agent.get("agent", {}) if isinstance(agent.get("agent"), dict) else {}
        limits = info.get("limits", {}) if isinstance(info.get("limits"), dict) else {}
        value = limits.get("delegation_depth", MAX_RECURSION_DEPTH)
        if isinstance(value, bool) or not isinstance(value, int):
            return MAX_RECURSION_DEPTH
        return min(max(value, 1), MAX_RECURSION_DEPTH)

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

    @staticmethod
    def _delegation_payload_message(payload: dict[str, Any]) -> str:
        for key in ("task", "message", "prompt"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, indent=2, sort_keys=True)

    def _gateway_task_handler(self, executor_agent_id: str) -> Any:
        executor = self._validate_agent_id(executor_agent_id)
        self.get_agent(executor)

        def _handle(msg: Any, repl: Any) -> dict[str, Any]:
            result = self.deliver_to_agent(
                executor,
                self._delegation_payload_message(dict(msg.payload)),
                tier=str(repl.model_tier or "balanced"),
                timeout=float(repl.timeout),
            )
            if not bool(result.get("ok", False)):
                raise SetupError(str(result.get("error", "gateway delivery failed")))
            return result

        return _handle

    def delegate_task(
        self,
        parent_id: str,
        child_id: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 300.0,
        model_tier: str = "",
    ) -> dict[str, Any]:
        from clawie.delegation import (
            DEFAULT_TIER,
            DelegationBus,
            DelegationCoordinator,
            DelegationTree,
        )

        parent_id = self._validate_agent_id(parent_id)
        child_id = self._validate_agent_id(child_id)
        if parent_id == child_id:
            raise ValueError("parent and child agent ids must differ")
        depth_limit = self._delegation_depth_limit(parent_id)
        if 1 >= depth_limit:
            raise ValueError(
                f"max recursion depth ({depth_limit}) exceeded at depth=1"
            )
        tier = model_tier or DEFAULT_TIER
        task_id = uuid.uuid4().hex
        created_at = now_iso()
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload or {},
            depth=0,
            created_at=created_at,
            timeout_seconds=timeout,
            model_tier=tier,
        )
        if not self._socket_alive(self._delegation_socket_path(child_id)):
            try:
                self.get_agent(child_id)
            except AgentNotFoundError:
                pass
            else:
                return self._deliver_managed_delegation(
                    task_id=task_id,
                    parent_id=parent_id,
                    child_id=child_id,
                    payload=payload or {},
                    timeout=timeout,
                    tier=tier,
                    created_at=created_at,
                )
        bus = DelegationBus(parent_id)
        tree = DelegationTree(max_depth=depth_limit)
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
                created_at=created_at,
                completed_at=now_iso(),
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
        tree_data = tree.to_dict()
        self.store.write_delegation_tree(parent_id, tree_data)
        clean_result = dict(result)
        child_node = tree.get_node(child_id)
        failed = child_node is None or child_node.status in {"failed", "timeout"}
        if failed:
            status_detail = child_node.status if child_node is not None else "failed"
            error = str(clean_result.get("error") or f"delegation {status_detail}")
            self.store.write_delegation_task(
                task_id=task_id,
                parent_agent_id=parent_id,
                child_agent_id=child_id,
                payload=payload or {},
                depth=0,
                timeout_seconds=timeout,
                status="failed",
                result=clean_result,
                error=error,
                created_at=created_at,
                completed_at=now_iso(),
                model_tier=tier,
            )
            state = self.store.read_state()
            self._event(
                state,
                "delegation.failed",
                f"Delegation {parent_id}->{child_id} failed: {error}",
                {"task_id": task_id, "parent": parent_id, "child": child_id},
            )
            self.store.write_state(state)
            return {
                "task_id": task_id,
                "status": "failed",
                "error": error,
                "result": clean_result,
            }
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload or {},
            depth=0,
            timeout_seconds=timeout,
            status="completed",
            result=clean_result,
            created_at=created_at,
            completed_at=now_iso(),
            model_tier=tier,
        )
        state = self.store.read_state()
        self._event(
            state,
            "delegation.completed",
            f"Delegation {parent_id}->{child_id} completed",
            {"task_id": task_id, "parent": parent_id, "child": child_id},
        )
        self.store.write_state(state)
        return {"task_id": task_id, "status": "completed", "result": clean_result}

    def _deliver_managed_delegation(
        self,
        *,
        task_id: str,
        parent_id: str,
        child_id: str,
        payload: dict[str, Any],
        timeout: float,
        tier: str,
        created_at: str,
    ) -> dict[str, Any]:
        from clawie.delegation import DelegationTree

        error = ""
        try:
            result = self.deliver_to_agent(
                child_id,
                self._delegation_payload_message(payload),
                tier=tier,
                timeout=timeout,
            )
            if not bool(result.get("ok", False)):
                error = str(result.get("error", "gateway delivery failed"))
        except Exception as exc:  # noqa: BLE001 - persist delivery failures.
            result = {"ok": False, "error": str(exc)}
            error = str(exc)

        status = "failed" if error else "completed"
        completed_at = now_iso()
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload,
            depth=1,
            timeout_seconds=timeout,
            status=status,
            result=result,
            error=error,
            created_at=created_at,
            completed_at=completed_at,
            model_tier=tier,
        )
        tree = DelegationTree(max_depth=self._delegation_depth_limit(parent_id))
        tree.register(parent_id, "", f"{task_id}:root", depth=0, model_tier=tier)
        tree.update_status(parent_id, "running")
        tree.register(child_id, parent_id, task_id, depth=1, model_tier=tier)
        tree.update_status(child_id, status)
        self.store.write_delegation_tree(parent_id, tree.to_dict())
        state = self.store.read_state()
        event_type = "delegation.failed" if error else "delegation.completed"
        message = (
            f"Delegation {parent_id}->{child_id} failed: {error}"
            if error
            else f"Delegation {parent_id}->{child_id} completed through the live gateway"
        )
        self._event(
            state,
            event_type,
            message,
            {"task_id": task_id, "parent": parent_id, "child": child_id, "gateway": True},
        )
        self.store.write_state(state)
        response = {"task_id": task_id, "status": status, "result": result}
        if error:
            response["error"] = error
        return response

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

        # First, refresh the shared auth store from the freshest source.
        # This converts source OAuth tokens into provider-native shared auth
        # stores so agents get a live token instead of a stale copy.
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
        agents = state.get("agents", {})
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

            self._session_managers[parent_id] = SessionAgentManager(
                parent_id,
                max_depth=self._delegation_depth_limit(parent_id),
            )
        return self._session_managers[parent_id]

    def session_tree_lines(self, parent_id: str) -> list[str]:
        if parent_id not in self._session_managers:
            records = self.list_session_agents(parent_id)
            if not records:
                data = self.store.read_delegation_tree(parent_id) or {}
                if not data:
                    return []
                from clawie.delegation import render_tree_ascii

                return render_tree_ascii(data, root_id=parent_id)
            data = self._session_tree_data(parent_id, records)
            from clawie.delegation import render_tree_ascii

            return render_tree_ascii(data, root_id=parent_id)
        return self._session_managers[parent_id].tree_lines()

    def _delegation_socket_path(self, agent_id: str) -> Path:
        from clawie.delegation import DELEGATION_DIR

        return DELEGATION_DIR / f"{agent_id}.sock"

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return False
        except ChildProcessError:
            pass
        except OSError:
            pass
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def _socket_alive(self, path: str | Path) -> bool:
        socket_path = Path(path)
        if not socket_path.exists():
            return False
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            sock.connect(str(socket_path))
            return True
        except OSError:
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _wait_for_session_socket(
        self,
        agent_id: str,
        *,
        timeout: float,
        pid: int = 0,
    ) -> bool:
        deadline = time.monotonic() + max(0.1, timeout)
        path = self._delegation_socket_path(agent_id)
        while time.monotonic() < deadline:
            if pid > 0 and not self._pid_alive(pid):
                return False
            if self._socket_alive(path):
                return True
            time.sleep(0.02)
        return False

    def _session_record_with_liveness(self, record: dict[str, Any]) -> dict[str, Any]:
        item = dict(record)
        pid = int(item.get("pid", 0) or 0)
        socket_path = str(item.get("socket_path") or self._delegation_socket_path(str(item.get("child_agent_id", ""))))
        socket_is_alive = self._socket_alive(socket_path)
        pid_is_alive = self._pid_alive(pid) if pid else socket_is_alive
        running = bool(socket_is_alive and pid_is_alive)
        item["running"] = running
        item["socket_alive"] = socket_is_alive
        item["pid_alive"] = pid_is_alive
        item["agent_id"] = str(item.get("child_agent_id", ""))
        item["parent_id"] = str(item.get("parent_agent_id", ""))
        item["session"] = True
        if running:
            item["status"] = "running"
        elif str(item.get("status", "")) == "stopped":
            item["status"] = "stopped"
        else:
            item["status"] = "stale"
        return item

    def _session_tree_data(
        self,
        parent_id: str,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from clawie.delegation import DelegationTree

        tree = DelegationTree()
        tree.register(parent_id, "", "root", depth=0)
        tree.update_status(parent_id, "running")
        for record in records:
            child_id = str(record.get("agent_id") or record.get("child_agent_id") or "")
            if not child_id:
                continue
            depth = int(record.get("depth", 1) or 1)
            model_tier = str(record.get("model_tier", "") or "")
            try:
                tree.register(
                    child_id,
                    parent_id,
                    f"session:{child_id}",
                    depth=depth,
                    model_tier=model_tier,
                )
            except ValueError:
                continue
            tree.update_status(child_id, "running" if record.get("running") else "failed")
        return tree.to_dict()

    def _persist_session_tree(self, parent_id: str) -> None:
        records = [
            self._session_record_with_liveness(row)
            for row in self.store.read_session_agents(parent_id)
        ]
        if records:
            self.store.write_delegation_tree(parent_id, self._session_tree_data(parent_id, records))

    def _mark_delegation_tree_status(
        self,
        root_agent_id: str,
        agent_id: str,
        status: str,
    ) -> None:
        data = self.store.read_delegation_tree(root_agent_id) or {}
        if not data:
            return
        from clawie.delegation import DelegationTree

        tree = DelegationTree.from_dict(data)
        if tree.get_node(agent_id) is None:
            return
        tree.update_status(agent_id, status)
        self.store.write_delegation_tree(root_agent_id, tree.to_dict())

    def _shutdown_session_process(self, parent_id: str, child_id: str, pid: int) -> None:
        from clawie.delegation import DelegationCoordinator

        if self._socket_alive(self._delegation_socket_path(child_id)):
            coordinator = DelegationCoordinator(parent_id)
            coordinator.shutdown_child(child_id)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not self._socket_alive(self._delegation_socket_path(child_id)):
                    return
                time.sleep(0.05)

        if pid > 0 and self._pid_alive(pid):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except OSError:
                    return
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if not self._pid_alive(pid):
                        return
                    time.sleep(0.05)

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

        requested_timeout = float(timeout)
        if not math.isfinite(requested_timeout) or requested_timeout <= 0:
            raise ValueError("delivery timeout must be a positive finite number")
        limits = info.get("limits", {}) if isinstance(info.get("limits"), dict) else {}
        configured_timeout = limits.get("gateway_timeout")
        if isinstance(configured_timeout, int) and not isinstance(configured_timeout, bool):
            effective_timeout = min(requested_timeout, float(configured_timeout))
        else:
            effective_timeout = requested_timeout

        task = Task(task_id=uuid.uuid4().hex, message=str(message), tier=str(tier or "balanced"))
        cmd = adapter.deliver_command(agent_id, task, timeout=effective_timeout)
        runner = run or self._default_deliver_runner(
            provider,
            linux_user,
            timeout=effective_timeout,
        )
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
            "transport": str(reply.raw.get("meta", {}).get("transport", ""))
            if isinstance(reply.raw.get("meta"), dict)
            else "",
            "fallback_from": str(reply.raw.get("meta", {}).get("fallbackFrom", ""))
            if isinstance(reply.raw.get("meta"), dict)
            else "",
            "timeout_seconds": effective_timeout,
        }
        state = self.store.read_state()
        self._event(
            state,
            "delegation.delivered" if reply.ok else "delegation.delivery_failed",
            f"Delivered task to {agent_id}" if reply.ok else f"Delivery to {agent_id} failed",
            {
                "agent_id": agent_id,
                "task_id": task.task_id,
                "ok": reply.ok,
                "tier": task.tier,
                "timeout_seconds": effective_timeout,
            },
        )
        self.store.write_state(state)
        return result

    def _default_deliver_runner(
        self,
        provider: str,
        linux_user: str,
        *,
        timeout: float,
    ) -> Callable[[list[str]], str]:
        """Run an adapter delivery command in the agent user's service env."""
        provider_name = str(provider).strip().lower()

        def _run(cmd: list[str]) -> str:
            executable = self._resolve_provider_executable(provider_name)
            argv = [executable, *list(cmd)[1:]]
            wrapped = self._wrap_user_command(argv, linux_user, purpose="agent delegation")
            try:
                result = subprocess.run(
                    wrapped,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._service_env(linux_user),
                    timeout=timeout + 10.0,
                )
            except subprocess.TimeoutExpired as exc:
                raise SetupError(
                    f"{provider_name} agent delivery exceeded {timeout:g}s timeout"
                ) from exc
            if result.returncode != 0 and not str(result.stdout).strip():
                detail = (result.stderr or "").strip() or f"exit {result.returncode}"
                raise SetupError(f"{provider_name} agent delivery failed: {detail}")
            return result.stdout

        return _run
