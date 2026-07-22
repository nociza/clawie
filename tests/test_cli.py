from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pytest import CaptureFixture, MonkeyPatch, fixture, raises

from clawie.provider_auth import (
    auth_status_from_picoclaw_auth_json,
    auth_status_from_profiles_json,
    parse_openclaw_models_status_output,
    parse_provider_auth_status_output,
)
from clawie.cli import build_parser, main
from clawie.providers import credential_paths_for_providers
from clawie.auth_sources import load_codex_auth
from clawie.addon_auth import parse_gws_status_output
from clawie.provider_channels import OpenClawChannelAdapter
from clawie.service import SetupError, ClawieService
from clawie.store import StateStore
import clawie._service_watchdog as watchdog_module
import clawie._service_shared as shared_module
import clawie._service_prompts as prompts_module
from clawie.safe_fs import UnsafePathError


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


@fixture(autouse=True)
def _test_only_unknown_owner_fallback(monkeypatch: MonkeyPatch) -> None:
    """Tests that simulate root still use synthetic Linux usernames.

    Production ownership resolution remains fail-closed.  Within this module,
    leave ownership unchanged when a test's fake passwd database has no entry.
    """
    original = shared_module.owner_for_username

    def resolve(username: str) -> tuple[int, int] | None:
        try:
            return original(username)
        except UnsafePathError:
            return None

    monkeypatch.setattr(shared_module, "owner_for_username", resolve)
    monkeypatch.setattr(prompts_module, "owner_for_username", resolve)


def _mock_spawn_passwd_home(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    username: str,
) -> Path:
    home = tmp_path / f"{username}-home"
    home.mkdir()

    class PwdRow:
        pw_dir = str(home)
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    class PwdModule:
        @staticmethod
        def getpwnam(_username: str) -> PwdRow:
            return PwdRow()

    monkeypatch.setattr("clawie._service_spawn.pwd", PwdModule())
    source_home = tmp_path / "source-home"
    source_home.mkdir(exist_ok=True)
    monkeypatch.setattr(ClawieService, "_default_source_home", lambda _self: source_home)
    return home


