"""Provider runtime install, process supervision, and service control (ClawieService mixin)."""
from __future__ import annotations

import json
import os
import pwd
import re
import signal
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from clawie.providers import (
    get_provider,
    provider_names,
)
from clawie.ipc_paths import control_socket_path, delegation_socket_path
from clawie.service_common import SetupError, AgentNotFoundError, now_iso


class RuntimeOpsMixin:

    @staticmethod
    def _installed_runtime_names(config: dict[str, Any]) -> set[str]:
        names = {
            str(item).strip().lower()
            for item in config.get("installed_runtimes", [])
            if str(item).strip()
        }
        if bool(config.get("runtime_installed", False)):
            provider = str(config.get("provider", "")).strip().lower()
            if provider:
                names.add(provider)
        return names

    def _is_runtime_marked_installed(self, config: dict[str, Any], provider: str) -> bool:
        return str(provider).strip().lower() in self._installed_runtime_names(config)

    def _mark_runtime_installed(self, config: dict[str, Any], provider: str) -> None:
        names = sorted(self._installed_runtime_names(config) | {str(provider).strip().lower()})
        config["installed_runtimes"] = names
        config["runtime_installed"] = bool(names)

    def _verify_installed_runtime_version(self, provider: str, executable: str) -> str:
        """Fail closed before recording a verified-delivery runtime as installed."""
        spec = get_provider(provider)
        if not spec.verified_delivery:
            return ""
        from clawie.adapters import get_adapter

        adapter = get_adapter(spec.name)
        command = adapter.version_command()
        if command:
            command[0] = executable
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(""),
            )
        except OSError as exc:
            raise SetupError(
                f"failed to probe the installed {spec.name} runtime at {executable}: {exc}"
            ) from exc
        output = "\n".join(
            str(part).strip()
            for part in (result.stdout, result.stderr)
            if part and str(part).strip()
        ).strip()
        gate = adapter.version_gate(output)
        if result.returncode != 0 or not gate.supported:
            raise SetupError(
                f"installed {spec.name} runtime is outside the verified delivery range: "
                f"{gate.message or output or f'exit {result.returncode}'}"
            )
        return str(gate.version or "")

    def _verify_detected_runtime_before_write(self, provider: str) -> str:
        """Gate schema writes when a verified runtime is present on the host.

        Auth may be staged before the runtime is installed.  That is safe: the
        later install path is pinned and gated.  Once an executable is present
        (or the store says it should be), unknown versions fail closed.
        """
        spec = get_provider(provider)
        if not spec.verified_delivery:
            return ""
        try:
            executable = self._resolve_provider_executable(spec.name)
        except SetupError:
            config = self.store.read_config()
            if self._is_runtime_marked_installed(config, spec.name):
                raise SetupError(
                    f"{spec.name} is recorded as installed but its executable is unavailable; "
                    "runtime schema writes are disabled"
                )
            return ""
        return self._verify_installed_runtime_version(spec.name, executable)

    def install_provider_runtime(self, provider: str) -> dict[str, Any]:
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        executable = self._resolve_executable_in_service_env(spec.name)
        if executable:
            runtime_version = self._verify_installed_runtime_version(spec.name, executable)
            config = self.store.read_config()
            self._mark_runtime_installed(config, spec.name)
            config["updated_at"] = now_iso()
            self.store.write_config(config)
            return {
                "provider": spec.name,
                "installed": False,
                "already_present": True,
                "method": spec.install_method,
                "package": spec.install_package or spec.name,
                "executable": executable,
                "runtime_version": runtime_version,
            }

        shared_toolchain: Path | None = None
        if spec.install_method == "brew":
            brew = self._resolve_executable_in_service_env("brew")
            if not brew:
                raise SetupError("Homebrew is required to install provider runtimes but was not found in PATH.")
            cmd = [brew, "install", spec.install_package or spec.name]
        elif spec.install_method == "pnpm":
            pnpm = self._resolve_executable_in_service_env("pnpm")
            if not pnpm:
                raise SetupError("pnpm is required to install provider runtimes but was not found in PATH.")
            shared_toolchain = self._ensure_shared_toolchain_root()
            cmd = [
                pnpm,
                "add",
                "-g",
                "--global-dir",
                str(shared_toolchain / "pnpm-global"),
                "--global-bin-dir",
                str(shared_toolchain / "bin"),
                spec.install_package or spec.name,
            ]
        else:
            raise SetupError(f"provider '{spec.name}' does not define an install method")

        env = self._service_env("")
        if shared_toolchain is not None:
            env["CLAWIE_SHARED_TOOLCHAIN"] = str(shared_toolchain)
            # pnpm 11 derives its executable directory as $PNPM_HOME/bin.
            # Keep PNPM_HOME at the toolchain root and also pass the bin path
            # explicitly above so older and newer pnpm releases agree.
            env["PNPM_HOME"] = str(shared_toolchain)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        output = "\n".join(part for part in [result.stdout, result.stderr] if str(part).strip()).strip()
        if shared_toolchain is not None:
            self._harden_shared_toolchain_permissions(shared_toolchain)
        if result.returncode != 0:
            raise SetupError(
                f"failed to install runtime for {spec.name} via {spec.install_method}: {output or f'exit {result.returncode}'}"
            )

        executable = self._resolve_provider_executable(spec.name)
        runtime_version = self._verify_installed_runtime_version(spec.name, executable)
        config = self.store.read_config()
        self._mark_runtime_installed(config, spec.name)
        config["updated_at"] = now_iso()
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(
            state,
            "runtime.installed",
            f"Installed runtime for {spec.name}",
            {
                "provider": spec.name,
                "method": spec.install_method,
                "package": spec.install_package or spec.name,
                "executable": executable,
            },
        )
        self.store.write_state(state)
        return {
            "provider": spec.name,
            "installed": True,
            "already_present": False,
            "method": spec.install_method,
            "package": spec.install_package or spec.name,
            "executable": executable,
            "runtime_version": runtime_version,
            "output": output,
        }

    def ensure_provider_runtime(self, provider: str) -> dict[str, Any]:
        try:
            executable = self._resolve_provider_executable(provider)
        except SetupError:
            return self.install_provider_runtime(provider)
        runtime_version = self._verify_installed_runtime_version(provider, executable)
        config = self.store.read_config()
        self._mark_runtime_installed(config, provider)
        config["updated_at"] = now_iso()
        self.store.write_config(config)
        return {
            "provider": str(provider).strip().lower(),
            "installed": False,
            "already_present": True,
            "method": get_provider(provider).install_method,
            "package": get_provider(provider).install_package or str(provider).strip().lower(),
            "executable": executable,
            "runtime_version": runtime_version,
        }

    def _run_managed_provider_service_action(
        self,
        *,
        provider: str,
        action: str,
        linux_user: str,
        agent_info: dict[str, Any],
    ) -> dict[str, Any]:
        command = str(action).strip().lower()
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")

        if self._provider_uses_generated_user_unit(provider):
            generated = self._run_generated_user_service_action(
                provider=provider,
                action=command,
                linux_user=linux_user,
                agent_info=agent_info,
            )
            if generated is not None:
                if command == "status":
                    return generated
                desired_running = command in {"start", "restart"}
                observed = self._wait_for_managed_provider_state(
                    provider=provider,
                    linux_user=linux_user,
                    agent_info=agent_info,
                    should_be_running=desired_running,
                    timeout_seconds=5.0,
                )
                if not desired_running and observed == "running":
                    self._force_stop_provider_processes(provider, linux_user)
                    observed = self._wait_for_managed_provider_state(
                        provider=provider,
                        linux_user=linux_user,
                        agent_info=agent_info,
                        should_be_running=False,
                        timeout_seconds=5.0,
                    )
                if desired_running and observed != "running":
                    message = (
                        f"{provider} service {command} reported success but no live {provider} runtime was detected"
                        + (f" for {linux_user}" if linux_user else "")
                    )
                    detail = self._provider_start_failure_detail(provider, linux_user)
                    if detail:
                        message = f"{message}\n{detail}"
                    raise SetupError(message)
                if not desired_running and observed == "running":
                    raise SetupError(
                        f"{provider} service stop reported success but {provider} is still running"
                        + (f" for {linux_user}" if linux_user else "")
                    )
                return {
                    **generated,
                    "service_status": "running" if desired_running else "stopped",
                }

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
                executable=self._command_executable(cmd),
                agent_info=agent_info,
            )
            return {
                "provider": provider,
                "linux_user": linux_user,
                "action": command,
                "service_status": str(fallback.get("service_status", "unknown")),
                "service_mode": "fallback",
                "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
                "output": str(fallback.get("output", "")),
                "command": cmd,
            }

        if result.returncode != 0:
            raise SetupError(
                f"{provider} service {command} failed"
                + (f" for {linux_user}" if linux_user else "")
                + ": "
                + (output or f"exit {result.returncode}")
            )

        if command == "status":
            service_status = self._infer_service_status(output)
        else:
            desired_running = command in {"start", "restart"}
            observed = self._wait_for_managed_provider_state(
                provider=provider,
                linux_user=linux_user,
                agent_info=agent_info,
                should_be_running=desired_running,
            )
            if not desired_running and observed == "running":
                self._force_stop_provider_processes(provider, linux_user)
                observed = self._wait_for_managed_provider_state(
                    provider=provider,
                    linux_user=linux_user,
                    agent_info=agent_info,
                    should_be_running=False,
                )
            if desired_running and observed != "running":
                message = (
                    f"{provider} service {command} reported success but no live {provider} runtime was detected"
                    + (f" for {linux_user}" if linux_user else "")
                )
                detail = self._provider_start_failure_detail(provider, linux_user)
                if detail:
                    message = f"{message}\n{detail}"
                raise SetupError(message)
            if not desired_running and observed == "running":
                raise SetupError(
                    f"{provider} service stop reported success but {provider} is still running"
                    + (f" for {linux_user}" if linux_user else "")
                )
            service_status = "running" if desired_running else "stopped"

        return {
            "provider": provider,
            "linux_user": linux_user,
            "action": command,
            "service_status": service_status,
            "service_mode": "systemd",
            "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
            "output": output,
            "command": cmd,
        }

    def _run_generated_user_service_action(
        self,
        *,
        provider: str,
        action: str,
        linux_user: str,
        agent_info: dict[str, Any],
    ) -> dict[str, Any] | None:
        token = str(linux_user).strip()
        if not token:
            return None
        if action == "status":
            unit_path = self._generated_user_service_unit_path(provider, token)
            try:
                if unit_path is None or unit_path.is_symlink() or not unit_path.is_file():
                    return None
            except OSError:
                return None
            service_status = self._systemd_user_service_status(provider, token)
            if service_status == "unknown":
                return None
            return {
                "provider": provider,
                "linux_user": token,
                "action": action,
                "service_status": service_status,
                "service_mode": "systemd",
                "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
                "output": service_status,
                "command": ["systemctl", "--user", "is-active", f"{provider}.service"],
            }
        try:
            self._ensure_generated_user_service_unit(provider, token)
        except Exception:
            return None
        if os.geteuid() == 0:
            self._bootstrap_user_bus(token)
        reloaded = self._run_systemd_user_command(token, ["daemon-reload"])
        if not reloaded.get("ok", False):
            return None

        if action in {"start", "restart"}:
            self._run_systemd_user_command(token, ["reset-failed", f"{provider}.service"])
            enabled = self._run_systemd_user_command(token, ["enable", f"{provider}.service"])
            if not enabled.get("ok", False):
                return None

        managed = self._systemd_user_service_manage(provider, action, token)
        if not managed.get("ok", False):
            return None
        return {
            "provider": provider,
            "linux_user": token,
            "action": action,
            "service_status": "unknown",
            "service_mode": "systemd",
            "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
            "output": str(managed.get("output", "")),
            "command": managed.get("command", []),
        }

    def _wait_for_managed_provider_state(
        self,
        *,
        provider: str,
        linux_user: str,
        agent_info: dict[str, Any],
        should_be_running: bool,
        timeout_seconds: float = 2.0,
        poll_seconds: float = 0.2,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self._run_managed_provider_service_action(
                provider=provider,
                action="status",
                linux_user=linux_user,
                agent_info=agent_info,
            )
            live = self._provider_process_live(provider, linux_user)
            service_running = str(status.get("service_status", "")).strip().lower() == "running"
            if should_be_running and (live or service_running):
                return "running"
            if not should_be_running and not live and not service_running:
                return "stopped"
            if time.monotonic() >= deadline:
                return "running" if (live or service_running) else "stopped"
            time.sleep(poll_seconds)

    def _managed_provider_is_running(
        self,
        *,
        provider: str,
        linux_user: str,
        agent_info: dict[str, Any],
    ) -> bool:
        status = self._run_managed_provider_service_action(
            provider=provider,
            action="status",
            linux_user=linux_user,
            agent_info=agent_info,
        )
        return (
            str(status.get("service_status", "unknown")) == "running"
            or self._provider_process_live_ps_only(provider, linux_user)
            or int(agent_info.get("fallback_pid", 0) or 0) > 0
        )

    def _provider_process_live_ps_only(self, provider: str, linux_user: str) -> bool:
        token = str(linux_user).strip()
        if not token:
            return False
        daemon_map = self._running_provider_daemons_by_user()
        return any(
            str(entry.get("provider", "")).strip().lower() == str(provider).strip().lower()
            for entry in daemon_map.get(token, [])
        )

    def _provider_process_live(self, provider: str, linux_user: str) -> bool:
        token = str(linux_user).strip()
        if not token:
            return False
        if self._provider_process_live_ps_only(provider, token):
            return True
        return bool(self._provider_reports_running(provider, token))

    def _live_provider_names_for_user(self, linux_user: str) -> list[str]:
        token = str(linux_user).strip()
        if not token:
            return []
        daemon_map = self._running_provider_daemons_by_user()
        seen: set[str] = set()
        ordered: list[str] = []
        for entry in daemon_map.get(token, []):
            provider = str(entry.get("provider", "")).strip().lower()
            if not provider or provider in seen:
                continue
            seen.add(provider)
            ordered.append(provider)
        for provider in provider_names():
            if provider in seen:
                continue
            try:
                self._resolve_provider_executable(provider)
            except SetupError:
                continue
            if not self._provider_reports_running(provider, token):
                continue
            seen.add(provider)
            ordered.append(provider)
        return ordered

    def _provider_reports_running(self, provider: str, linux_user: str) -> bool | None:
        token = str(linux_user).strip()
        if not token:
            return False
        if not self._can_manage_linux_user(token):
            return None
        try:
            status = self._run_managed_provider_service_action(
                provider=provider,
                action="status",
                linux_user=token,
                agent_info={"service_status": "unknown", "service_mode": "unknown", "fallback_pid": 0},
            )
        except Exception:
            return None
        return str(status.get("service_status", "")).strip().lower() == "running"

    def _force_stop_provider_processes(self, provider: str, linux_user: str) -> None:
        token = str(linux_user).strip()
        if not token:
            return
        pattern = self._provider_process_pattern(provider)
        quoted = shlex.quote(pattern)
        script = (
            f'if pgrep -u "$(id -u)" -f {quoted} >/dev/null 2>&1; then '
            f'pkill -u "$(id -u)" -f {quoted} >/dev/null 2>&1 || true; '
            'sleep 1; '
            f'if pgrep -u "$(id -u)" -f {quoted} >/dev/null 2>&1; then '
            f'pkill -9 -u "$(id -u)" -f {quoted} >/dev/null 2>&1 || true; '
            "fi; "
            "fi"
        )
        cmd = self._user_shell_command(token, script)
        subprocess.run(cmd, capture_output=True, text=True, check=False)

    def _provider_start_failure_detail(self, provider: str, linux_user: str, lines: int = 40) -> str:
        sections: list[str] = []
        probe = self._provider_start_probe_output(provider=provider, linux_user=linux_user)
        if probe:
            sections.append(probe)
        excerpt = self._provider_daemon_log_excerpt(provider=provider, linux_user=linux_user, lines=lines)
        if excerpt:
            spec = get_provider(provider)
            label = f"Last lines from ~/{spec.state_dir}/daemon.log:\n{excerpt}"
            if label not in sections:
                sections.append(label)
        return "\n\n".join(section for section in sections if section)

    def _provider_daemon_log_excerpt(self, *, provider: str, linux_user: str, lines: int = 40) -> str:
        spec = get_provider(provider)
        script = (
            f'log="$HOME/{spec.state_dir}/daemon.log"; '
            'if [ -f "$log" ]; then '
            f'tail -n {int(lines)} "$log"; '
            "fi"
        )
        try:
            cmd = self._wrap_user_command(["bash", "-lc", script], linux_user, purpose="service log inspection")
        except SetupError:
            return ""
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=self._service_env(linux_user),
        )
        return "\n".join(part.strip() for part in [result.stdout, result.stderr] if str(part).strip()).strip()

    def _provider_start_probe_output(self, *, provider: str, linux_user: str) -> str:
        spec = get_provider(provider)
        try:
            executable = self._resolve_provider_executable(provider)
            cmd = self._wrap_user_command(
                [executable, *spec.background_command],
                linux_user,
                purpose="service startup probe",
            )
        except SetupError:
            return ""
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._service_env(linux_user),
                start_new_session=True,
            )
        except OSError as exc:
            return f"{provider} foreground startup probe could not start: {exc}"
        stdout: str | None
        stderr: str | None
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = self._terminate_timed_out_process_group(process)
            output = self._join_process_output(stdout, stderr)
            prefix = f"{provider} foreground startup probe stayed alive for 5s; process detection may be wrong"
            return f"{prefix}\n{output}".strip()

        output = self._join_process_output(stdout, stderr)
        returncode = int(process.returncode or 0)
        if returncode == 0 and not output:
            return ""
        if returncode == 0:
            return f"{provider} foreground startup probe output:\n{output}".strip()
        if output:
            return f"{provider} foreground startup probe exited {returncode}:\n{output}".strip()
        return f"{provider} foreground startup probe exited {returncode}"

    @staticmethod
    def _terminate_timed_out_process_group(
        process: subprocess.Popen[str],
    ) -> tuple[str | None, str | None]:
        """Terminate a timed-out probe and every descendant in its session."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            process.terminate()

        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
            stdout, stderr = process.communicate()
            return stdout, stderr

        # The immediate wrapper (often sudo) can exit before a descendant that
        # closed its inherited pipes. Fence the whole session before returning.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return stdout, stderr

    def _assert_provider_postflight_ready(
        self,
        *,
        provider: str,
        linux_user: str,
        home: Path | None,
        auth_mode: str,
    ) -> None:
        spec = get_provider(provider)
        self._wait_for_provider_gateway_ready(
            provider=provider,
            linux_user=linux_user,
            timeout_seconds=15.0,
        )
        command = tuple(str(part).strip() for part in spec.readiness_command if str(part).strip())
        if command:
            executable = self._resolve_provider_executable(provider)
            cmd = self._wrap_user_command([executable, *command], linux_user, purpose="provider readiness probe")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._service_env(linux_user),
                    timeout=10,
                )
            except subprocess.TimeoutExpired as exc:
                output = self._join_process_output(exc.stdout, exc.stderr)
                raise SetupError(
                    f"{provider} readiness probe timed out after startup"
                    + (f" for {linux_user}" if linux_user else "")
                    + (f": {output}" if output else "")
                ) from exc
            if result.returncode != 0:
                output = self._join_process_output(result.stdout, result.stderr)
                raise SetupError(
                    f"{provider} readiness probe failed after startup"
                    + (f" for {linux_user}" if linux_user else "")
                    + ": "
                    + (output or f"exit {result.returncode}")
                )

        if str(auth_mode).strip().lower() == "linked" and home is not None:
            status = self._inspect_provider_auth_state(
                provider=provider,
                auth_mode="linked",
                linux_user=linux_user,
                home=home,
            )
            if not self._auth_status_ready(status):
                detail = str(status.get("detail", "")).strip()
                suffix = f" ({detail})" if detail else ""
                raise SetupError(
                    f"{provider} linked auth is not ready after startup"
                    + (f" for {linux_user}" if linux_user else "")
                    + f": {status.get('auth_status', 'unknown')}{suffix}"
                )

    def _wait_for_provider_gateway_ready(
        self,
        *,
        provider: str,
        linux_user: str,
        timeout_seconds: float,
    ) -> None:
        """Wait for a gateway RPC handshake, not merely a live daemon process."""
        from clawie.adapters import AdapterError, get_adapter

        try:
            adapter = get_adapter(provider)
        except AdapterError:
            return
        status_command = getattr(adapter, "gateway_status_command", None)
        if not callable(status_command):
            return
        executable = self._resolve_provider_executable(provider)
        command = status_command(executable)
        wrapped = self._wrap_user_command(command, linux_user, purpose="provider gateway probe")
        deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
        last_detail = "gateway RPC is not ready"
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.1)
            try:
                result = subprocess.run(
                    wrapped,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._service_env(linux_user),
                    timeout=min(5.0, remaining),
                )
            except subprocess.TimeoutExpired:
                last_detail = "gateway status probe timed out"
            else:
                try:
                    payload = json.loads(result.stdout or "")
                except json.JSONDecodeError:
                    payload = {}
                rpc = payload.get("rpc", {}) if isinstance(payload, dict) else {}
                if result.returncode == 0 and isinstance(rpc, dict) and rpc.get("ok") is True:
                    return
                if result.returncode != 0:
                    last_detail = f"gateway status exited {result.returncode}"
                elif not isinstance(rpc, dict) or rpc.get("ok") is not True:
                    last_detail = "gateway RPC handshake is not ready"
            time.sleep(min(0.25, max(deadline - time.monotonic(), 0.0)))
        suffix = f" for {linux_user}" if linux_user else ""
        raise SetupError(
            f"{provider} gateway did not become reachable after startup{suffix}: {last_detail}"
        )

    @staticmethod
    def _join_process_output(stdout: Any, stderr: Any) -> str:
        parts: list[str] = []
        for item in (stdout, stderr):
            if item is None:
                continue
            if isinstance(item, bytes):
                text = item.decode("utf-8", errors="ignore")
            else:
                text = str(item)
            text = text.strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _login_shell_env(self, linux_user: str) -> dict[str, str]:
        env = self._service_env(linux_user)
        if not linux_user:
            return env
        try:
            cmd = self._user_shell_command(linux_user, "env -0")
        except Exception:
            return env
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=False,
                check=False,
                env=self._service_env(linux_user),
                timeout=5,
            )
        except Exception:
            return env
        if result.returncode != 0 or not result.stdout:
            return env
        try:
            raw = result.stdout.decode("utf-8", errors="ignore")
        except Exception:
            return env
        for item in raw.split("\x00"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = str(key).strip()
            if key:
                env[key] = value
        return env

    @staticmethod
    def _resolve_shell_placeholders(payload: Any, env: dict[str, str]) -> Any:
        if isinstance(payload, dict):
            return {key: RuntimeOpsMixin._resolve_shell_placeholders(value, env) for key, value in payload.items()}
        if isinstance(payload, list):
            return [RuntimeOpsMixin._resolve_shell_placeholders(item, env) for item in payload]
        if not isinstance(payload, str):
            return payload

        def replace(match: re.Match[str]) -> str:
            token = str(match.group(1) or match.group(2) or "").strip()
            if not token:
                return match.group(0)
            return str(env.get(token, match.group(0)))

        return re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, payload)

    def agent_service_action(self, agent_id: str, action: str) -> dict[str, Any]:
        self._require_setup()
        command = str(action).strip().lower()
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")
        self._refresh_managed_agent_provider_alignment(agent_id)

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
        # Permission is the most fundamental precondition: check it before
        # resolving or installing the provider runtime so callers get an
        # actionable "requires root" error instead of an install hint.
        self._require_linux_user_access(linux_user, "service control")
        home = self._agent_linux_home(agent)
        if command in {"start", "restart"}:
            self.ensure_provider_runtime(provider)
            if home and linux_user:
                self._apply_staged_prompts_if_possible(provider, home, linux_user)
            if home:
                self._write_prompt_files_for_home(
                    provider, home, agent.get("core_prompts", {}), linux_user,
                )
            self._prepare_agent_provider_home(
                provider=provider,
                agent=agent,
                linux_user=linux_user,
                home=home,
                channels=self._effective_agent_channels(agent),
                live_payloads=self._discover_live_channel_payloads(agent),
            )

        result = self._run_managed_provider_service_action(
            provider=provider,
            action=command,
            linux_user=linux_user,
            agent_info=agent_info,
        )
        if provider == "openclaw" and command in {"start", "restart"} and str(
            result.get("service_status", "")
        ).strip().lower() == "running":
            try:
                self._assert_provider_postflight_ready(
                    provider=provider,
                    linux_user=linux_user,
                    home=home,
                    auth_mode=str(agent_info.get("auth_mode", "")),
                )
            except Exception:
                # A process is not a usable service until its gateway and auth
                # are ready. Stop a failed startup so callers cannot mistake a
                # restart loop or an occupied-port fallback for production.
                try:
                    self._run_managed_provider_service_action(
                        provider=provider,
                        action="stop",
                        linux_user=linux_user,
                        agent_info=agent_info,
                    )
                except Exception:
                    pass
                raise
        agent_info["service_status"] = str(result.get("service_status", "unknown"))
        agent_info["service_mode"] = str(result.get("service_mode", "unknown"))
        if "fallback_pid" in agent_info or int(result.get("fallback_pid", 0) or 0) > 0:
            agent_info["fallback_pid"] = int(result.get("fallback_pid", 0) or 0)
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
                "service_status": str(result.get("service_status", "unknown")),
                "mode": str(result.get("service_mode", "unknown")),
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": agent_id,
            "provider": provider,
            "linux_user": linux_user,
            "action": command,
            "service_status": str(result.get("service_status", "unknown")),
            "service_mode": str(result.get("service_mode", "unknown")),
            "output": str(result.get("output", "")),
        }

    def local_claw_service_action(self, provider: str, action: str) -> dict[str, Any]:
        self._require_setup()
        name = str(provider).strip().lower()
        command = str(action).strip().lower()
        if not name:
            raise ValueError("provider is required")
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")
        if command in {"start", "restart"}:
            self.ensure_provider_runtime(name)

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

    def openclaw_version_gate(self, run=None) -> dict[str, Any]:
        """Probe the installed openclaw version and classify it against the
        adapter's tested range (notify-on-upgrade). Never raises: an undetectable
        or out-of-band version degrades to read-only with a message.
        """
        from clawie.adapters import detect_version, get_adapter

        adapter = get_adapter("openclaw")
        runner = run or self._adapter_version_runner()
        gate = detect_version(adapter, runner)
        return {
            "runtime": adapter.name,
            "version": str(gate.version) if gate.version else "",
            "supported": gate.supported,
            "degraded": gate.degraded,
            "message": gate.message,
        }

    def _adapter_version_runner(self):
        def _run(cmd: list[str]) -> str:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, env=self._service_env("")
            )
            return f"{result.stdout or ''}\n{result.stderr or ''}"

        return _run

    def list_local_runtime_statuses(self, refresh: bool = True) -> list[dict[str, Any]]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        installed = self.list_installed_claws()
        user_hints: dict[str, str] = {}
        providers: list[str] = []
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            providers.append(provider)
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                user_hints[provider] = hint
        if refresh and providers:
            local_state = self._refresh_local_service_statuses(providers, local_state, user_hints=user_hints)
            config = self.store.read_config()
            local_state = self._normalized_local_service_state(config)

        rows: list[dict[str, Any]] = []
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            info = dict(local_state.get(provider, {}))
            auth = self.local_claw_auth_status(provider, probe_cli=refresh)
            rows.append(
                {
                    "provider": provider,
                    "linux_user": str(info.get("linux_user", auth.get("linux_user", ""))),
                    "service_status": str(info.get("service_status", "unknown")),
                    "service_mode": str(info.get("service_mode", "unknown")),
                    "root": str(claw.get("root", "")),
                    "markers": list(claw.get("markers", [])),
                    "auth_mode": str(auth.get("auth_mode", "")),
                    "auth_status": str(auth.get("auth_status", "unknown")),
                    "auth_profile": str(auth.get("auth_profile", "")),
                    "account": str(auth.get("account", "")),
                    "expires_at": str(auth.get("expires_at", "")),
                    "login_required": bool(auth.get("login_required", False)),
                    "source": str(auth.get("source", "")),
                    "detail": str(auth.get("detail", "")),
                }
            )
        return sorted(rows, key=lambda row: str(row.get("provider", "")))

    def _probe_process(self, pid: int) -> dict[str, Any] | None:
        if pid <= 0:
            return None
        cmd = ["ps", "-p", str(pid), "-o", "%cpu=,%mem=,rss="]
        cgroup_probe = self._probe_process_cgroup(pid)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError:
            return cgroup_probe or self._probe_process_procfs(pid)
        if result.returncode != 0 or not result.stdout.strip():
            return cgroup_probe or self._probe_process_procfs(pid)
        parts = result.stdout.strip().split()
        if len(parts) < 3:
            return cgroup_probe or self._probe_process_procfs(pid)
        try:
            probe = {
                "cpu_percent": float(parts[0]),
                "mem_percent": float(parts[1]),
                "rss_kb": int(parts[2]),
            }
            if cgroup_probe is not None:
                probe["mem_percent"] = float(cgroup_probe["mem_percent"])
                probe["rss_kb"] = int(cgroup_probe["rss_kb"])
            return probe
        except ValueError:
            return cgroup_probe or self._probe_process_procfs(pid)

    @staticmethod
    def _probe_process_cgroup(
        pid: int,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> dict[str, Any] | None:
        if pid <= 0:
            return None
        for cgroup_dir in RuntimeOpsMixin._process_memory_cgroup_dirs(pid, proc_root, cgroup_root):
            usage_bytes = RuntimeOpsMixin._read_positive_int_file(cgroup_dir / "memory.current")
            if usage_bytes <= 0:
                usage_bytes = RuntimeOpsMixin._read_positive_int_file(cgroup_dir / "memory.usage_in_bytes")
            if usage_bytes <= 0:
                continue
            memory_kb = int((usage_bytes + 1023) // 1024)
            mem_total_kb = RuntimeOpsMixin._read_proc_mem_total_kb(proc_root)
            mem_percent = round((memory_kb / mem_total_kb) * 100, 2) if mem_total_kb > 0 else 0.0
            return {"cpu_percent": 0.0, "mem_percent": mem_percent, "rss_kb": memory_kb}
        return None

    @staticmethod
    def _process_memory_cgroup_dirs(
        pid: int,
        proc_root: Path,
        cgroup_root: Path,
    ) -> list[Path]:
        try:
            cgroup_text = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
        except OSError:
            return []

        dirs: list[Path] = []
        seen: set[str] = set()
        for line in cgroup_text.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            _hierarchy, controllers_text, cgroup_path = parts
            rel_parts = [
                part
                for part in cgroup_path.strip().split("/")
                if part and part not in {".", ".."}
            ]
            controllers = {item.strip() for item in controllers_text.split(",") if item.strip()}
            candidates: list[Path] = []
            if not controllers:
                candidates.append(cgroup_root.joinpath(*rel_parts))
            elif "memory" in controllers:
                candidates.append(cgroup_root.joinpath("memory", *rel_parts))
                candidates.append(cgroup_root.joinpath(*rel_parts))
            for candidate in candidates:
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                dirs.append(candidate)
        return dirs

    @staticmethod
    def _probe_process_procfs(pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any] | None:
        if pid <= 0:
            return None
        status_path = proc_root / str(pid) / "status"
        try:
            status_text = status_path.read_text(encoding="utf-8")
        except OSError:
            return None

        rss_kb = 0
        for line in status_text.splitlines():
            if not line.startswith("VmRSS:"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    rss_kb = int(parts[1])
                except ValueError:
                    rss_kb = 0
            break
        if rss_kb <= 0:
            return None

        mem_total_kb = RuntimeOpsMixin._read_proc_mem_total_kb(proc_root)
        mem_percent = round((rss_kb / mem_total_kb) * 100, 2) if mem_total_kb > 0 else 0.0
        return {"cpu_percent": 0.0, "mem_percent": mem_percent, "rss_kb": rss_kb}

    @staticmethod
    def _read_proc_mem_total_kb(proc_root: Path = Path("/proc")) -> int:
        try:
            meminfo_text = (proc_root / "meminfo").read_text(encoding="utf-8")
        except OSError:
            return 0
        for line in meminfo_text.splitlines():
            if not line.startswith("MemTotal:"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return max(0, int(parts[1]))
                except ValueError:
                    return 0
            break
        return 0

    @staticmethod
    def _read_positive_int_file(path: Path) -> int:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return 0
        if not text or text == "max":
            return 0
        try:
            value = int(text.split()[0])
        except ValueError:
            return 0
        return value if value > 0 else 0

    def _service_command(self, provider: str, action: str, linux_user: str) -> list[str]:
        # Check permission before resolving the executable: a missing-root error
        # is more fundamental (and more actionable) than a missing-binary error.
        self._require_linux_user_access(linux_user, "service control")
        spec = get_provider(provider)
        executable = self._resolve_provider_executable(spec.name)
        if spec.service_group:
            base = [executable, spec.service_group, action]
        else:
            base = ["bash", "-lc", self._process_service_shell(spec.name, executable, action)]
        return self._wrap_user_command(base, linux_user, purpose="service control")

    def _wrap_user_command(self, base: list[str], linux_user: str, *, purpose: str) -> list[str]:
        if not linux_user or linux_user == self._current_linux_user():
            return base
        if os.geteuid() != 0:
            raise SetupError(
                f"{purpose} requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )
        return ["sudo", "-u", linux_user, "-H", "--", *base]

    @staticmethod
    def _command_executable(cmd: list[str]) -> str:
        for marker in ("service", "daemon", "auth", "gateway"):
            if marker not in cmd:
                continue
            idx = cmd.index(marker)
            if idx > 0:
                return str(cmd[idx - 1])
        return str(cmd[0])

    def _resolve_executable_in_service_env(self, executable: str, *, linux_user: str = "") -> str:
        token = str(executable).strip()
        if not token:
            return ""
        env = self._service_env(linux_user)
        env_path = env.get("PATH", "")
        try:
            resolved = shutil.which(token, path=env_path)
        except TypeError:
            try:
                resolved = shutil.which(token)
            except OSError:
                resolved = None
        except OSError:
            resolved = None
        if resolved:
            return resolved
        if "/" in token:
            candidate = Path(token)
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
            except OSError:
                return ""
        for segment in env_path.split(":"):
            piece = segment.strip()
            if not piece:
                continue
            candidate = Path(piece) / token
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
            except OSError:
                continue
        fallback = f"/home/linuxbrew/.linuxbrew/bin/{token}"
        try:
            if Path(fallback).is_file() and os.access(fallback, os.X_OK):
                return fallback
        except OSError:
            pass
        return ""

    def _resolve_provider_executable(self, provider: str) -> str:
        resolved = self._resolve_executable_in_service_env(provider)
        if resolved:
            return resolved
        raise SetupError(
            f"provider executable '{provider}' was not found in PATH. Run 'clawie runtime install {provider}' first."
        )

    def _service_env(self, linux_user: str) -> dict[str, str]:
        env = dict(os.environ)
        shared_toolchain = self._shared_toolchain_home()
        current_path = env.get("PATH", "")
        required_paths = [
            *self._shared_toolchain_path_entries(),
            "/home/linuxbrew/.linuxbrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        merged: list[str] = []
        for segment in [*required_paths, *current_path.split(":")]:
            piece = segment.strip()
            if not piece or piece in merged:
                continue
            merged.append(piece)
        env["PATH"] = ":".join(merged)
        env["CLAWIE_SHARED_TOOLCHAIN"] = str(shared_toolchain)
        env["PNPM_HOME"] = str(shared_toolchain)

        if linux_user:
            home = self._linux_home_for_user(linux_user)
            if home is not None:
                env["HOME"] = str(home)
            env["USER"] = linux_user
            env["LOGNAME"] = linux_user
            try:
                record = pwd.getpwnam(linux_user)
            except KeyError:
                record = None
            if record is not None:
                uid = int(record.pw_uid)
                env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
        return env

    @staticmethod
    def _provider_uses_generated_user_unit(provider: str) -> bool:
        spec = get_provider(provider)
        if spec.name == "openclaw":
            return True
        return not bool(spec.service_group) and bool(spec.background_command)

    def _generated_user_service_unit_path(self, provider: str, linux_user: str) -> Path | None:
        home = self._linux_home_for_user(linux_user)
        if home is None:
            return None
        return home / ".config" / "systemd" / "user" / f"{provider}.service"

    def _generated_user_service_unit_contents(self, provider: str, executable: str) -> str:
        spec = get_provider(provider)
        command = " ".join([shlex.quote(executable), *[shlex.quote(part) for part in spec.background_command]])
        state_dir = spec.state_dir
        workspace_dir = spec.workspace_dir
        path_entries = self._service_env("").get("PATH", "")
        pickup = self._staged_prompt_pickup_shell(provider, state_dir, workspace_dir)
        control_socket = control_socket_path(self.store.root, "%U")
        delegation_socket = delegation_socket_path(self.store.root, "%U")
        shell = (
            f'mkdir -p "$HOME/{state_dir}" "$HOME/{state_dir}/{workspace_dir}"; '
            f'cd "$HOME/{state_dir}/{workspace_dir}"; '
            f'exec {command} >>"$HOME/{state_dir}/daemon.log" 2>&1'
        )
        lines = [
            "[Unit]",
            f"Description=Clawie managed {provider} runtime",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "Environment=HOME=%h",
            "Environment=USER=%u",
            "Environment=LOGNAME=%u",
            f"Environment=CLAWIE_CONTROL_SOCKET={control_socket}",
            f"Environment=CLAWIE_DELEGATION_SOCKET={delegation_socket}",
            f"Environment=PATH={path_entries}",
            f"ExecStartPre=/bin/bash -c {shlex.quote(pickup)}",
            f"ExecStart=/bin/bash -lc {shlex.quote(shell)}",
            "Restart=always",
            "RestartSec=2",
            "KillMode=control-group",
            "TimeoutStopSec=20",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        return "\n".join(lines)

    def _ensure_generated_user_service_unit(self, provider: str, linux_user: str) -> bool:
        unit_path = self._generated_user_service_unit_path(provider, linux_user)
        if unit_path is None:
            return False
        executable = self._resolve_provider_executable(provider)
        unit_text = self._generated_user_service_unit_contents(provider, executable)
        home = self._linux_home_for_user(linux_user)
        if home is None:
            return False
        relative = unit_path.relative_to(home)
        try:
            current = self._read_agent_text_file(home, relative)
        except FileNotFoundError:
            current = ""
        if current != unit_text:
            self._write_agent_text_file(home, relative, unit_text, linux_user, mode=0o600)
        return True

    def _run_systemd_user_command(self, linux_user: str, args: list[str]) -> dict[str, Any]:
        candidates = self._systemd_user_candidates(linux_user)
        last_output = ""
        for candidate in candidates:
            if candidate == "root":
                continue
            direct = self._run_direct_systemd_user_command(candidate, args)
            if direct.get("ok", False):
                return direct
            last_output = str(direct.get("output", "") or last_output)
            cmd = ["systemctl", "--machine", f"{candidate}@", "--user", *args]
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
        cmd = ["systemctl", "--user", *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(fallback_user),
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": cmd}
            last_output = output or f"exit {result.returncode}"
        except Exception as exc:
            last_output = str(exc)
        return {"ok": False, "output": last_output, "command": cmd}

    def _run_direct_systemd_user_command(
        self,
        linux_user: str,
        args: list[str],
    ) -> dict[str, Any]:
        """Run systemctl as the target user against that user's own bus."""
        token = str(linux_user).strip()
        if not token:
            return {"ok": False, "output": "linux user is required", "command": []}
        env = self._service_env(token)
        base = ["systemctl", "--user", *args]
        if os.geteuid() == 0 and token != self._current_linux_user():
            try:
                uid = int(pwd.getpwnam(token).pw_uid)
            except KeyError:
                return {"ok": False, "output": f"linux user not found: {token}", "command": []}
            cmd = [
                "sudo",
                "-u",
                token,
                "-H",
                "--",
                "env",
                f"XDG_RUNTIME_DIR=/run/user/{uid}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                f"PATH={env.get('PATH', '')}",
                *base,
            ]
        else:
            cmd = base
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except Exception as exc:
            return {"ok": False, "output": str(exc), "command": cmd}
        output = (result.stdout or result.stderr or "").strip()
        return {
            "ok": result.returncode == 0,
            "output": output or ("" if result.returncode == 0 else f"exit {result.returncode}"),
            "command": cmd,
            "returncode": int(result.returncode),
        }

    def _process_service_shell(self, provider: str, executable: str, action: str) -> str:
        spec = get_provider(provider)
        state_dir = spec.state_dir
        pattern = self._provider_process_pattern(spec.name)
        quoted_executable = shlex.quote(executable)
        start_cmd = " ".join([quoted_executable, *[shlex.quote(part) for part in spec.background_command]])
        lines = [f'pattern={shlex.quote(pattern)}']
        lines.append('existing="$(pgrep -u "$(id -u)" -f "$pattern" | tr \'\\n\' \' \' | sed \'s/[[:space:]]*$//\')"')
        if action == "status":
            lines.append('if [ -n "$existing" ]; then echo "active (running)"; else echo "inactive"; fi')
        elif action == "stop":
            lines.append('if [ -n "$existing" ]; then pkill -u "$(id -u)" -f "$pattern" || true; echo "stopped"; else echo "inactive"; fi')
        elif action == "restart":
            lines.append('if [ -n "$existing" ]; then pkill -u "$(id -u)" -f "$pattern" || true; fi')
            lines.append(f'mkdir -p "$HOME/{state_dir}"')
            lines.append(f'setsid {start_cmd} < /dev/null >>"$HOME/{state_dir}/daemon.log" 2>&1 & echo "started pid=$!"')
        else:
            lines.append('if [ -n "$existing" ]; then echo "already running"; exit 0; fi')
            lines.append(f'mkdir -p "$HOME/{state_dir}"')
            lines.append(f'setsid {start_cmd} < /dev/null >>"$HOME/{state_dir}/daemon.log" 2>&1 & echo "started pid=$!"')
        return "; ".join(lines)

    @staticmethod
    def _provider_process_pattern(provider: str) -> str:
        spec = get_provider(provider)
        return " ".join([spec.name, *spec.background_command]).strip()

    def _resolve_local_runtime_target(self, provider: str) -> dict[str, str]:
        name = str(provider).strip().lower()
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        cached = dict(local_state.get(name, {}))
        root_path: Path | None = None
        hint_user = ""
        for claw in self.list_installed_claws():
            if str(claw.get("provider", "")).strip().lower() != name:
                continue
            root_path = Path(str(claw.get("root", "")).strip())
            hint_user = self._linux_user_from_provider_root(root_path)
            break

        linux_user = self._preferred_local_linux_user(
            default_user=self._local_target_user(),
            hint_user=hint_user,
            cached_user=str(cached.get("linux_user", "")),
        )
        if root_path:
            home = root_path.parent
        elif linux_user and linux_user != "root":
            home = Path("/home") / linux_user
        else:
            home = Path.home()
        return {
            "linux_user": linux_user,
            "home": str(home),
            "root": str(root_path or ""),
        }

    def _running_provider_daemons_by_user(self) -> dict[str, list[dict[str, Any]]]:
        try:
            result = subprocess.run(["ps", "-eo", "user=,pid=,args="], capture_output=True, text=True, check=False)
        except OSError:
            return {}
        if result.returncode != 0:
            return {}

        rows: dict[str, list[dict[str, Any]]] = {}
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            linux_user, pid_text, args = parts
            provider = self._provider_from_process_args(args)
            if not provider:
                continue
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            rows.setdefault(linux_user, []).append(
                {
                    "provider": provider,
                    "pid": pid,
                    "args": args,
                }
            )
        return rows

    @staticmethod
    def _provider_from_process_args(args: str) -> str:
        raw = str(args).strip()
        if not raw:
            return ""
        # `raw` is an arbitrary command line from `ps` and can be very long or
        # oddly quoted. shlex.split() is pure-Python and char-by-char, so a
        # pathological process arg string can make it pathologically slow (and
        # this runs for every process on the host). We only need basename
        # matching against known provider executables, so a plain whitespace
        # split is both sufficient and safely O(n).
        tokens = raw.split()
        if len(tokens) < 2:
            return ""
        known = set(provider_names())
        for idx, token in enumerate(tokens):
            name = Path(token).name.strip().lower()
            candidate = name
            if candidate not in known and "." in candidate:
                stem = Path(token).stem.strip().lower()
                if stem in known:
                    candidate = stem
            if candidate not in known:
                continue
            expected = [str(item).strip().lower() for item in get_provider(candidate).background_command]
            if not expected:
                continue
            tail = [str(item).strip().lower() for item in tokens[idx + 1 : idx + 1 + len(expected)]]
            if tail == expected:
                return candidate
        return ""

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
            running = self._is_pid_running(pid, linux_user) or self._provider_process_live_ps_only(provider, linux_user)
            return {
                "service_status": "running" if running else "stopped",
                "output": "fallback daemon " + ("running" if running else "stopped"),
            }

        if action == "stop":
            if pid and self._is_pid_running(pid, linux_user):
                self._kill_pid(pid, linux_user)
            self._force_stop_provider_processes(provider, linux_user)
            agent_info["fallback_pid"] = 0
            running = self._provider_process_live_ps_only(provider, linux_user)
            return {
                "service_status": "running" if running else "stopped",
                "output": "fallback daemon " + ("running" if running else "stopped"),
            }

        if action == "restart":
            if pid and self._is_pid_running(pid, linux_user):
                self._kill_pid(pid, linux_user)
            new_pid = self._start_fallback_daemon(provider, executable, linux_user)
            agent_info["fallback_pid"] = new_pid
            return {"service_status": "running", "output": f"fallback daemon restarted pid={new_pid}"}

        # start
        if pid and self._is_pid_running(pid, linux_user):
            return {"service_status": "running", "output": f"fallback daemon already running pid={pid}"}
        new_pid = self._start_fallback_daemon(provider, executable, linux_user)
        agent_info["fallback_pid"] = new_pid
        return {"service_status": "running", "output": f"fallback daemon started pid={new_pid}"}

    def _start_fallback_daemon(self, provider: str, executable: str, linux_user: str) -> int:
        spec = get_provider(provider)
        state_dir = spec.state_dir
        workspace_dir = spec.workspace_dir
        pickup = self._staged_prompt_pickup_shell(provider, state_dir, workspace_dir)
        background = " ".join([f'"{executable}"', *[shlex.quote(part) for part in spec.background_command]])
        script = (
            f'{pickup}; '
            f'mkdir -p "$HOME/{state_dir}"; '
            f'setsid {background} < /dev/null >>"$HOME/{state_dir}/daemon.log" 2>&1 & echo $!'
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

    def _systemd_user_candidates(self, linux_user: str, provider: str = "") -> list[str]:
        candidates: list[str] = []
        explicit = str(linux_user).strip()
        if explicit and explicit != "root":
            return [explicit]
        hint = str(self._local_linux_user_hint(provider, "")).strip() if provider else ""
        for token in (
            str(linux_user).strip(),
            str(self._local_target_user()).strip(),
            hint,
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
        return candidates

    def _systemd_user_service_status(self, provider: str, linux_user: str) -> str:
        service = f"{provider}.service"
        candidates = self._systemd_user_candidates(linux_user, provider)

        saw_stopped = False
        for candidate in candidates:
            if candidate == "root":
                continue
            direct = self._run_direct_systemd_user_command(
                candidate,
                ["is-active", service],
            )
            parsed = self._parse_systemctl_status(
                str(direct.get("output", "")),
                "",
            )
            if parsed == "running":
                return "running"
            if parsed == "stopped":
                saw_stopped = True
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
        candidates = self._systemd_user_candidates(linux_user, provider)

        last_output = ""
        for candidate in candidates:
            if candidate == "root":
                continue
            direct = self._run_direct_systemd_user_command(
                candidate,
                [action, service],
            )
            if direct.get("ok", False):
                return direct
            last_output = str(direct.get("output", "") or last_output)
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
        if "inactive" in text or "stopped" in text or "dead" in text:
            return "stopped"
        if "running" in text or "active" in text or "started" in text:
            return "running"
        return "unknown"

    @staticmethod
    def _normalize_status_text(value: str) -> str:
        token = str(value).strip().lower()
        if token in {"running", "active", "started"}:
            return "running"
        if token in {"stopped", "inactive", "dead", "offline"}:
            return "stopped"
        if token in {"configured", "ready", "syncing"}:
            return token
        return "unknown"

    def maintenance_status(self) -> dict[str, Any]:
        """Check whether the maintenance cron job is installed."""
        self._require_setup()
        config = self.store.read_config()
        cron_exists = self.MAINTENANCE_CRON_FILE.exists()
        cron_content = ""
        if cron_exists:
            try:
                cron_content = self.MAINTENANCE_CRON_FILE.read_text(encoding="utf-8")
            except OSError:
                cron_content = "<unreadable>"
        return {
            "enabled": bool(config.get("maintenance_cron_enabled", False)),
            "cron_file_exists": cron_exists,
            "interval_hours": int(config.get("maintenance_cron_interval_hours", 4)),
            "cron_file": str(self.MAINTENANCE_CRON_FILE),
            "cron_content": cron_content,
        }
