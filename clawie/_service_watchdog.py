"""Systemd watchdog for clawied/control runtime supervision."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from clawie.service_common import SetupError


class ControlWatchdogMixin:
    """Install/remove a small systemd supervisor for ``clawie clawied run``."""

    def control_watchdog_install(
        self,
        *,
        interval_seconds: int = 60,
        notify_command: str = "",
        start: bool = True,
    ) -> dict[str, Any]:
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("control watchdog install requires root. Re-run with sudo.")
        interval = max(1, int(interval_seconds))
        notify = str(notify_command or "").strip()
        unit_path = self.CONTROL_WATCHDOG_UNIT_FILE
        alert_path = self.CONTROL_WATCHDOG_ALERT_UNIT_FILE
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(
            self._control_watchdog_unit_contents(interval_seconds=interval, notify_command=notify),
            encoding="utf-8",
        )
        os.chmod(unit_path, 0o644)
        if notify:
            alert_path.write_text(
                self._control_watchdog_alert_unit_contents(notify_command=notify),
                encoding="utf-8",
            )
            os.chmod(alert_path, 0o644)
        else:
            alert_path.unlink(missing_ok=True)

        self._systemctl("daemon-reload", check=False)
        started = False
        if start:
            enabled = self._systemctl(
                "enable",
                "--now",
                unit_path.name,
                check=False,
            )
            started = enabled.returncode == 0
            if not started:
                detail = str(enabled.stderr or enabled.stdout or f"exit {enabled.returncode}").strip()
                raise SetupError(f"failed to enable/start control watchdog: {detail}")

        config = self.store.read_config()
        config["control_watchdog_enabled"] = bool(started)
        config["control_watchdog_interval_seconds"] = interval
        config["control_watchdog_notify_command"] = notify
        self.store.write_config(config)

        state = self.store.read_state()
        self._event(
            state,
            "control.watchdog_installed",
            "Installed control watchdog systemd unit",
            {
                "unit_file": str(unit_path),
                "alert_unit_file": str(alert_path) if notify else "",
                "interval_seconds": interval,
                "started": started,
            },
        )
        self.store.write_state(state)
        return {
            "enabled": bool(config["control_watchdog_enabled"]),
            "started": started,
            "unit_file": str(unit_path),
            "alert_unit_file": str(alert_path) if notify else "",
            "interval_seconds": interval,
            "notify_command": notify,
        }

    def control_watchdog_remove(self) -> dict[str, Any]:
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("control watchdog remove requires root. Re-run with sudo.")
        unit_path = self.CONTROL_WATCHDOG_UNIT_FILE
        alert_path = self.CONTROL_WATCHDOG_ALERT_UNIT_FILE
        if unit_path.exists():
            self._systemctl("disable", "--now", unit_path.name, check=False)
        removed = []
        for path in (unit_path, alert_path):
            if path.exists() or path.is_symlink():
                path.unlink()
                removed.append(str(path))
        self._systemctl("daemon-reload", check=False)

        config = self.store.read_config()
        config["control_watchdog_enabled"] = False
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(
            state,
            "control.watchdog_removed",
            "Removed control watchdog systemd unit",
            {"removed": removed},
        )
        self.store.write_state(state)
        return {"enabled": False, "removed": removed, "unit_file": str(unit_path)}

    def control_watchdog_status(self) -> dict[str, Any]:
        config = self.store.read_config()
        unit_path = self.CONTROL_WATCHDOG_UNIT_FILE
        alert_path = self.CONTROL_WATCHDOG_ALERT_UNIT_FILE
        return {
            "enabled": bool(config.get("control_watchdog_enabled", False)),
            "unit_file": str(unit_path),
            "unit_file_exists": unit_path.exists(),
            "alert_unit_file": str(alert_path),
            "alert_unit_file_exists": alert_path.exists(),
            "interval_seconds": int(config.get("control_watchdog_interval_seconds", 60) or 60),
            "notify_command_configured": bool(
                str(config.get("control_watchdog_notify_command", "")).strip()
            ),
            "active": self._systemctl_probe("is-active", unit_path.name),
            "systemd_enabled": self._systemctl_probe("is-enabled", unit_path.name),
        }

    def control_watchdog_verify(
        self,
        *,
        exercise_restart: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        """Verify watchdog readiness on a real systemd host.

        With ``exercise_restart=True`` this intentionally kills the watchdog
        service's main process and waits for systemd to restart it. That is the
        production proof for ``Restart=always``; the normal status command only
        proves that systemd reports the unit as active.
        """
        self._require_setup()
        unit_path = self.CONTROL_WATCHDOG_UNIT_FILE
        unit_name = unit_path.name
        checks: list[dict[str, Any]] = []

        def add(status: str, message: str, **context: Any) -> None:
            row: dict[str, Any] = {"status": status, "message": message}
            if context:
                row["context"] = context
            checks.append(row)

        if not shutil.which("systemctl"):
            add("fail", "systemctl is not available")
            return {"status": "failed", "checks": checks, "restart_exercised": False}

        if unit_path.exists() and unit_path.is_file():
            add("pass", f"unit file exists: {unit_path}")
            unit_text = unit_path.read_text(encoding="utf-8")
            if "Restart=always" in unit_text:
                add("pass", "unit has Restart=always")
            else:
                add("fail", "unit is missing Restart=always")
            if self._control_watchdog_execstart_matches_config_dir(unit_text):
                add("pass", "unit ExecStart points at this config directory")
            else:
                add("fail", "unit ExecStart does not match this config directory")
        else:
            add("fail", f"unit file is missing: {unit_path}")

        status = self.control_watchdog_status()
        if status.get("active") == "active":
            add("pass", "systemd reports watchdog active")
        else:
            add("fail", "systemd does not report watchdog active", active=status.get("active", ""))
        if status.get("systemd_enabled") == "enabled":
            add("pass", "systemd reports watchdog enabled")
        else:
            add("fail", "systemd does not report watchdog enabled", enabled=status.get("systemd_enabled", ""))

        show = self._systemctl_show(unit_name, ("ActiveState", "SubState", "MainPID", "NRestarts", "Restart"))
        restart_policy = str(show.get("Restart", "")).strip()
        if restart_policy == "always":
            add("pass", "systemd loaded Restart=always")
        else:
            add("fail", "systemd loaded restart policy is not always", restart=restart_policy)

        restart_exercised = False
        if exercise_restart:
            restart_exercised = self._control_watchdog_exercise_restart(
                unit_name=unit_name,
                before=show,
                timeout_seconds=max(1, int(timeout_seconds)),
                add_check=add,
            )

        final_status = "passed"
        if any(row["status"] == "fail" for row in checks):
            final_status = "failed"
        elif any(row["status"] == "skip" for row in checks):
            final_status = "skipped"
        return {
            "status": final_status,
            "checks": checks,
            "restart_exercised": restart_exercised,
            "unit_file": str(unit_path),
            "unit_name": unit_name,
        }

    def _control_watchdog_exercise_restart(
        self,
        *,
        unit_name: str,
        before: dict[str, str],
        timeout_seconds: int,
        add_check: Any,
    ) -> bool:
        try:
            before_pid = int(str(before.get("MainPID", "0") or "0"))
        except ValueError:
            before_pid = 0
        try:
            before_restarts = int(str(before.get("NRestarts", "0") or "0"))
        except ValueError:
            before_restarts = 0
        if before_pid <= 0:
            add_check("fail", "cannot exercise restart because watchdog has no MainPID")
            return False

        killed = self._systemctl("kill", "--signal=SIGTERM", unit_name, check=False)
        if killed.returncode != 0:
            add_check(
                "fail",
                "systemctl kill failed while exercising watchdog restart",
                stderr=str(killed.stderr or "").strip(),
            )
            return False
        add_check("pass", f"sent SIGTERM to watchdog MainPID {before_pid}")

        deadline = time.monotonic() + timeout_seconds
        last: dict[str, str] = {}
        while time.monotonic() < deadline:
            time.sleep(0.25)
            last = self._systemctl_show(unit_name, ("ActiveState", "MainPID", "NRestarts"))
            try:
                after_pid = int(str(last.get("MainPID", "0") or "0"))
            except ValueError:
                after_pid = 0
            try:
                after_restarts = int(str(last.get("NRestarts", "0") or "0"))
            except ValueError:
                after_restarts = 0
            active = str(last.get("ActiveState", "")).strip()
            restarted_pid = after_pid > 0 and after_pid != before_pid
            if active == "active" and restarted_pid:
                add_check(
                    "pass",
                    "systemd restarted the watchdog service",
                    before_pid=before_pid,
                    after_pid=after_pid,
                    before_restarts=before_restarts,
                    after_restarts=after_restarts,
                )
                return True
        add_check(
            "fail",
            "watchdog service did not restart before timeout",
            before_pid=before_pid,
            before_restarts=before_restarts,
            last=last,
        )
        return False

    def _control_watchdog_unit_contents(
        self,
        *,
        interval_seconds: int,
        notify_command: str,
    ) -> str:
        clawie_bin = shutil.which("clawie") or "/usr/local/bin/clawie"
        command = (
            f"{shlex.quote(clawie_bin)} --config-dir {shlex.quote(str(self.store.root))} "
            f"clawied run --interval {int(interval_seconds)}"
        )
        lines = [
            "[Unit]",
            "Description=Clawie control watchdog",
            "After=network-online.target",
            "Wants=network-online.target",
        ]
        if str(notify_command or "").strip():
            lines.append(f"OnFailure={self.CONTROL_WATCHDOG_ALERT_UNIT_FILE.name}")
        lines.extend(
            [
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart=/bin/bash -lc {shlex.quote(command)}",
                "Restart=always",
                "RestartSec=5",
                "KillSignal=SIGTERM",
                "TimeoutStopSec=20",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )
        return "\n".join(lines)

    def _control_watchdog_alert_unit_contents(self, *, notify_command: str) -> str:
        command = str(notify_command or "").strip()
        return "\n".join(
            [
                "[Unit]",
                "Description=Clawie control watchdog alert",
                "",
                "[Service]",
                "Type=oneshot",
                f"ExecStart=/bin/bash -lc {shlex.quote(command)}",
                "",
            ]
        )

    def _control_watchdog_execstart_matches_config_dir(self, unit_text: str) -> bool:
        expected_roots = {str(self.store.root)}
        try:
            expected_roots.add(str(self.store.root.resolve()))
        except OSError:
            pass
        for raw_line in str(unit_text or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("ExecStart="):
                continue
            value = line.split("=", 1)[1].strip()
            try:
                outer = shlex.split(value)
            except ValueError:
                continue
            if len(outer) >= 3 and Path(outer[0]).name == "bash" and outer[1] == "-lc":
                command = outer[2]
            else:
                command = " ".join(outer)
            try:
                argv = shlex.split(command)
            except ValueError:
                continue
            if "clawied" not in argv or "run" not in argv:
                continue
            for index, token in enumerate(argv[:-1]):
                if token != "--config-dir":
                    continue
                configured = argv[index + 1]
                candidates = {configured}
                try:
                    candidates.add(str(Path(configured).resolve()))
                except OSError:
                    pass
                if candidates & expected_roots:
                    return True
        return False

    @staticmethod
    def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        if not shutil.which("systemctl"):
            return subprocess.CompletedProcess(["systemctl", *args], 127, "", "systemctl not found")
        return subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def _systemctl_probe(self, *args: str) -> str:
        result = self._systemctl(*args, check=False)
        if result.returncode == 127:
            return "unavailable"
        output = (result.stdout or result.stderr or "").strip()
        return output or ("ok" if result.returncode == 0 else "unknown")

    def _systemctl_show(self, unit_name: str, properties: tuple[str, ...]) -> dict[str, str]:
        args = ["show", unit_name]
        for prop in properties:
            args.append(f"--property={prop}")
        result = self._systemctl(*args, check=False)
        rows: dict[str, str] = {}
        if result.returncode == 127:
            return rows
        for line in str(result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            rows[key.strip()] = value.strip()
        return rows