def test_cli_version_exits_without_state(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    with raises(SystemExit) as exc:
        main(["--config-dir", str(tmp_path), "--version"])

    assert exc.value.code == 0
    assert "clawie 0.1.13" in capsys.readouterr().out
    assert not (tmp_path / "clawie.db").exists()


def test_cli_without_arguments_prints_help_without_creating_state(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    output = capsys.readouterr().out
    assert "Clawie control plane" in output
    assert "{status,config,agent" in output
    assert not (tmp_path / ".clawie").exists()


def test_corrupt_state_db_reports_clean_error_not_traceback(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """A corrupt/unreadable state DB must surface as a clean CLI error (exit 1),
    never an uncaught sqlite3.DatabaseError traceback."""
    (tmp_path / "clawie.db").write_bytes(b"this is not a sqlite database" * 64)

    for command in (["health"], ["agent", "list"], ["config", "show"], ["backup", "status"]):
        code = main(["--config-dir", str(tmp_path), "--no-color", *command])
        assert code == 1, command
        assert "database" in capsys.readouterr().err.lower(), command


def test_corrupt_state_db_status_json_degrades_and_exits_nonzero(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """status --json is a monitoring gate: even on a corrupt DB it must emit
    valid JSON on stdout and exit nonzero, rather than crash."""
    (tmp_path / "clawie.db").write_bytes(b"this is not a sqlite database" * 64)

    code = main(["--config-dir", str(tmp_path), "--no-color", "status", "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "health" in payload


def test_corrupt_private_db_status_exits_nonzero(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """Real-world corruption path: a state dir with correct private permissions
    whose database content is unreadable must still fail the monitoring gate.
    Regression for a bug where SQLite corruption errors ("file is not a
    database") did not match the fatal-error allowlist, so status exited 0."""
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    assert run_cli(state, "config", "set", "--workspace", "x") == 0
    capsys.readouterr()

    db = state / "clawie.db"
    db.write_bytes(os.urandom(4096))
    db.chmod(0o600)

    code = main(["--config-dir", str(state), "--no-color", "status", "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    # Every data section reports the store-level read failure.
    errored = [k for k, v in payload.items() if isinstance(v, dict) and set(v) == {"error"}]
    assert errored, payload


def test_json_command_errors_are_structured_on_stderr(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "status_snapshot",
        lambda self, **_kwargs: (_ for _ in ()).throw(ValueError("broken status")),
    )

    code = run_cli(tmp_path, "status", "--json")
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {"ok": False, "error": "broken status", "error_type": "ValueError"}
    assert "\x1b[" not in captured.err


def test_non_tty_output_does_not_contain_ansi_sequences(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    captured = capsys.readouterr()
    assert "\x1b[" not in captured.out
    assert "\x1b[" not in captured.err


def test_fresh_store_is_not_reported_as_configured(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    code = run_cli(tmp_path, "config", "show")
    output = capsys.readouterr().out

    assert code == 1
    assert "configured: False" in output
    assert "clawie config set" in output


def test_maintenance_cron_preserves_state_root_and_exact_schedule(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state % with spaces"))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="",
    )
    cron_dir = tmp_path / "cron.d"
    cron_dir.mkdir()
    cron_file = cron_dir / "clawie-maintenance"
    monkeypatch.setattr(service, "MAINTENANCE_CRON_FILE", cron_file)
    monkeypatch.setattr(service, "MAINTENANCE_LOG_FILE", tmp_path / "maintenance.log")
    monkeypatch.setattr("clawie._service_delegation.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "clawie._service_delegation.shutil.which",
        lambda _name: "/opt/clawie % bin/clawie",
    )
    monkeypatch.setattr(
        service,
        "_validated_root_automation_executable",
        lambda path: str(Path(path)),
    )

    result = service.maintenance_enable(interval_hours=6)
    cron = cron_file.read_text(encoding="utf-8")

    assert result["interval_hours"] == 6
    assert "0 */6 * * * root" in cron
    assert "'/opt/clawie \\% bin/clawie'" in cron
    expected_root = str(service.store.root.resolve()).replace("%", r"\%")
    assert f"--config-dir '{expected_root}' maintenance run" in cron
    assert (cron_file.stat().st_mode & 0o777) == 0o644

    with raises(ValueError, match="must be one of"):
        service.maintenance_enable(interval_hours=5)


def test_maintenance_rejects_user_writable_executable(tmp_path: Path) -> None:
    executable = tmp_path / "clawie"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    with raises(SetupError, match="root-owned, non-group/world-writable"):
        ClawieService._validated_root_automation_executable(executable)


def _fake_jwt(payload: dict[str, object]) -> str:
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode("utf-8")).decode("utf-8").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{header}.{body}.sig"


def _read_openclaw_native_profiles(home: Path) -> dict[str, Any]:
    db_path = home / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT store_json FROM auth_profile_store WHERE store_key = ?",
            ("primary",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(str(row[0]))["profiles"]


def _fake_telegram_token() -> str:
    return "123456:" + ("a" * 35)


def test_setup_defaults_to_none_auth_for_openclaw(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    code = run_cli(tmp_path, "config", "set")
    output = capsys.readouterr().out
    assert code == 0, output
    assert "provider: openclaw" in output
    assert "auth_mode: none" in output


def test_setup_openclaw_without_api_key(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "install_provider_runtime",
        lambda self, provider: {
            "provider": provider,
            "installed": True,
            "already_present": False,
            "method": "pnpm",
            "package": "openclaw",
            "executable": "/mock/bin/openclaw",
        },
    )
    code = run_cli(
        tmp_path,
        "config",
        "set",
        "--provider",
        "openclaw",
        "--workspace",
        "dev",
        "--install-runtime",
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "provider: openclaw" in output
    assert "auth_mode: none" in output
    assert "api_url: <not set>" in output
    assert "spawn_password_default: not set" in output
    assert "runtime_installed: True" in output

    status = run_cli(tmp_path, "config", "show")
    status_output = capsys.readouterr().out
    assert status == 0
    assert "configured: True" in status_output
    assert "api_url: <not set>" in status_output


def test_config_set_control_github_escalation(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    token_path = tmp_path / "github-token"
    code = run_cli(
        tmp_path,
        "config",
        "set",
        "--control-github-repo",
        "octo/example",
        "--control-github-token-path",
        str(token_path),
        "--control-operator",
        "@op",
        "--control-issue-label",
        "clawie-control",
        "--control-github-rate-limit-seconds",
        "42",
    )
    output = capsys.readouterr().out

    assert code == 0, output
    assert "github_repo: octo/example" in output
    assert "operator_allowlist: @op" in output
    config = ClawieService(StateStore(config_dir=tmp_path)).store.read_config()
    assert config["control_github_repo"] == "octo/example"
    assert config["control_github_token_path"] == str(token_path)
    assert config["control_operator_allowlist"] == ["@op"]
    assert config["control_github_issue_labels"] == ["clawie-control"]
    assert config["control_github_rate_limit_seconds"] == 42


def test_control_watchdog_install_writes_systemd_units(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    unit_dir = tmp_path / "systemd"
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_UNIT_FILE",
        unit_dir / "clawie-control-watchdog.service",
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_ALERT_UNIT_FILE",
        unit_dir / "clawie-control-alert.service",
    )
    monkeypatch.setattr(watchdog_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        service,
        "_validated_root_automation_executable",
        lambda path: str(path),
    )

    def fake_which(name: str) -> str | None:
        if name == "clawie":
            return "/usr/bin/clawie"
        if name == "systemctl":
            return "/bin/systemctl"
        return None

    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(watchdog_module.shutil, "which", fake_which)
    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)

    result = service.control_watchdog_install(
        interval_seconds=9,
        notify_command="printf alert",
    )

    unit_text = (unit_dir / "clawie-control-watchdog.service").read_text(encoding="utf-8")
    alert_text = (unit_dir / "clawie-control-alert.service").read_text(encoding="utf-8")
    assert result["enabled"] is True
    assert result["started"] is True
    assert "Description=Clawie control watchdog" in unit_text
    assert "OnFailure=clawie-control-alert.service" in unit_text
    assert "/usr/bin/clawie --config-dir" in unit_text
    assert "clawied run --interval 9" in unit_text
    assert "Restart=always" in unit_text
    # A bounded start limit must be present so a sustained crash-loop trips the
    # unit into `failed` state (halting the hot-loop and firing OnFailure=).
    assert "StartLimitBurst=8" in unit_text
    assert "StartLimitIntervalSec=120" in unit_text
    assert "printf alert" in alert_text
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "clawie-control-watchdog.service"] in calls


def test_control_watchdog_install_fails_when_systemd_start_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    unit_dir = tmp_path / "systemd"
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_UNIT_FILE",
        unit_dir / "clawie-control-watchdog.service",
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_ALERT_UNIT_FILE",
        unit_dir / "clawie-control-alert.service",
    )
    monkeypatch.setattr(watchdog_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        service,
        "_validated_root_automation_executable",
        lambda path: str(path),
    )
    monkeypatch.setattr(watchdog_module.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None)

    def fake_run(
        cmd: list[str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["enable", "--now"]:
            return subprocess.CompletedProcess(cmd, 1, "", "unit failed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)

    with raises(SetupError, match="failed to enable/start control watchdog: unit failed"):
        service.control_watchdog_install(interval_seconds=9)

    config = service.store.read_config()
    assert config["control_watchdog_enabled"] is False


def test_control_watchdog_install_no_start_does_not_mark_enabled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    unit_dir = tmp_path / "systemd"
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_UNIT_FILE",
        unit_dir / "clawie-control-watchdog.service",
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_ALERT_UNIT_FILE",
        unit_dir / "clawie-control-alert.service",
    )
    monkeypatch.setattr(watchdog_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        service,
        "_validated_root_automation_executable",
        lambda path: str(path),
    )
    monkeypatch.setattr(watchdog_module.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None)
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)

    result = service.control_watchdog_install(interval_seconds=9, start=False)

    assert result["enabled"] is False
    assert result["started"] is False
    assert service.store.read_config()["control_watchdog_enabled"] is False
    assert (unit_dir / "clawie-control-watchdog.service").is_file()
    assert ["systemctl", "enable", "--now", "clawie-control-watchdog.service"] not in calls


def test_control_watchdog_cli_status(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    unit_dir = tmp_path / "systemd"
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_UNIT_FILE",
        unit_dir / "clawie-control-watchdog.service",
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_ALERT_UNIT_FILE",
        unit_dir / "clawie-control-alert.service",
    )
    monkeypatch.setattr(watchdog_module.shutil, "which", lambda _name: None)

    code = run_cli(tmp_path, "control", "watchdog", "status")
    output = capsys.readouterr().out

    assert code == 0, output
    assert "unit_file_exists: False" in output
    assert "active: unavailable" in output


def test_control_watchdog_verify_exercises_systemd_restart(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    unit_path = unit_dir / "clawie-control-watchdog.service"
    unit_path.write_text(
        "\n".join(
            [
                "[Service]",
                f"ExecStart=/bin/bash -lc '/usr/bin/clawie --config-dir {tmp_path} clawied run --interval 9'",
                "Restart=always",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_UNIT_FILE",
        unit_path,
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_ALERT_UNIT_FILE",
        unit_dir / "clawie-control-alert.service",
    )

    show_calls = {"count": 0}
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name == "systemctl":
            return "/bin/systemctl"
        return None

    def fake_run(
        cmd: list[str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[1:3] == ["is-active", "clawie-control-watchdog.service"]:
            return subprocess.CompletedProcess(cmd, 0, "active\n", "")
        if cmd[1:3] == ["is-enabled", "clawie-control-watchdog.service"]:
            return subprocess.CompletedProcess(cmd, 0, "enabled\n", "")
        if cmd[1:3] == ["show", "clawie-control-watchdog.service"]:
            show_calls["count"] += 1
            pid = "111" if show_calls["count"] == 1 else "222"
            restarts = "1" if show_calls["count"] == 1 else "2"
            return subprocess.CompletedProcess(
                cmd,
                0,
                f"ActiveState=active\nSubState=running\nMainPID={pid}\nNRestarts={restarts}\nRestart=always\n",
                "",
            )
        if cmd[1:4] == ["kill", "--signal=SIGTERM", "clawie-control-watchdog.service"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

    monkeypatch.setattr(watchdog_module.shutil, "which", fake_which)
    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)
    monkeypatch.setattr(watchdog_module.time, "sleep", lambda _seconds: None)

    code = run_cli(
        tmp_path,
        "control",
        "watchdog",
        "verify",
        "--exercise-restart",
        "--timeout",
        "1",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "passed"
    assert payload["restart_exercised"] is True
    assert ["systemctl", "kill", "--signal=SIGTERM", "clawie-control-watchdog.service"] in calls
    assert any(
        row["message"] == "systemd restarted the watchdog service"
        for row in payload["checks"]
    )


def test_control_watchdog_verify_accepts_quoted_config_dir(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    config_dir = tmp_path / "state with spaces"
    service = ClawieService(StateStore(config_dir=config_dir))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    unit_path = unit_dir / "clawie-control-watchdog.service"
    unit_path.write_text(
        service._control_watchdog_unit_contents(interval_seconds=9, notify_command=""),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_UNIT_FILE",
        unit_path,
    )
    monkeypatch.setattr(
        ClawieService,
        "CONTROL_WATCHDOG_ALERT_UNIT_FILE",
        unit_dir / "clawie-control-alert.service",
    )

    def fake_which(name: str) -> str | None:
        if name == "systemctl":
            return "/bin/systemctl"
        return None

    def fake_run(
        cmd: list[str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["is-active", "clawie-control-watchdog.service"]:
            return subprocess.CompletedProcess(cmd, 0, "active\n", "")
        if cmd[1:3] == ["is-enabled", "clawie-control-watchdog.service"]:
            return subprocess.CompletedProcess(cmd, 0, "enabled\n", "")
        if cmd[1:3] == ["show", "clawie-control-watchdog.service"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                "ActiveState=active\nSubState=running\nMainPID=111\nNRestarts=1\nRestart=always\n",
                "",
            )
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

    monkeypatch.setattr(watchdog_module.shutil, "which", fake_which)
    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)

    code = run_cli(
        config_dir,
        "control",
        "watchdog",
        "verify",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "passed"
    assert any(
        row["message"] == "unit ExecStart points at this config directory"
        for row in payload["checks"]
    )


def test_production_verify_json_aggregates_target_host_proofs(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        ClawieService,
        "doctor",
        lambda self: {"status": "healthy", "checks": [{"status": "pass", "message": "ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "host_validation_report",
        lambda self: {"status": "passed", "checks": [{"status": "pass", "message": "host ok"}]},
    )

    def fake_watchdog_verify(
        self: ClawieService,
        *,
        exercise_restart: bool = False,
        timeout_seconds: int = 30,
    ) -> dict[str, object]:
        calls["exercise_restart"] = exercise_restart
        calls["timeout_seconds"] = timeout_seconds
        return {
            "status": "passed",
            "checks": [{"status": "pass", "message": "watchdog ok"}],
            "restart_exercised": exercise_restart,
        }

    monkeypatch.setattr(ClawieService, "control_watchdog_verify", fake_watchdog_verify)

    def fake_runtime_checks(
        self: ClawieService,
        *,
        all_provider_contracts: bool = False,
        exercise_delivery: bool = False,
    ) -> list[dict[str, object]]:
        calls["all_provider_contracts"] = all_provider_contracts
        calls["exercise_delivery"] = exercise_delivery
        return [{
            "name": "runtime_adapter_openclaw",
            "status": "pass",
            "message": "live runtime proof passed",
        }]

    monkeypatch.setattr(
        ClawieService,
        "_production_runtime_adapter_contract_checks",
        fake_runtime_checks,
    )

    code = run_cli(
        tmp_path,
        "production",
        "verify",
        "--exercise-watchdog-restart",
        "--watchdog-timeout",
        "7",
        "--exercise-runtime-delivery",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)

    checks = {row["name"]: row for row in payload["checks"]}
    assert code == 0
    assert calls == {
        "exercise_restart": True,
        "timeout_seconds": 7,
        "all_provider_contracts": False,
        "exercise_delivery": True,
    }
    assert payload["status"] == "passed"
    assert payload["all_provider_contracts"] is False
    assert payload["exercise_runtime_delivery"] is True
    assert checks["doctor"]["status"] == "pass"
    assert checks["host_validation"]["status"] == "pass"
    assert checks["watchdog"]["status"] == "pass"
    assert checks["watchdog_restart_exercise"]["status"] == "pass"
    assert checks["runtime_adapter_openclaw"]["status"] == "pass"


def test_production_verify_all_provider_contracts_checks_verified_delivery_surface(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "doctor",
        lambda self: {"status": "healthy", "checks": [{"status": "pass", "message": "ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "host_validation_report",
        lambda self: {"status": "passed", "checks": [{"status": "pass", "message": "host ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "control_watchdog_verify",
        lambda self, **kwargs: {
            "status": "passed",
            "checks": [{"status": "pass", "message": "watchdog ok"}],
            "restart_exercised": bool(kwargs.get("exercise_restart", False)),
        },
    )
    monkeypatch.setattr(
        ClawieService,
        "_production_runtime_adapter_contract_checks",
        lambda self, **kwargs: [{
            "name": "runtime_adapter_openclaw",
            "status": "pass",
            "message": "live runtime proof passed",
            "evidence": kwargs,
        }],
    )

    code = run_cli(
        tmp_path,
        "production",
        "verify",
        "--exercise-watchdog-restart",
        "--all-provider-contracts",
        "--exercise-runtime-delivery",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)

    checks = {row["name"]: row for row in payload["checks"]}
    assert code == 0
    assert payload["status"] == "passed"
    assert payload["all_provider_contracts"] is True
    assert checks["watchdog_restart_exercise"]["status"] == "pass"
    assert checks["runtime_adapter_openclaw"]["status"] == "pass"
    assert "runtime_adapter_picoclaw" not in checks
    assert "runtime_adapter_zeroclaw" not in checks


def test_production_verify_requires_watchdog_restart_exercise(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "doctor",
        lambda self: {"status": "healthy", "checks": [{"status": "pass", "message": "ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "host_validation_report",
        lambda self: {"status": "passed", "checks": [{"status": "pass", "message": "host ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "control_watchdog_verify",
        lambda self, **_kwargs: {
            "status": "passed",
            "checks": [{"status": "pass", "message": "watchdog ok"}],
            "restart_exercised": False,
        },
    )

    code = run_cli(tmp_path, "production", "verify", "--json")
    payload = json.loads(capsys.readouterr().out)

    checks = {row["name"]: row for row in payload["checks"]}
    assert code == 1
    assert payload["status"] == "failed"
    assert checks["watchdog"]["status"] == "pass"
    assert checks["watchdog_restart_exercise"]["status"] == "fail"
    assert "--exercise-watchdog-restart" in checks["watchdog_restart_exercise"]["message"]


def test_production_verify_configured_provider_without_delivery_adapter_fails(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        auth_mode="api_key",
        api_key="test-key",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    monkeypatch.setattr(
        ClawieService,
        "doctor",
        lambda self: {"status": "healthy", "checks": [{"status": "pass", "message": "ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "host_validation_report",
        lambda self: {"status": "passed", "checks": [{"status": "pass", "message": "host ok"}]},
    )
    monkeypatch.setattr(
        ClawieService,
        "control_watchdog_verify",
        lambda self, **kwargs: {
            "status": "passed",
            "checks": [{"status": "pass", "message": "watchdog ok"}],
            "restart_exercised": bool(kwargs.get("exercise_restart", False)),
        },
    )

    code = run_cli(
        tmp_path,
        "production",
        "verify",
        "--exercise-watchdog-restart",
        "--all-provider-contracts",
        "--json",
    )
    payload = json.loads(capsys.readouterr().out)

    checks = {row["name"]: row for row in payload["checks"]}
    assert code == 1
    assert payload["status"] == "failed"
    assert checks["runtime_adapter_picoclaw"]["status"] == "fail"
    assert "does not have a verified delegated-task delivery adapter" in checks["runtime_adapter_picoclaw"]["message"]


def test_runtime_install_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_install(self: ClawieService, provider: str) -> dict[str, object]:
        seen.append(provider)
        return {
            "provider": provider,
            "installed": True,
            "already_present": False,
            "method": "brew",
            "package": provider,
            "executable": f"/mock/bin/{provider}",
            "output": "installed",
        }

    monkeypatch.setattr(ClawieService, "install_provider_runtime", fake_install)

    code = run_cli(tmp_path, "runtime", "install", "picoclaw")
    output = capsys.readouterr().out

    assert code == 0
    assert seen == ["picoclaw"]
    assert "Installed runtime for picoclaw" in output
    assert "provider: picoclaw" in output
    assert "method: brew" in output
    assert "executable: /mock/bin/picoclaw" in output


def test_runtime_install_rejects_unverified_existing_openclaw(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(
        service,
        "_resolve_executable_in_service_env",
        lambda executable, linux_user="": "/mock/bin/openclaw",
    )

    class Result:
        returncode = 0
        stdout = "openclaw 2027.1.0"
        stderr = ""

    monkeypatch.setattr("clawie._service_runtime.subprocess.run", lambda *args, **kwargs: Result())

    with raises(SetupError, match="outside the verified delivery range"):
        service.install_provider_runtime("openclaw")

    assert not service._is_runtime_marked_installed(service.store.read_config(), "openclaw")


def test_runtime_install_uses_pinned_openclaw_package_and_verifies_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    calls: list[list[str]] = []
    environments: list[dict[str, str]] = []
    monkeypatch.setattr(
        service,
        "_resolve_executable_in_service_env",
        lambda executable, linux_user="": "/mock/bin/pnpm" if executable == "pnpm" else None,
    )
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda provider: "/mock/bin/openclaw")

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            environments.append({str(key): str(value) for key, value in environment.items()})
        if command == ["/mock/bin/openclaw", "--version"]:
            return Result("openclaw 2026.7.1")
        return Result("installed")

    monkeypatch.setattr("clawie._service_runtime.subprocess.run", fake_run)

    result = service.install_provider_runtime("openclaw")

    toolchain = tmp_path / "shared-toolchain"
    assert calls[0] == [
        "/mock/bin/pnpm",
        "add",
        "-g",
        "--global-dir",
        str(toolchain / "pnpm-global"),
        "--global-bin-dir",
        str(toolchain / "bin"),
        "openclaw@2026.7.1",
    ]
    assert calls[1] == ["/mock/bin/openclaw", "--version"]
    assert environments[0]["PNPM_HOME"] == str(toolchain)
    assert environments[0]["CLAWIE_SHARED_TOOLCHAIN"] == str(toolchain)
    assert str(toolchain / "bin") in environments[0]["PATH"].split(":")
    assert str(Path(environments[0]["PNPM_HOME"]) / "bin") == str(toolchain / "bin")
    assert result["runtime_version"] == "2026.7.1"


def test_generated_openclaw_unit_exposes_request_only_agent_sockets(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    unit = service._generated_user_service_unit_contents("openclaw", "/usr/bin/openclaw")

    assert "Environment=CLAWIE_CONTROL_SOCKET=/run/clawie/control/%U-" in unit
    assert "Environment=CLAWIE_DELEGATION_SOCKET=/run/clawie/control/delegation-%U-" in unit
    assert unit.count(".sock") >= 2


def test_addon_install_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "install_addon",
        lambda self, addon: {
            "addon": addon,
            "installed": True,
            "already_present": False,
            "method": "npm",
            "package": "@googleworkspace/cli",
            "executable": "/mock/bin/gws",
        },
    )
    monkeypatch.setattr(
        ClawieService,
        "get_addon_status",
        lambda self, addon: {
            "addon": addon,
            "label": "Google Workspace CLI",
            "description": "Google Workspace API CLI",
            "installed": True,
            "executable": "/mock/bin/gws",
            "install_method": "npm",
            "install_package": "@googleworkspace/cli",
            "auth_status": "missing",
            "auth_detail": "no addon credentials configured",
            "config_dir": "/mock/shared/gws",
            "shared_scope": "local",
            "linked_agents": [],
        },
    )

    code = run_cli(tmp_path, "addon", "install", "gws")
    output = capsys.readouterr().out

    assert code == 0
    assert "Installed addon gws" in output
    assert "install_method: npm" in output
    assert "install_package: @googleworkspace/cli" in output


def test_ensure_addon_installed_uses_service_path_for_existing_binary(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    def fake_which(cmd: str, path: str | None = None) -> str | None:
        if cmd == "gws" and path and "/home/linuxbrew/.linuxbrew/bin" in path:
            return "/home/linuxbrew/.linuxbrew/bin/gws"
        return None

    monkeypatch.setattr("shutil.which", fake_which)

    result = service.ensure_addon_installed("gws")

    assert result["already_present"] is True
    assert result["executable"] == "/home/linuxbrew/.linuxbrew/bin/gws"


def test_tool_addon_status_does_not_require_installable_executable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(
        service,
        "_resolve_executable_in_service_env",
        lambda executable, linux_user="": f"/usr/bin/{executable}",
    )

    status = service.get_addon_status("web-fetch")
    installed = service.install_addon("web-fetch")

    assert status["addon"] == "web-fetch"
    assert status["installed"] is True
    assert status["auth_status"] == "n/a"
    assert status["install_method"] == "system"
    assert installed["already_present"] is True
    assert installed["method"] == "system"


def test_addon_list_cli_handles_tool_addons(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "_resolve_executable_in_service_env",
        lambda self, executable, linux_user="": f"/usr/bin/{executable}",
    )

    code = run_cli(tmp_path, "addon", "list")
    output = capsys.readouterr().out

    assert code == 0
    assert "web-fetch" in output
    assert "n/a" in output


def test_tool_addon_enable_disable_updates_stored_tools_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    monkeypatch.setattr(
        service,
        "_resolve_executable_in_service_env",
        lambda executable, linux_user="": f"/usr/bin/{executable}",
    )

    service.enable_agent_addon("alice", "web-fetch")
    enabled = service.get_agent("alice")["core_prompts"]["TOOLS.md"]
    assert "clawie-web-fetch-tools-begin" in enabled

    service.disable_agent_addon("alice", "web-fetch")
    disabled = service.get_agent("alice")["core_prompts"]["TOOLS.md"]
    assert "clawie-web-fetch-tools-begin" not in disabled


def test_install_addon_falls_back_to_pnpm_when_npm_missing(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    state = {"installed": False}
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> Result:
        calls.append(cmd)
        state["installed"] = True
        return Result(stdout="installed")

    monkeypatch.setattr(
        service,
        "_resolve_executable_in_service_env",
        lambda executable, linux_user="": (
            "/mock/bin/pnpm"
            if executable == "pnpm"
            else ("/mock/bin/gws" if executable == "gws" and state["installed"] else "")
        ),
    )
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.install_addon("gws")

    assert result["installed"] is True
    assert result["method"] == "pnpm"
    assert calls == [["/mock/bin/pnpm", "add", "-g", "@googleworkspace/cli"]]


def test_service_env_includes_shared_toolchain_paths(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    path_entries = service._service_env("").get("PATH", "").split(":")

    assert str(service._shared_toolchain_home() / "bin") in path_entries
    assert str(service._shared_toolchain_home() / "google-cloud-sdk" / "bin") in path_entries


def test_resolve_executable_skips_inaccessible_path(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    blocked = Path("/blocked-for-clawie/tool")

    def inaccessible_lookup(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("blocked PATH")

    monkeypatch.setattr("shutil.which", inaccessible_lookup)

    def inaccessible(candidate: Path) -> bool:
        if candidate == blocked:
            raise PermissionError("blocked")
        return False

    monkeypatch.setattr(Path, "is_file", inaccessible)

    assert service._resolve_executable_in_service_env(str(blocked)) == ""


def test_install_support_tool_gcloud_downloads_into_shared_toolchain(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(ClawieService, "_shared_toolchain_home", lambda self: tmp_path / "shared-toolchain")
    # Hosted runners may already have gcloud on PATH; this test exercises the
    # archive installer and must not depend on the host image.
    monkeypatch.setattr(service, "_resolve_executable_in_service_env", lambda _name: "")
    downloads: list[str] = []

    class FakeResponse(io.BytesIO):
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self.close()
            return False

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_urlopen(url: str, timeout: int = 0) -> FakeResponse:
        downloads.append(url)
        return FakeResponse(b"archive")

    def fake_extract(archive_path: Path, target_dir: Path) -> None:
        assert archive_path.exists()
        bin_dir = target_dir / "google-cloud-sdk" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        executable = bin_dir / "gcloud"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(executable, 0o755)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        ClawieService,
        "_gcloud_archive_spec",
        staticmethod(
            lambda: (
                "google-cloud-cli-linux-x86_64.tar.gz",
                __import__("hashlib").sha256(b"archive").hexdigest(),
            )
        ),
    )
    monkeypatch.setattr(ClawieService, "_extract_tarball_safe", staticmethod(fake_extract))
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **_: Result(stdout="Google Cloud SDK 1.0\n") if cmd[-1] == "version" else Result(),
    )

    result = service.install_support_tool("gcloud")

    assert result["installed"] is True
    assert result["tool"] == "gcloud"
    assert result["method"] == "archive"
    assert result["scope"] == "local"
    assert result["executable"].endswith("/google-cloud-sdk/bin/gcloud")
    assert os.access(Path(result["executable"]), os.X_OK)
    assert downloads and downloads[0].endswith(".tar.gz")


def test_install_support_tool_rejects_unverified_archive(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(
        ClawieService,
        "_shared_toolchain_home",
        lambda self: tmp_path / "shared-toolchain",
    )
    monkeypatch.setattr(service, "_resolve_executable_in_service_env", lambda _name: "")

    class FakeResponse(io.BytesIO):
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self.close()
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"tampered archive"),
    )
    monkeypatch.setattr(
        ClawieService,
        "_gcloud_archive_spec",
        staticmethod(lambda: ("google-cloud-cli-linux-x86_64.tar.gz", "0" * 64)),
    )

    with raises(SetupError, match="SHA256 mismatch"):
        service.install_support_tool("gcloud")

    assert not (tmp_path / "shared-toolchain" / "google-cloud-sdk").exists()
    assert not list((tmp_path / "shared-toolchain").glob(".clawie-gcloud-stage-*"))


def test_shared_toolchain_permissions_are_not_world_writable(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    toolchain = tmp_path / "toolchain"
    bin_dir = toolchain / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(toolchain, 0o777)
    os.chmod(bin_dir, 0o777)
    os.chmod(executable, 0o777)

    monkeypatch.setattr(ClawieService, "SHARED_TOOLCHAIN_DIR", toolchain)
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    service._ensure_shared_toolchain_root()

    assert (toolchain.stat().st_mode & 0o777) == 0o755
    assert (bin_dir.stat().st_mode & 0o777) == 0o755
    assert (executable.stat().st_mode & 0o777) == 0o755


def test_parse_gws_status_output_marks_invalid_client_config_missing(tmp_path: Path) -> None:
    config_dir = tmp_path / "gws"
    payload = parse_gws_status_output(
        json.dumps(
            {
                "auth_method": "oauth2",
                "client_config": str(config_dir / "client_secret.json"),
                "client_config_error": "missing field `client_secret`",
                "client_config_exists": True,
                "credential_source": "none",
                "has_refresh_token": True,
                "plain_credentials": str(config_dir / "credentials.json"),
                "plain_credentials_exists": True,
                "storage": "plaintext",
            }
        ),
        config_dir=config_dir,
    )

    assert payload["auth_status"] == "missing"
    assert payload["login_required"] is True
    assert "invalid" in str(payload["detail"]).lower()
    assert payload["client_secret_present"] is True
    assert payload["client_config_error"] == "missing field `client_secret`"


def test_setup_api_key_mode_requires_api_key(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    code = run_cli(tmp_path, "config", "set", "--provider", "picoclaw", "--auth-mode", "api_key")
    output = capsys.readouterr().err
    assert code == 1
    assert "API key is required when --auth-mode api_key is selected" in output


def test_config_set_preserves_every_omitted_value(tmp_path: Path) -> None:
    assert run_cli(
        tmp_path,
        "config",
        "set",
        "--provider",
        "picoclaw",
        "--auth-mode",
        "linked",
        "--subscription",
        "pro",
        "--workspace",
        "production",
        "--api-url",
        "https://api.picoclaw.example/v1",
    ) == 0

    assert run_cli(tmp_path, "config", "set", "--subscription", "enterprise") == 0

    config = StateStore(config_dir=tmp_path).read_config()
    assert config["provider"] == "picoclaw"
    assert config["auth_mode"] == "linked"
    assert config["subscription"] == "enterprise"
    assert config["workspace"] == "production"
    assert config["api_url"] == "https://api.picoclaw.example/v1"
    assert config["provider_credentials"]["picoclaw"]["auth_mode"] == "linked"


def test_create_agent_with_provider_override(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    code = run_cli(
        tmp_path,
        "agent",
        "create",
        "pico",
        "--provider",
        "picoclaw",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "provider: picoclaw" in output


def test_create_agent_uses_openclaw_as_default_provider(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "create", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider: openclaw" in output


def test_create_agent_uses_random_default_name_when_omitted(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("clawie.default_names.random.choice", lambda _items: "Abulafia")
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "create")
    output = capsys.readouterr().out

    assert code == 0
    assert "Created agent definition Abulafia" in output
    assert "no Linux user or provider service" in output
    state = StateStore(config_dir=tmp_path).read_state()
    assert "Abulafia" in state["agents"]


def test_random_default_name_skips_existing_names_case_insensitively(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    seen_candidates: list[list[str]] = []

    def choose(items: list[str]) -> str:
        seen_candidates.append(list(items))
        return items[0]

    monkeypatch.setattr("clawie.default_names.random.choice", choose)
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "abulafia") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "create")
    output = capsys.readouterr().out

    assert code == 0
    assert "Abulafia" not in seen_candidates[0]
    assert "Created agent definition Diotallevi" in output


def test_agent_provider_set_changes_provider(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "teleclaw", "--provider", "zeroclaw") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "provider", "set", "teleclaw", "openclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "Changed provider for teleclaw to openclaw" in output
    assert "provider: openclaw" in output


def test_agent_service_status_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "agent_service_action",
        lambda self, agent_id, action: {
            "agent_id": agent_id,
            "provider": "openclaw",
            "linux_user": "alice",
            "action": action,
            "service_status": "running",
            "service_mode": "systemd",
            "output": "active (running)",
        },
    )

    code = run_cli(tmp_path, "agent", "service", "status", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "alice: running (systemd)" in output
    assert "Provider: openclaw" in output


def test_runtime_service_status_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "local_claw_service_action",
        lambda self, provider, action: {
            "provider": provider,
            "action": action,
            "service_status": "running",
            "service_mode": "systemd",
            "output": "active (running)",
        },
    )

    code = run_cli(tmp_path, "runtime", "service", "status", "picoclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "picoclaw: running (systemd)" in output


def test_create_agent_and_monitor_snapshot(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--api-key", "zc_live_1234", "--workspace", "prod") == 0
    capsys.readouterr()
    assert (
        run_cli(
            tmp_path,
            "agent",
            "create",
            "alice",
            "--template",
            "baseline",
            "--channel-strategy",
            "new",
        )
        == 0
    )
    capsys.readouterr()

    # `dashboard` is now a deprecated alias for `status --watch`; piped (non-TTY)
    # it prints a single status snapshot.
    code = run_cli(tmp_path, "dashboard")
    output = capsys.readouterr().out
    assert code == 0
    assert "deprecated" in output
    assert "alice" in output
    assert "cpu%" in output


def test_agent_list_shows_created_agents(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "alice") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "list")
    output = capsys.readouterr().out
    assert code == 0
    assert "agent_id" in output
    assert "status" in output
    assert "alice" in output


def test_legacy_top_level_commands_are_rejected(tmp_path: Path) -> None:
    with raises(SystemExit) as setup_exit:
        run_cli(tmp_path, "setup")
    assert setup_exit.value.code == 2

    with raises(SystemExit) as list_exit:
        run_cli(tmp_path, "list")
    assert list_exit.value.code == 2


def test_runtime_status_shows_auth_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "list_local_runtime_statuses",
        lambda self, refresh=True: [
            {
                "provider": "zeroclaw",
                "linux_user": "alice",
                "service_status": "running",
                "service_mode": "systemd",
                "auth_mode": "linked",
                "auth_status": "expired",
                "auth_profile": "openai-codex:default",
                "expires_at": "2026-03-02T05:47:04Z",
                "root": "/home/alice/.zeroclaw",
            }
        ],
    )

    code = run_cli(tmp_path, "runtime", "status")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider" in output
    assert "auth" in output
    assert "zeroclaw" in output
    assert "expired" in output


def test_runtime_login_prints_auth_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "local_claw_auth_login",
        lambda self, provider: {
            "provider": provider,
            "linux_user": "alice",
            "home": "/home/alice",
            "auth_mode": "linked",
            "auth_status": "ready",
            "auth_profile": "openai-codex:default",
            "account": "acct-1",
            "expires_at": "2026-03-22T05:47:04Z",
            "last_refresh": "2026-03-08T01:02:03Z",
            "source": "cli",
            "detail": "oauth",
            "login_required": False,
            "action_performed": "login",
        },
    )

    code = run_cli(tmp_path, "runtime", "login", "zeroclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "Completed linked login for zeroclaw" in output
    assert "auth_status: ready" in output
    assert "auth_profile: openai-codex:default" in output


def test_agent_auth_show_prints_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "agent_auth_status",
        lambda self, agent_id: {
            "agent_id": agent_id,
            "provider": "zeroclaw",
            "linux_user": "alice",
            "home": "/home/alice",
            "auth_mode": "linked",
            "auth_status": "expired",
            "auth_profile": "openai-codex:default",
            "account": "acct-1",
            "expires_at": "2026-03-02T05:47:04Z",
            "last_refresh": "2026-02-28T08:43:04Z",
            "source": "file:auth-profiles.json",
            "detail": "oauth",
            "login_required": True,
        },
    )

    code = run_cli(tmp_path, "agent", "auth", "show", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "Agent Auth" in output
    assert "auth_status: expired" in output
    assert "login_required: True" in output


def test_agent_auth_login_prints_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "agent_auth_login",
        lambda self, agent_id: {
            "agent_id": agent_id,
            "provider": "zeroclaw",
            "linux_user": "alice",
            "home": "/home/alice",
            "auth_mode": "linked",
            "auth_status": "ready",
            "auth_profile": "openai-codex:default",
            "account": "acct-1",
            "expires_at": "2026-03-22T05:47:04Z",
            "last_refresh": "2026-03-08T01:02:03Z",
            "source": "cli",
            "detail": "oauth",
            "login_required": False,
            "action_performed": "refresh",
        },
    )

    code = run_cli(tmp_path, "agent", "auth", "login", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "Refreshed linked login for alice" in output
    assert "auth_status: ready" in output


def test_spawn_requires_root(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    # Force a non-root euid so the test also holds when the suite runs as root.
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert run_cli(tmp_path, "config", "set", "--api-key", "zc_live_1234") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "runtime", "create", "sam")
    output = capsys.readouterr().err
    assert code == 1
    assert "requires root privileges" in output


def test_spawn_success_with_mocks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--api-key", "zc_live_1234") == 0
    capsys.readouterr()

    src_home = tmp_path / "source-home"
    src_home.mkdir(parents=True)
    (src_home / ".bashrc").write_text("# test", encoding="utf-8")
    (src_home / ".gitconfig").write_text("[user]\nname = test\n", encoding="utf-8")

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    agent_home = tmp_path / "sam-home"
    agent_home.mkdir()

    class PwdRow:
        pw_dir = str(agent_home)
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda user: PwdRow())
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "sam",
        "--user",
        "sam",
        "--source-home",
        str(src_home),
        "--skip-config-copy",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Spawned linux user sam" in output

    state = StateStore(config_dir=tmp_path).read_state()
    assert "sam" in state["agents"]
    assert state["agents"]["sam"]["agent"]["linux_user"] == "sam"


def test_spawn_uses_random_default_name_when_agent_id_omitted(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr("clawie.default_names.random.choice", lambda _items: "Abulafia")
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    agent_home = tmp_path / "abulafia-home"
    agent_home.mkdir()
    source_home = tmp_path / "source-home"
    source_home.mkdir()

    class PwdRow:
        pw_dir = str(agent_home)
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda user: PwdRow())
    monkeypatch.setattr(ClawieService, "_default_source_home", lambda self: source_home)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(tmp_path, "runtime", "create", "--skip-config-copy")
    output = capsys.readouterr().out

    assert code == 0, output
    assert "Spawned linux user abulafia and provisioned Abulafia" in output
    state = StateStore(config_dir=tmp_path).read_state()
    assert state["agents"]["Abulafia"]["agent"]["linux_user"] == "abulafia"


def test_spawn_rolls_back_user_and_agent_record_after_late_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://example.invalid",
    )
    home = tmp_path / "worker-home"
    home.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    calls: list[list[str]] = []

    class PwdRow:
        pw_dir = str(home)
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    class Result:
        def __init__(self, returncode: int = 0) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        return Result(1 if command[:2] == ["id", "-u"] else 0)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda user: PwdRow())
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(service, "ensure_provider_runtime", lambda provider: {})
    monkeypatch.setattr(service, "_disable_ssh_login_for_user", lambda user: False)
    monkeypatch.setattr(service, "_default_source_home", lambda: source)
    monkeypatch.setattr(
        service,
        "_ensure_workspace_accessible",
        lambda *args, **kwargs: (_ for _ in ()).throw(SetupError("late failure")),
    )

    with raises(SetupError, match="late failure"):
        service.spawn_linux_user("worker", linux_user="worker", copy_configs=False)

    assert any(command[:2] == ["userdel", "-r"] for command in calls)
    assert "worker" not in service.store.read_state()["agents"]


def test_setup_sets_global_spawn_password(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    code = run_cli(
        tmp_path,
        "config",
        "set",
        "--provider",
        "openclaw",
        "--spawn-password",
        "GlobalPass123!",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "spawn_password_default: set" in output

    config = StateStore(config_dir=tmp_path).read_config()
    assert str(config.get("spawn_password_hash", "")).startswith("$6$")


def test_spawn_uses_global_password_hash(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(
        tmp_path,
        "config",
        "set",
        "--provider",
        "openclaw",
        "--spawn-password",
        "GlobalPass123!",
    ) == 0
    capsys.readouterr()

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_spawn_passwd_home(monkeypatch, tmp_path, "sam")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "sam",
        "--user",
        "sam",
        "--skip-config-copy",
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "Spawned linux user sam" in output
    # The pre-hashed password is applied via `chpasswd -e` on stdin, never as a
    # `usermod -p <hash>` argv element (which would leak the hash through
    # world-readable /proc/<pid>/cmdline).
    assert any(cmd == ["chpasswd", "-e"] for cmd in calls)
    assert not any(cmd[:2] == ["usermod", "-p"] for cmd in calls)


def test_spawn_generates_password_and_prints_it(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    calls: list[tuple[list[str], object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((cmd, kwargs.get("input")))

        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_spawn_passwd_home(monkeypatch, tmp_path, "sam-default")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "sam-default",
        "--user",
        "sam-default",
        "--skip-config-copy",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Password source: generated-password" in output
    assert "SSH login: disabled for spawned Linux user" in output
    chpasswd_inputs = [str(input_data) for cmd, input_data in calls if cmd == ["chpasswd"]]
    assert len(chpasswd_inputs) == 1
    username, _, password = chpasswd_inputs[0].rstrip("\n").partition(":")
    assert username == "sam-default"
    assert len(password) >= 12
    assert password != "clawie"
    assert f"Password: {password}" in output


def test_spawn_uses_per_agent_plaintext_password(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    calls: list[tuple[list[str], object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((cmd, kwargs.get("input")))

        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_spawn_passwd_home(monkeypatch, tmp_path, "sam2")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "sam2",
        "--user",
        "sam2",
        "--skip-config-copy",
        "--password",
        "LocalPass123!",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Spawned linux user sam2" in output
    assert "Password source: spawn-password" in output
    assert "Password: LocalPass123!" in output
    assert any(cmd == ["chpasswd"] and input_data == "sam2:LocalPass123!\n" for cmd, input_data in calls)


def test_spawn_creates_linux_user_with_bash_shell(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_spawn_passwd_home(monkeypatch, tmp_path, "sam-shell")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "sam-shell",
        "--user",
        "sam-shell",
        "--skip-config-copy",
    )
    _ = capsys.readouterr().out
    assert code == 0
    useradd_cmd = next((cmd for cmd in calls if cmd[:2] == ["useradd", "-m"]), [])
    assert useradd_cmd[:3] == ["useradd", "-m", "-s"]
    assert useradd_cmd[3] == "/bin/bash"


def test_disable_ssh_login_for_user_writes_denyusers_and_reloads(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    deny_file = tmp_path / "sshd_config.d" / "99-clawie-no-ssh.conf"
    monkeypatch.setattr(ClawieService, "SSHD_DENY_USERS_FILE", deny_file)

    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return Result(0)

    monkeypatch.setattr("subprocess.run", fake_run)

    assert service._disable_ssh_login_for_user("alice") is True
    assert service._disable_ssh_login_for_user("bob") is True

    rendered = deny_file.read_text(encoding="utf-8")
    assert "DenyUsers alice bob" in rendered
    assert any(cmd[:3] == ["systemctl", "reload", "ssh"] for cmd in calls)


def test_purge_removes_agent_and_linux_user_with_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "teleclaw") == 0
    capsys.readouterr()

    store = StateStore(config_dir=tmp_path)
    state = store.read_state()
    managed_home = tmp_path / "teleclaw-home"
    managed_home.mkdir()
    operation_id = "a" * 32
    info = state["agents"]["teleclaw"]["agent"]
    info["linux_user"] = "teleclaw"
    info["linux_uid"] = 12345
    info["linux_home"] = str(managed_home)
    info["linux_user_managed"] = True
    info["managed_user_operation_id"] = operation_id
    (managed_home / ".clawie-managed-user.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "agent_id": "teleclaw",
                "linux_user": "teleclaw",
                "linux_uid": 12345,
                "operation_id": operation_id,
                "state_root": str(store.root.resolve()),
            }
        ),
        encoding="utf-8",
    )
    store.write_state(state)

    calls: list[list[str]] = []

    removed = False

    def fake_run(cmd: list[str], **_: object) -> object:
        nonlocal removed
        calls.append(cmd)

        class Result:
            def __init__(self, returncode: int = 0) -> None:
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""

        if cmd[:2] == ["id", "-u"]:
            return Result(1 if removed else 0)
        if cmd[:2] == ["userdel", "-r"]:
            removed = True
            (managed_home / ".clawie-managed-user.json").unlink()
            managed_home.rmdir()
        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_linux_home_for_user", lambda self, user: managed_home)
    monkeypatch.setattr(
        ClawieService,
        "_run_managed_provider_service_action",
        lambda self, **kwargs: {"service_status": "stopped"},
    )
    monkeypatch.setattr(ClawieService, "_provider_process_live_ps_only", lambda self, *args: False)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)

    code = run_cli(tmp_path, "agent", "purge", "teleclaw", "--yes")
    output = capsys.readouterr().out
    assert code == 0
    assert "Purged agent teleclaw" in output
    assert "runtime_stopped=True" in output
    assert any(cmd[:2] == ["userdel", "-r"] for cmd in calls)
    assert "teleclaw" not in StateStore(config_dir=tmp_path).read_state()["agents"]


def test_purge_stops_managed_user_manager_before_userdel(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    calls: list[list[str]] = []
    process_states = iter((True, False))

    class Result:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = stderr

    def fake_run(command: list[str], **_kwargs: object) -> Result:
        calls.append(command)
        if command[1:2] == ["terminate-user"]:
            return Result(1, "not logged in")
        return Result()

    monkeypatch.setattr(service, "_run_managed_provider_service_action", lambda **_kwargs: {"service_status": "stopped"})
    monkeypatch.setattr(service, "_run_systemd_user_command", lambda *_args: {"ok": True})
    monkeypatch.setattr(service, "_linux_home_for_user", lambda _user: None)
    monkeypatch.setattr(service, "_managed_provider_process_live_for_purge", lambda *_args: False)
    monkeypatch.setattr(
        service,
        "_linux_uid_has_processes_for_purge",
        lambda _uid: next(process_states),
    )
    monkeypatch.setattr(
        "clawie._service_agents.shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"loginctl", "systemctl"} else None,
    )
    monkeypatch.setattr("clawie._service_agents.subprocess.run", fake_run)

    stopped = service._stop_managed_runtime_for_purge(
        agent_id="worker",
        linux_user="worker",
        info={"provider": "openclaw", "linux_uid": 12345},
    )

    assert stopped is True
    assert ["/usr/bin/loginctl", "disable-linger", "worker"] in calls
    assert ["/usr/bin/loginctl", "terminate-user", "worker"] in calls
    assert ["/usr/bin/systemctl", "stop", "user@12345.service"] in calls


def test_purge_preserves_agent_and_user_when_runtime_cannot_be_stopped(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(provider="openclaw")
    agent = service.create_agent(
        agent_id="worker",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
    )
    managed_home = tmp_path / "worker-home"
    managed_home.mkdir()
    operation_id = "b" * 32
    info = agent["agent"]
    info.update(
        {
            "linux_user": "worker",
            "linux_uid": 12346,
            "linux_home": str(managed_home),
            "linux_user_managed": True,
            "managed_user_operation_id": operation_id,
        }
    )
    (managed_home / ".clawie-managed-user.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "agent_id": "worker",
                "linux_user": "worker",
                "linux_uid": 12346,
                "operation_id": operation_id,
                "state_root": str(service.store.root.resolve()),
            }
        ),
        encoding="utf-8",
    )
    state = service.store.read_state()
    state["agents"]["worker"] = agent
    service.store.write_state(state)
    calls: list[list[str]] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(service, "_linux_user_exists", lambda _user: True)
    monkeypatch.setattr(service, "_linux_home_for_user", lambda _user: managed_home)
    monkeypatch.setattr(
        service,
        "_run_managed_provider_service_action",
        lambda **kwargs: (_ for _ in ()).throw(SetupError("stop failed")),
    )
    monkeypatch.setattr(service, "_run_systemd_user_command", lambda *args: {"ok": False})
    monkeypatch.setattr(service, "_force_stop_provider_processes", lambda *args: None)
    monkeypatch.setattr(service, "_managed_provider_process_live_for_purge", lambda *args: True)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kwargs: calls.append(cmd))

    with raises(SetupError, match="runtime is still running"):
        service.purge_agent("worker")

    assert not any(command[:2] == ["userdel", "-r"] for command in calls)
    assert "worker" in service.store.read_state()["agents"]


def test_purge_preserves_record_when_userdel_leaves_the_managed_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(provider="openclaw")
    agent = service.create_agent(
        agent_id="worker",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
    )
    managed_home = tmp_path / "worker-home"
    managed_home.mkdir()
    operation_id = "f" * 32
    agent["agent"].update(
        {
            "linux_user": "worker",
            "linux_uid": 12347,
            "linux_home": str(managed_home),
            "linux_user_managed": True,
            "managed_user_operation_id": operation_id,
        }
    )
    (managed_home / ".clawie-managed-user.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "agent_id": "worker",
                "linux_user": "worker",
                "linux_uid": 12347,
                "operation_id": operation_id,
                "state_root": str(service.store.root.resolve()),
            }
        ),
        encoding="utf-8",
    )
    state = service.store.read_state()
    state["agents"]["worker"] = agent
    service.store.write_state(state)
    existence_checks = iter((True, False))

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(service, "_linux_user_exists", lambda _user: next(existence_checks))
    monkeypatch.setattr(service, "_linux_home_for_user", lambda _user: managed_home)
    monkeypatch.setattr(service, "_stop_managed_runtime_for_purge", lambda **_kwargs: True)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())

    with raises(SetupError, match="managed home .* still exists"):
        service.purge_agent("worker")

    assert managed_home.is_dir()
    assert "worker" in service.store.read_state()["agents"]


def test_purge_refuses_unmarked_linux_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "legacy") == 0
    capsys.readouterr()
    store = StateStore(config_dir=tmp_path)
    state = store.read_state()
    home = tmp_path / "legacy-home"
    home.mkdir()
    state["agents"]["legacy"]["agent"].update(
        {"linux_user": "legacy", "linux_home": str(home)}
    )
    store.write_state(state)

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(ClawieService, "_linux_home_for_user", lambda self, user: home)

    code = run_cli(tmp_path, "agent", "purge", "legacy", "--yes")
    output = capsys.readouterr().err

    assert code == 1
    assert "no managed-user ownership proof" in output
    assert "legacy" in StateStore(config_dir=tmp_path).read_state()["agents"]


def test_purge_requires_root_for_spawned_linux_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "teleclaw") == 0
    capsys.readouterr()

    store = StateStore(config_dir=tmp_path)
    state = store.read_state()
    state["agents"]["teleclaw"]["agent"]["linux_user"] = "teleclaw"
    store.write_state(state)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    code = run_cli(tmp_path, "agent", "purge", "teleclaw", "--yes")
    output = capsys.readouterr().err
    assert code == 1
    assert "purge requires root privileges" in output


def test_delete_refuses_to_orphan_a_managed_linux_runtime(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(provider="openclaw")
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "clawie-alice"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)

    with raises(SetupError, match="use 'clawie agent purge alice'"):
        service.delete_agent("alice")

    assert "alice" in service.store.read_state()["agents"]


def test_agent_delete_requires_confirmation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    assert run_cli(tmp_path, "agent", "create", "draft") == 0
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert run_cli(tmp_path, "agent", "delete", "draft") == 1

    assert "delete cancelled" in capsys.readouterr().err
    assert "draft" in StateStore(config_dir=tmp_path).read_state()["agents"]


def test_agent_delete_yes_is_noninteractive(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    assert run_cli(tmp_path, "agent", "create", "draft") == 0
    capsys.readouterr()

    assert run_cli(tmp_path, "agent", "delete", "draft", "--yes") == 0

    assert "Deleted agent draft" in capsys.readouterr().out
    assert "draft" not in StateStore(config_dir=tmp_path).read_state()["agents"]


def test_purge_accepts_positional_agent_id(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "teleclaw") == 0
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    code = run_cli(tmp_path, "agent", "purge", "teleclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "Purged agent teleclaw" in output


def test_spawn_imports_channels_from_zeroclaw_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "zeroclaw") == 0
    capsys.readouterr()

    source_home = tmp_path / "source-home"
    source_home.mkdir(parents=True)
    zc_dir = source_home / ".zeroclaw"
    zc_dir.mkdir(parents=True)
    (zc_dir / "config.toml").write_text(
        """
[channels_config]
cli = true

[channels_config.telegram]
bot_token = "abc123"
allowed_users = ["*"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_spawn_passwd_home(monkeypatch, tmp_path, "teleclaw")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "teleclaw",
        "--user",
        "teleclaw",
        "--source-home",
        str(source_home),
        "--skip-config-copy",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Spawned linux user teleclaw" in output

    agent = StateStore(config_dir=tmp_path).read_state()["agents"]["teleclaw"]
    kinds = {str(row.get("kind", "")) for row in agent.get("channels", [])}
    assert "telegram" in kinds
    assert str(agent.get("channel_strategy", "")) == "migrate"


def test_spawn_clones_core_prompts_from_local_source_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "zeroclaw") == 0
    capsys.readouterr()

    source_home = tmp_path / "source-home"
    workspace = source_home / ".zeroclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("You are teleclaw.\n", encoding="utf-8")

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            # `id -u` must report the user is absent (non-zero) so spawn
            # proceeds; every provisioning command (useradd/chpasswd/usermod)
            # succeeds, mirroring a real root spawn.
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _mock_spawn_passwd_home(monkeypatch, tmp_path, "teleclaw2")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(
        tmp_path,
        "runtime",
        "create",
        "teleclaw2",
        "--user",
        "teleclaw2",
        "--source-home",
        str(source_home),
        "--skip-config-copy",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Spawned linux user teleclaw2" in output

    agent = StateStore(config_dir=tmp_path).read_state()["agents"]["teleclaw2"]
    prompts = agent.get("core_prompts", {})
    assert str(prompts.get("SOUL.md", "")) == "You are teleclaw.\n"


def test_source_prompt_discovery_ignores_provider_workspace_symlink(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    source_home = tmp_path / "source-home"
    provider_home = source_home / ".openclaw"
    external_workspace = tmp_path / "external-workspace"
    provider_home.mkdir(parents=True)
    external_workspace.mkdir()
    (external_workspace / "SOUL.md").write_text("must not be followed\n", encoding="utf-8")
    (provider_home / "workspace").symlink_to(external_workspace, target_is_directory=True)

    prompts = service._read_core_prompts_from_home("openclaw", source_home)

    assert prompts
    assert all(value == "" for value in prompts.values())
    assert (external_workspace / "SOUL.md").read_text(encoding="utf-8") == "must not be followed\n"


def test_agents_clone_prompts_copies_core_prompt_payload(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "zeroclaw") == 0
    capsys.readouterr()
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.create_agent(
        agent_id="src",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    service.create_agent(
        agent_id="dst",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    service.set_agent_core_prompt("src", "SOUL.md", "source soul", sync_to_disk=False)

    code = run_cli(
        tmp_path,
        "agent",
        "prompt",
        "copy",
        "src",
        "dst",
        "--no-apply-to-disk",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Cloned core prompts src -> dst" in output
    dst = StateStore(config_dir=tmp_path).read_state()["agents"]["dst"]
    assert str(dst.get("core_prompts", {}).get("SOUL.md", "")) == "source soul"


def test_prompt_write_permission_error_does_not_stage_to_tmp(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    service = ClawieService(StateStore(config_dir=tmp_path))
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "alice"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)
    service.set_agent_credential_bundles("alice", ["provider-auth"])

    home = tmp_path / "alice-home"
    home.mkdir()
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    def deny_write(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError("denied")

    staged: list[str] = []

    def fail_stage(*_args: object, **_kwargs: object) -> Path:
        staged.append("called")
        raise AssertionError("prompt staging should not be used")

    monkeypatch.setattr(service, "_write_core_prompt_file", deny_write)
    monkeypatch.setattr(service, "_stage_prompt_file", fail_stage)

    with raises(SetupError, match="staging is disabled"):
        service.write_agent_core_prompts_to_disk("alice")
    assert staged == []


def test_store_creates_sqlite_db(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    store.ensure()
    assert store.db_path.exists()


def test_store_migrates_legacy_default_channels_from_baseline_and_agents(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    store.ensure()

    legacy_baseline = {
        "channels": [
            {"kind": "chat", "name": "support"},
            {"kind": "email", "name": "inbox"},
        ],
        "agent_defaults": {
            "runtime": "picoclaw-agent",
            "autostart": True,
            "heartbeat_seconds": 30,
        },
    }
    legacy_agent = {
        "agent_id": "alice",
        "display_name": "",
        "source_template": "baseline",
        "channel_strategy": "new",
        "channels": [
            {"kind": "chat", "name": "alice-support", "enabled": True},
            {"kind": "email", "name": "alice-inbox", "enabled": True},
            {"kind": "telegram", "name": "team", "enabled": True},
        ],
        "agent": {
            "provider": "picoclaw",
            "runtime": "picoclaw-agent",
        },
    }

    with store._connect() as conn:
        conn.execute(
            "UPDATE templates SET payload = ? WHERE name = ?",
            (json.dumps(legacy_baseline, sort_keys=True), "baseline"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agents(agent_id, payload) VALUES (?, ?)",
            ("alice", json.dumps(legacy_agent, sort_keys=True)),
        )
        conn.execute("DELETE FROM config WHERE key = ?", ("schema_version",))
        conn.commit()

    store.ensure()
    state = store.read_state()
    baseline = state["templates"]["baseline"]
    alice = state["agents"]["alice"]
    assert baseline["channels"] == []
    assert alice["channels"] == [{"kind": "telegram", "name": "team", "enabled": True}]
    assert store.read_config()["schema_version"] == 3


def test_store_falls_back_to_tmp_when_home_is_unwritable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True)
    home_dir.chmod(0o500)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.delenv("CLAWIE_HOME", raising=False)
    try:
        store = StateStore()
        store.ensure()
        assert store.db_path.exists()
        assert str(store.root).startswith(tempfile.gettempdir())
    finally:
        home_dir.chmod(0o700)


def test_store_uses_sudo_user_home_by_default(monkeypatch: MonkeyPatch) -> None:
    class UserInfo:
        pw_dir = "/home/alice"

    monkeypatch.delenv("CLAWIE_HOME", raising=False)
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda _: UserInfo())

    store = StateStore()
    assert str(store.root) == "/home/alice/.clawie"


def test_dashboard_local_rows_use_sudo_user_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class UserInfo:
        pw_dir = "/home/alice"

    seen: list[str] = []

    def fake_detect(home_dir: str) -> list[dict[str, object]]:
        seen.append(home_dir)
        return [{"provider": "zeroclaw", "root": f"{home_dir}/.zeroclaw", "markers": []}]

    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda _: UserInfo())
    monkeypatch.setattr("clawie.service.detect_installed_providers", fake_detect)

    service = ClawieService(StateStore(config_dir=tmp_path))
    snapshot = service.performance_snapshot(refresh=False)
    ids = {str(row.get("agent_id", "")) for row in snapshot["rows"]}

    assert seen == ["/home/alice"]
    assert "@local:zeroclaw" in ids


def test_batch_create_returns_nonzero_on_errors(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--api-key", "zc_live_1234") == 0
    capsys.readouterr()

    batch_file = tmp_path / "agents.json"
    batch_file.write_text(
        json.dumps(
            [
                {"agent_id": "maria", "display_name": "Maria"},
                {"display_name": "MissingId"},
            ]
        ),
        encoding="utf-8",
    )

    code = run_cli(tmp_path, "agent", "create-batch", str(batch_file))
    output = capsys.readouterr().out
    assert code == 1
    assert "created: 1" in output
    assert "errors: 1" in output


def test_provider_credential_path_registry_contains_codex_and_openai() -> None:
    paths = credential_paths_for_providers(["zeroclaw", "openclaw"])
    assert ".codex" in paths
    assert ".config/openai" in paths


def test_load_codex_auth_prefers_access_token_expiry_over_id_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": _fake_jwt({"exp": 1773823443}),
                    "refresh_token": "ref",
                    "id_token": _fake_jwt({"exp": 1772963042}),
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-03-08T08:44:02.652868418Z",
            }
        ),
        encoding="utf-8",
    )

    payload = load_codex_auth(home)
    assert payload["expires_at"] == "2026-03-18T08:44:03Z"


def test_agents_credentials_commands_show_and_set(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "alice") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "credentials", "list")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider-auth" in output
    assert "git" in output

    code = run_cli(tmp_path, "agent", "credentials", "show", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "selected: <none>" in output
    assert "provider-auth" in output

    code = run_cli(
        tmp_path,
        "agent",
        "credentials",
        "set",
        "alice",
        "provider-auth",
        "git",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Selected bundles: provider-auth, git" in output

    code = run_cli(tmp_path, "agent", "credentials", "show", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider-auth" in output
    assert "git" in output


def test_new_agents_do_not_select_shared_provider_auth_by_default(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )

    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    policy = service.get_agent_credential_sync("alice")

    assert agent["credential_sync"]["bundles"] == []
    assert agent["credential_sync"]["shared_provider_auth"] is False
    assert policy["selected_bundles"] == []
    assert all(not bool(row["default"]) for row in policy["bundles"])


def test_service_syncs_and_revokes_selected_credential_bundles(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "alice"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)
    service.set_agent_credential_bundles("alice", ["provider-auth"])

    source_home = tmp_path / "source-home"
    source_home.mkdir(parents=True)
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": _fake_jwt({"exp": 1893456000}),
                    "refresh_token": "ref",
                    "id_token": "",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-03-08T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (source_home / ".gitconfig").write_text("[user]\nname = Alice\n", encoding="utf-8")
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", lambda _cmd, **_kwargs: Result())
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)

    service.set_agent_credential_bundles("alice", ["provider-auth", "git"])
    sync = service.sync_agent_credentials("alice", source_home=source_home)
    assert "provider-auth" in sync["bundles"]
    assert "git" in sync["bundles"]
    assert (shared_home / ".codex" / "auth.json").exists()
    assert (shared_home / ".codex" / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".codex" / "auth.json").is_file()
    assert not (target_home / ".codex" / "auth.json").is_symlink()
    assert (target_home / ".codex" / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".gitconfig").exists()

    revoked = service.revoke_agent_credentials("alice", bundles=["git"])
    assert "git" in revoked["bundles"]
    assert not (target_home / ".gitconfig").exists()
    assert (target_home / ".codex" / "auth.json").exists()

    updated = service.get_agent("alice")
    assert "provider-auth" in updated["credential_sync"]["bundles"]
    assert "git" not in updated["credential_sync"]["bundles"]
    assert updated["credential_sync"]["shared_provider_auth"] is True


def test_revoke_refuses_to_delete_through_agent_planted_symlink(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """An agent-controlled intermediate symlink must not redirect a root-run
    revoke into deleting files outside the sandbox (regression for the
    ``shutil.rmtree(target_home / token)`` symlink-traversal escape)."""
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "alice"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)

    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)

    # A victim tree outside the agent home, containing entries whose basenames
    # match credential-bundle leaves (``gh`` dir for the git bundle,
    # ``auth.json`` file for provider-auth).
    victim = tmp_path / "victim"
    (victim / "gh").mkdir(parents=True)
    (victim / "gh" / "important.txt").write_text("keep me", encoding="utf-8")
    (victim / "auth.json").write_text("secret", encoding="utf-8")

    # The agent plants intermediate symlinks in its own home.
    (target_home / ".config").symlink_to(victim, target_is_directory=True)
    (target_home / ".codex").symlink_to(victim, target_is_directory=True)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)

    service.set_agent_credential_bundles("alice", ["provider-auth", "git"])
    service.revoke_agent_credentials("alice", bundles=["provider-auth", "git"])

    # Nothing outside the home was deleted through the planted symlinks.
    assert (victim / "gh" / "important.txt").exists()
    assert (victim / "gh").is_dir()
    assert (victim / "auth.json").exists()


def test_import_shared_auth_from_codex_links_agents_and_exposes_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "alice"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)
    service.set_agent_credential_bundles("alice", ["provider-auth"])

    source_home = tmp_path / "source-home"
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": _fake_jwt({"exp": 1893456000}),
                    "refresh_token": "ref",
                    "id_token": "",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-03-08T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", lambda cmd, **_kwargs: calls.append(cmd) or Result())
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)

    result = service.import_shared_auth("picoclaw", source="codex", source_home=source_home)
    assert result["source"] == "codex"
    assert result["restart_required_agents"] == ["alice"]
    assert (shared_home / ".codex" / "auth.json").exists()
    native_path = shared_home / ".picoclaw" / "auth.json"
    assert native_path.exists()
    profile_path = shared_home / ".picoclaw" / "auth-profiles.json"
    assert profile_path.exists()
    assert not (shared_home / ".zeroclaw" / "auth-profiles.json").exists()
    assert not (shared_home / ".openclaw" / "auth-profiles.json").exists()
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    assert payload["active_profiles"]["openai-codex"] == "openai-codex:default"
    native_payload = json.loads(native_path.read_text(encoding="utf-8"))
    assert native_payload["credentials"]["openai"]["access_token"] == _fake_jwt({"exp": 1893456000})
    assert (target_home / ".picoclaw" / "auth.json").is_file()
    assert not (target_home / ".picoclaw" / "auth.json").is_symlink()
    assert (target_home / ".picoclaw" / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".picoclaw" / "auth-profiles.json").is_file()
    assert not (target_home / ".picoclaw" / "auth-profiles.json").is_symlink()
    assert (target_home / ".picoclaw" / "auth-profiles.json").stat().st_mode & 0o777 == 0o600
    assert not (target_home / ".zeroclaw" / "auth-profiles.json").is_symlink()
    assert (target_home / ".codex" / "auth.json").is_file()
    assert not (target_home / ".codex" / "auth.json").is_symlink()
    assert (target_home / ".codex" / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".picoclaw").stat().st_mode & 0o777 == 0o700
    assert (target_home / ".codex").stat().st_mode & 0o777 == 0o700

    status = service.agent_auth_status("alice")
    assert status["auth_status"] == "ready"
    assert status["shared_provider_auth"] is True
    assert status["source"] == "file:auth.json"


def test_import_shared_auth_from_codex_replaces_unwritable_shared_profile(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )

    source_home = tmp_path / "source-home"
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": _fake_jwt({"exp": 1893456000}),
                    "refresh_token": "ref",
                    "id_token": "",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-03-08T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    locked_profile = shared_home / ".openclaw" / "auth-profiles.json"
    locked_profile.parent.mkdir(parents=True)
    locked_profile.write_text("{}", encoding="utf-8")
    os.chmod(locked_profile, 0o400)

    result = service.import_shared_auth("openclaw", source="codex", source_home=source_home)

    assert result["auth"]["auth_status"] == "ready"
    profiles = _read_openclaw_native_profiles(shared_home)
    profile = profiles["openai:default"]
    assert profile["access"] == _fake_jwt({"exp": 1893456000})
    assert profile["refresh"] == "ref"
    assert profile["provider"] == "openai"
    assert profile["type"] == "oauth"
    assert locked_profile.read_text(encoding="utf-8") == "{}"
    assert (locked_profile.stat().st_mode & 0o777) == 0o600


def test_prepare_linked_auth_for_provider_switch_imports_codex_from_source_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    target_home = tmp_path / "teleclaw-home"
    target_home.mkdir(parents=True)
    source_home = tmp_path / "source-home"
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": _fake_jwt({"exp": 1893456000}),
                    "refresh_token": "ref",
                    "id_token": "",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-03-16T08:45:02Z",
            }
        ),
        encoding="utf-8",
    )

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: target_home)
    monkeypatch.setattr(service, "_run_provider_auth_status", lambda **_kwargs: None)
    monkeypatch.setattr(ClawieService, "_default_source_home", staticmethod(lambda: source_home))

    prepared = service._prepare_linked_auth_for_provider_switch(provider="openclaw", agent=agent)

    assert prepared["prepared"] is True
    assert prepared["source"] == "codex"
    assert prepared["source_home"] == str(source_home)
    assert prepared["auth"]["auth_status"] == "ready"
    shared_profiles = _read_openclaw_native_profiles(shared_home)
    assert shared_profiles["openai:default"]["access"] == _fake_jwt({"exp": 1893456000})
    native_db = target_home / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    assert native_db.is_file()
    assert not native_db.is_symlink()
    assert native_db.stat().st_mode & 0o777 == 0o600
    assert _read_openclaw_native_profiles(target_home)["openai:default"]["refresh"] == "ref"


def test_prepare_linked_auth_for_provider_switch_fails_when_no_source_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    source_home = tmp_path / "source-home"
    source_home.mkdir(parents=True)
    monkeypatch.setattr(ClawieService, "_default_source_home", staticmethod(lambda: source_home))
    monkeypatch.setattr(service, "_run_provider_auth_status", lambda **_kwargs: None)

    with raises(SetupError, match="Sign in to Codex first"):
        service._prepare_linked_auth_for_provider_switch(provider="openclaw", agent=agent)


def test_shared_auth_status_prefers_linked_for_openclaw_when_shared_profiles_exist(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    (shared_home / ".openclaw").mkdir(parents=True)
    (shared_home / ".openclaw" / "auth-profiles.json").write_text(
        json.dumps(
            {
                "active_profiles": {"openai-codex": "openai-codex:default"},
                "profiles": {
                    "openai-codex:default": {
                        "profile_name": "default",
                        "provider": "openai-codex",
                        "account_id": "acct-1",
                        "kind": "oauth",
                        "access_token": "tok",
                        "refresh_token": "ref",
                        "expires_at": "2000-01-01T00:00:00Z",
                        "updated_at": "1999-12-31T23:59:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_run_provider_auth_status", lambda **_kwargs: None)

    status = service.shared_auth_status("openclaw")

    assert status["auth_mode"] == "linked"
    assert status["auth_status"] == "expired"
    assert status["login_required"] is True
    assert status["source"] == "file:auth-profiles.json"


def test_auth_status_enriches_cli_ready_state_with_private_account_metadata(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(provider="openclaw", auth_mode="linked")
    monkeypatch.setattr(
        service,
        "_run_provider_auth_status",
        lambda **_kwargs: {
            "auth_status": "ready",
            "auth_profile": "",
            "account": "",
            "source": "cli",
        },
    )
    monkeypatch.setattr(
        "clawie._service_auth.inspect_auth_files",
        lambda **_kwargs: {
            "auth_status": "expired",
            "auth_profile": "openai-codex:default",
            "account": "acct-1",
            "expires_at": "2000-01-01T00:00:00Z",
            "detail": "oauth",
            "source": "file:openclaw-agent.sqlite",
        },
    )

    status = service._inspect_provider_auth_state(
        provider="openclaw",
        auth_mode="linked",
        linux_user="",
        home=tmp_path,
    )

    assert status["auth_status"] == "ready"
    assert status["source"] == "cli"
    assert status["login_required"] is False
    assert status["auth_profile"] == "openai-codex:default"
    assert status["account"] == "acct-1"
    assert status["metadata_source"] == "file:openclaw-agent.sqlite"


def test_auth_status_from_profiles_json_supports_openclaw_native_oauth_store(tmp_path: Path) -> None:
    path = tmp_path / "auth-profiles.json"
    expires_ms = 2147483647000
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai-codex:default": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "tok",
                        "refresh": "ref",
                        "expires": expires_ms,
                        "accountId": "acct-1",
                    }
                },
                "order": {"openai-codex": ["openai-codex:default"]},
            }
        ),
        encoding="utf-8",
    )

    status = auth_status_from_profiles_json(path)

    assert status["auth_status"] == "ready"
    assert status["account"] == "acct-1"
    assert status["auth_profile"] == "openai-codex:default"
    assert status["source"] == "file:auth-profiles.json"


def test_auth_status_from_picoclaw_auth_json_prefers_openai_credential(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "credentials": {
                    "anthropic": {
                        "access_token": "other",
                        "provider": "anthropic",
                        "auth_method": "token",
                    },
                    "openai": {
                        "access_token": "tok",
                        "refresh_token": "ref",
                        "account_id": "acct-1",
                        "provider": "openai",
                        "auth_method": "oauth",
                        "expires_at": "2099-03-18T08:44:03Z",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    status = auth_status_from_picoclaw_auth_json(path)
    assert status["auth_status"] == "ready"
    assert status["auth_profile"] == "openai"
    assert status["account"] == "acct-1"
    assert status["source"] == "file:auth.json"


def test_prepare_picoclaw_home_backfills_missing_shared_native_auth_from_codex(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "fixture-sync"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    target_home = tmp_path / "fixture-sync-home"
    target_home.mkdir(parents=True)
    (shared_home / ".codex").mkdir(parents=True)
    (shared_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "tok",
                    "refresh_token": "ref",
                    "id_token": "",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-03-08T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/picoclaw")
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: Result(stdout="ok"))

    service._prepare_agent_provider_home(
        provider="picoclaw",
        agent=agent,
        linux_user="teleclaw",
        home=target_home,
        channels=[{"kind": "telegram", "name": "team"}],
        live_payloads={
            ("telegram", "team"): {
                "kind": "telegram",
                "name": "team",
                "settings": {"bot_token": _fake_telegram_token()},
            }
        },
    )

    assert (shared_home / ".picoclaw" / "auth.json").exists()
    assert (target_home / ".picoclaw" / "auth.json").is_file()
    assert not (target_home / ".picoclaw" / "auth.json").is_symlink()
    assert (target_home / ".picoclaw" / "auth.json").stat().st_mode & 0o777 == 0o600
    config = json.loads((target_home / ".picoclaw" / "config.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["token"] == _fake_telegram_token()


def test_prepare_picoclaw_home_resolves_env_backed_channel_values(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
        provider="picoclaw",
    )
    target_home = tmp_path / "teleclaw-home"
    target_home.mkdir(parents=True)
    (target_home / ".picoclaw").mkdir(parents=True)
    (target_home / ".picoclaw" / "auth.json").write_text(
        json.dumps(
            {
                "credentials": {
                    "openai": {
                        "access_token": "tok",
                        "provider": "openai",
                        "auth_method": "oauth",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class Result:
        def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[-2:] == ["bash", "-lc"]:
            return Result()
        if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"]:
            return Result(stdout=f"TELEGRAM_TOKEN={_fake_telegram_token()}\n\0".encode("utf-8"))
        return Result(stdout=b"")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)

    service._prepare_agent_provider_home(
        provider="picoclaw",
        agent=agent,
        linux_user="teleclaw",
        home=target_home,
        channels=[{"kind": "telegram", "name": "team"}],
        live_payloads={
            ("telegram", "team"): {
                "kind": "telegram",
                "name": "team",
                "settings": {"bot_token": "${TELEGRAM_TOKEN}"},
            }
        },
    )

    config = json.loads((target_home / ".picoclaw" / "config.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["token"] == _fake_telegram_token()


def test_sync_agent_channels_from_provider_replaces_stale_channels(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[
            {"kind": "telegram", "name": "team", "enabled": False},
            {"kind": "cli", "name": "teleclaw-local"},
        ],
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "fixture-switch"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    target_home = tmp_path / "teleclaw-home"
    provider_root = target_home / ".zeroclaw"
    provider_root.mkdir(parents=True)
    (provider_root / "config.toml").write_text(
        """
[channels_config.telegram]
enabled = true
bot_token = "tok"
name = "team"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)
    monkeypatch.setattr(ClawieService, "_can_manage_linux_user", lambda self, _user: True)

    synced = service.sync_agent_channels_from_provider("teleclaw")
    channels = synced["channels"]
    assert [(row.get("kind"), row.get("name")) for row in channels] == [("telegram", "team")]
    assert channels[0]["enabled"] is True
    assert channels[0]["channel_source"] == "live"
    assert channels[0]["discovered_provider"] == "zeroclaw"


def test_discover_agent_channels_uses_readable_provider_home_without_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    home = tmp_path / "teleclaw-home"
    root = home / ".openclaw"
    root.mkdir(parents=True)
    (root / "openclaw.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "botToken": _fake_telegram_token(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _payload: home)
    monkeypatch.setattr(ClawieService, "_can_manage_linux_user", lambda self, _user: False)
    monkeypatch.setattr(ClawieService, "_live_provider_names_for_user", lambda self, _user: ["openclaw"])

    discovery = service._discover_agent_channels(agent)
    payloads = service._discover_live_channel_payloads(agent)

    assert discovery["source"] == "provider"
    assert discovery["channels"] == [
        {"kind": "telegram", "name": "telegram", "enabled": True, "discovered_provider": "openclaw"}
    ]
    assert sorted(payloads) == [("telegram", "telegram")]


def test_agent_channel_view_prefers_live_provider_channels_after_cutover(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    payload = {
        "agent_id": "teleclaw",
        "channels": [{"kind": "cli", "name": "local", "enabled": True, "external_id": "teleclaw:cli:1"}],
        "agent": {
            "provider": "picoclaw",
            "linux_user": "teleclaw",
        },
    }
    home = tmp_path / "teleclaw-home"
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _payload: home)
    monkeypatch.setattr(ClawieService, "_can_manage_linux_user", lambda self, _user: True)
    monkeypatch.setattr(ClawieService, "_live_provider_names_for_user", lambda self, _user: ["picoclaw"])

    def fake_discover(self: ClawieService, provider: str, root: Path) -> list[dict[str, str]]:
        if provider == "picoclaw":
            return [{"kind": "telegram", "name": "telegram", "enabled": True}]
        if provider == "zeroclaw":
            return [{"kind": "cli", "name": "local", "enabled": True}]
        return []

    monkeypatch.setattr(ClawieService, "_discover_channels_for_provider_root", fake_discover)

    view = service._attach_agent_channel_view(payload)
    live_or_discovered = [
        (row.get("kind"), row.get("name"), row.get("channel_source"), row.get("discovered_provider"))
        for row in view["channels"]
        if row.get("channel_source") in {"live", "discovered"}
    ]
    assert live_or_discovered == [("telegram", "telegram", "discovered", "picoclaw")]
    stale_rows = [
        (row.get("kind"), row.get("name"), row.get("channel_source"))
        for row in view["channels"]
        if row.get("kind") == "cli"
    ]
    assert stale_rows == [("cli", "local", "stale")]


def test_shared_auth_show_cli_lists_rows(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "list_shared_auth_statuses",
        lambda self: [
            {
                "provider": "picoclaw",
                "auth_status": "ready",
                "auth_profile": "openai-codex:default",
                "shared_scope": "system",
                "shared_agents": ["alice", "bob"],
                "home": "/var/lib/clawie/provider-auth",
            }
        ],
    )

    code = run_cli(tmp_path, "auth", "show")
    output = capsys.readouterr().out
    assert code == 0
    assert "picoclaw" in output
    assert "openai-codex:default" in output
    assert "system" in output


def test_detect_installed_claws_command(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    source_home = tmp_path / "home"
    source_home.mkdir(parents=True)
    zeroclaw = source_home / ".zeroclaw"
    zeroclaw.mkdir(parents=True)
    (zeroclaw / "config.toml").write_text("default_provider='openai-codex'\n", encoding="utf-8")
    (zeroclaw / "auth-profiles.json").write_text("{}", encoding="utf-8")

    code = run_cli(tmp_path, "runtime", "detect", "--source-home", str(source_home))
    output = capsys.readouterr().out
    assert code == 0
    assert "zeroclaw" in output
    assert str(zeroclaw) in output


def test_copy_selected_paths_deduplicates_and_copies(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    source_home.mkdir(parents=True)
    target_home.mkdir(parents=True)

    (source_home / ".profile").write_text("export TEST=1\n", encoding="utf-8")
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "config.toml").write_text("[model]\n", encoding="utf-8")

    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    copied = service._copy_selected_paths(
        source_home=source_home,
        target_home=target_home,
        username="sam",
        relative_paths=[".profile", ".codex", ".profile"],
        enabled=True,
    )

    assert str(target_home / ".profile") in copied
    assert str(target_home / ".codex") in copied
    assert len(copied) == 2
    assert (target_home / ".profile").exists()
    assert (target_home / ".codex" / "config.toml").exists()
    assert (target_home / ".profile").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".codex").stat().st_mode & 0o777 == 0o700
    assert (target_home / ".codex" / "config.toml").stat().st_mode & 0o777 == 0o600


def test_ensure_shared_toolchain_shell_init_writes_profiles(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    (target_home / ".profile").write_text("# existing\n", encoding="utf-8")

    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    updated = service._ensure_shared_toolchain_shell_init(target_home=target_home, username="sam")

    assert str(target_home / ".profile") in updated
    assert str(target_home / ".bashrc") in updated
    profile_text = (target_home / ".profile").read_text(encoding="utf-8")
    bashrc_text = (target_home / ".bashrc").read_text(encoding="utf-8")
    assert "clawie-shared-toolchain" in profile_text
    assert 'export PNPM_HOME="$HOMEBREW_PREFIX/bin"' in profile_text
    assert "fnm env --use-on-cd --shell bash" in bashrc_text
    assert (target_home / ".profile").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".bashrc").stat().st_mode & 0o777 == 0o600


def test_ensure_shared_toolchain_shell_init_is_idempotent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    (target_home / ".profile").write_text("", encoding="utf-8")
    (target_home / ".bashrc").write_text("", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    first = service._ensure_shared_toolchain_shell_init(target_home=target_home, username="sam")
    assert len(first) == 2

    calls.clear()
    second = service._ensure_shared_toolchain_shell_init(target_home=target_home, username="sam")
    assert second == []
    assert calls == []


def test_ensure_system_shared_runtime_seeds_claude_and_profiles(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source_home = tmp_path / "source-home"
    (source_home / ".claude").mkdir(parents=True)
    (source_home / ".claude" / ".credentials.json").write_text('{"access_token":"abc"}\n', encoding="utf-8")
    (source_home / ".claude.json").write_text('{"userID":"u1"}\n', encoding="utf-8")

    profile_dir = tmp_path / "etc" / "profile.d"
    profile_dir.mkdir(parents=True)
    shared_dir = tmp_path / "var" / "lib" / "clawie" / "claude-shared"
    shared_dir.parent.mkdir(parents=True)
    brew_prefix = tmp_path / "homebrew"

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(ClawieService, "HOMEBREW_PREFIX", brew_prefix)
    monkeypatch.setattr(ClawieService, "GLOBAL_PROFILE_DIR", profile_dir)
    monkeypatch.setattr(ClawieService, "GLOBAL_HOMEBREW_PROFILE_FILE", profile_dir / "00-homebrew.sh")
    monkeypatch.setattr(ClawieService, "GLOBAL_FNM_PROFILE_FILE", profile_dir / "zz-fnm.sh")
    monkeypatch.setattr(ClawieService, "GLOBAL_CLAUDE_PROFILE_FILE", profile_dir / "20-claude-shared.sh")
    monkeypatch.setattr(ClawieService, "SHARED_CLAUDE_DIR", shared_dir)

    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    updated = service._ensure_system_shared_runtime(source_home)

    assert (profile_dir / "00-homebrew.sh").exists()
    assert (profile_dir / "zz-fnm.sh").exists()
    assert (profile_dir / "20-claude-shared.sh").exists()
    assert "CLAUDE_CONFIG_DIR" in (profile_dir / "20-claude-shared.sh").read_text(encoding="utf-8")
    assert "unset CLAUDE_CONFIG_DIR" in (profile_dir / "20-claude-shared.sh").read_text(encoding="utf-8")
    assert "unset XDG_RUNTIME_DIR" in (profile_dir / "zz-fnm.sh").read_text(encoding="utf-8")

    shared_credentials = shared_dir / ".credentials.json"
    shared_state = shared_dir / ".claude.json"
    assert shared_credentials.exists()
    assert shared_state.exists()
    assert (shared_credentials.stat().st_mode & 0o777) == 0o600
    assert (shared_state.stat().st_mode & 0o777) == 0o600

    assert str(shared_credentials) in updated
    assert str(profile_dir / "20-claude-shared.sh") in updated


def test_ensure_shared_claude_links_copies_private_config_to_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    (target_home / ".claude").mkdir(parents=True)
    (target_home / ".claude" / "old.txt").write_text("old\n", encoding="utf-8")
    (target_home / ".claude.json").write_text("old\n", encoding="utf-8")

    shared_dir = tmp_path / "var" / "lib" / "clawie" / "claude-shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / ".claude.json").write_text('{"userID":"u1"}\n', encoding="utf-8")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(ClawieService, "SHARED_CLAUDE_DIR", shared_dir)

    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    updated = service._ensure_shared_claude_links(target_home=target_home, username="sam")

    assert str(target_home / ".claude") in updated
    assert str(target_home / ".claude.json") in updated
    assert (target_home / ".claude").is_dir()
    assert not (target_home / ".claude").is_symlink()
    assert (target_home / ".claude" / ".claude.json").exists()
    assert (target_home / ".claude.json").is_file()
    assert not (target_home / ".claude.json").is_symlink()
    assert (target_home / ".claude" / ".claude.json").read_text(encoding="utf-8") == '{"userID":"u1"}\n'
    assert (target_home / ".claude.json").read_text(encoding="utf-8") == '{"userID":"u1"}\n'
    assert (target_home / ".claude.json").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".claude").stat().st_mode & 0o777 == 0o700
    assert (target_home / ".claude" / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_service_toggles_channel_plugin_and_autostart(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
    )
    assert agent["channels"][0]["enabled"] is True
    assert agent["agent"]["plugins"]["scheduler"] is True
    assert agent["agent"]["autostart"] is True

    toggled_channel = service.toggle_agent_channel("alice", 0)
    assert toggled_channel["channels"][0]["enabled"] is False

    toggled_plugin = service.toggle_agent_plugin("alice", "scheduler")
    assert toggled_plugin["agent"]["plugins"]["scheduler"] is False

    toggled_autostart = service.toggle_agent_autostart("alice")
    assert toggled_autostart["agent"]["autostart"] is False


def test_enable_agent_addon_imports_shared_gws_and_links_agent_home(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    shared_root = tmp_path / "shared-addon-auth"
    monkeypatch.setattr(ClawieService, "_shared_addon_auth_home", lambda self: shared_root)
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name="Alice",
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="picoclaw",
    )
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    service.store.write_state(state)

    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    source_home = tmp_path / "source-home"
    source_config = source_home / ".config" / "gws"
    source_config.mkdir(parents=True)
    (source_config / "credentials.json").write_text('{"refresh_token":"rtok"}\n', encoding="utf-8")
    (source_config / "client_secret.json").write_text(
        '{"installed":{"client_id":"cid","client_secret":"sec"}}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)
    monkeypatch.setattr(ClawieService, "_can_manage_linux_user", lambda self, _user: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_addon_installed",
        lambda self, addon: {
            "addon": addon,
            "installed": False,
            "already_present": True,
            "method": "npm",
            "package": "@googleworkspace/cli",
            "executable": "/mock/bin/gws",
        },
    )
    monkeypatch.setattr(
        ClawieService,
        "_resolve_executable_in_service_env",
        lambda self, executable, linux_user="": "",
    )

    result = service.enable_agent_addon("alice", "gws", source_home=source_home)

    shared_dir = service._shared_addon_config_dir("gws")
    assert result["addon"] == "gws"
    assert result["pending"] is False
    assert (shared_dir / "credentials.json").exists()
    assert (shared_dir / "credentials.json").stat().st_mode & 0o777 == 0o600
    assert (target_home / ".config" / "gws").is_dir()
    assert not (target_home / ".config" / "gws").is_symlink()
    assert (target_home / ".config" / "gws" / "credentials.json").is_file()
    assert (target_home / ".config" / "gws" / "credentials.json").stat().st_mode & 0o777 == 0o600

    addon_payload = service.get_agent_addons("alice")
    row = next(item for item in addon_payload["addons"] if item["addon"] == "gws")
    assert row["enabled"] is True
    assert row["auth_status"] == "ready"
    assert row["applied"] is True


def test_enable_agent_addon_can_trigger_shared_login_when_requested(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    shared_root = tmp_path / "shared-addon-auth"
    monkeypatch.setattr(ClawieService, "_shared_addon_auth_home", lambda self: shared_root)
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name="Alice",
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="picoclaw",
    )
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    service.store.write_state(state)

    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    shared_dir = service._ensure_shared_addon_config_dir("gws")

    calls: list[str] = []

    def fake_status(self: ClawieService, addon: str) -> dict[str, object]:
        if (shared_dir / "credentials.json").exists():
            return {
                "addon": addon,
                "auth_status": "ready",
                "detail": "plaintext credentials",
                "login_required": False,
                "config_dir": str(shared_dir),
                "shared_scope": "local",
                "linked_agents": [],
            }
        return {
            "addon": addon,
            "auth_status": "missing",
            "detail": "no addon credentials configured",
            "login_required": True,
            "config_dir": str(shared_dir),
            "shared_scope": "local",
            "linked_agents": [],
        }

    def fake_login(self: ClawieService, addon: str) -> dict[str, object]:
        calls.append(addon)
        (shared_dir / "credentials.json").write_text('{"refresh_token":"rtok"}\n', encoding="utf-8")
        return fake_status(self, addon)

    monkeypatch.setattr(ClawieService, "shared_addon_auth_status", fake_status)
    monkeypatch.setattr(ClawieService, "shared_addon_auth_login", fake_login)
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)
    monkeypatch.setattr(ClawieService, "_can_manage_linux_user", lambda self, _user: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_addon_installed",
        lambda self, addon: {
            "addon": addon,
            "installed": False,
            "already_present": True,
            "method": "npm",
            "package": "@googleworkspace/cli",
            "executable": "/mock/bin/gws",
        },
    )

    result = service.enable_agent_addon("alice", "gws", login_if_missing=True)

    assert calls == ["gws"]
    assert result["pending"] is False
    assert (target_home / ".config" / "gws").is_dir()
    assert not (target_home / ".config" / "gws").is_symlink()
    assert (target_home / ".config" / "gws" / "credentials.json").is_file()
    assert (target_home / ".config" / "gws" / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_get_agent_addons_reports_permission_for_other_linux_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    # Force a non-root euid: root can manage any linux_user, which would
    # bypass the permission report this test asserts on.
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    shared_root = tmp_path / "shared-addon-auth"
    monkeypatch.setattr(ClawieService, "_shared_addon_auth_home", lambda self: shared_root)
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name="Alice",
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="picoclaw",
    )
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    state["agents"]["alice"]["addons"] = {"gws": {"enabled": True}}
    service.store.write_state(state)

    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: target_home)
    monkeypatch.setattr(
        ClawieService,
        "_resolve_executable_in_service_env",
        lambda self, executable, linux_user="": "/mock/bin/gws" if executable == "gws" else "",
    )
    monkeypatch.setattr(
        ClawieService,
        "shared_addon_auth_status",
        lambda self, addon: {
            "addon": addon,
            "auth_status": "ready",
            "detail": "plaintext credentials",
            "login_required": False,
            "config_dir": str(shared_root / "gws"),
            "shared_scope": "local",
            "linked_agents": [],
        },
    )

    payload = service.get_agent_addons("alice")

    row = next(item for item in payload["addons"] if item["addon"] == "gws")
    assert row["enabled"] is True
    assert row["access_status"] == "permission"
    assert "requires root" in row["access_detail"]
    assert row["applied"] is False


def test_shared_addon_auth_login_gws_bootstraps_gcloud_before_setup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    shared_root = tmp_path / "shared-addon-auth"
    monkeypatch.setattr(ClawieService, "_shared_addon_auth_home", lambda self: shared_root)
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="",
        workspace="dev",
        api_url="",
        auth_mode="linked",
    )
    shared_dir = service._ensure_shared_addon_config_dir("gws")
    tool_calls: list[str] = []
    shell_calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> Result:
        shell_calls.append(cmd)
        script = cmd[-1] if cmd else ""
        if "gws" in script and "auth export" in script:
            return Result(stdout='{"refresh_token":"rtok"}\n')
        return Result()

    monkeypatch.setattr(
        ClawieService,
        "ensure_addon_installed",
        lambda self, addon: {
            "addon": addon,
            "installed": False,
            "already_present": True,
            "method": "npm",
            "package": "@googleworkspace/cli",
            "executable": "/mock/bin/gws",
        },
    )
    monkeypatch.setattr(
        ClawieService,
        "ensure_support_tool_installed",
        lambda self, tool: tool_calls.append(tool) or {
            "tool": tool,
            "installed": True,
            "already_present": False,
            "method": "archive",
            "scope": "local",
            "executable": "/mock/bin/gcloud",
        },
    )
    monkeypatch.setattr("subprocess.run", fake_run)

    payload = service.shared_addon_auth_login("gws")

    assert tool_calls == ["gcloud"]
    assert payload["action_performed"] == "login"
    assert (shared_dir / "credentials.json").exists()
    assert any("auth setup" in str(cmd[-1]) for cmd in shell_calls)


def test_shared_addon_auth_status_gws_uses_command_output_over_stub_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    shared_root = tmp_path / "shared-addon-auth"
    monkeypatch.setattr(ClawieService, "_shared_addon_auth_home", lambda self: shared_root)
    shared_dir = service._ensure_shared_addon_config_dir("gws")
    (shared_dir / "credentials.json").write_text('{"refresh_token":"rtok"}\n', encoding="utf-8")
    (shared_dir / "client_secret.json").write_text('{"installed":{"client_id":"cid"}}\n', encoding="utf-8")

    class Result:
        def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    monkeypatch.setattr(
        ClawieService,
        "_resolve_executable_in_service_env",
        lambda self, executable, linux_user="": "/mock/bin/gws" if executable == "gws" else "",
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **_: Result(
            stdout=json.dumps(
                {
                    "auth_method": "oauth2",
                    "client_config": str(shared_dir / "client_secret.json"),
                    "client_config_error": "missing field `client_secret`",
                    "client_config_exists": True,
                    "credential_source": "none",
                    "has_refresh_token": True,
                    "plain_credentials": str(shared_dir / "credentials.json"),
                    "plain_credentials_exists": True,
                    "storage": "plaintext",
                }
            )
        ),
    )

    payload = service.shared_addon_auth_status("gws")

    assert payload["auth_status"] == "missing"
    assert payload["source"] == "command:auth status"
    assert "invalid" in str(payload["detail"]).lower()
    assert payload["client_config_error"] == "missing field `client_secret`"


def test_service_action_runs_provider_service_command(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "testuser"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "active (running)"
            stderr = ""

        if cmd[:2] == ["id", "-u"]:
            return Result()
        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/openclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    # On real hosts the generated user unit cannot be written for a foreign
    # user without root; replicate that here so the direct provider-command
    # path is exercised even when the suite itself runs as root.
    def deny_unit(self: ClawieService, provider: str, linux_user: str) -> None:
        raise PermissionError("unit dir not writable")

    monkeypatch.setattr(ClawieService, "_ensure_generated_user_service_unit", deny_unit)

    result = service.agent_service_action("alice", "status")
    assert result["service_status"] == "running"
    assert any(cmd[:6] == ["sudo", "-u", "testuser", "-H", "--", "/usr/bin/openclaw"] for cmd in calls)


def test_dashboard_status_prefers_service_status(
    tmp_path: Path,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["service_status"] = "running"
    state["agents"]["alice"]["agent"]["status"] = "offline"
    service.store.write_state(state)
    service.store.write_metric(
        timestamp="2026-02-21T00:00:00Z",
        user_id="alice",
        cpu_percent=0.0,
        mem_percent=0.0,
        rss_kb=0,
        status="offline",
    )

    snapshot = service.performance_snapshot(refresh=False)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "alice")
    assert row["status"] == "running"


def test_doctor_flags_insecure_shared_provider_auth_copy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "alice"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)
    home = tmp_path / "alice-home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    os.chmod(auth_file, 0o644)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    report = service.doctor()
    messages = [str(row.get("message", "")) for row in report["checks"]]

    assert report["status"] == "unhealthy"
    assert any("Provider auth file is not private" in message for message in messages)


def test_doctor_verifies_private_shared_provider_auth_copy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "alice"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)
    home = tmp_path / "alice-home"
    auth_file = home / ".codex" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("{}", encoding="utf-8")
    os.chmod(auth_file, 0o600)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    report = service.doctor()
    messages = [str(row.get("message", "")) for row in report["checks"]]

    assert any("Private provider auth copies verified for: alice" in message for message in messages)
    assert not any(row["status"] == "fail" for row in report["checks"])


def test_doctor_accepts_headless_delegation_agents(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="test-key",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="worker",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="zeroclaw",
    )

    report = service.doctor()

    assert report["status"] == "healthy"
    assert any(
        row["message"] == "Headless agents available through delegation or CLI: worker"
        for row in report["checks"]
    )


def test_doctor_does_not_claim_linked_credentials_are_ready(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        auth_mode="linked",
        subscription="pro",
        workspace="production",
    )

    report = service.doctor()
    messages = [str(row.get("message", "")) for row in report["checks"]]

    assert any("Provider auth mode configured (openclaw/linked)" in message for message in messages)
    assert not any("Provider auth configured" in message for message in messages)


def test_host_validation_skips_without_linux_proc(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(service, "_linux_proc_available", lambda: False)

    report = service.host_validation_report()

    assert report["status"] == "skipped"
    assert report["checks"] == [
        {
            "status": "skip",
            "message": "Host validation requires Linux with /proc available",
        }
    ]


def test_host_validation_passes_private_cross_user_layout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    homes: dict[str, Path] = {}
    for agent_id in ("alice", "bob"):
        agent = service.create_agent(
            agent_id=agent_id,
            display_name=None,
            template="baseline",
            clone_from=None,
            channel_strategy="new",
            channels=[{"kind": "telegram", "name": "team"}],
            agent_version="1.0.0",
        )
        agent["agent"]["linux_user"] = agent_id
        home = tmp_path / f"{agent_id}-home"
        auth_file = home / ".codex" / "auth.json"
        auth_file.parent.mkdir(parents=True)
        auth_file.write_text("{}", encoding="utf-8")
        os.chmod(home, 0o700)
        os.chmod(auth_file, 0o600)
        homes[agent_id] = home
        state = service.store.read_state()
        state["agents"][agent_id] = agent
        service.store.write_state(state)

    class PwdRow:
        def __init__(self, home: Path) -> None:
            self.pw_dir = str(home)

    monkeypatch.setattr(service, "_linux_proc_available", lambda: True)
    monkeypatch.setattr("clawie.service.os.geteuid", lambda: 0)
    monkeypatch.setattr("clawie.service.pwd.getpwnam", lambda user: PwdRow(homes[user]))
    monkeypatch.setattr(service, "_path_unreadable_as_user", lambda path, linux_user: (True, ""))

    report = service.host_validation_report()

    assert report["status"] == "passed"
    messages = [row["message"] for row in report["checks"]]
    assert any("Found 2 managed agents across 2 Linux users" in message for message in messages)
    assert any("alice cannot read" in message for message in messages)
    assert any("bob cannot read" in message for message in messages)


def test_host_validation_fails_when_cross_user_read_is_allowed(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    homes: dict[str, Path] = {}
    for agent_id in ("alice", "bob"):
        agent = service.create_agent(
            agent_id=agent_id,
            display_name=None,
            template="baseline",
            clone_from=None,
            channel_strategy="new",
            channels=[{"kind": "telegram", "name": "team"}],
            agent_version="1.0.0",
        )
        agent["agent"]["linux_user"] = agent_id
        home = tmp_path / f"{agent_id}-home"
        home.mkdir(parents=True)
        os.chmod(home, 0o700)
        homes[agent_id] = home
        state = service.store.read_state()
        state["agents"][agent_id] = agent
        service.store.write_state(state)

    class PwdRow:
        def __init__(self, home: Path) -> None:
            self.pw_dir = str(home)

    monkeypatch.setattr(service, "_linux_proc_available", lambda: True)
    monkeypatch.setattr("clawie.service.os.geteuid", lambda: 0)
    monkeypatch.setattr("clawie.service.pwd.getpwnam", lambda user: PwdRow(homes[user]))
    monkeypatch.setattr(service, "_path_unreadable_as_user", lambda path, linux_user: (False, "readable"))

    report = service.host_validation_report()

    assert report["status"] == "failed"
    assert any("can read or probe failed" in row["message"] for row in report["checks"])


def test_health_host_validate_json_uses_validation_exit_codes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "host_validation_report",
        lambda self: {"status": "skipped", "checks": [{"status": "skip", "message": "needs linux"}]},
    )

    code = run_cli(tmp_path, "health", "--host-validate", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["status"] == "skipped"
    assert payload["checks"][0]["message"] == "needs linux"


def test_health_text_exits_nonzero_when_doctor_is_unhealthy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "doctor",
        lambda self: {"status": "unhealthy", "checks": [{"status": "fail", "message": "missing config"}]},
    )

    code = run_cli(tmp_path, "health")
    output = capsys.readouterr().out

    assert code == 1
    assert "overall: unhealthy" in output
    assert "missing config" in output


def test_health_text_allows_degraded_doctor_with_warning(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "doctor",
        lambda self: {"status": "degraded", "checks": [{"status": "warn", "message": "no agents"}]},
    )

    code = run_cli(tmp_path, "health")
    output = capsys.readouterr().out

    assert code == 0
    assert "overall: degraded" in output
    assert "no agents" in output


def test_dashboard_refresh_updates_local_service_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )

    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            returncode = 0
            stdout = "active (running)"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "running"


def test_dashboard_refresh_local_status_uses_sudo_user_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class AliceInfo:
        pw_uid = 1001
        pw_dir = "/home/alice"
        pw_name = "alice"

    class RootInfo:
        pw_uid = 0
        pw_dir = "/root"
        pw_name = "root"

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda _: AliceInfo())
    monkeypatch.setattr("pwd.getpwuid", lambda uid: RootInfo() if int(uid) == 0 else AliceInfo())
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = "active (running)"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "running"
    assert any(
        cmd[:5] == ["sudo", "-u", "alice", "-H", "--"]
        and cmd[-3:] == ["--user", "is-active", "zeroclaw.service"]
        for cmd in calls
    )


def test_local_agent_view_refreshes_service_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            returncode = 0
            stdout = "active (running)"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    payload = service.get_dashboard_agent("@local:zeroclaw")
    agent = payload.get("agent", {})
    assert payload.get("display_name") == "local-user"
    assert agent.get("service_status") == "running"
    assert agent.get("status") == "running"


def test_parse_provider_auth_status_output_marks_expired(tmp_path: Path) -> None:
    _ = tmp_path
    parsed = parse_provider_auth_status_output(
        "* openai-codex:default kind=OAuth account=acct-1 "
        "expires=expired at 2026-03-02T05:47:04.775201553+00:00"
    )

    assert parsed["auth_status"] == "expired"
    assert parsed["auth_profile"] == "openai-codex:default"
    assert parsed["account"] == "acct-1"
    assert parsed["expires_at"] == "2026-03-02T05:47:04.775201553+00:00"
    assert parsed["detail"].lower() == "oauth"


def test_parse_openclaw_models_status_json_prefers_openai_record() -> None:
    parsed = parse_openclaw_models_status_output(
        json.dumps(
            {
                "providers": [
                    {"provider": "anthropic", "authStatus": "missing"},
                    {
                        "provider": "openai",
                        "authStatus": "ready",
                        "profileId": "default",
                        "accountId": "acct-1",
                    },
                ]
            }
        )
    )

    assert parsed["auth_status"] == "ready"
    assert parsed["auth_profile"] == "default"
    assert parsed["account"] == "acct-1"


def test_parse_openclaw_models_status_json_marks_login_required() -> None:
    parsed = parse_openclaw_models_status_output(
        json.dumps({"auth": {"provider": "openai", "loginRequired": True}})
    )

    assert parsed["auth_status"] == "missing"


def test_auth_status_from_profiles_json_marks_expired(tmp_path: Path) -> None:
    path = tmp_path / "auth-profiles.json"
    path.write_text(
        json.dumps(
            {
                "active_profiles": {"openai-codex": "openai-codex:default"},
                "profiles": {
                    "openai-codex:default": {
                        "profile_name": "default",
                        "provider": "openai-codex",
                        "account_id": "acct-1",
                        "kind": "oauth",
                        "access_token": "tok",
                        "refresh_token": "ref",
                        "expires_at": "2026-03-02T05:47:04.775201553+00:00",
                        "updated_at": "2026-02-28T08:43:04.625578501Z",
                    }
                },
                "updated_at": "2026-02-28T08:43:04.625578501Z",
            }
        ),
        encoding="utf-8",
    )

    parsed = auth_status_from_profiles_json(path)
    assert parsed["auth_status"] == "expired"
    assert parsed["auth_profile"] == "default"
    assert parsed["account"] == "acct-1"
    assert parsed["source"] == "file:auth-profiles.json"


def test_local_claw_auth_login_refreshes_before_login(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )

    states = iter(
        [
            {
                "provider": "zeroclaw",
                "auth_mode": "linked",
                "auth_status": "expired",
                "login_required": True,
            },
            {
                "provider": "zeroclaw",
                "auth_mode": "linked",
                "auth_status": "missing",
                "login_required": True,
            },
            {
                "provider": "zeroclaw",
                "auth_mode": "linked",
                "auth_status": "ready",
                "login_required": False,
            },
        ]
    )

    monkeypatch.setattr(
        ClawieService,
        "_inspect_provider_auth_state",
        lambda self, **kwargs: dict(next(states)),
    )
    monkeypatch.setattr(
        ClawieService,
        "_resolve_local_runtime_target",
        lambda self, provider: {"linux_user": "", "home": str(tmp_path), "root": ""},
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/zeroclaw")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append(cmd)

        class Result:
            def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if kwargs.get("capture_output"):
            return Result(returncode=0, stdout="refreshed")
        return Result(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    payload = service.local_claw_auth_login("zeroclaw")
    assert payload["auth_status"] == "ready"
    assert payload["action_performed"] == "login"
    assert [cmd[-1] for cmd in calls] == ["refresh", "login"]


def test_openclaw_auth_login_uses_models_auth_surface(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
        auth_mode="linked",
    )

    states = iter(
        [
            {
                "provider": "openclaw",
                "auth_mode": "linked",
                "auth_status": "expired",
                "login_required": True,
            },
            {
                "provider": "openclaw",
                "auth_mode": "linked",
                "auth_status": "missing",
                "login_required": True,
            },
            {
                "provider": "openclaw",
                "auth_mode": "linked",
                "auth_status": "ready",
                "login_required": False,
            },
        ]
    )
    monkeypatch.setattr(
        ClawieService,
        "_inspect_provider_auth_state",
        lambda self, **kwargs: dict(next(states)),
    )
    monkeypatch.setattr(
        ClawieService,
        "_resolve_local_runtime_target",
        lambda self, provider: {"linux_user": "", "home": str(tmp_path), "root": ""},
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/openclaw")

    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **kwargs: object) -> Result:
        calls.append(cmd)
        if kwargs.get("capture_output"):
            return Result(returncode=0, stdout='{"providers":[{"provider":"openai","authStatus":"missing"}]}')
        return Result(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    payload = service.local_claw_auth_login("openclaw")

    assert payload["auth_status"] == "ready"
    assert calls[0] == ["/usr/bin/openclaw", "models", "status", "--json"]
    assert calls[1] == [
        "/usr/bin/openclaw",
        "models",
        "auth",
        "login",
        "--provider",
        "openai",
        "--set-default",
    ]


def test_set_agent_provider_updates_runtime_and_auth_mode(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
            agent_version="1.0.0",
            provider="zeroclaw",
        )

    state = service.store.read_state()
    state["agents"]["teleclaw"]["credential_sync"] = {"bundles": [], "shared_provider_auth": False}
    service.store.write_state(state)

    updated = service.set_agent_provider("teleclaw", "openclaw")
    info = updated["agent"]
    assert info["provider"] == "openclaw"
    assert info["runtime"] == "openclaw-agent"
    assert info["auth_mode"] == "none"
    assert info["service_status"] == "unknown"
    assert info["service_mode"] == "unknown"


def test_set_agent_provider_uses_default_auth_mode_when_target_has_no_global_config(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )

    updated = service.set_agent_provider("teleclaw", "picoclaw")
    info = updated["agent"]

    assert info["provider"] == "picoclaw"
    assert info["runtime"] == "picoclaw-agent"
    assert info["auth_mode"] == "linked"


def test_set_agent_provider_prefers_linked_auth_when_shared_provider_auth_is_ready(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    monkeypatch.setattr(service, "_shared_linked_auth_available", lambda provider: provider == "openclaw")

    updated = service.set_agent_provider("teleclaw", "openclaw")
    info = updated["agent"]

    assert info["provider"] == "openclaw"
    assert info["auth_mode"] == "linked"


def test_agent_auth_status_reports_missing_until_shared_auth_is_copied(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["auth_mode"] = "none"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    (shared_home / ".openclaw").mkdir(parents=True)
    (shared_home / ".openclaw" / "auth-profiles.json").write_text(
        json.dumps(
            {
                "active_profiles": {"openai-codex": "openai-codex:default"},
                "profiles": {
                    "openai-codex:default": {
                        "profile_name": "default",
                        "provider": "openai-codex",
                        "account_id": "acct-1",
                        "kind": "oauth",
                        "access_token": "tok",
                        "refresh_token": "ref",
                        "expires_at": "2000-01-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_run_provider_auth_status", lambda **_kwargs: None)

    status = service.agent_auth_status("teleclaw")

    assert status["auth_mode"] == "linked"
    assert status["auth_status"] == "missing"
    assert status["source"] == "none"
    assert status["shared_provider_auth"] is True


def test_prepare_openclaw_home_prefers_linked_auth_when_shared_auth_exists(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["auth_mode"] = "none"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    home = tmp_path / "teleclaw-home"
    home.mkdir(parents=True)
    (shared_home / ".openclaw").mkdir(parents=True)
    (shared_home / ".openclaw" / "auth-profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai-codex:default": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "tok",
                        "refresh": "ref",
                        "expires": 2147483647000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(service, "_shared_linked_auth_available", lambda provider: provider == "openclaw")
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "/opt/openclaw")
    monkeypatch.setattr(
        service,
        "_verify_installed_runtime_version",
        lambda _provider, _executable: "2026.7.1",
    )
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    service._prepare_agent_provider_home(
        provider="openclaw",
        agent=agent,
        linux_user="teleclaw",
        home=home,
        channels=[],
        live_payloads={},
    )

    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["model"] == "openai/gpt-5.6-sol"
    assert (home / ".openclaw" / "auth-profiles.json").is_file()
    assert not (home / ".openclaw" / "auth-profiles.json").is_symlink()
    assert (home / ".openclaw" / "auth-profiles.json").stat().st_mode & 0o777 == 0o600
    agent_auth = home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    assert agent_auth.is_file()
    assert not agent_auth.is_symlink()
    assert json.loads(agent_auth.read_text(encoding="utf-8"))["profiles"]["openai-codex:default"]["access"] == "tok"


def test_prepare_openclaw_home_fails_closed_before_schema_writes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="",
    )
    agent = service.create_agent(
        agent_id="blocked",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    home = tmp_path / "blocked-home"
    home.mkdir()
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "/opt/openclaw")
    monkeypatch.setattr(
        service,
        "_verify_installed_runtime_version",
        lambda _provider, _executable: (_ for _ in ()).throw(
            SetupError("outside the verified delivery range")
        ),
    )

    with raises(SetupError, match="verified delivery range"):
        service._prepare_agent_provider_home(
            provider="openclaw",
            agent=agent,
            linux_user="blocked",
            home=home,
            channels=[],
            live_payloads={},
        )

    assert list(home.iterdir()) == []


def test_prepare_openclaw_home_repairs_legacy_shared_auth_store_format(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_home)
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    home = tmp_path / "teleclaw-home"
    home.mkdir(parents=True)
    (shared_home / ".openclaw").mkdir(parents=True)
    (shared_home / ".openclaw" / "auth-profiles.json").write_text(
        json.dumps(
            {
                "active_profiles": {"openai-codex": "openai-codex:default"},
                "profiles": {
                    "openai-codex:default": {
                        "profile_name": "default",
                        "provider": "openai-codex",
                        "account_id": "acct-1",
                        "kind": "oauth",
                        "access_token": "tok",
                        "refresh_token": "ref",
                        "expires_at": "2030-01-01T00:00:00Z",
                        "updated_at": "2029-12-31T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "/opt/openclaw")
    monkeypatch.setattr(
        service,
        "_verify_installed_runtime_version",
        lambda _provider, _executable: "2026.7.1",
    )
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    service._prepare_agent_provider_home(
        provider="openclaw",
        agent=agent,
        linux_user="teleclaw",
        home=home,
        channels=[],
        live_payloads={},
    )

    repaired = json.loads((shared_home / ".openclaw" / "auth-profiles.json").read_text(encoding="utf-8"))
    profile = repaired["profiles"]["openai-codex:default"]
    assert profile["type"] == "oauth"
    assert profile["access"] == "tok"
    assert profile["refresh"] == "ref"
    assert profile["accountId"] == "acct-1"
    assert "expires" in profile
    agent_auth = home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
    assert agent_auth.is_file()
    assert not agent_auth.is_symlink()
    assert json.loads(agent_auth.read_text(encoding="utf-8"))["profiles"]["openai-codex:default"]["access"] == "tok"


def test_ensure_openclaw_agent_auth_link_uses_atomic_write_not_access_hint(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    root = home / ".openclaw"
    source = root / "auth-profiles.json"
    target = root / "agents" / "main" / "agent" / "auth-profiles.json"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai-codex:default": {
                        "type": "oauth",
                        "provider": "openai-codex",
                        "access": "new",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    target.write_text('{"profiles":{"openai-codex:default":{"access":"old"}}}', encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    real_access = os.access

    def fake_access(path: object, mode: int, *args: object, **kwargs: object) -> bool:
        if Path(path) == target and mode == os.W_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", fake_access)

    service._ensure_openclaw_agent_auth_link(home=home, linux_user="teleclaw")

    assert json.loads(target.read_text(encoding="utf-8"))["profiles"]["openai-codex:default"]["access"] == "new"
    assert target.stat().st_mode & 0o777 == 0o600


def test_get_dashboard_agent_reconciles_provider_to_live_runtime_and_sets_remediation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "fixture-switch"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    monkeypatch.setattr(
        service,
        "agent_auth_status",
        lambda _agent_id: {
            "auth_mode": "none",
            "auth_status": "ready",
            "auth_profile": "default",
            "account": "",
            "expires_at": "",
            "last_refresh": "",
            "source": "file",
            "detail": "",
            "login_required": False,
            "can_login": True,
        },
    )

    class Result:
        returncode = 0
        stdout = "fixture-switch 4321 /home/linuxbrew/.linuxbrew/bin/zeroclaw daemon\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **_: Result() if cmd[:2] == ["ps", "-eo"] else Result())
    payload = service.get_dashboard_agent("teleclaw")
    info = payload["agent"]

    assert info["provider"] == "zeroclaw"
    assert info["live_provider"] == "zeroclaw"
    assert info["provider_status"] == "warning"
    assert "aligned state away from openclaw" in info["provider_issue"]
    assert "sudo clawie agent provider set teleclaw openclaw" in info["provider_remediation"]
    assert info["service_status"] == "running"
    assert info["live_pid"] == 4321
    assert service.get_agent("teleclaw")["agent"]["provider"] == "zeroclaw"


def test_get_dashboard_agent_uses_managed_provider_status_when_ps_misses_openclaw(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["service_mode"] = "systemd"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **_: Result())
    monkeypatch.setattr(service, "_provider_reports_running", lambda provider, linux_user: provider == "openclaw")

    payload = service.get_dashboard_agent("teleclaw")
    info = payload["agent"]

    assert info["provider"] == "openclaw"
    assert info["service_status"] == "running"
    assert info["service_mode"] == "systemd"
    assert info["live_provider"] == "openclaw"
    assert info["status"] == "running"


def test_get_dashboard_agent_marks_managed_status_unknown_when_non_root_cannot_probe_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **_: Result())
    monkeypatch.setattr(service, "_can_manage_linux_user", lambda linux_user: linux_user != "teleclaw")

    payload = service.get_dashboard_agent("teleclaw")
    info = payload["agent"]

    assert info["provider"] == "openclaw"
    assert info["service_status"] == "unknown"
    assert info["service_mode"] == "systemd"
    assert info["live_provider"] == ""


def test_switch_agent_provider_cuts_over_runtime_and_reconnects_channels(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "fixture-switch"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".zeroclaw").mkdir(parents=True)
    (home / ".zeroclaw" / "config.toml").write_text(
        (
            "[channels_config.telegram]\n"
            "enabled = true\n"
            f'bot_token = "{_fake_telegram_token()}"\n'
            'name = "teleclaw-team"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    calls: list[list[str]] = []
    runtime_state = {"zeroclaw": True, "openclaw": False}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd == ["/usr/bin/openclaw", "--version"]:
            return Result(stdout="openclaw 2026.7.1")
        if cmd[-3:] == ["gateway", "status", "--json"]:
            return Result(stdout='{"rpc":{"ok":true}}')
        if cmd[:2] == ["ps", "-eo"]:
            lines: list[str] = []
            if runtime_state["zeroclaw"]:
                lines.append("teleclaw 4321 /usr/bin/zeroclaw daemon")
            if runtime_state["openclaw"]:
                lines.append("teleclaw 5432 /usr/bin/openclaw gateway run")
            return Result(stdout="\n".join(lines) + ("\n" if lines else ""))
        tail3 = cmd[-3:]
        tail2 = cmd[-2:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)" if runtime_state["zeroclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            runtime_state["zeroclaw"] = False
            return Result(stdout="stopped")
        if tail3 == ["/usr/bin/openclaw", "daemon", "start"]:
            runtime_state["openclaw"] = True
            return Result(stdout="started")
        if tail3 == ["/usr/bin/openclaw", "daemon", "stop"]:
            runtime_state["openclaw"] = False
            return Result(stdout="stopped")
        if "openclaw" in script and "gateway" in script and "run" in script:
            if "setsid" in script:
                runtime_state["openclaw"] = True
                return Result(stdout="started pid=123")
            runtime_state["openclaw"] = True
            return Result(stdout="active (running)" if runtime_state["openclaw"] else "inactive")
        if tail3 == ["/usr/bin/openclaw", "daemon", "status"]:
            return Result(stdout="active (running)" if runtime_state["openclaw"] else "inactive")
        if tail2 == ["/usr/bin/openclaw", "status"]:
            return Result(stdout="ok")
        if cmd[:2] == ["chown", "fixture-switch:fixture-switch"]:
            return Result(stdout="")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    unit_actions = _mock_openclaw_generated_user_unit(monkeypatch, service, runtime_state)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.switch_agent_provider("teleclaw", "openclaw")
    updated = service.get_agent("teleclaw")
    info = updated["agent"]

    assert result["service"]["service_status"] == "running"
    assert result["reconnected_channels"] == [{"kind": "telegram", "name": "teleclaw-team"}]
    assert info["provider"] == "openclaw"
    assert info["runtime"] == "openclaw-agent"
    assert info["service_status"] == "running"
    assert info["service_mode"] == "systemd"
    assert any(cmd[-3:] == ["/usr/bin/zeroclaw", "service", "stop"] for cmd in calls)
    assert ("openclaw", "start", "fixture-switch") in unit_actions


def test_picoclaw_home_prepare_rejects_invalid_telegram_token(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    with raises(SetupError, match="invalid Telegram bot token"):
        service._ensure_picoclaw_home_prepared(
            home=home,
            linux_user="teleclaw",
            channels=[{"kind": "telegram", "name": "teleclaw-team", "enabled": True}],
            live_payloads={
                ("telegram", "teleclaw-team"): {
                    "kind": "telegram",
                    "name": "teleclaw-team",
                    "settings": {
                        "enabled": True,
                        "bot_token": "telegram-token",
                    },
                }
            },
            auth_mode="linked",
            api_key="",
        )


def test_ensure_openclaw_home_prepared_sets_gateway_mode_and_telegram_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    token = _fake_telegram_token()
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="teleclaw",
        channels=[{"kind": "telegram", "name": "teleclaw-team", "enabled": True}],
        live_payloads={
            ("telegram", "teleclaw-team"): {
                "kind": "telegram",
                "name": "teleclaw-team",
                "settings": {
                    "enabled": True,
                    "bot_token": token,
                    "allow_from": ["tg:123"],
                    "group_trigger": {"mention_only": True},
                },
            }
        },
        auth_mode="linked",
        api_key="",
    )

    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    assert config["gateway"]["mode"] == "local"
    assert config["agents"]["defaults"]["workspace"] == str(home / ".openclaw" / "workspace")
    assert config["agents"]["defaults"]["model"] == "openai/gpt-5.6-sol"
    assert config["agents"]["defaults"]["heartbeat"]["every"] == "0m"
    assert config["agents"]["defaults"]["heartbeat"]["directPolicy"] == "block"
    assert config["agents"]["defaults"]["heartbeat"]["lightContext"] is True
    assert config["agents"]["defaults"]["heartbeat"]["ackMaxChars"] == 300
    assert "openai-codex:default" not in config.get("auth", {}).get("profiles", {})
    assert "openai-codex" not in config.get("auth", {}).get("order", {})
    assert config["channels"]["defaults"]["heartbeat"] == {
        "showOk": False,
        "showAlerts": False,
        "useIndicator": False,
    }
    assert config["channels"]["telegram"]["botToken"] == token
    assert config["channels"]["telegram"]["streaming"] == {"mode": "off"}
    assert config["channels"]["telegram"]["allowFrom"] == ["tg:123"]
    assert config["channels"]["telegram"]["dmPolicy"] == "allowlist"
    assert config["channels"]["telegram"]["groups"]["*"]["requireMention"] is True


def test_ensure_openclaw_home_prepared_preserves_private_telegram_token_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    token_path = home / ".openclaw" / "telegram.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(_fake_telegram_token() + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="teleclaw",
        channels=[{"kind": "telegram", "name": "telegram", "enabled": True}],
        live_payloads={
            ("telegram", "telegram"): {
                "kind": "telegram",
                "name": "telegram",
                "provider": "openclaw",
                "settings": {
                    "enabled": True,
                    "tokenFile": str(token_path),
                },
            }
        },
        auth_mode="linked",
        api_key="",
    )

    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    telegram = config["channels"]["telegram"]
    assert telegram["enabled"] is True
    assert telegram["tokenFile"] == str(token_path)
    assert "botToken" not in telegram
    assert "allowFrom" not in telegram
    assert telegram["dmPolicy"] == "pairing"


def test_ensure_openclaw_home_prepared_rejects_exposed_telegram_token_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    token_path = home / ".openclaw" / "telegram.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(_fake_telegram_token() + "\n", encoding="utf-8")
    token_path.chmod(0o640)
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    with raises(SetupError, match="token file must be private"):
        service._ensure_openclaw_home_prepared(
            home=home,
            linux_user="teleclaw",
            channels=[{"kind": "telegram", "name": "telegram", "enabled": True}],
            live_payloads={
                ("telegram", "telegram"): {
                    "kind": "telegram",
                    "name": "telegram",
                    "provider": "openclaw",
                    "settings": {"tokenFile": str(token_path)},
                }
            },
            auth_mode="linked",
            api_key="",
        )


def test_ensure_openclaw_home_prepared_defaults_to_pairing_when_migrating_telegram_without_allowlist(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    token = _fake_telegram_token()
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="teleclaw",
        channels=[{"kind": "telegram", "name": "teleclaw-team", "enabled": True}],
        live_payloads={
            ("telegram", "teleclaw-team"): {
                "kind": "telegram",
                "name": "teleclaw-team",
                "provider": "zeroclaw",
                "settings": {
                    "enabled": True,
                    "bot_token": token,
                },
            }
        },
        auth_mode="linked",
        api_key="",
    )

    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["botToken"] == token
    assert config["channels"]["telegram"]["streaming"] == {"mode": "off"}
    assert "allowFrom" not in config["channels"]["telegram"]
    assert config["channels"]["telegram"]["dmPolicy"] == "pairing"


def test_ensure_openclaw_home_prepared_preserves_pairing_dm_policy_without_allowlist(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    token = _fake_telegram_token()
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="teleclaw",
        channels=[{"kind": "telegram", "name": "telegram", "enabled": True}],
        live_payloads={
            ("telegram", "telegram"): {
                "kind": "telegram",
                "name": "telegram",
                "provider": "openclaw",
                "settings": {
                    "enabled": True,
                    "botToken": token,
                    "dmPolicy": "pairing",
                },
            }
        },
        auth_mode="linked",
        api_key="",
    )

    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["botToken"] == token
    assert config["channels"]["telegram"]["streaming"] == {"mode": "off"}
    assert "allowFrom" not in config["channels"]["telegram"]
    assert config["channels"]["telegram"]["dmPolicy"] == "pairing"


def test_ensure_openclaw_home_prepared_heals_existing_telegram_streaming_without_live_channel(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    root = home / ".openclaw"
    root.mkdir(parents=True)
    (root / "openclaw.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "botToken": _fake_telegram_token(),
                        "streaming": "partial",
                        "streamMode": "partial",
                        "chunkMode": "newline",
                        "blockStreaming": True,
                        "draftChunk": {"minChars": 50},
                        "blockStreamingCoalesce": {"idleMs": 100},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="teleclaw",
        channels=[],
        live_payloads={},
        auth_mode="linked",
        api_key="",
    )

    config = json.loads((root / "openclaw.json").read_text(encoding="utf-8"))
    telegram = config["channels"]["telegram"]
    assert telegram["streaming"] == {"mode": "off"}
    for legacy_key in (
        "streamMode",
        "chunkMode",
        "blockStreaming",
        "draftChunk",
        "blockStreamingCoalesce",
    ):
        assert legacy_key not in telegram
    assert config["agents"]["defaults"]["heartbeat"]["every"] == "0m"
    assert config["channels"]["defaults"]["heartbeat"]["showAlerts"] is False


def test_read_openclaw_channel_payloads_ignores_channel_defaults(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    root = tmp_path / ".openclaw"
    root.mkdir()
    (root / "openclaw.json").write_text(
        json.dumps(
            {
                "channels": {
                    "defaults": {
                        "heartbeat": {
                            "showOk": False,
                            "showAlerts": False,
                            "useIndicator": False,
                        }
                    },
                    "telegram": {
                        "enabled": True,
                        "botToken": _fake_telegram_token(),
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    payloads = service._read_openclaw_channel_payloads(root)

    assert ("defaults", "defaults") not in payloads
    assert sorted(payloads) == [("telegram", "telegram")]


def test_openclaw_channel_adapter_ignores_channel_defaults(tmp_path: Path) -> None:
    root = tmp_path / ".openclaw"
    root.mkdir()
    (root / "openclaw.json").write_text(
        json.dumps(
            {
                "channels": {
                    "defaults": {
                        "heartbeat": {
                            "showOk": False,
                            "showAlerts": False,
                            "useIndicator": False,
                        }
                    },
                    "telegram": {
                        "enabled": True,
                        "botToken": _fake_telegram_token(),
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    channels = OpenClawChannelAdapter().discover_channels(root)

    assert channels == [{"kind": "telegram", "name": "telegram"}]


def test_assert_provider_postflight_ready_runs_openclaw_models_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    home = tmp_path / "teleclaw-home"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "auth-profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai-codex:default": {
                        "provider": "openai-codex",
                        "type": "oauth",
                        "access": "tok",
                        "expires": 4102444800000,
                    }
                },
                "active_profiles": {"openai-codex": "openai-codex:default"},
                "order": {"openai-codex": ["openai-codex:default"]},
            }
        ),
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if "gateway" in cmd and "status" in cmd:
            return Result(stdout='{"rpc":{"ok":true}}')
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr("subprocess.run", fake_run)

    service._assert_provider_postflight_ready(
        provider="openclaw",
        linux_user="teleclaw",
        home=home,
        auth_mode="linked",
    )

    assert any(cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and cmd[-2:] == ["models", "status"] for cmd in calls)
    assert any("gateway" in cmd and "status" in cmd for cmd in calls)


def test_provider_from_process_args_detects_openclaw_module_process() -> None:
    args = (
        "node "
        "/home/linuxbrew/.linuxbrew/bin/global/5/.pnpm/openclaw@2026.3.7/node_modules/openclaw/openclaw.mjs "
        "gateway run"
    )
    assert ClawieService._provider_from_process_args(args) == "openclaw"


def _mock_openclaw_generated_user_unit(
    monkeypatch: MonkeyPatch,
    service: ClawieService,
    runtime_state: dict[str, bool],
) -> list[tuple[str, str, str]]:
    actions: list[tuple[str, str, str]] = []
    unit_path = service.store.root / "openclaw.service"
    unit_path.write_text("[Service]\nExecStart=/usr/bin/openclaw\n", encoding="utf-8")

    monkeypatch.setattr(service, "_ensure_generated_user_service_unit", lambda provider, linux_user: True)
    monkeypatch.setattr(
        service,
        "_generated_user_service_unit_path",
        lambda _provider, _linux_user: unit_path,
    )
    monkeypatch.setattr(
        service,
        "_run_systemd_user_command",
        lambda linux_user, args: {"ok": True, "output": "", "command": ["systemctl", "--user", *args]},
    )

    def fake_manage(provider: str, action: str, linux_user: str) -> dict[str, Any]:
        actions.append((provider, action, linux_user))
        if provider == "openclaw":
            if action in {"start", "restart"}:
                runtime_state["openclaw"] = True
            elif action == "stop":
                runtime_state["openclaw"] = False
        return {"ok": True, "output": "", "command": ["systemctl", "--user", action, f"{provider}.service"]}

    monkeypatch.setattr(service, "_systemd_user_service_manage", fake_manage)
    monkeypatch.setattr(
        service,
        "_systemd_user_service_status",
        lambda provider, linux_user: "running" if runtime_state.get(provider, False) else "stopped",
    )
    return actions


def test_switch_agent_provider_reconciles_same_provider_runtime(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["auth_mode"] = "linked"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".zeroclaw").mkdir(parents=True)
    (home / ".zeroclaw" / "config.toml").write_text(
        (
            "[channels_config.telegram]\n"
            "enabled = true\n"
            f'bot_token = "{_fake_telegram_token()}"\n'
            'name = "teleclaw-team"\n'
        ),
        encoding="utf-8",
    )
    agent["channels"] = [{"kind": "telegram", "name": "teleclaw-team", "enabled": True}]
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    calls: list[list[str]] = []
    runtime_state = {"zeroclaw": True, "openclaw": False}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd == ["/usr/bin/openclaw", "--version"]:
            return Result(stdout="openclaw 2026.7.1")
        if cmd[:2] == ["ps", "-eo"]:
            lines: list[str] = []
            if runtime_state["zeroclaw"]:
                lines.append("teleclaw 4321 /usr/bin/zeroclaw daemon")
            if runtime_state["openclaw"]:
                lines.append("teleclaw 6543 /usr/bin/openclaw gateway run")
            return Result(stdout="\n".join(lines) + ("\n" if lines else ""))
        tail3 = cmd[-3:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if "openclaw" in script and "gateway" in script and "run" in script:
            if "setsid" in script:
                runtime_state["openclaw"] = True
                return Result(stdout="started pid=123")
            runtime_state["openclaw"] = True
            return Result(stdout="active (running)" if runtime_state["openclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)" if runtime_state["zeroclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            runtime_state["zeroclaw"] = False
            return Result(stdout="stopped")
        if tail3 == ["/usr/bin/openclaw", "daemon", "start"]:
            runtime_state["openclaw"] = True
            return Result(stdout="started")
        if tail3 == ["/usr/bin/openclaw", "daemon", "stop"]:
            runtime_state["openclaw"] = False
            return Result(stdout="stopped")
        if tail3 == ["/usr/bin/openclaw", "daemon", "status"]:
            return Result(stdout="active (running)" if runtime_state["openclaw"] else "inactive")
        if cmd[:2] == ["chown", "teleclaw:teleclaw"]:
            return Result(stdout="")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    unit_actions = _mock_openclaw_generated_user_unit(monkeypatch, service, runtime_state)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.switch_agent_provider("teleclaw", "openclaw")
    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    assert result["changed"] is True
    assert result["from_provider"] == "zeroclaw"
    assert result["service"]["service_status"] == "running"
    assert result["stopped_service"]["provider"] == "zeroclaw"
    assert config["gateway"]["mode"] == "local"
    assert config["channels"]["telegram"]["botToken"] == _fake_telegram_token()
    assert config["agents"]["defaults"]["model"] == "openai/gpt-5.6-sol"
    assert ("openclaw", "start", "teleclaw") in unit_actions
    assert any(cmd[-3:] == ["/usr/bin/zeroclaw", "service", "stop"] for cmd in calls)


def test_switch_agent_provider_returns_auth_prepare_details(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    home.mkdir(parents=True)

    started = {"openclaw": False}

    monkeypatch.setattr(
        service,
        "_prepare_linked_auth_for_provider_switch",
        lambda **_kwargs: {
            "provider": "openclaw",
            "required": True,
            "prepared": True,
            "action": "import",
            "source": "codex",
            "source_home": "/home/alice",
            "auth": {"auth_status": "ready"},
        },
    )
    monkeypatch.setattr(service, "_shared_linked_auth_available", lambda provider: provider == "openclaw")
    monkeypatch.setattr(service, "ensure_provider_runtime", lambda _provider: {"provider": "openclaw"})
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr(service, "_write_prompt_files_for_home", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)
    monkeypatch.setattr(service, "_require_linux_user_access", lambda _linux_user, _purpose: None)
    monkeypatch.setattr(service, "_prepare_agent_provider_home", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_reconnect_agent_channels", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_live_provider_names_for_user", lambda _linux_user: ["openclaw"] if started["openclaw"] else [])
    monkeypatch.setattr(
        service,
        "agent_auth_status",
        lambda _agent_id: {"auth_mode": "linked", "auth_status": "ready", "detail": "oauth"},
    )

    def fake_action(*, provider: str, action: str, linux_user: str, agent_info: dict[str, Any]) -> dict[str, Any]:
        assert linux_user == "teleclaw"
        if action == "start" and provider == "openclaw":
            started["openclaw"] = True
            return {"provider": provider, "service_status": "running", "service_mode": "systemd", "fallback_pid": 0}
        if action == "status":
            running = provider == "openclaw" and started["openclaw"]
            return {
                "provider": provider,
                "service_status": "running" if running else "stopped",
                "service_mode": "systemd",
                "fallback_pid": 0,
            }
        return {"provider": provider, "service_status": "stopped", "service_mode": "systemd", "fallback_pid": 0}

    monkeypatch.setattr(service, "_run_managed_provider_service_action", fake_action)

    result = service.switch_agent_provider("teleclaw", "openclaw")

    assert result["auth_prepare"]["prepared"] is True
    assert result["auth_prepare"]["source"] == "codex"
    assert result["agent"]["agent"]["provider"] == "openclaw"
    assert result["agent"]["agent"]["auth_mode"] == "linked"


def test_switch_agent_provider_restarts_same_provider_to_apply_reconciled_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["auth_mode"] = "linked"
    agent["credential_sync"] = {"bundles": ["provider-auth"], "shared_provider_auth": True}
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    home.mkdir(parents=True)

    runtime_state = {"openclaw": True}

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(service, "_prepare_linked_auth_for_provider_switch", lambda **_kwargs: {"auth": {"auth_status": "ready"}})
    monkeypatch.setattr(service, "ensure_provider_runtime", lambda _provider: {"provider": "openclaw"})
    monkeypatch.setattr(
        service,
        "_resolve_provider_executable",
        lambda provider: "/usr/bin/openclaw" if provider == "openclaw" else (_ for _ in ()).throw(SetupError("missing")),
    )
    monkeypatch.setattr(service, "_write_prompt_files_for_home", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)
    monkeypatch.setattr(service, "_require_linux_user_access", lambda _linux_user, _purpose: None)
    monkeypatch.setattr(service, "_prepare_agent_provider_home", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_reconnect_agent_channels", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_live_provider_names_for_user", lambda _linux_user: ["openclaw"] if runtime_state["openclaw"] else [])
    monkeypatch.setattr(
        service,
        "agent_auth_status",
        lambda _agent_id: {"auth_mode": "linked", "auth_status": "ready", "detail": "oauth"},
    )

    unit_actions = _mock_openclaw_generated_user_unit(monkeypatch, service, runtime_state)

    result = service.switch_agent_provider("teleclaw", "openclaw")

    assert result["changed"] is False
    assert result["service"]["action"] == "restart"
    assert ("openclaw", "restart", "teleclaw") in unit_actions


def test_switch_agent_provider_succeeds_when_status_reports_running_but_ps_misses_openclaw(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["auth_mode"] = "linked"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".zeroclaw").mkdir(parents=True)
    (home / ".zeroclaw" / "config.toml").write_text(
        (
            "[channels_config.telegram]\n"
            "enabled = true\n"
            f'bot_token = "{_fake_telegram_token()}"\n'
            'name = "teleclaw-team"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)
    runtime_state = {"zeroclaw": True}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd == ["/usr/bin/openclaw", "--version"]:
            return Result(stdout="openclaw 2026.7.1")
        if cmd[:2] == ["ps", "-eo"]:
            return Result(stdout="")
        tail3 = cmd[-3:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)" if runtime_state["zeroclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            runtime_state["zeroclaw"] = False
            return Result(stdout="stopped")
        if tail3 == ["/usr/bin/openclaw", "daemon", "status"]:
            if "setsid" in script:
                return Result(stdout="started pid=123")
            if "pgrep" in script:
                return Result(stdout="active (running)")
            return Result(stdout="active (running)")
        if "openclaw" in script and "gateway" in script and "run" in script:
            if "setsid" in script:
                return Result(stdout="started pid=123")
            if "pgrep" in script:
                return Result(stdout="active (running)")
            return Result(stdout="ok")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    _mock_openclaw_generated_user_unit(monkeypatch, service, runtime_state)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.switch_agent_provider("teleclaw", "openclaw")
    assert result["service"]["service_status"] == "running"
    assert result["agent"]["agent"]["provider"] == "openclaw"


def test_agent_service_start_installs_generated_picoclaw_user_unit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["auth_mode"] = "linked"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)
    monkeypatch.setattr(service, "_linux_home_for_user", lambda _user: home)
    monkeypatch.setattr(service, "_prepare_agent_provider_home", lambda **_: None)

    calls: list[list[str]] = []
    runtime_running = False

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        nonlocal runtime_running
        calls.append(cmd)
        if cmd[:2] == ["ps", "-eo"]:
            if runtime_running:
                return Result(0, stdout="teleclaw 4321 /usr/bin/picoclaw gateway\n")
            return Result(0, stdout="")
        if cmd[:5] == ["systemctl", "--machine", "teleclaw@", "--user", "daemon-reload"]:
            return Result(0, stdout="")
        if cmd[:5] == ["systemctl", "--machine", "teleclaw@", "--user", "reset-failed"]:
            return Result(0, stdout="")
        if cmd[:5] == ["systemctl", "--machine", "teleclaw@", "--user", "enable"]:
            return Result(0, stdout="")
        if cmd[:5] == ["systemctl", "--machine", "teleclaw@", "--user", "start"]:
            runtime_running = True
            return Result(0, stdout="")
        if cmd[:5] == ["systemctl", "--machine", "teleclaw@", "--user", "is-active"]:
            if runtime_running:
                return Result(0, stdout="active")
            return Result(3, stdout="inactive")
        if cmd[:2] == ["chown", "teleclaw:teleclaw"]:
            return Result(0, stdout="")
        return Result(0, stdout="")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/picoclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.agent_service_action("teleclaw", "start")

    assert result["service_status"] == "running"
    unit_path = home / ".config" / "systemd" / "user" / "picoclaw.service"
    assert unit_path.exists()
    unit_text = unit_path.read_text(encoding="utf-8")
    assert "Clawie managed picoclaw runtime" in unit_text
    assert "ExecStart=/bin/bash -lc" in unit_text
    assert "/usr/bin/picoclaw gateway" in unit_text
    assert "Restart=always" in unit_text
    assert ["systemctl", "--machine", "teleclaw@", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--machine", "teleclaw@", "--user", "enable", "picoclaw.service"] in calls
    assert ["systemctl", "--machine", "teleclaw@", "--user", "start", "picoclaw.service"] in calls


def test_switch_agent_provider_force_stops_lingering_zeroclaw_when_bus_control_falls_back(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "telegram", "name": "team"}],
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["auth_mode"] = "linked"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".zeroclaw").mkdir(parents=True)
    (home / ".zeroclaw" / "config.toml").write_text(
        (
            "[channels_config.telegram]\n"
            "enabled = true\n"
            f'bot_token = "{_fake_telegram_token()}"\n'
            'name = "teleclaw-team"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    runtime_state = {"zeroclaw": True, "openclaw": False}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd == ["/usr/bin/openclaw", "--version"]:
            return Result(stdout="openclaw 2026.7.1")
        if cmd[:2] == ["ps", "-eo"]:
            lines: list[str] = []
            if runtime_state["zeroclaw"]:
                lines.append("teleclaw 4321 /usr/bin/zeroclaw daemon")
            if runtime_state["openclaw"]:
                lines.append("teleclaw 5432 /usr/bin/openclaw gateway run")
            return Result(stdout="\n".join(lines) + ("\n" if lines else ""))
        if cmd[:3] == ["loginctl", "enable-linger", "teleclaw"]:
            return Result(0)
        if cmd[:2] == ["systemctl", "start"]:
            return Result(0)
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and cmd[-3:] == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(1, stderr="Failed to connect to bus: No medium found")
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and cmd[-3:] == ["/usr/bin/zeroclaw", "service", "stop"]:
            return Result(1, stderr="Failed to connect to bus: No medium found")
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and cmd[-3:] == ["/usr/bin/openclaw", "daemon", "start"]:
            runtime_state["openclaw"] = True
            return Result(0, stdout="started")
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and cmd[-3:] == ["/usr/bin/openclaw", "daemon", "stop"]:
            runtime_state["openclaw"] = False
            return Result(0, stdout="stopped")
        if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"]:
            script = str(cmd[-1])
            if "pkill" in script and "zeroclaw daemon" in script:
                runtime_state["zeroclaw"] = False
                return Result(0, stdout="")
            if "openclaw" in script and "gateway" in script and "run" in script:
                if "setsid" in script:
                    runtime_state["openclaw"] = True
                    return Result(0, stdout="started pid=5432")
                if "pgrep" in script:
                    return Result(0, stdout="active (running)" if runtime_state["openclaw"] else "inactive")
            return Result(0, stdout="")
        if cmd[-3:] == ["/usr/bin/openclaw", "daemon", "status"]:
            return Result(0, stdout="active (running)" if runtime_state["openclaw"] else "inactive")
        return Result(0, stdout="")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    _mock_openclaw_generated_user_unit(monkeypatch, service, runtime_state)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.switch_agent_provider("teleclaw", "openclaw")
    assert result["service"]["service_status"] == "running"
    assert result["agent"]["agent"]["provider"] == "openclaw"
    assert runtime_state["zeroclaw"] is False


def test_switch_agent_provider_fails_when_live_runtime_does_not_cut_over(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd == ["/usr/bin/openclaw", "--version"]:
            return Result(stdout="openclaw 2026.7.1")
        if cmd[:2] == ["ps", "-eo"]:
            return Result(stdout="teleclaw 4321 /usr/bin/zeroclaw daemon\n")
        tail3 = cmd[-3:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            return Result(stdout="stopped")
        if tail3 == ["/usr/bin/openclaw", "daemon", "status"]:
            return Result(stdout="inactive")
        if "openclaw" in script and "gateway" in script and "run" in script:
            if "setsid" in script:
                return Result(stdout="started pid=123")
            if "pgrep" in script:
                return Result(stdout="inactive")
            return Result(stdout="inactive")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    _mock_openclaw_generated_user_unit(monkeypatch, service, {"openclaw": False})
    monkeypatch.setattr("subprocess.run", fake_run)

    with raises(Exception) as exc:
        service.switch_agent_provider("teleclaw", "openclaw")

    assert (
        "did not produce a live openclaw runtime" in str(exc.value)
        or "no live openclaw runtime was detected" in str(exc.value)
        or "zeroclaw service stop reported success but zeroclaw is still running" in str(exc.value)
    )
    info = service.get_agent("teleclaw")["agent"]
    assert info["provider"] == "zeroclaw"
    assert info["provider_status"] == "error"
    assert "provider switch to openclaw failed" in info["provider_issue"]


def test_set_agent_provider_requires_root_for_managed_user_switch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "fixture-switch"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    try:
        service.set_agent_provider("teleclaw", "openclaw")
        assert False, "expected SetupError"
    except Exception as exc:  # noqa: BLE001
        assert "provider switching requires root" in str(exc)

    info = service.get_agent("teleclaw")["agent"]
    assert info["provider"] == "zeroclaw"
    assert info["provider_status"] == "error"
    assert "provider switch to openclaw failed" in info["provider_issue"]
    assert "sudo clawie agent provider set teleclaw openclaw" in info["provider_remediation"]


def test_agent_auth_status_reports_permission_barrier_for_managed_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["auth_mode"] = "linked"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    payload = service.agent_auth_status("teleclaw")
    assert payload["auth_status"] == "unknown"
    assert payload["source"] == "permission"
    assert payload["can_login"] is False
    assert "requires root" in payload["detail"]


def test_service_action_requires_root_for_other_linux_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "otheruser"
    state = service.store.read_state()
    state["agents"]["alice"] = agent
    service.store.write_state(state)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    try:
        service.agent_service_action("alice", "status")
        assert False, "expected SetupError"
    except Exception as exc:  # noqa: BLE001
        assert "requires root" in str(exc)


def test_create_agent_defaults_to_no_channels(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    service = ClawieService(store)
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )

    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )

    assert agent["channels"] == []


def test_create_agent_ignores_stale_template_runtime_for_new_agents(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    service = ClawieService(store)
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )

    state = store.read_state()
    baseline = state["templates"]["baseline"]
    baseline["agent_defaults"]["runtime"] = "zeroclaw-agent"
    store.write_state(state)

    agent = service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )

    assert agent["agent"]["runtime"] == "openclaw-agent"


def test_service_action_falls_back_when_bus_unavailable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    home.mkdir()
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:3] == ["loginctl", "enable-linger", "teleclaw"]:
            return Result(0)
        if cmd[:2] == ["systemctl", "start"]:
            return Result(0)
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and "service" in cmd:
            return Result(1, stderr="Failed to connect to bus: No medium found")
        if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"]:
            return Result(0, stdout="4321\n")
        return Result(0)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/home/linuxbrew/.linuxbrew/bin/zeroclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.agent_service_action("teleclaw", "start")
    assert result["service_status"] == "running"

    updated = service.get_agent("teleclaw")
    assert updated["agent"]["service_mode"] == "fallback"
    assert int(updated["agent"]["fallback_pid"]) == 4321


def test_service_action_fallback_uses_provider_state_dir(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".picoclaw").mkdir(parents=True)
    (home / ".picoclaw" / "config.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "token": _fake_telegram_token(),
                        "name": "teleclaw-team",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)
    agent["channels"] = [{"kind": "telegram", "name": "teleclaw-team", "enabled": True}]

    commands: list[list[str]] = []
    runtime_running = False

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        nonlocal runtime_running
        commands.append(cmd)
        if cmd[:2] == ["ps", "-eo"]:
            if runtime_running:
                return Result(0, stdout="teleclaw 4321 /home/linuxbrew/.linuxbrew/bin/picoclaw gateway\n")
            return Result(0, stdout="")
        if cmd[:3] == ["loginctl", "enable-linger", "teleclaw"]:
            return Result(0)
        if cmd[:2] == ["systemctl", "start"]:
            return Result(0)
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and "service" in cmd:
            return Result(1, stderr="Failed to connect to bus: No medium found")
        if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"]:
            script = str(cmd[-1])
            if "setsid" in script:
                runtime_running = True
                return Result(0, stdout="4321\n")
            if "pgrep" in script:
                return Result(0, stdout="active (running)" if runtime_running else "inactive")
            return Result(0, stdout="4321\n")
        return Result(0)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/home/linuxbrew/.linuxbrew/bin/picoclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    # Replicate the non-root reality where the foreign user's unit file
    # cannot be written, so the process-fallback path under test is taken
    # even when the suite itself runs as root.
    def deny_unit(self: ClawieService, provider: str, linux_user: str) -> None:
        raise PermissionError("unit dir not writable")

    monkeypatch.setattr(ClawieService, "_ensure_generated_user_service_unit", deny_unit)

    result = service.agent_service_action("teleclaw", "start")
    assert result["service_status"] == "running"
    assert any(".picoclaw/daemon.log" in cmd[-1] for cmd in commands if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"])


def test_agent_service_start_surfaces_picoclaw_daemon_log_on_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".picoclaw").mkdir(parents=True)
    (home / ".picoclaw" / "config.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "token": _fake_telegram_token(),
                        "name": "teleclaw-team",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)
    agent["channels"] = [{"kind": "telegram", "name": "teleclaw-team", "enabled": True}]

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-eo"]:
            return Result(0, stdout="")
        if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"]:
            script = str(cmd[-1])
            if "setsid" in script:
                return Result(0, stdout="4321\n")
            if "tail -n" in script and ".picoclaw/daemon.log" in script:
                return Result(
                    0,
                    stdout="Error starting channels: failed to create telegram bot: telego: invalid token format\n",
                )
            if "pgrep" in script:
                return Result(0, stdout="inactive")
            return Result(0, stdout="")
        return Result(0, stdout="")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/home/linuxbrew/.linuxbrew/bin/picoclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    with raises(SetupError, match="failed to create telegram bot"):
        service.agent_service_action("teleclaw", "start")


def test_agent_service_start_surfaces_picoclaw_probe_output_when_log_is_empty(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".picoclaw").mkdir(parents=True)
    (home / ".picoclaw" / "config.json").write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "enabled": True,
                        "token": _fake_telegram_token(),
                        "name": "teleclaw-team",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)
    agent["channels"] = [{"kind": "telegram", "name": "teleclaw-team", "enabled": True}]

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-eo"]:
            return Result(0, stdout="")
        if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"]:
            script = str(cmd[-1])
            if "setsid" in script:
                return Result(0, stdout="4321\n")
            if "tail -n" in script and ".picoclaw/daemon.log" in script:
                return Result(0, stdout="")
            if "pgrep" in script:
                return Result(0, stdout="inactive")
            return Result(0, stdout="")
        if cmd[:5] == ["sudo", "-u", "teleclaw", "-H", "--"] and cmd[-2:] == ["picoclaw", "gateway"]:
            return Result(1, stderr="Error starting channels: failed to create telegram bot: telego: invalid token format")
        return Result(0, stdout="")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "picoclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    class ProbeProcess:
        pid = 4321
        returncode = 1

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            del timeout
            return "", "Error starting channels: failed to create telegram bot: invalid token format"

    monkeypatch.setattr(
        "clawie._service_runtime.subprocess.Popen",
        lambda *_args, **_kwargs: ProbeProcess(),
    )

    with raises(SetupError, match="foreground startup probe exited 1"):
        service.agent_service_action("teleclaw", "start")


def test_provider_start_probe_terminates_the_full_process_group_on_timeout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    popen_kwargs: dict[str, object] = {}
    signals: list[tuple[int, int]] = []

    class ProbeProcess:
        pid = 9876
        returncode = -15
        calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["openclaw", "gateway", "run"], 5)
            return "gateway booted", ""

    process = ProbeProcess()

    def fake_popen(*_args: object, **kwargs: object) -> ProbeProcess:
        popen_kwargs.update(kwargs)
        return process

    def fake_killpg(pid: int, signum: int) -> None:
        signals.append((pid, signum))
        if signum == 0:
            raise ProcessLookupError

    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "/usr/bin/openclaw")
    monkeypatch.setattr(service, "_wrap_user_command", lambda command, *_args, **_kwargs: command)
    monkeypatch.setattr("clawie._service_runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr("clawie._service_runtime.os.killpg", fake_killpg)

    output = service._provider_start_probe_output(provider="openclaw", linux_user="agent-a")

    assert "stayed alive for 5s" in output
    assert "gateway booted" in output
    assert popen_kwargs["start_new_session"] is True
    assert signals == [(9876, signal.SIGTERM), (9876, 0)]


def test_attach_agent_runtime_status_marks_managed_agent_stopped_when_no_live_daemon(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    payload = {
        "agent": {
            "provider": "openclaw",
            "linux_user": "teleclaw",
            "service_status": "running",
            "service_mode": "systemd",
        }
    }
    monkeypatch.setattr(service, "_provider_reports_running", lambda _provider, _linux_user: False)

    updated = service._attach_agent_runtime_status(payload, daemon_map={})

    assert updated["agent"]["service_status"] == "stopped"
    assert updated["agent"]["live_provider"] == ""
    assert updated["agent"]["live_providers"] == []
    assert updated["agent"]["live_pid"] == 0


def test_performance_snapshot_includes_local_user_claw_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    monkeypatch.setattr(
        service,
        "list_installed_claws",
        lambda source_home=None: [{"provider": "zeroclaw", "root": "/home/me/.zeroclaw", "markers": []}],
    )
    snapshot = service.performance_snapshot(refresh=False)
    ids = {str(row.get("agent_id", "")) for row in snapshot["rows"]}
    assert "alice" in ids
    assert "@local:zeroclaw" in ids


def test_performance_snapshot_survives_denied_ps_process_list(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    monkeypatch.setattr(service, "list_installed_claws", lambda source_home=None: [])

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-eo"]:
            raise PermissionError("ps denied")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)

    snapshot = service.performance_snapshot(refresh=False)

    assert snapshot["rows"] == []
    assert snapshot["totals"]["agents"] == 0


def test_performance_snapshot_uses_live_runtime_as_provider_source_of_truth(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-eo"]:
            return Result(stdout="teleclaw 4321 /home/linuxbrew/.linuxbrew/bin/zeroclaw daemon\n")
        if cmd[:2] == ["ps", "-p"]:
            return Result(stdout=" 1.5  2.5  4096\n")
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(service, "_probe_process_cgroup", lambda pid: None)
    snapshot = service.performance_snapshot(refresh=False)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "teleclaw")

    assert row["provider"] == "zeroclaw"
    assert row["provider_status"] == "warning"
    assert "aligned state away from openclaw" in row["provider_issue"]
    assert "sudo clawie agent provider set teleclaw openclaw" in row["provider_remediation"]
    assert row["status"] == "running"
    assert row["pid"] == 4321
    assert row["cpu_percent"] == 1.5
    assert row["mem_percent"] == 2.5
    assert row["rss_kb"] == 4096
    # Observational status may show live drift but must not rewrite desired state.
    assert service.store.read_state()["agents"]["teleclaw"]["agent"]["provider"] == "openclaw"


def test_probe_process_falls_back_when_ps_is_denied(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    fallback = {"cpu_percent": 0.0, "mem_percent": 0.0, "rss_kb": 2048}

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-p"]:
            raise PermissionError("ps denied")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(service, "_probe_process_cgroup", lambda pid: None)
    monkeypatch.setattr(service, "_probe_process_procfs", lambda pid: fallback)

    assert service._probe_process(12345) == fallback


def test_collect_metrics_uses_live_provider_process_when_stored_pid_is_empty(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    agent = service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    agent["agent"]["pid"] = 0
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-eo"]:
            return Result(stdout="teleclaw 4321 /home/linuxbrew/.linuxbrew/bin/openclaw gateway run\n")
        if cmd[:2] == ["ps", "-p"]:
            return Result(stdout=" 3.5  4.5  8192\n")
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(service, "_probe_process_cgroup", lambda pid: None)

    result = service.collect_metrics()
    metric = service.store.latest_metrics(limit_per_user=1)["teleclaw"][0]
    updated = service.get_agent("teleclaw")

    assert result["sampled"] == 1
    assert metric["status"] == "running"
    assert metric["cpu_percent"] == 3.5
    assert metric["mem_percent"] == 4.5
    assert metric["rss_kb"] == 8192
    assert updated["agent"]["pid"] == 4321


def test_probe_process_procfs_parses_rss_and_memory_percent(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    pid_dir = proc / "123"
    pid_dir.mkdir(parents=True)
    (pid_dir / "status").write_text(
        "Name:\topenclaw\nVmRSS:\t2048 kB\n",
        encoding="utf-8",
    )
    (proc / "meminfo").write_text("MemTotal:       4096 kB\n", encoding="utf-8")

    probe = ClawieService._probe_process_procfs(123, proc_root=proc)

    assert probe == {"cpu_percent": 0.0, "mem_percent": 50.0, "rss_kb": 2048}


def test_probe_process_cgroup_v2_parses_memory_current(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup_root = tmp_path / "sys" / "fs" / "cgroup"
    pid_dir = proc / "123"
    cgroup_dir = cgroup_root / "user.slice" / "user-1000.slice" / "openclaw.service"
    pid_dir.mkdir(parents=True)
    cgroup_dir.mkdir(parents=True)
    (pid_dir / "cgroup").write_text(
        "0::/user.slice/user-1000.slice/openclaw.service\n",
        encoding="utf-8",
    )
    (proc / "meminfo").write_text("MemTotal:       8192 kB\n", encoding="utf-8")
    (cgroup_dir / "memory.current").write_text("4194304\n", encoding="utf-8")

    probe = ClawieService._probe_process_cgroup(123, proc_root=proc, cgroup_root=cgroup_root)

    assert probe == {"cpu_percent": 0.0, "mem_percent": 50.0, "rss_kb": 4096}


def test_probe_process_prefers_cgroup_memory_over_ps_rss(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    class Result:
        returncode = 0
        stdout = " 7.5  1.0  1024\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(
        service,
        "_probe_process_cgroup",
        lambda pid: {"cpu_percent": 0.0, "mem_percent": 12.5, "rss_kb": 8192},
    )

    assert service._probe_process(123) == {
        "cpu_percent": 7.5,
        "mem_percent": 12.5,
        "rss_kb": 8192,
    }


def test_probe_process_falls_back_to_procfs(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    class Result:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(service, "_probe_process_cgroup", lambda pid: None)
    monkeypatch.setattr(
        service,
        "_probe_process_procfs",
        lambda pid: {"cpu_percent": 0.0, "mem_percent": 1.25, "rss_kb": 512},
    )

    assert service._probe_process(123) == {
        "cpu_percent": 0.0,
        "mem_percent": 1.25,
        "rss_kb": 512,
    }


def test_local_claw_service_action_updates_local_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["/usr/bin/zeroclaw", "service"]:
            return Result(0, stdout="active (running)")
        return Result(0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/zeroclaw")
    monkeypatch.setattr("subprocess.run", fake_run)
    result = service.local_claw_service_action("zeroclaw", "status")
    assert result["service_status"] == "running"
    assert result["service_mode"] == "systemd"

    cfg = service.store.read_config()
    local_state = cfg.get("local_service_state", {})
    assert isinstance(local_state, dict)
    assert str(local_state.get("zeroclaw", {}).get("service_status", "")) == "running"


def test_local_claw_service_status_falls_back_to_stopped_on_command_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["/usr/bin/zeroclaw", "service"]:
            return Result(1, stderr="random status failure")
        return Result(0)

    monkeypatch.setattr("subprocess.run", fake_run)
    result = service.local_claw_service_action("zeroclaw", "status")
    assert result["service_status"] == "stopped"
    assert result["service_mode"] == "fallback"


def test_local_claw_service_status_empty_success_output_uses_best_effort(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: Result(0, stdout=""))
    result = service.local_claw_service_action("zeroclaw", "status")
    assert result["service_status"] == "stopped"
    assert result["service_mode"] == "fallback"


def test_local_claw_service_stop_prefers_systemd_machine_control(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class AliceInfo:
        pw_uid = 1001
        pw_dir = "/home/alice"
        pw_name = "alice"

    class RootInfo:
        pw_uid = 0
        pw_dir = "/root"
        pw_name = "root"

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda _: AliceInfo())
    monkeypatch.setattr("pwd.getpwuid", lambda _uid: RootInfo())
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd[:4] == ["systemctl", "--machine", "alice@", "--user"] and "stop" in cmd:
            return Result(0, stdout="")
        return Result(1, stderr="unexpected")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = service.local_claw_service_action("zeroclaw", "stop")
    assert result["service_status"] == "stopped"
    assert result["service_mode"] == "systemd"
    assert any(cmd[:4] == ["systemctl", "--machine", "alice@", "--user"] and "stop" in cmd for cmd in calls)


def test_dashboard_refresh_local_status_non_bus_error_uses_best_effort(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["/usr/bin/zeroclaw", "service"]:
            return Result(1, stderr="unexpected failure")
        return Result(0)

    monkeypatch.setattr("subprocess.run", fake_run)
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "stopped"


def test_dashboard_refresh_local_status_empty_success_output_uses_best_effort(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: Result(0, stdout=""))
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "stopped"


def test_dashboard_refresh_local_status_retries_as_sudo_user_for_parseable_output(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class AliceInfo:
        pw_uid = 1001
        pw_dir = "/home/alice"
        pw_name = "alice"

    class RootInfo:
        pw_uid = 0
        pw_dir = "/root"
        pw_name = "root"

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda _: AliceInfo())
    monkeypatch.setattr("pwd.getpwuid", lambda uid: RootInfo() if int(uid) == 0 else AliceInfo())
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd[:2] == ["/usr/bin/zeroclaw", "service"]:
            return Result(0, stdout="")
        if cmd[:3] == ["sudo", "-u", "alice"]:
            return Result(0, stdout="Service state: active")
        return Result(1, stderr="unexpected")

    monkeypatch.setattr("subprocess.run", fake_run)
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "running"
    assert any(cmd[:3] == ["sudo", "-u", "alice"] for cmd in calls)


def test_dashboard_refresh_uses_user_hint_from_provider_root_when_sudo_user_missing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class RootInfo:
        pw_uid = 0
        pw_dir = "/root"
        pw_name = "root"

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwuid", lambda _uid: RootInfo())
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/home/alice/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd[:2] == ["/usr/bin/zeroclaw", "service"]:
            return Result(0, stdout="")
        if cmd[:3] == ["sudo", "-u", "alice"]:
            return Result(0, stdout="Service state: active")
        return Result(1, stderr="unexpected")

    monkeypatch.setattr("subprocess.run", fake_run)
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "running"
    assert any(cmd[:3] == ["sudo", "-u", "alice"] for cmd in calls)


def test_dashboard_refresh_prefers_sudo_user_over_root_hint(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class AliceInfo:
        pw_uid = 1001
        pw_dir = "/home/alice"
        pw_name = "alice"

    class RootInfo:
        pw_uid = 0
        pw_dir = "/root"
        pw_name = "root"

    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("pwd.getpwnam", lambda _: AliceInfo())
    monkeypatch.setattr("pwd.getpwuid", lambda _uid: RootInfo())
    monkeypatch.setattr(
        "clawie.service.detect_installed_providers",
        lambda _: [{"provider": "zeroclaw", "root": "/root/.zeroclaw", "markers": []}],
    )
    monkeypatch.setattr("clawie.service.shutil.which", lambda _: "/usr/bin/zeroclaw")

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd[:4] == ["systemctl", "--machine", "alice@", "--user"]:
            return Result(0, stdout="active")
        return Result(1, stderr="failed")

    monkeypatch.setattr("subprocess.run", fake_run)
    snapshot = service.performance_snapshot(refresh=True)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "@local:zeroclaw")
    assert row["status"] == "running"
    assert any(cmd[:4] == ["systemctl", "--machine", "alice@", "--user"] for cmd in calls)


def test_preferred_local_linux_user_prioritizes_default_over_cached() -> None:
    selected = ClawieService._preferred_local_linux_user(
        default_user="azicon",
        hint_user="teleclaw",
        cached_user="teleclaw",
    )
    assert selected == "azicon"


def test_parse_systemctl_status_ignores_bus_errors() -> None:
    assert ClawieService._parse_systemctl_status("", "Failed to connect to bus: No medium found") == "unknown"
    assert ClawieService._parse_systemctl_status("active\n", "") == "running"
    assert ClawieService._parse_systemctl_status("inactive\n", "") == "stopped"


def test_systemd_status_prefers_any_running_candidate_over_stopped(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(service, "_local_target_user", lambda: "root")
    monkeypatch.setattr(service, "_local_linux_user_hint", lambda _provider, _fallback: "root")

    class FakeEntry:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_dir(self) -> bool:
            return True

    class FakeHomePath:
        def __init__(self, raw: str) -> None:
            self.raw = raw

        def exists(self) -> bool:
            return self.raw == "/home"

        def iterdir(self) -> list[FakeEntry]:
            return [FakeEntry("root"), FakeEntry("azicon")]

    # _systemd_user_candidates lives in the runtime mixin module.
    monkeypatch.setattr("clawie._service_runtime.Path", FakeHomePath)

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:4] == ["systemctl", "--machine", "root@", "--user"]:
            return Result(3, stdout="inactive")
        if cmd[:4] == ["systemctl", "--machine", "azicon@", "--user"]:
            return Result(0, stdout="active")
        return Result(1, stderr="Failed to connect to bus")

    monkeypatch.setattr("subprocess.run", fake_run)
    status = service._systemd_user_service_status("zeroclaw", linux_user="root")
    assert status == "running"


def test_managed_systemd_command_runs_as_exact_target_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    calls: list[list[str]] = []

    class PwdRow:
        pw_uid = 12345
        pw_dir = "/home/managed-agent"

    class Result:
        returncode = 0
        stdout = "active\n"
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(service, "_current_linux_user", lambda: "root")
    monkeypatch.setattr("clawie._service_runtime.pwd.getpwnam", lambda _user: PwdRow())
    monkeypatch.setattr("clawie._service_runtime.subprocess.run", fake_run)

    result = service._run_systemd_user_command("managed-agent", ["is-active", "openclaw.service"])

    assert result["ok"] is True
    assert calls == [
        [
            "sudo",
            "-u",
            "managed-agent",
            "-H",
            "--",
            "env",
            "XDG_RUNTIME_DIR=/run/user/12345",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/12345/bus",
            f"PATH={service._service_env('managed-agent')['PATH']}",
            "systemctl",
            "--user",
            "is-active",
            "openclaw.service",
        ]
    ]
    assert service._systemd_user_candidates("managed-agent", "openclaw") == ["managed-agent"]


def test_channel_inventory_includes_agent_and_local_channels(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "ops"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    monkeypatch.setattr(
        service,
        "_local_channel_inventory",
        lambda: [
            {
                "source": "local",
                "owner_agent_id": "@local:zeroclaw",
                "provider": "zeroclaw",
                "kind": "telegram",
                "name": "primary",
                "enabled": True,
            }
        ],
    )
    snapshot = service.channel_inventory()
    rows = snapshot["rows"]
    assert any(row["owner_agent_id"] == "alice" and row["kind"] == "chat" for row in rows)
    assert any(row["owner_agent_id"] == "@local:zeroclaw" and row["kind"] == "telegram" for row in rows)


def test_assign_channel_moves_between_agents(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.create_agent(
        agent_id="bob",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.assign_channel_to_agent("alice", "chat", "alice-support", "bob")
    alice = service.get_agent("alice")
    bob = service.get_agent("bob")
    assert not any(c.get("name") == "alice-support" for c in alice.get("channels", []))
    assert any(c.get("name") == "alice-support" for c in bob.get("channels", []))


def test_assign_channel_without_source_still_moves_existing_owner(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.create_agent(
        agent_id="bob",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    result = service.assign_channel_to_agent("", "chat", "alice-support", "bob")
    assert result["moved"] is True
    assert "alice" in result.get("moved_from_agent_ids", [])

    alice = service.get_agent("alice")
    bob = service.get_agent("bob")
    assert not any(c.get("name") == "alice-support" for c in alice.get("channels", []))
    assert any(c.get("name") == "alice-support" for c in bob.get("channels", []))


def test_create_agent_rejects_channel_already_owned_by_another_agent(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    try:
        service.create_agent(
            agent_id="bob",
            display_name=None,
            template="baseline",
            clone_from=None,
            channel_strategy="migrate",
            channels=[{"kind": "chat", "name": "alice-support"}],
            agent_version="1.0.0",
            provider="openclaw",
        )
        assert False, "expected create_agent to reject duplicate channel ownership"
    except Exception as exc:  # noqa: BLE001
        assert "channel already assigned" in str(exc)


def test_clone_with_migrate_transfers_channels_from_source_agent(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="src",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.create_agent(
        agent_id="dst",
        display_name=None,
        template="baseline",
        clone_from="src",
        channel_strategy="migrate",
        channels=None,
        agent_version="1.0.0",
        provider="openclaw",
    )
    src = service.get_agent("src")
    dst = service.get_agent("dst")
    assert not any(c.get("name") == "src-support" for c in src.get("channels", []))
    assert any(
        c.get("name") == "src-support" and c.get("migrated_from") == "src"
        for c in dst.get("channels", [])
    )


def test_migrate_channels_moves_ownership_instead_of_copying(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.create_agent(
        agent_id="bob",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.migrate_channels("alice", "bob")
    alice = service.get_agent("alice")
    bob = service.get_agent("bob")
    assert not any(c.get("name") == "alice-support" for c in alice.get("channels", []))
    assert any(c.get("name") == "alice-support" for c in bob.get("channels", []))


def test_unassign_channel_moves_to_pool_and_stays_visible(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )

    service.unassign_channel_from_agent("alice", "chat", "alice-support")
    alice = service.get_agent("alice")
    assert not any(c.get("name") == "alice-support" for c in alice.get("channels", []))

    inventory = service.channel_inventory()
    assert any(
        row.get("source") == "pool"
        and row.get("owner_agent_id") == "@pool"
        and row.get("kind") == "chat"
        and row.get("name") == "alice-support"
        for row in inventory.get("rows", [])
    )


def test_assign_from_pool_removes_pool_entry(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[{"kind": "chat", "name": "support"}],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.create_agent(
        agent_id="bob",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    service.unassign_channel_from_agent("alice", "chat", "alice-support")
    service.assign_channel_to_agent("@pool", "chat", "alice-support", "bob")

    inventory = service.channel_inventory()
    assert not any(
        row.get("source") == "pool"
        and row.get("kind") == "chat"
        and row.get("name") == "alice-support"
        for row in inventory.get("rows", [])
    )
    bob = service.get_agent("bob")
    assert any(c.get("name") == "alice-support" for c in bob.get("channels", []))


def test_connect_agent_channel_runs_provider_command(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )

    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return Result(0, stdout="connected")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/openclaw")
    monkeypatch.setattr("subprocess.run", fake_run)
    result = service.connect_agent_channel("alice", "telegram", "team")
    assert result["status"] == "connected"
    assert any(cmd[:3] == ["/usr/bin/openclaw", "channels", "add"] for cmd in calls)


def test_connect_agent_channel_rolls_back_assignment_when_provider_command_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="zeroclaw",
    )

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/zeroclaw")
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **_: Result(1, stderr="connect failed"),
    )

    with raises(Exception):
        service.connect_agent_channel("alice", "telegram", "team")

    agent = service.get_agent("alice")
    assert not any(c.get("kind") == "telegram" and c.get("name") == "team" for c in agent.get("channels", []))


def test_channel_connect_commands_for_picoclaw_do_not_use_channel_add(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/picoclaw")
    commands = service._channel_connect_commands("picoclaw", "telegram", "team", linux_user="")
    assert commands == []
    assert not any(len(cmd) >= 2 and cmd[1] == "channel" for cmd in commands)


# ── clawie status (unified read-only overview) ──────────────────────────────


def _configured_service(tmp_path: Path) -> ClawieService:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    return ClawieService(StateStore(config_dir=tmp_path))


def test_status_snapshot_includes_all_sections(tmp_path: Path) -> None:
    snapshot = _configured_service(tmp_path).status_snapshot()
    for section in (
        "setup", "health", "agents", "runtimes",
        "auth", "delegation", "maintenance", "backup", "events",
    ):
        assert section in snapshot
    assert "generated_at" in snapshot


def test_status_snapshot_scopes_to_requested_sections(tmp_path: Path) -> None:
    snapshot = _configured_service(tmp_path).status_snapshot(sections=["agents"])
    assert "agents" in snapshot
    assert "health" not in snapshot
    assert "events" not in snapshot


def test_status_snapshot_rejects_unknown_section(tmp_path: Path) -> None:
    with raises(ValueError):
        _configured_service(tmp_path).status_snapshot(sections=["bogus"])


def test_status_snapshot_isolates_section_errors(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = _configured_service(tmp_path)

    def boom(*_a: object, **_k: object) -> dict[str, object]:
        raise RuntimeError("maintenance exploded")

    monkeypatch.setattr(service, "maintenance_status", boom)
    snapshot = service.status_snapshot()
    # The failing section degrades to an error note; the rest still render.
    assert snapshot["maintenance"] == {"error": "maintenance exploded"}
    assert "checks" in snapshot["health"]


def test_status_command_json_output(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "status", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["setup"]["provider"] == "openclaw"
    assert "agents" in payload


def test_status_command_partial_section_error_still_exits_zero(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    def boom(self: ClawieService) -> dict[str, object]:
        raise RuntimeError("maintenance exploded")

    monkeypatch.setattr(ClawieService, "maintenance_status", boom)
    assert run_cli(tmp_path, "status", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["maintenance"] == {"error": "maintenance exploded"}
    assert "checks" in payload["health"]


def test_status_command_unsafe_database_sidecar_exits_nonzero(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    (tmp_path / "clawie.db-wal").symlink_to(victim)

    assert run_cli(tmp_path, "status", "--json") == 1

    payload = json.loads(capsys.readouterr().out)
    assert any(
        "database sidecar must be a regular file" in str(section.get("error", ""))
        for section in payload.values()
        if isinstance(section, dict)
    )
    assert victim.read_text(encoding="utf-8") == "keep"


def test_status_command_does_not_initialize_or_chmod_an_unconfigured_state_root(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "unrelated.txt").write_text("keep", encoding="utf-8")
    shared.chmod(0o755)
    shared_auth = tmp_path / "system-shared-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared_auth)
    before = sorted(path.relative_to(shared) for path in shared.rglob("*"))

    code = run_cli(shared, "status", "--json")

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["setup"]["configured"] is False
    assert not (shared / "clawie.db").exists()
    assert (shared / "unrelated.txt").read_text(encoding="utf-8") == "keep"
    assert shared.stat().st_mode & 0o777 == 0o755
    assert sorted(path.relative_to(shared) for path in shared.rglob("*")) == before
    assert not shared_auth.exists()


def test_runtime_status_does_not_generate_or_start_a_user_service(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(service, "_linux_home_for_user", lambda _user: home)

    result = service._run_generated_user_service_action(
        provider="openclaw",
        action="status",
        linux_user="worker",
        agent_info={},
    )

    assert result is None
    assert not (home / ".config").exists()


def test_status_does_not_mutate_a_configured_state_tree(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    service = ClawieService(StateStore(config_dir=state_root))
    service.setup(provider="openclaw")
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared-auth")

    def fingerprint() -> list[tuple[str, int, int, int]]:
        return sorted(
            (
                str(path.relative_to(state_root)),
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            )
            for path in state_root.rglob("*")
        )

    before = fingerprint()
    service.status_snapshot()

    assert fingerprint() == before
    assert not (tmp_path / "shared-auth").exists()


def test_agent_create_and_show_print_requested_model_tier(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    assert run_cli(tmp_path, "agent", "create", "alice", "--model-tier", "fast") == 0
    create_output = capsys.readouterr().out
    assert "model_tier: fast" in create_output

    assert run_cli(tmp_path, "agent", "show", "alice") == 0
    show_output = capsys.readouterr().out
    assert "model_tier: fast" in show_output


def test_agent_create_labels_uninspected_statuses_accurately(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    assert run_cli(tmp_path, "agent", "create", "alice") == 0
    output = capsys.readouterr().out

    assert "auth_status: not checked" in output
    assert "channel_source: state" in output
    assert "channel_status:" not in output


def test_agent_clone_defaults_to_nondestructive_channel_names(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert (
        run_cli(
            tmp_path,
            "agent",
            "create",
            "alice",
            "--channel",
            "telegram:support",
        )
        == 0
    )
    capsys.readouterr()

    assert run_cli(tmp_path, "agent", "clone", "alice", "bob") == 0
    output = capsys.readouterr().out
    state = StateStore(config_dir=tmp_path).read_state()["agents"]

    assert "Transferred" not in output
    assert [row["name"] for row in state["alice"]["channels"]] == ["alice-support"]
    assert [row["name"] for row in state["bob"]["channels"]] == ["bob-alice-support"]


def test_agent_clone_migrate_warns_that_channel_ownership_moves(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert (
        run_cli(
            tmp_path,
            "agent",
            "create",
            "alice",
            "--channel",
            "telegram:support",
        )
        == 0
    )
    capsys.readouterr()

    assert (
        run_cli(
            tmp_path,
            "agent",
            "clone",
            "alice",
            "bob",
            "--channel-strategy",
            "migrate",
        )
        == 0
    )
    output = capsys.readouterr().out
    state = StateStore(config_dir=tmp_path).read_state()["agents"]

    assert "Transferred 1 channel(s) from alice" in output
    assert state["alice"]["channels"] == []
    assert [row["name"] for row in state["bob"]["channels"]] == ["alice-support"]


def test_agent_help_lists_every_registered_subcommand(capsys: CaptureFixture[str]) -> None:
    parser = build_parser()
    with raises(SystemExit) as exc:
        parser.parse_args(["agent", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "fix-permissions" in output.split("positional arguments:", 1)[0]


def test_setting_same_model_tier_does_not_emit_change_event(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(provider="openclaw")
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    before = list(service.store.read_state()["events"])

    assert service.set_agent_model_tier("alice", "balanced") == "balanced"

    after = service.store.read_state()["events"]
    assert after == before


def test_status_agents_include_model_tier(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    assert run_cli(tmp_path, "agent", "create", "alice", "--model-tier", "power") == 0
    capsys.readouterr()

    snapshot = ClawieService(StateStore(config_dir=tmp_path)).status_snapshot(sections=["agents"])
    assert snapshot["agents"]["rows"][0]["model_tier"] == "power"

    assert run_cli(tmp_path, "status", "agents") == 0
    output = capsys.readouterr().out
    assert "tier" in output
    assert "power" in output


def test_status_command_section_argument(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "status", "health") == 0
    output = capsys.readouterr().out
    assert "Health" in output
    assert "cpu%" not in output  # scoped to one section; no agents table


def test_status_command_rejects_unknown_section(tmp_path: Path) -> None:
    with raises(SystemExit):  # argparse choices rejection
        run_cli(tmp_path, "status", "nonsense")


def test_dashboard_is_deprecated_alias_for_status(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "dashboard") == 0
    output = capsys.readouterr().out
    assert "deprecated" in output
    assert "Setup" in output  # renders the status overview


# ── spawn hardening ─────────────────────────────────────────────────────────


def test_set_password_plaintext_surfaces_stderr(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    class Result:
        returncode = 1
        stdout = ""
        stderr = "chpasswd: permission denied"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    with raises(SetupError) as exc:
        service._set_password_plaintext("sam", "secret")
    assert "permission denied" in str(exc.value)


def test_set_password_hash_surfaces_stderr(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    class Result:
        returncode = 1
        stdout = ""
        stderr = "usermod: boom"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    with raises(SetupError) as exc:
        service._set_password_hash("sam", "$6$abc$def")
    assert "boom" in str(exc.value)


def test_spawn_fails_when_useradd_leaves_no_home(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "openclaw") == 0
    capsys.readouterr()

    def fake_run(cmd: list[str], **_: object) -> object:
        class Result:
            returncode = 1 if cmd[:2] == ["id", "-u"] else 0
            stdout = ""
            stderr = ""

        return Result()

    class FakePwd:
        pw_dir = "/nonexistent/clawie-home"

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    # useradd "succeeds" and registers the user, but its home was never created.
    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda _user: FakePwd())
    monkeypatch.setattr(ClawieService, "_disable_ssh_login_for_user", lambda self, _u: True)
    monkeypatch.setattr(
        ClawieService,
        "ensure_provider_runtime",
        lambda self, provider: {"provider": provider, "installed": False, "already_present": True},
    )

    code = run_cli(tmp_path, "runtime", "create", "sam", "--user", "sam", "--skip-config-copy")
    output = capsys.readouterr().err
    assert code == 1
    assert "was not created" in output


def test_disable_ssh_login_validates_config_before_reload(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    monkeypatch.setattr(ClawieService, "SSHD_DENY_USERS_FILE", tmp_path / "deny.conf")
    monkeypatch.setattr(
        "clawie._service_spawn.shutil.which",
        lambda name: "/usr/sbin/sshd" if name == "sshd" else None,
    )

    reloads: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["/usr/sbin/sshd", "-t"]:
            class Bad:
                returncode = 1
                stdout = ""
                stderr = "bad config line 3"

            return Bad()
        reloads.append(cmd)

        class Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return Ok()

    monkeypatch.setattr("subprocess.run", fake_run)
    with raises(SetupError) as exc:
        service._disable_ssh_login_for_user("sam")
    assert "validation failed" in str(exc.value)
    assert reloads == []  # a bad config is never reloaded


def test_ensure_workspace_accessible_collects_warnings_without_raising(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    class UserInfo:
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda _user: UserInfo())
    monkeypatch.setattr(
        "clawie._service_spawn.os.fchown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("operation not permitted")),
    )
    warnings = service._ensure_workspace_accessible("openclaw", home, "sam")
    assert warnings  # failures are collected, not silently swallowed
    assert any("not permitted" in w for w in warnings)


def test_ensure_workspace_accessible_enforces_private_modes_without_group_membership(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    home = tmp_path / "home"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    config = home / ".openclaw" / "openclaw.json"
    note = workspace / "MEMORY.md"
    config.write_text("{}", encoding="utf-8")
    note.write_text("memory", encoding="utf-8")
    for path in (home, home / ".openclaw", workspace):
        path.chmod(0o775)
    for path in (config, note):
        path.chmod(0o664)

    class UserInfo:
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda _user: UserInfo())
    monkeypatch.setattr("clawie._service_spawn.os.fchown", lambda *_args, **_kwargs: None)

    warnings = service._ensure_workspace_accessible("openclaw", home, "alice")

    assert warnings == []
    assert home.stat().st_mode & 0o777 == 0o700
    assert (home / ".openclaw").stat().st_mode & 0o777 == 0o700
    assert workspace.stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == 0o600
    assert note.stat().st_mode & 0o777 == 0o600


def test_ensure_workspace_accessible_rejects_a_symlinked_provider_tree(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    home = tmp_path / "home"
    home.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    secret = victim / "secret.txt"
    secret.write_text("keep", encoding="utf-8")
    victim.chmod(0o755)
    secret.chmod(0o644)
    (home / ".openclaw").symlink_to(victim, target_is_directory=True)

    class UserInfo:
        pw_uid = os.getuid()
        pw_gid = os.getgid()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("clawie._service_spawn.pwd.getpwnam", lambda _user: UserInfo())
    monkeypatch.setattr("clawie._service_spawn.os.fchown", lambda *_args, **_kwargs: None)

    warnings = service._ensure_workspace_accessible("openclaw", home, "alice")

    assert any("provider state is not a real directory" in warning for warning in warnings)
    assert victim.stat().st_mode & 0o777 == 0o755
    assert secret.stat().st_mode & 0o777 == 0o644
    assert secret.read_text(encoding="utf-8") == "keep"


def test_hash_password_falls_back_to_openssl_when_crypt_not_sha512(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))

    class FakeCrypt:
        METHOD_SHA512 = "sha512"

        @staticmethod
        def mksalt(_method: object) -> str:
            return "salt"

        @staticmethod
        def crypt(_pw: str, _salt: str) -> str:
            return "$1$weakhash"  # not SHA512 — must be rejected

    class OpensslResult:
        returncode = 0
        stdout = "$6$realsalt$realhash\n"
        stderr = ""

    monkeypatch.setattr("clawie._service_spawn.crypt", FakeCrypt)
    monkeypatch.setattr(
        "clawie._service_spawn.shutil.which",
        lambda name: "/usr/bin/openssl" if name == "openssl" else None,
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: OpensslResult())
    assert service._hash_password("strongpass").startswith("$6$")
