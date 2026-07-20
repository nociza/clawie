"""Foreground clawied runtime: manifest reconcile host and status files."""
from __future__ import annotations

import json
import hashlib
import os
import pwd
import select
import signal
import socket
import stat
import struct
import tempfile
import time
import threading
from pathlib import Path
from typing import Any

from clawie import __version__
from clawie.control import ControlGate
from clawie.ipc_paths import CONTROL_SOCKET_ROOT, control_socket_path
from clawie.safe_fs import read_text_under, write_text_under
from clawie.service_common import SetupError, now_iso

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


IPC_CLIENT_TIMEOUT_SECONDS = 2.0


class Clawied:
    """Small foreground daemon host for manifest reconciliation.

    The process is meant to be run under a supervisor. It does not fork or
    daemonize itself; it writes pid/status files under the configured Clawie
    state root and uses a nonblocking advisory lock to prevent concurrent
    reconcile loops.
    """

    _SERVICE_CALLS: dict[str, frozenset[str]] = {
        "agent_auth_login": frozenset({"agent_id"}),
        "agent_service_action": frozenset({"agent_id", "action"}),
        "apply_agent_addons": frozenset({"agent_id", "addons"}),
        "apply_staged_prompts": frozenset({"agent_id"}),
        "apply_shared_auth_links": frozenset({"agent_id"}),
        "backup_init": frozenset({"repo_path", "remote", "enable", "auto_push"}),
        "backup_restore": frozenset({"agent_id", "apply_to_disk", "include_workspace"}),
        "backup_run": frozenset({"message", "push"}),
        "bootstrap_channels": frozenset({"agent_id", "preset", "replace"}),
        "batch_create_agents": frozenset({"entries"}),
        "clone_agent_prompts": frozenset({"from_agent", "to_agent", "apply_to_disk"}),
        "cleanup_delegation": frozenset(),
        "configure_control_escalation": frozenset(
            {
                "github_repo",
                "github_token_path",
                "operators",
                "issue_labels",
                "rate_limit_seconds",
            }
        ),
        "create_agent": frozenset(
            {
                "agent_id",
                "display_name",
                "template",
                "clone_from",
                "channel_strategy",
                "channels",
                "agent_version",
                "provider",
                "core_prompts",
                "plugin_overrides",
            }
        ),
        "delegate_task": frozenset({"parent_id", "child_id", "payload", "timeout", "model_tier"}),
        "deliver_to_agent": frozenset({"agent_id", "message", "tier", "timeout"}),
        "delete_agent": frozenset({"agent_id"}),
        "disable_agent_addon": frozenset({"agent_id", "addon"}),
        "enable_agent_addon": frozenset(
            {"agent_id", "addon", "source_home", "source_agent", "login_if_missing"}
        ),
        "ensure_agent_permissions": frozenset({"agent_id", "manager_user"}),
        "import_shared_addon_auth": frozenset({"addon", "source_home", "source_agent"}),
        "import_shared_auth": frozenset({"provider", "source", "source_home"}),
        "import_state": frozenset({"input_path", "merge"}),
        "install_addon": frozenset({"addon"}),
        "install_provider_runtime": frozenset({"provider"}),
        "local_claw_auth_login": frozenset({"provider"}),
        "local_claw_service_action": frozenset({"provider", "action"}),
        "maintenance_disable": frozenset(),
        "maintenance_enable": frozenset({"interval_hours"}),
        "maintenance_run": frozenset(),
        "migrate_channels": frozenset({"from_agent", "to_agent", "replace"}),
        "port_shared_auth": frozenset({"from_provider", "to_provider"}),
        "purge_agent": frozenset({"agent_id"}),
        "revoke_agent_credentials": frozenset({"agent_id", "bundles"}),
        "set_agent_credential_bundles": frozenset({"agent_id", "bundles", "include_defaults"}),
        "set_agent_model_tier": frozenset({"agent_id", "tier"}),
        "setup": frozenset(
            {
                "provider",
                "api_key",
                "subscription",
                "workspace",
                "api_url",
                "auth_mode",
                "spawn_password",
                "clear_spawn_password",
                "install_runtime",
            }
        ),
        "shared_addon_auth_login": frozenset({"addon"}),
        "shared_auth_login": frozenset({"provider"}),
        "spawn_linux_user": frozenset(
            {
                "agent_id",
                "linux_user",
                "copy_configs",
                "source_home",
                "template",
                "agent_version",
                "provider",
                "password",
                "password_hash",
                "use_global_password",
                "clone_from_agent",
                "credential_bundles",
                "include_default_credentials",
                "plugin_overrides",
            }
        ),
        "stop_session_agent": frozenset({"parent_id", "child_id"}),
        "switch_agent_provider": frozenset({"agent_id", "provider"}),
        "sync_agent_credentials": frozenset({"agent_id", "source_home", "bundles", "include_defaults"}),
        "spawn_session_agent": frozenset({"parent_id", "child_id", "timeout", "model_tier", "detached"}),
    }
    def __init__(self, service: Any, *, interval_seconds: float = 60.0) -> None:
        self.service = service
        self.interval_seconds = max(1.0, float(interval_seconds))
        root = service.store.root
        self.pid_path = root / "clawied.pid"
        self.status_path = root / "clawied-status.json"
        self.lock_path = root / "clawied.lock"
        self.socket_path = self._default_socket_path(root)
        self._stop = False
        self._lock_handle: Any = None
        self._server: socket.socket | None = None
        self._control_servers: list[tuple[socket.socket, Path, int]] = []
        self._control_gate = ControlGate(allowlist=self._control_allowlist())

    def status(self) -> dict[str, Any]:
        pid = self._read_pid()
        last = self._read_json(self.status_path)
        return {
            "running": self._pid_running(pid),
            "pid": pid,
            "pid_file": str(self.pid_path),
            "status_file": str(self.status_path),
            "lock_file": str(self.lock_path),
            "socket_file": str(self.socket_path),
            "control_socket_files": [str(path) for _server, path, _uid in self._control_servers],
            "last": last,
        }

    def stop(self) -> dict[str, Any]:
        pid = self._read_pid()
        if pid <= 0 or not self._pid_running(pid):
            return {"stopped": False, "running": False, "pid": pid}
        os.kill(pid, signal.SIGTERM)
        return {"stopped": True, "running": self._pid_running(pid), "pid": pid}

    def run_once(self, *, dry_run: bool = False) -> dict[str, Any]:
        started_at = now_iso()
        results = self.service.reconcile_all_manifests(dry_run=dry_run)
        errors = sum(len(row.get("errors", [])) for row in results)
        unconverged = sum(1 for row in results if not bool(row.get("converged", False)))
        failed = errors > 0 or (unconverged > 0 and not dry_run)
        status = {
            "status": "error" if failed else "ok",
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": now_iso(),
            "interval_seconds": self.interval_seconds,
            "dry_run": bool(dry_run),
            "manifests": len(results),
            "errors": errors,
            "unconverged": unconverged,
            "results": results,
        }
        self._write_json(self.status_path, status)
        return status

    def run_forever(self, *, dry_run: bool = False, max_cycles: int | None = None) -> dict[str, Any]:
        self._acquire_lock()
        self._write_pid()
        self._start_ipc()
        previous_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_stop_signal)

        cycles = 0
        last: dict[str, Any] = {}
        try:
            while not self._stop:
                last = self.run_once(dry_run=dry_run)
                self._refresh_control_ipc()
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                self._sleep_until_next_cycle()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            self._stop_ipc()
            self._remove_pid()
            self._release_lock()
        return {
            "status": "stopped" if self._stop else str(last.get("status", "ok") or "ok"),
            "pid": os.getpid(),
            "cycles": cycles,
            "last": last,
        }

    def _handle_stop_signal(self, _signum: int, _frame: Any) -> None:
        self._stop = True

    def _sleep_until_next_cycle(self) -> None:
        deadline = time.monotonic() + self.interval_seconds
        while not self._stop and time.monotonic() < deadline:
            self._serve_ipc_once(timeout=min(0.5, max(0.0, deadline - time.monotonic())))

    def request(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send one command to a running clawied instance over its Unix socket."""
        request = {"command": str(command), "payload": payload or {}}
        override = str(os.environ.get("CLAWIE_CONTROL_SOCKET", "")).strip()
        request_socket = Path(override).expanduser() if override else self.socket_path
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(str(request_socket))
            client.sendall(json.dumps(request, sort_keys=True).encode("utf-8"))
            client.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            client.close()
        if not chunks:
            raise SetupError("clawied IPC returned an empty response")
        try:
            response = json.loads(b"".join(chunks).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SetupError(f"clawied IPC returned invalid JSON: {exc}") from exc
        if not isinstance(response, dict):
            raise SetupError("clawied IPC returned a non-object response")
        if not response.get("ok", False):
            error = str(response.get("error", "clawied IPC request failed"))
            raise SetupError(error)
        result = response.get("result", {})
        return result if isinstance(result, dict) else {"result": result}

    def _start_ipc(self) -> None:
        self.service.store.ensure()
        self._ensure_socket_parent()
        if self.socket_path.exists() or self.socket_path.is_symlink():
            existing = self.socket_path.lstat()
            if not stat.S_ISSOCK(existing.st_mode):
                raise SetupError(f"refusing to replace non-socket clawied IPC path: {self.socket_path}")
            allowed_owners = {os.geteuid(), self._state_owner()[0]}
            if int(existing.st_uid) not in allowed_owners:
                raise SetupError(f"refusing to replace clawied socket owned by another user: {self.socket_path}")
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            owner = self._state_owner()
            if os.geteuid() == 0:
                os.chown(self.socket_path, owner[0], owner[1])
            server.listen(8)
            server.settimeout(0.5)
        except Exception:
            server.close()
            raise
        self._server = server
        self._refresh_control_ipc()

    def _stop_ipc(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        for control_server, path, _uid in self._control_servers:
            control_server.close()
            self._unlink_control_socket(path, _uid)
        self._control_servers = []
        if self.socket_path.exists() or self.socket_path.is_symlink():
            self.socket_path.unlink()

    def _serve_ipc_once(self, *, timeout: float = 0.5) -> None:
        server = self._server
        if server is None:
            time.sleep(timeout)
            return
        servers = [server, *[item[0] for item in self._control_servers]]
        try:
            ready, _writable, _errors = select.select(servers, [], [], max(0.0, timeout))
        except OSError:
            return
        if not ready:
            return
        selected = ready[0]
        try:
            conn, _ = selected.accept()
        except OSError:
            return
        with conn:
            # A local peer must not be able to freeze the single-writer
            # reconcile loop by opening a socket and withholding EOF.
            conn.settimeout(IPC_CLIENT_TIMEOUT_SECONDS)
            peer_uid = self._peer_uid(conn)
            control_entry = next(
                (item for item in self._control_servers if item[0] is selected),
                None,
            )
            scope = "control" if control_entry is not None else "operator"
            allowed_uids = (
                {control_entry[2]} if control_entry is not None else {os.geteuid(), self._state_owner()[0]}
            )
            if peer_uid is None or peer_uid not in allowed_uids:
                try:
                    conn.sendall(b'{"ok":false,"error":"clawied IPC peer is not authorized"}')
                except OSError:
                    pass
                return
            response = self._handle_ipc_connection(conn, scope=scope, peer_uid=peer_uid)
            try:
                conn.sendall(json.dumps(self._json_safe(response), sort_keys=True).encode("utf-8"))
            except BrokenPipeError:
                return

    def _handle_ipc_connection(
        self,
        conn: socket.socket,
        *,
        scope: str,
        peer_uid: int,
    ) -> dict[str, Any]:
        try:
            request_bytes = self._recv_all(conn, max_bytes=1_000_000)
            request = json.loads(request_bytes.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            command = str(request.get("command", "")).strip()
            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            result = self._handle_ipc_command(command, payload, scope=scope, peer_uid=peer_uid)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 - return errors to the client.
            return {"ok": False, "error": str(exc)}

    def _handle_ipc_command(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        scope: str = "operator",
        peer_uid: int | None = None,
    ) -> dict[str, Any]:
        if scope == "control" and command != "control_request":
            raise PermissionError("control-agent IPC only permits control_request")
        if command == "status":
            result = self.status()
            result["via"] = "ipc"
            return result
        if command == "stop":
            self._stop = True
            return {"stopped": True, "running": False, "pid": os.getpid(), "via": "ipc"}
        if command == "run_once":
            return self.run_once(dry_run=bool(payload.get("dry_run", False)))
        if command == "reconcile":
            dry_run = bool(payload.get("dry_run", False))
            manifest = str(payload.get("manifest", "") or "").strip()
            agent = str(payload.get("agent", "") or "").strip()
            if manifest and agent:
                raise ValueError("use either manifest or agent, not both")
            if manifest:
                result = self.service.reconcile_agent_manifest(Path(manifest), dry_run=dry_run)
            elif agent:
                result = self.service.reconcile_agent_manifest(self.service.agent_manifest_path(agent), dry_run=dry_run)
            else:
                result = {"results": self.service.reconcile_all_manifests(dry_run=dry_run)}
            return {"via": "ipc", "dry_run": dry_run, **(result if isinstance(result, dict) else {"result": result})}
        if command == "service_call":
            return self._handle_service_call(payload)
        if command == "control_request":
            return self._handle_control_request(payload)
        if command == "control_confirm":
            if peer_uid is None:
                raise PermissionError("control confirmation requires an authenticated local peer")
            return self._handle_control_confirm(payload, peer_uid=peer_uid)
        raise ValueError(f"unsupported clawied IPC command: {command}")

    def _handle_service_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = str(payload.get("method", "") or "").strip()
        allowed = self._SERVICE_CALLS.get(method)
        if allowed is None:
            raise ValueError(f"unsupported clawied service method: {method}")
        kwargs = payload.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise ValueError("service_call kwargs must be a JSON object")
        extra = sorted(str(key) for key in kwargs if str(key) not in allowed)
        if extra:
            raise ValueError(f"unsupported service_call argument(s) for {method}: {', '.join(extra)}")
        func = getattr(self.service, method, None)
        if not callable(func):
            raise ValueError(f"service method is unavailable: {method}")
        result = func(**kwargs)
        return {"via": "ipc", "method": method, "result": self._json_safe(result)}

    def _handle_control_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        verb = str(payload.get("verb", "") or "").strip()
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("control_request args must be a JSON object")
        gate = self._control_gate.authorize(verb, args)
        response: dict[str, Any] = {
            "via": "ipc",
            "verb": gate.verb,
            "tier": gate.tier.value,
            "decision": gate.decision.value,
            "allowed": gate.allowed,
            "reason": gate.reason,
        }
        if gate.nonce:
            response["nonce"] = gate.nonce
        if gate.allowed:
            response["result"] = self._json_safe(self._execute_control_verb(gate.verb, args))
        self._record_control_event("control.request", response)
        return response

    def _handle_control_confirm(self, payload: dict[str, Any], *, peer_uid: int) -> dict[str, Any]:
        verb = str(payload.get("verb", "") or "").strip()
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("control_confirm args must be a JSON object")
        self._control_gate.set_allowlist(self._control_allowlist())
        confirmer = self._authenticated_operator_principal(peer_uid)
        gate = self._control_gate.confirm(
            str(payload.get("nonce", "") or ""),
            confirmer=confirmer,
            verb=verb,
            args=args,
        )
        response: dict[str, Any] = {
            "via": "ipc",
            "verb": gate.verb,
            "tier": gate.tier.value,
            "decision": gate.decision.value,
            "allowed": gate.allowed,
            "reason": gate.reason,
            "confirmer": confirmer,
        }
        if gate.allowed:
            response["result"] = self._json_safe(self._execute_control_verb(gate.verb, args))
        self._record_control_event("control.confirm", response)
        return response

    def _execute_control_verb(self, verb: str, args: dict[str, Any]) -> Any:
        if verb == "status":
            sections = args.get("sections")
            if sections is not None and not isinstance(sections, list):
                raise ValueError("status sections must be a list")
            return self.service.status_snapshot(
                agent_id=str(args.get("agent_id", "") or "") or None,
                sections=sections,
                refresh=bool(args.get("refresh", False)),
            )
        if verb == "logs":
            return {"events": self.service.list_events(limit=int(args.get("limit", 20) or 20))}
        if verb == "list":
            return {"agents": self.service.list_agents()}
        if verb == "tree":
            return {"lines": self.service.delegation_tree_lines(str(args.get("agent_id", "") or ""))}
        if verb == "version":
            return {"clawie": __version__, "openclaw": self.service.openclaw_version_gate()}
        if verb == "restart":
            return self.service.agent_service_action(str(args.get("agent_id", "") or ""), "restart")
        if verb == "apply_prompts":
            return self.service.apply_staged_prompts(str(args.get("agent_id", "") or ""))
        if verb == "sync_auth":
            forbidden = sorted(
                key
                for key in ("source_home", "bundles", "include_defaults")
                if key in args
            )
            if forbidden:
                raise PermissionError(
                    "autonomous sync_auth uses the agent's stored credential policy; "
                    "policy/source overrides require an operator command"
                )
            return self.service.sync_agent_credentials(
                str(args.get("agent_id", "") or ""),
            )
        if verb == "backup":
            if "push" in args and args.get("push") is not False and args.get("push") is not None:
                raise PermissionError(
                    "autonomous control backups are local-only; use the operator backup command to push"
                )
            return self.service.backup_run(
                message=str(args.get("message", "") or ""),
                # Override even an operator-configured automatic push policy:
                # a prompt-injected control agent must never create outward
                # network writes through a SAFE_HEAL verb.
                push=False,
            )
        if verb == "reconcile":
            dry_run = bool(args.get("dry_run", False))
            manifest = str(args.get("manifest", "") or "").strip()
            agent = str(args.get("agent", "") or "").strip()
            if manifest and agent:
                raise ValueError("use either manifest or agent, not both")
            if manifest:
                raise PermissionError(
                    "autonomous reconcile only accepts manager-owned stored manifests"
                )
            if agent:
                return self.service.reconcile_agent_manifest(self.service.agent_manifest_path(agent), dry_run=dry_run)
            return {"results": self.service.reconcile_all_manifests(dry_run=dry_run)}
        if verb == "delete_agent":
            self.service.delete_agent(str(args.get("agent_id", "") or ""))
            return {"deleted": True, "agent_id": str(args.get("agent_id", "") or "")}
        if verb == "purge_agent":
            return self.service.purge_agent(str(args.get("agent_id", "") or ""))
        if verb == "set_credentials":
            return self.service.set_agent_credential_bundles(
                str(args.get("agent_id", "") or ""),
                bundles=list(args.get("bundles", []) or []),
                include_defaults=bool(args.get("include_defaults", False)),
            )
        if verb == "revoke_credentials":
            bundles = args.get("bundles")
            return self.service.revoke_agent_credentials(
                str(args.get("agent_id", "") or ""),
                bundles=list(bundles) if isinstance(bundles, list) else None,
            )
        if verb == "set_provider":
            return self.service.switch_agent_provider(
                str(args.get("agent_id", "") or ""),
                str(args.get("provider", "") or ""),
            )
        if verb == "open_issue":
            return self.service.open_control_issue(
                title=str(args.get("title", "") or ""),
                body=str(args.get("body", "") or ""),
                labels=list(args.get("labels", []) or []) if isinstance(args.get("labels"), list) else None,
                dedupe_key=str(args.get("dedupe_key", "") or ""),
            )
        if verb == "open_pr":
            return self.service.open_control_pr(
                title=str(args.get("title", "") or ""),
                body=str(args.get("body", "") or ""),
                head=str(args.get("head", "") or ""),
                base=str(args.get("base", "main") or "main"),
                draft=bool(args.get("draft", True)),
                maintainer_can_modify=bool(args.get("maintainer_can_modify", False)),
                dedupe_key=str(args.get("dedupe_key", "") or ""),
            )
        raise ValueError(f"unsupported control verb: {verb}")

    def _record_control_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = getattr(self.service, "_event", None)
        if not callable(event):
            return
        state = self.service.store.read_state()
        event(
            state,
            event_type,
            f"{event_type}: {payload.get('verb', '')} {payload.get('decision', '')}",
            {
                "verb": str(payload.get("verb", "")),
                "tier": str(payload.get("tier", "")),
                "decision": str(payload.get("decision", "")),
                "allowed": bool(payload.get("allowed", False)),
            },
        )
        self.service.store.write_state(state)

    def _control_allowlist(self) -> list[str]:
        try:
            config = self.service.store.read_config()
        except Exception:  # noqa: BLE001
            return []
        raw = config.get("control_operator_allowlist", [])
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, str):
            return [item.strip() for item in raw.split(",") if item.strip()]
        return []

    def _authenticated_operator_principal(self, peer_uid: int) -> str:
        candidates = [f"uid:{int(peer_uid)}"]
        try:
            candidates.insert(0, str(pwd.getpwuid(int(peer_uid)).pw_name))
        except KeyError:
            pass
        allowlist = set(self._control_allowlist())
        return next((candidate for candidate in candidates if candidate in allowlist), candidates[0])

    def _control_socket_specs(self) -> list[tuple[Path, int, int]]:
        """Return request-only socket endpoints for isolated control users."""
        state = self.service.store.read_state()
        agents = state.get("agents", {})
        if not isinstance(agents, dict):
            return []
        rows: list[tuple[Path, int, int]] = []
        seen: set[Path] = set()
        for agent in agents.values():
            if not isinstance(agent, dict):
                continue
            info = agent.get("agent", {})
            if not isinstance(info, dict) or str(info.get("role", "")).strip() != "control":
                continue
            linux_user = str(info.get("linux_user", "")).strip()
            if not linux_user:
                continue
            try:
                record = pwd.getpwnam(linux_user)
            except KeyError:
                continue
            path = control_socket_path(self.service.store.root, int(record.pw_uid))
            if path in seen:
                continue
            seen.add(path)
            rows.append((path, int(record.pw_uid), int(record.pw_gid)))
        return rows

    def _refresh_control_ipc(self) -> None:
        desired = {path: (uid, gid) for path, uid, gid in self._control_socket_specs()}
        retained: list[tuple[socket.socket, Path, int]] = []
        for server, path, uid in self._control_servers:
            if (
                path in desired
                and desired[path][0] == uid
                and self._control_listener_current(path, uid)
            ):
                retained.append((server, path, uid))
                desired.pop(path, None)
                continue
            server.close()
            self._unlink_control_socket(path, uid)
        self._control_servers = retained
        for path, (uid, gid) in desired.items():
            self._control_servers.append(self._start_control_listener(path, uid=uid, gid=gid))

    def _start_control_listener(
        self,
        path: Path,
        *,
        uid: int,
        gid: int,
    ) -> tuple[socket.socket, Path, int]:
        if os.geteuid() not in {0, uid}:
            raise SetupError(f"root is required to expose control IPC for uid {uid}")
        parent = path.parent
        self._ensure_control_socket_parent(parent, uid=uid)
        if path.exists() or path.is_symlink():
            existing = path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or int(existing.st_uid) != uid:
                raise SetupError(f"refusing to replace unsafe control IPC path: {path}")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            if os.geteuid() == 0:
                os.chown(path, uid, gid)
            server.listen(8)
        except Exception:
            server.close()
            raise
        return server, path, uid

    @staticmethod
    def _control_listener_current(path: Path, uid: int) -> bool:
        try:
            current = path.lstat()
        except OSError:
            return False
        return stat.S_ISSOCK(current.st_mode) and int(current.st_uid) == uid

    @staticmethod
    def _unlink_control_socket(path: Path, uid: int) -> None:
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(current.st_mode) and int(current.st_uid) == uid:
            path.unlink()

    @staticmethod
    def _validate_control_directory(path: Path, *, owner_uid: int) -> None:
        try:
            current = path.lstat()
        except FileNotFoundError as exc:
            raise SetupError(f"control socket directory does not exist: {path}") from exc
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise SetupError(f"control socket directory must be a real directory: {path}")
        if int(current.st_uid) != owner_uid:
            raise SetupError(f"control socket directory has unexpected owner: {path}")
        if stat.S_IMODE(current.st_mode) & 0o022:
            raise SetupError(f"control socket directory must not be group/world writable: {path}")

    def _ensure_control_socket_parent(self, parent: Path, *, uid: int) -> None:
        effective_uid = os.geteuid()
        if effective_uid == 0 and parent == CONTROL_SOCKET_ROOT:
            manager_root = CONTROL_SOCKET_ROOT.parent
            if not manager_root.exists() and not manager_root.is_symlink():
                manager_root.mkdir(mode=0o755)
            self._validate_control_directory(manager_root, owner_uid=0)
            if not parent.exists() and not parent.is_symlink():
                parent.mkdir(mode=0o711)
            self._validate_control_directory(parent, owner_uid=0)
            # Execute-only access lets isolated agents reach their own 0600
            # socket without listing or modifying the manager-owned directory.
            os.chmod(parent, 0o711)  # nosec B103
            return

        if effective_uid == 0 and uid != 0:
            raise SetupError(
                f"isolated control IPC must use the manager-owned runtime directory: {CONTROL_SOCKET_ROOT}"
            )

        if parent.exists() or parent.is_symlink():
            self._validate_control_directory(parent, owner_uid=effective_uid)
        else:
            parent.mkdir(mode=0o700)
        os.chmod(parent, 0o700)

    @staticmethod
    def _recv_all(conn: socket.socket, *, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("clawied IPC request is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _acquire_lock(self) -> None:
        self.service.store.ensure()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise SetupError(f"could not open clawied lock safely: {self.lock_path}: {exc}") from exc
        try:
            lock_st = os.fstat(fd)
            if not stat.S_ISREG(lock_st.st_mode):
                raise SetupError(f"clawied lock is not a regular file: {self.lock_path}")
            os.fchmod(fd, 0o600)
            owner = self._state_owner()
            if os.geteuid() == 0:
                os.fchown(fd, owner[0], owner[1])
            self._lock_handle = os.fdopen(fd, "a+", encoding="utf-8")
            fd = -1
        finally:
            if fd >= 0:
                os.close(fd)
        if fcntl is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError(f"clawied is already running or lock is held: {self.lock_path}") from exc

    def _release_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _write_pid(self) -> None:
        write_text_under(
            self.service.store.root,
            self.pid_path.name,
            f"{os.getpid()}\n",
            mode=0o600,
            owner=self._state_owner() if os.geteuid() == 0 else None,
        )

    def _remove_pid(self) -> None:
        if self._read_pid() == os.getpid():
            self.pid_path.unlink(missing_ok=True)

    def _read_pid(self) -> int:
        try:
            return int(
                read_text_under(
                    self.service.store.root,
                    self.pid_path.name,
                    max_bytes=128,
                ).strip()
                or "0"
            )
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(
                read_text_under(self.service.store.root, path.name, max_bytes=16 * 1024 * 1024)
            )
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        write_text_under(
            self.service.store.root,
            path.name,
            json.dumps(Clawied._json_safe(payload), indent=2, sort_keys=True) + "\n",
            mode=0o600,
            owner=self._state_owner() if os.geteuid() == 0 else None,
        )

    def _state_owner(self) -> tuple[int, int]:
        st = self.service.store.root.stat()
        return int(st.st_uid), int(st.st_gid)

    def _ensure_socket_parent(self) -> None:
        parent = self.socket_path.parent
        if parent == self.service.store.root:
            return
        if parent.exists() or parent.is_symlink():
            parent_st = parent.lstat()
            if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
                raise SetupError(f"clawied socket parent must be a real directory: {parent}")
            allowed_owners = {os.geteuid(), self._state_owner()[0]}
            if int(parent_st.st_uid) not in allowed_owners:
                raise SetupError(f"clawied socket parent is owned by another user: {parent}")
        else:
            parent.mkdir(mode=0o700)
        if os.geteuid() == 0:
            owner = self._state_owner()
            os.chown(parent, owner[0], owner[1])
        os.chmod(parent, 0o700)

    @staticmethod
    def _peer_uid(conn: socket.socket) -> int | None:
        getpeereid = getattr(conn, "getpeereid", None)
        if callable(getpeereid):
            uid, _gid = getpeereid()
            return int(uid)
        peercred = getattr(socket, "SO_PEERCRED", None)
        if peercred is not None:
            raw = conn.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
        local_peercred = getattr(socket, "LOCAL_PEERCRED", None)
        if local_peercred is not None:
            # Darwin's xucred starts with cr_version then cr_uid. Python does
            # not expose SOL_LOCAL there, whose platform value is zero.
            raw = conn.getsockopt(getattr(socket, "SOL_LOCAL", 0), local_peercred, 256)
            _version, uid = struct.unpack_from("@II", raw)
            return int(uid)
        return None

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): Clawied._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [Clawied._json_safe(item) for item in value]
        return str(value)

    @staticmethod
    def _default_socket_path(root: Path) -> Path:
        candidate = root / "clawied.sock"
        if len(str(candidate).encode("utf-8")) < 100:
            return candidate
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / f"clawie-clawied-{digest}" / "rpc.sock"
