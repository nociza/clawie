from __future__ import annotations

import curses
import json
import os
import tempfile
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch, raises

import clawie.dashboard as dashboard
from clawie.provider_auth import (
    auth_status_from_picoclaw_auth_json,
    auth_status_from_profiles_json,
    parse_provider_auth_status_output,
)
from clawie.cli import main
from clawie.dashboard import DashboardState, _handle_detail_key, _run_setting_action, _settings_items
from clawie.providers import credential_paths_for_providers
from clawie.auth_sources import load_codex_auth
from clawie.service import ZeroClawService
from clawie.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


def _fake_jwt(payload: dict[str, object]) -> str:
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode("utf-8")).decode("utf-8").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{header}.{body}.sig"


def test_setup_defaults_to_linked_auth_for_picoclaw(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    code = run_cli(tmp_path, "config", "set")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider: picoclaw" in output
    assert "auth_mode: linked" in output


def test_setup_openclaw_without_api_key(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ZeroClawService,
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
    assert code == 0
    assert "provider: openclaw" in output
    assert "auth_mode: none" in output
    assert "api_url: https://api.openclaw.example/v1" in output
    assert "spawn_password_default: not set" in output
    assert "runtime_installed: True" in output

    status = run_cli(tmp_path, "config", "show")
    status_output = capsys.readouterr().out
    assert status == 0
    assert "configured: True" in status_output
    assert "api_url: https://api.openclaw.example/v1" in status_output


def test_runtime_install_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_install(self: ZeroClawService, provider: str) -> dict[str, object]:
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

    monkeypatch.setattr(ZeroClawService, "install_provider_runtime", fake_install)

    code = run_cli(tmp_path, "runtime", "install", "picoclaw")
    output = capsys.readouterr().out

    assert code == 0
    assert seen == ["picoclaw"]
    assert "Installed runtime for picoclaw" in output
    assert "provider: picoclaw" in output
    assert "method: brew" in output
    assert "executable: /mock/bin/picoclaw" in output


def test_setup_api_key_mode_requires_api_key(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    code = run_cli(tmp_path, "config", "set", "--provider", "picoclaw", "--auth-mode", "api_key")
    output = capsys.readouterr().out
    assert code == 1
    assert "API key is required when --auth-mode api_key is selected" in output


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
    assert code == 1
    assert "provider 'picoclaw' is not configured" in output

    assert run_cli(tmp_path, "config", "set", "--provider", "picoclaw") == 0
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


def test_create_agent_uses_picoclaw_as_default_provider(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "agent", "create", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider: picoclaw" in output


def test_agent_provider_set_changes_provider(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "picoclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "teleclaw", "--provider", "zeroclaw") == 1
    output = capsys.readouterr().out
    assert "provider 'zeroclaw' is not configured" in output

    assert run_cli(tmp_path, "config", "set", "--provider", "zeroclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agent", "create", "teleclaw", "--provider", "zeroclaw") == 0
    capsys.readouterr()

    assert run_cli(tmp_path, "config", "set", "--provider", "picoclaw") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "agent", "provider", "set", "teleclaw", "picoclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "Changed provider for teleclaw to picoclaw" in output
    assert "provider: picoclaw" in output


def test_agent_service_status_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ZeroClawService,
        "agent_service_action",
        lambda self, agent_id, action: {
            "agent_id": agent_id,
            "provider": "picoclaw",
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
    assert "Provider: picoclaw" in output


def test_runtime_service_status_cli(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ZeroClawService,
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

    code = run_cli(tmp_path, "dashboard")
    output = capsys.readouterr().out
    assert code == 0
    assert "Clawie Monitor" in output
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
        ZeroClawService,
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
        ZeroClawService,
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
        ZeroClawService,
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
        ZeroClawService,
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


def test_spawn_requires_root(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--api-key", "zc_live_1234") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "runtime", "create", "sam")
    output = capsys.readouterr().out
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
            returncode = 1
            stdout = ""

        if cmd[:2] == ["id", "-u"]:
            return Result()
        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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
            returncode = 1
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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
    assert code == 0
    assert "Spawned linux user sam" in output
    assert any(cmd[:2] == ["usermod", "-p"] for cmd in calls)


def test_spawn_uses_builtin_default_password_and_prints_it(
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
            returncode = 1
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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
    assert "Password source: default-password" in output
    assert "Password: clawie" in output
    assert "SSH login: disabled for spawned Linux user" in output
    assert any(cmd == ["chpasswd"] and input_data == "sam-default:clawie\n" for cmd, input_data in calls)


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
            returncode = 1
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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
            returncode = 1
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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
    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
    deny_file = tmp_path / "sshd_config.d" / "99-clawie-no-ssh.conf"
    monkeypatch.setattr(ZeroClawService, "SSHD_DENY_USERS_FILE", deny_file)

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
    state["agents"]["teleclaw"]["agent"]["linux_user"] = "teleclaw"
    store.write_state(state)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""

        if cmd[:2] == ["id", "-u"]:
            return Result()
        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

    code = run_cli(tmp_path, "agent", "purge", "teleclaw", "--yes")
    output = capsys.readouterr().out
    assert code == 0
    assert "Purged agent teleclaw" in output
    assert any(cmd[:2] == ["userdel", "-r"] for cmd in calls)
    assert "teleclaw" not in StateStore(config_dir=tmp_path).read_state()["agents"]


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
    output = capsys.readouterr().out
    assert code == 1
    assert "purge requires root privileges" in output


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
            returncode = 1
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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
            returncode = 1
            stdout = ""

        return Result()

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "_disable_ssh_login_for_user", lambda self, _username: True)

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


def test_agents_clone_prompts_copies_core_prompt_payload(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set", "--provider", "zeroclaw") == 0
    capsys.readouterr()
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
            "INSERT OR REPLACE INTO users(user_id, payload) VALUES (?, ?)",
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
    assert store.read_config()["schema_version"] == 1


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

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    assert "selected: provider-auth" in output

    code = run_cli(
        tmp_path,
        "agent",
        "credentials",
        "set",
        "alice",
        "git",
        "--include-defaults",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Selected bundles: provider-auth, git" in output

    code = run_cli(tmp_path, "agent", "credentials", "show", "alice")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider-auth" in output
    assert "git" in output


def test_service_syncs_and_revokes_selected_credential_bundles(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", shared_home)
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    source_home = tmp_path / "source-home"
    source_home.mkdir(parents=True)
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
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
    (source_home / ".gitconfig").write_text("[user]\nname = Alice\n", encoding="utf-8")
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", lambda cmd, **_kwargs: calls.append(cmd) or Result())
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: target_home)

    service.set_agent_credential_bundles("alice", ["provider-auth", "git"])
    sync = service.sync_agent_credentials("alice", source_home=source_home)
    assert "provider-auth" in sync["bundles"]
    assert "git" in sync["bundles"]
    assert (shared_home / ".codex" / "auth.json").exists()
    assert (target_home / ".codex" / "auth.json").is_symlink()
    assert (target_home / ".codex" / "auth.json").resolve() == (shared_home / ".codex" / "auth.json").resolve()
    assert (target_home / ".gitconfig").exists()

    revoked = service.revoke_agent_credentials("alice", bundles=["git"])
    assert "git" in revoked["bundles"]
    assert not (target_home / ".gitconfig").exists()
    assert (target_home / ".codex" / "auth.json").exists()

    updated = service.get_agent("alice")
    assert "provider-auth" in updated["credential_sync"]["bundles"]
    assert "git" not in updated["credential_sync"]["bundles"]
    assert updated["credential_sync"]["shared_provider_auth"] is True


def test_import_shared_auth_from_codex_links_agents_and_exposes_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    shared_home = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    source_home = tmp_path / "source-home"
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
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
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("subprocess.run", lambda cmd, **_kwargs: calls.append(cmd) or Result())
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: target_home)

    result = service.import_shared_auth("picoclaw", source="codex", source_home=source_home)
    assert result["source"] == "codex"
    assert (shared_home / ".codex" / "auth.json").exists()
    native_path = shared_home / ".picoclaw" / "auth.json"
    assert native_path.exists()
    profile_path = shared_home / ".picoclaw" / "auth-profiles.json"
    assert profile_path.exists()
    assert (shared_home / ".zeroclaw" / "auth-profiles.json").exists()
    assert (shared_home / ".openclaw" / "auth-profiles.json").exists()
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    assert payload["active_profiles"]["openai-codex"] == "openai-codex:default"
    native_payload = json.loads(native_path.read_text(encoding="utf-8"))
    assert native_payload["credentials"]["openai"]["access_token"] == "tok"
    assert (target_home / ".picoclaw" / "auth.json").is_symlink()
    assert (target_home / ".picoclaw" / "auth-profiles.json").is_symlink()
    assert (target_home / ".zeroclaw" / "auth-profiles.json").is_symlink()
    assert (target_home / ".codex" / "auth.json").is_symlink()
    assert ["chown", "alice:alice", str(target_home / ".picoclaw")] in calls

    status = service.agent_auth_status("alice")
    assert status["auth_status"] == "ready"
    assert status["shared_provider_auth"] is True
    assert status["source"] == "file:auth.json"


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
                        "expires_at": "2026-03-18T08:44:03Z",
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
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", shared_home)

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    agent["agent"]["linux_user"] = "teleclaw"
    target_home = tmp_path / "teleclaw-home"
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
                "settings": {"bot_token": "telegram-token"},
            }
        },
    )

    assert (shared_home / ".picoclaw" / "auth.json").exists()
    assert (target_home / ".picoclaw" / "auth.json").is_symlink()
    config = json.loads((target_home / ".picoclaw" / "config.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["token"] == "telegram-token"


def test_sync_agent_channels_from_provider_replaces_stale_channels(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
        channels=[{"kind": "cli", "name": "teleclaw-local"}],
        agent_version="1.0.0",
        provider="picoclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
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

    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: target_home)
    monkeypatch.setattr(ZeroClawService, "_can_manage_linux_user", lambda self, _user: True)

    synced = service.sync_agent_channels_from_provider("teleclaw")
    channels = synced["channels"]
    assert [(row.get("kind"), row.get("name")) for row in channels] == [("telegram", "team")]
    assert channels[0]["channel_source"] == "live"
    assert channels[0]["discovered_provider"] == "zeroclaw"


def test_shared_auth_show_cli_lists_rows(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ZeroClawService,
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

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
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
    assert len(calls) == 2


def test_ensure_shared_toolchain_shell_init_writes_profiles(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    target_home = tmp_path / "target-home"
    target_home.mkdir(parents=True)
    (target_home / ".profile").write_text("# existing\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
    updated = service._ensure_shared_toolchain_shell_init(target_home=target_home, username="sam")

    assert str(target_home / ".profile") in updated
    assert str(target_home / ".bashrc") in updated
    profile_text = (target_home / ".profile").read_text(encoding="utf-8")
    bashrc_text = (target_home / ".bashrc").read_text(encoding="utf-8")
    assert "clawie-shared-toolchain" in profile_text
    assert 'export PNPM_HOME="$HOMEBREW_PREFIX/bin"' in profile_text
    assert "fnm env --use-on-cd --shell bash" in bashrc_text
    assert calls == [
        ["chown", "sam:sam", str(target_home / ".profile")],
        ["chown", "sam:sam", str(target_home / ".bashrc")],
    ]


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

    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
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
    claude_cli = (
        brew_prefix
        / "bin"
        / "global"
        / "5"
        / ".pnpm"
        / "@anthropic-ai+claude-code@2.1.62"
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "cli.js"
    )
    claude_cli.parent.mkdir(parents=True)
    claude_cli.write_text("mode:384\nbt9(K,384)\n", encoding="utf-8")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(ZeroClawService, "HOMEBREW_PREFIX", brew_prefix)
    monkeypatch.setattr(ZeroClawService, "GLOBAL_PROFILE_DIR", profile_dir)
    monkeypatch.setattr(ZeroClawService, "GLOBAL_HOMEBREW_PROFILE_FILE", profile_dir / "00-homebrew.sh")
    monkeypatch.setattr(ZeroClawService, "GLOBAL_FNM_PROFILE_FILE", profile_dir / "zz-fnm.sh")
    monkeypatch.setattr(ZeroClawService, "GLOBAL_CLAUDE_PROFILE_FILE", profile_dir / "20-claude-shared.sh")
    monkeypatch.setattr(ZeroClawService, "SHARED_CLAUDE_DIR", shared_dir)

    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
    updated = service._ensure_system_shared_runtime(source_home)

    assert (profile_dir / "00-homebrew.sh").exists()
    assert (profile_dir / "zz-fnm.sh").exists()
    assert (profile_dir / "20-claude-shared.sh").exists()
    assert "CLAUDE_CONFIG_DIR" in (profile_dir / "20-claude-shared.sh").read_text(encoding="utf-8")
    assert "unset XDG_RUNTIME_DIR" in (profile_dir / "zz-fnm.sh").read_text(encoding="utf-8")

    shared_credentials = shared_dir / ".credentials.json"
    shared_state = shared_dir / ".claude.json"
    assert shared_credentials.exists()
    assert shared_state.exists()
    assert (shared_credentials.stat().st_mode & 0o777) == 0o666
    assert (shared_state.stat().st_mode & 0o777) == 0o666

    patched_cli = claude_cli.read_text(encoding="utf-8")
    assert "mode:438" in patched_cli
    assert "bt9(K,438)" in patched_cli

    assert str(shared_credentials) in updated
    assert str(profile_dir / "20-claude-shared.sh") in updated


def test_ensure_shared_claude_links_points_home_to_shared_store(
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

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ZeroClawService, "SHARED_CLAUDE_DIR", shared_dir)

    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
    updated = service._ensure_shared_claude_links(target_home=target_home, username="sam")

    assert str(target_home / ".claude") in updated
    assert str(target_home / ".claude.json") in updated
    assert (target_home / ".claude").is_symlink()
    assert (target_home / ".claude.json").is_symlink()
    assert (target_home / ".claude").resolve() == shared_dir.resolve()
    assert (target_home / ".claude.json").resolve() == (shared_dir / ".claude.json").resolve()
    assert ["chown", "-h", "sam:sam", str(target_home / ".claude")] in calls
    assert ["chown", "-h", "sam:sam", str(target_home / ".claude.json")] in calls


def test_service_toggles_channel_plugin_and_autostart(tmp_path: Path) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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


def test_service_action_runs_provider_service_command(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    result = service.agent_service_action("alice", "status")
    assert result["service_status"] == "running"
    assert any(cmd[:6] == ["sudo", "-u", "testuser", "-H", "--", "/usr/bin/openclaw"] for cmd in calls)


def test_dashboard_status_prefers_service_status(
    tmp_path: Path,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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


def test_dashboard_refresh_updates_local_service_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    assert any(cmd[:4] == ["systemctl", "--machine", "alice@", "--user"] for cmd in calls)


def test_local_agent_view_refreshes_service_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
        ZeroClawService,
        "_inspect_provider_auth_state",
        lambda self, **kwargs: dict(next(states)),
    )
    monkeypatch.setattr(
        ZeroClawService,
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


def test_set_agent_provider_updates_runtime_and_auth_mode(tmp_path: Path) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
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

    updated = service.set_agent_provider("teleclaw", "picoclaw")
    info = updated["agent"]
    assert info["provider"] == "picoclaw"
    assert info["runtime"] == "picoclaw-agent"
    assert info["auth_mode"] == "linked"
    assert info["service_status"] == "unknown"
    assert info["service_mode"] == "unknown"


def test_get_dashboard_agent_reconciles_provider_to_live_runtime_and_sets_remediation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    monkeypatch.setattr(
        service,
        "agent_auth_status",
        lambda _agent_id: {
            "auth_mode": "linked",
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
        stdout = "teleclaw 4321 /home/linuxbrew/.linuxbrew/bin/zeroclaw daemon\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **_: Result() if cmd[:2] == ["ps", "-eo"] else Result())
    payload = service.get_dashboard_agent("teleclaw")
    info = payload["agent"]

    assert info["provider"] == "zeroclaw"
    assert info["live_provider"] == "zeroclaw"
    assert info["provider_status"] == "warning"
    assert "aligned state away from picoclaw" in info["provider_issue"]
    assert "sudo clawie agent provider set teleclaw picoclaw" in info["provider_remediation"]
    assert info["service_status"] == "running"
    assert info["live_pid"] == 4321
    assert service.get_agent("teleclaw")["agent"]["provider"] == "zeroclaw"


def test_switch_agent_provider_cuts_over_runtime_and_reconnects_channels(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
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
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".zeroclaw").mkdir(parents=True)
    (home / ".zeroclaw" / "config.toml").write_text(
        """
[channels_config.telegram]
enabled = true
bot_token = "telegram-token"
name = "teleclaw-team"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    calls: list[list[str]] = []
    runtime_state = {"zeroclaw": True, "picoclaw": False}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd[:2] == ["ps", "-eo"]:
            lines: list[str] = []
            if runtime_state["zeroclaw"]:
                lines.append("teleclaw 4321 /usr/bin/zeroclaw daemon")
            if runtime_state["picoclaw"]:
                lines.append("teleclaw 5432 /usr/bin/picoclaw gateway")
            return Result(stdout="\n".join(lines) + ("\n" if lines else ""))
        tail3 = cmd[-3:]
        tail2 = cmd[-2:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)" if runtime_state["zeroclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            runtime_state["zeroclaw"] = False
            return Result(stdout="stopped")
        if "picoclaw" in script and "gateway" in script:
            if "nohup" in script:
                runtime_state["picoclaw"] = True
                return Result(stdout="started pid=123")
            runtime_state["picoclaw"] = True
            return Result(stdout="active (running)" if runtime_state["picoclaw"] else "inactive")
        if tail2 == ["/usr/bin/picoclaw", "status"]:
            return Result(stdout="ok")
        if cmd[:2] == ["chown", "teleclaw:teleclaw"]:
            return Result(stdout="")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.switch_agent_provider("teleclaw", "picoclaw")
    updated = service.get_agent("teleclaw")
    info = updated["agent"]

    assert result["service"]["service_status"] == "running"
    assert result["reconnected_channels"] == [{"kind": "telegram", "name": "teleclaw-team"}]
    assert info["provider"] == "picoclaw"
    assert info["runtime"] == "picoclaw-agent"
    assert info["service_status"] == "running"
    assert info["service_mode"] == "systemd"
    assert ["chown", "teleclaw:teleclaw", str(home / ".picoclaw")] in calls
    assert any(cmd[-3:] == ["/usr/bin/zeroclaw", "service", "stop"] for cmd in calls)
    assert any(cmd[-2:-1] == ["-lc"] and "picoclaw" in str(cmd[-1]) and "gateway" in str(cmd[-1]) for cmd in calls)
    config = json.loads((home / ".picoclaw" / "config.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"]["token"] == "telegram-token"


def test_switch_agent_provider_reconciles_same_provider_runtime(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
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
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    home = tmp_path / "teleclaw-home"
    (home / ".zeroclaw").mkdir(parents=True)
    (home / ".zeroclaw" / "config.toml").write_text(
        """
[channels_config.telegram]
enabled = true
bot_token = "telegram-token"
name = "teleclaw-team"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    agent["channels"] = [{"kind": "telegram", "name": "teleclaw-team", "enabled": True}]
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)

    calls: list[list[str]] = []
    runtime_state = {"zeroclaw": True, "picoclaw": False, "openclaw": False}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        if cmd[:2] == ["ps", "-eo"]:
            lines: list[str] = []
            if runtime_state["zeroclaw"]:
                lines.append("teleclaw 4321 /usr/bin/zeroclaw daemon")
            if runtime_state["picoclaw"]:
                lines.append("teleclaw 5432 /usr/bin/picoclaw gateway")
            if runtime_state["openclaw"]:
                lines.append("teleclaw 6543 /usr/bin/openclaw gateway run")
            return Result(stdout="\n".join(lines) + ("\n" if lines else ""))
        tail3 = cmd[-3:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if "picoclaw" in script and "gateway" in script:
            if "nohup" in script:
                runtime_state["picoclaw"] = True
                return Result(stdout="started pid=123")
            runtime_state["picoclaw"] = True
            return Result(stdout="active (running)" if runtime_state["picoclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)" if runtime_state["zeroclaw"] else "inactive")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            runtime_state["zeroclaw"] = False
            return Result(stdout="stopped")
        if tail3 == ["/usr/bin/openclaw", "daemon", "status"]:
            return Result(stdout="active (running)" if runtime_state["openclaw"] else "inactive")
        if cmd[:2] == ["chown", "teleclaw:teleclaw"]:
            return Result(stdout="")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.switch_agent_provider("teleclaw", "picoclaw")
    assert result["changed"] is True
    assert result["from_provider"] == "zeroclaw"
    assert result["service"]["service_status"] == "running"
    assert result["stopped_service"]["provider"] == "zeroclaw"
    assert any(cmd[-2:-1] == ["-lc"] and "picoclaw" in str(cmd[-1]) and "gateway" in str(cmd[-1]) for cmd in calls)
    assert any(cmd[-3:] == ["/usr/bin/zeroclaw", "service", "stop"] for cmd in calls)


def test_switch_agent_provider_fails_when_live_runtime_does_not_cut_over(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
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
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: tmp_path / "teleclaw-home")

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd: list[str], **_: object) -> object:
        if cmd[:2] == ["ps", "-eo"]:
            return Result(stdout="teleclaw 4321 /usr/bin/zeroclaw daemon\n")
        tail3 = cmd[-3:]
        script = str(cmd[-1]) if cmd and cmd[-2:-1] == ["-lc"] else ""
        if tail3 == ["/usr/bin/zeroclaw", "service", "status"]:
            return Result(stdout="active (running)")
        if tail3 == ["/usr/bin/zeroclaw", "service", "stop"]:
            return Result(stdout="stopped")
        if "picoclaw" in script and "gateway" in script:
            if "nohup" in script:
                return Result(stdout="started pid=123")
            if "pgrep" in script:
                return Result(stdout="inactive")
            return Result(stdout="inactive")
        return Result(stdout="ok")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda provider: f"/usr/bin/{provider}")
    monkeypatch.setattr("subprocess.run", fake_run)

    with raises(Exception) as exc:
        service.switch_agent_provider("teleclaw", "picoclaw")

    assert (
        "did not produce a live picoclaw runtime" in str(exc.value)
        or "no live picoclaw runtime was detected" in str(exc.value)
        or "zeroclaw service stop reported success but zeroclaw is still running" in str(exc.value)
    )
    info = service.get_agent("teleclaw")["agent"]
    assert info["provider"] == "zeroclaw"
    assert info["provider_status"] == "error"
    assert "provider switch to picoclaw failed" in info["provider_issue"]


def test_set_agent_provider_requires_root_for_managed_user_switch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="zeroclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.zeroclaw.example/v1",
    )
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
        provider="zeroclaw",
    )
    agent["agent"]["linux_user"] = "teleclaw"
    state = service.store.read_state()
    state["agents"]["teleclaw"] = agent
    service.store.write_state(state)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    try:
        service.set_agent_provider("teleclaw", "picoclaw")
        assert False, "expected SetupError"
    except Exception as exc:  # noqa: BLE001
        assert "provider switching requires root" in str(exc)

    info = service.get_agent("teleclaw")["agent"]
    assert info["provider"] == "zeroclaw"
    assert info["provider_status"] == "error"
    assert "provider switch to picoclaw failed" in info["provider_issue"]
    assert "sudo clawie agent provider set teleclaw picoclaw" in info["provider_remediation"]


def test_agent_auth_status_reports_permission_barrier_for_managed_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    payload = service.agent_auth_status("teleclaw")
    assert payload["auth_status"] == "unknown"
    assert payload["source"] == "permission"
    assert payload["can_login"] is False
    assert "requires root" in payload["detail"]


def test_dashboard_settings_navigation_not_capped_to_first_three_items() -> None:
    class FakeService:
        def get_dashboard_agent(self, _: str) -> dict[str, object]:
            return {
                "channels": [],
                "agent": {
                    "plugins": {},
                    "provider": "zeroclaw",
                    "status": "running",
                    "version": "local",
                    "autostart": False,
                    "service_status": "running",
                    "service_mode": "systemd",
                    "heartbeat_seconds": 30,
                    "auth_mode": "linked",
                    "local_user": True,
                },
            }

    state = DashboardState(view="detail", selected_agent_id="@local:zeroclaw", focus_idx=2, setting_idx=2)
    _handle_detail_key(ord("j"), state, FakeService())
    _handle_detail_key(ord("j"), state, FakeService())
    _handle_detail_key(ord("j"), state, FakeService())
    assert state.setting_idx > 2


def test_dashboard_detail_right_arrow_twice_reaches_settings_panel() -> None:
    class FakeService:
        def get_dashboard_agent(self, _: str) -> dict[str, object]:
            return {
                "channels": [{"kind": "telegram", "name": "team", "enabled": True}],
                "core_prompts": {"SOUL.md": "hello"},
                "agent": {
                    "plugins": {"memory": True},
                    "provider": "picoclaw",
                    "status": "running",
                    "version": "1.0.0",
                    "autostart": True,
                    "service_status": "running",
                    "service_mode": "systemd",
                    "heartbeat_seconds": 30,
                    "auth_mode": "linked",
                    "auth_status": "ready",
                    "local_user": False,
                },
                "credential_sync": {"bundles": []},
            }

    state = DashboardState(view="detail", selected_agent_id="alice", focus_idx=0)
    _handle_detail_key(curses.KEY_RIGHT, state, FakeService())
    _handle_detail_key(curses.KEY_RIGHT, state, FakeService())
    assert dashboard._focus_name(state) == "settings"


def test_dashboard_detail_navigation_uses_cached_agent_payload() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.calls = 0

        def get_dashboard_agent(self, _: str) -> dict[str, object]:
            self.calls += 1
            return {
                "channels": [{"kind": "telegram", "name": "team", "enabled": True}],
                "core_prompts": {"SOUL.md": "hello"},
                "agent": {
                    "plugins": {"memory": True},
                    "provider": "picoclaw",
                    "status": "running",
                    "version": "1.0.0",
                    "autostart": True,
                    "service_status": "running",
                    "service_mode": "systemd",
                    "heartbeat_seconds": 30,
                    "auth_mode": "linked",
                    "auth_status": "ready",
                    "local_user": False,
                },
                "credential_sync": {"bundles": []},
            }

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice", focus_idx=2)
    _handle_detail_key(ord("j"), state, service)
    _handle_detail_key(ord("k"), state, service)
    assert service.calls == 1


def test_dashboard_channels_navigation_uses_cached_inventory() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.inventory_calls = 0

        def channel_inventory(self) -> dict[str, object]:
            self.inventory_calls += 1
            return {
                "rows": [
                    {"source": "pool", "kind": "telegram", "name": "team"},
                    {"source": "agent", "owner_agent_id": "alice", "kind": "slack", "name": "ops", "enabled": True},
                ]
            }

    service = FakeService()
    state = DashboardState(view="overview", overview_mode="channels")
    snapshot = {"rows": [{"agent_id": "alice"}, {"agent_id": "@local:picoclaw"}]}
    dashboard._handle_overview_key(ord("j"), state, snapshot, service)
    dashboard._handle_overview_key(ord("k"), state, snapshot, service)
    assert service.inventory_calls == 1


def test_dashboard_settings_include_credential_rows_for_managed_agent() -> None:
    rows = _settings_items(
        {
            "channels": [{"kind": "telegram", "name": "team", "enabled": True}],
            "core_prompts": {"SOUL.md": "hi"},
            "credential_sync": {"bundles": ["provider-auth"], "last_synced_at": ""},
            "agent": {
                "local_user": False,
                "autostart": True,
                "service_status": "running",
                "service_mode": "systemd",
                "heartbeat_seconds": 30,
                "auth_mode": "linked",
            },
        }
    )
    kinds = {str(row.get("kind", "")) for row in rows}
    assert "channel_status" in kinds
    assert "channel_sync" in kinds
    assert "channel_add" in kinds
    assert "channel_connect" in kinds
    assert "provider_current" in kinds
    assert "auth_status" in kinds
    assert "auth_login" in kinds
    assert "prompt_sync_from_disk" in kinds
    assert "prompt_write_to_disk" in kinds
    assert "cred_bundle:provider-auth" in kinds
    assert "cred_bundle:git" in kinds
    assert "cred_sync_now" in kinds
    assert "cred_revoke_now" in kinds


def test_dashboard_settings_include_provider_switch_rows_when_choices_available() -> None:
    rows = _settings_items(
        {
            "credential_sync": {"bundles": [], "last_synced_at": ""},
            "agent": {
                "local_user": False,
                "provider": "picoclaw",
                "auth_mode": "linked",
                "auth_status": "ready",
                "service_status": "running",
                "service_mode": "systemd",
                "heartbeat_seconds": 30,
                "autostart": True,
            },
        },
        provider_choices=["picoclaw", "zeroclaw", "openclaw"],
    )
    kinds = [str(row.get("kind", "")) for row in rows]
    assert "provider_switch:zeroclaw" in kinds
    assert "provider_switch:openclaw" in kinds
    assert "provider_switch:picoclaw" not in kinds


def test_dashboard_settings_provider_rows_show_issue_and_fix() -> None:
    rows = _settings_items(
        {
            "agent": {
                "provider": "zeroclaw",
                "provider_status": "warning",
                "provider_issue": "live runtime was zeroclaw; Clawie aligned state away from picoclaw",
                "provider_remediation": "Run 'sudo clawie agent provider set teleclaw picoclaw' if you still want to switch.",
                "auth_status": "ready",
                "auth_mode": "linked",
            }
        },
        provider_choices=["picoclaw", "zeroclaw"],
    )

    current = next(row["label"] for row in rows if row["kind"] == "provider_current")
    issue = next(row["label"] for row in rows if row["kind"] == "provider_issue")
    fix = next(row["label"] for row in rows if row["kind"] == "provider_fix")
    assert current == "provider: zeroclaw"
    assert "aligned state away from picoclaw" in issue
    assert "sudo clawie agent provider set teleclaw picoclaw" in fix


def test_dashboard_setting_actions_call_credential_operations() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.toggled: list[str] = []
            self.synced = 0
            self.revoked = 0

        def toggle_agent_credential_bundle(self, _agent_id: str, bundle: str) -> None:
            self.toggled.append(bundle)

        def sync_agent_credentials(self, _agent_id: str) -> dict[str, object]:
            self.synced += 1
            return {"copied_paths": ["/tmp/a", "/tmp/b"]}

        def revoke_agent_credentials(self, _agent_id: str) -> dict[str, object]:
            self.revoked += 1
            return {"removed_paths": ["/tmp/a"]}

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice")
    _run_setting_action(service, state, {"kind": "cred_bundle:git"})
    assert state.notice == "credential bundle toggled: git"
    assert service.toggled == ["git"]

    _run_setting_action(service, state, {"kind": "cred_sync_now"})
    assert state.notice == "credentials synced (2 paths)"
    assert service.synced == 1

    _run_setting_action(service, state, {"kind": "cred_revoke_now"})
    assert state.notice == "credentials revoked (1 paths)"
    assert service.revoked == 1


def test_dashboard_setting_action_syncs_channels_from_provider() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.synced: list[str] = []

        def get_dashboard_agent(self, _agent_id: str) -> dict[str, object]:
            return {
                "channels": [{"kind": "telegram", "name": "team", "channel_source": "discovered"}],
                "agent": {
                    "plugins": {},
                    "provider": "picoclaw",
                    "service_status": "running",
                    "service_mode": "systemd",
                    "heartbeat_seconds": 30,
                    "auth_mode": "linked",
                    "local_user": False,
                },
                "credential_sync": {"bundles": []},
            }

        def sync_agent_channels_from_provider(self, agent_id: str) -> dict[str, object]:
            self.synced.append(agent_id)
            return {}

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice")
    assert _run_setting_action(service, state, {"kind": "channel_sync"}) is True
    assert state.notice == "synced channels from provider"
    assert service.synced == ["alice"]


def test_dashboard_setting_actions_call_auth_and_provider_operations() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.auth_calls: list[str] = []
            self.provider_calls: list[tuple[str, str]] = []

        def agent_auth_login(self, agent_id: str) -> dict[str, object]:
            self.auth_calls.append(agent_id)
            return {"action_performed": "refresh"}

        def switch_agent_provider(self, agent_id: str, provider: str) -> dict[str, object]:
            self.provider_calls.append((agent_id, provider))
            return {"changed": True}

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice")

    _run_setting_action(service, state, {"kind": "auth_login"})
    assert state.notice == "auth refreshed"
    assert service.auth_calls == ["alice"]

    _run_setting_action(service, state, {"kind": "provider_switch:zeroclaw"})
    assert state.notice == "provider changed to zeroclaw"
    assert service.provider_calls == [("alice", "zeroclaw")]


def test_dashboard_settings_panel_runs_channel_actions(monkeypatch: MonkeyPatch) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.channels = [{"kind": "telegram", "name": "team", "enabled": True}]
            self.assigned: list[tuple[str, str, str, str]] = []
            self.connected: list[tuple[str, str, str]] = []
            self.unlinked: list[tuple[str, str, str]] = []

        def get_dashboard_agent(self, _agent_id: str) -> dict[str, object]:
            return {
                "channels": list(self.channels),
                "core_prompts": {"SOUL.md": "hello"},
                "agent": {
                    "plugins": {},
                    "provider": "picoclaw",
                    "status": "running",
                    "service_status": "running",
                    "service_mode": "systemd",
                    "version": "1.0.0",
                    "auth_mode": "linked",
                    "auth_status": "ready",
                    "local_user": False,
                },
                "credential_sync": {"bundles": []},
            }

        def assign_channel_to_agent(self, source: str, kind: str, name: str, agent_id: str) -> None:
            self.assigned.append((source, kind, name, agent_id))
            self.channels.append({"kind": kind, "name": name, "enabled": True})

        def connect_agent_channel(self, agent_id: str, kind: str, name: str) -> None:
            self.connected.append((agent_id, kind, name))

        def unassign_channel_from_agent(self, agent_id: str, kind: str, name: str) -> None:
            self.unlinked.append((agent_id, kind, name))
            self.channels = [row for row in self.channels if not (row["kind"] == kind and row["name"] == name)]

    def setting_idx(service: FakeService, state: DashboardState, kind: str) -> int:
        rows = _settings_items(
            service.get_dashboard_agent(state.selected_agent_id),
            selected_channel=service.channels[state.channel_idx] if service.channels else None,
        )
        return next(idx for idx, row in enumerate(rows) if str(row.get("kind", "")) == kind)

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice", focus_idx=2, channel_idx=0)

    monkeypatch.setattr(dashboard, "_prompt_channel_values", lambda default_kind="", default_name="": ("slack", "ops"))

    state.setting_idx = setting_idx(service, state, "channel_add")
    _handle_detail_key(ord(" "), state, service)
    assert state.notice == "added slack:ops"
    assert service.assigned == [("", "slack", "ops", "alice")]

    state.setting_idx = setting_idx(service, state, "channel_connect")
    _handle_detail_key(ord(" "), state, service)
    assert state.notice == "linked telegram:team"
    assert service.connected == [("alice", "telegram", "team")]

    state.setting_idx = setting_idx(service, state, "channel_unlink")
    _handle_detail_key(ord(" "), state, service)
    assert state.notice == "unlinked telegram:team"
    assert service.unlinked == [("alice", "telegram", "team")]


def test_dashboard_settings_panel_runs_prompt_actions() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.synced = 0
            self.written = 0

        def get_dashboard_agent(self, _agent_id: str) -> dict[str, object]:
            return {
                "channels": [],
                "core_prompts": {"SOUL.md": "hello"},
                "agent": {
                    "plugins": {},
                    "provider": "picoclaw",
                    "status": "running",
                    "service_status": "running",
                    "service_mode": "systemd",
                    "version": "1.0.0",
                    "auth_mode": "linked",
                    "auth_status": "ready",
                    "local_user": False,
                },
                "credential_sync": {"bundles": []},
            }

        def sync_agent_core_prompts_from_disk(self, _agent_id: str) -> None:
            self.synced += 1

        def write_agent_core_prompts_to_disk(self, _agent_id: str) -> None:
            self.written += 1

    def setting_idx(service: FakeService, kind: str) -> int:
        rows = _settings_items(service.get_dashboard_agent("alice"))
        return next(idx for idx, row in enumerate(rows) if str(row.get("kind", "")) == kind)

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice", focus_idx=2)

    state.setting_idx = setting_idx(service, "prompt_sync_from_disk")
    _handle_detail_key(ord(" "), state, service)
    assert state.notice == "prompts synced from disk"
    assert service.synced == 1

    state.setting_idx = setting_idx(service, "prompt_write_to_disk")
    _handle_detail_key(ord(" "), state, service)
    assert state.notice == "prompts written to disk"
    assert service.written == 1


def test_dashboard_channel_shortcuts_add_link_and_unlink(monkeypatch: MonkeyPatch) -> None:
    class FakeService:
        def __init__(self) -> None:
            self.channels = [{"kind": "telegram", "name": "team", "enabled": True}]
            self.assigned: list[tuple[str, str, str, str]] = []
            self.connected: list[tuple[str, str, str]] = []
            self.unlinked: list[tuple[str, str, str]] = []

        def get_dashboard_agent(self, _agent_id: str) -> dict[str, object]:
            return {
                "channels": list(self.channels),
                "agent": {
                    "plugins": {},
                    "provider": "picoclaw",
                    "status": "running",
                    "service_status": "running",
                    "service_mode": "systemd",
                    "version": "1.0.0",
                    "auth_mode": "linked",
                    "auth_status": "ready",
                    "local_user": False,
                },
                "credential_sync": {"bundles": []},
            }

        def assign_channel_to_agent(self, source: str, kind: str, name: str, agent_id: str) -> None:
            self.assigned.append((source, kind, name, agent_id))
            self.channels.append({"kind": kind, "name": name, "enabled": True})

        def connect_agent_channel(self, agent_id: str, kind: str, name: str) -> None:
            self.connected.append((agent_id, kind, name))
            if not any(row["kind"] == kind and row["name"] == name for row in self.channels):
                self.channels.append({"kind": kind, "name": name, "enabled": True})

        def unassign_channel_from_agent(self, agent_id: str, kind: str, name: str) -> None:
            self.unlinked.append((agent_id, kind, name))
            self.channels = [row for row in self.channels if not (row["kind"] == kind and row["name"] == name)]

    service = FakeService()
    state = DashboardState(view="detail", selected_agent_id="alice", focus_idx=0, channel_idx=0)

    monkeypatch.setattr(dashboard, "_prompt_channel_values", lambda default_kind="", default_name="": ("slack", "ops"))
    _handle_detail_key(ord("n"), state, service)
    assert state.notice == "added slack:ops"
    assert service.assigned == [("", "slack", "ops", "alice")]

    _handle_detail_key(ord("N"), state, service)
    assert state.notice == "added + linked slack:ops"
    assert service.connected[-1] == ("alice", "slack", "ops")

    _handle_detail_key(ord("c"), state, service)
    assert state.notice == "linked telegram:team"
    assert service.connected[0] == ("alice", "slack", "ops")
    assert service.connected[1] == ("alice", "telegram", "team")

    _handle_detail_key(ord("u"), state, service)
    assert state.notice == "unlinked telegram:team"
    assert service.unlinked == [("alice", "telegram", "team")]


def test_service_action_requires_root_for_other_linux_user(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(store)
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

    assert agent["channels"] == []


def test_create_agent_ignores_stale_template_runtime_for_new_agents(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    service = ZeroClawService(store)
    service.setup(
        provider="picoclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.picoclaw.example/v1",
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

    assert agent["agent"]["runtime"] == "picoclaw-agent"


def test_service_action_falls_back_when_bus_unavailable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: tmp_path / "teleclaw-home")

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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
                        "token": "telegram-token",
                        "name": "teleclaw-team",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: home)
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
            if "nohup" in script:
                runtime_running = True
                return Result(0, stdout="4321\n")
            if "pgrep" in script:
                return Result(0, stdout="active (running)" if runtime_running else "inactive")
            return Result(0, stdout="4321\n")
        return Result(0)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr("shutil.which", lambda _: "/home/linuxbrew/.linuxbrew/bin/picoclaw")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = service.agent_service_action("teleclaw", "start")
    assert result["service_status"] == "running"
    assert any(".picoclaw/daemon.log" in cmd[-1] for cmd in commands if cmd[:7] == ["sudo", "-u", "teleclaw", "-H", "--", "bash", "-lc"])


def test_performance_snapshot_includes_local_user_claw_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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


def test_performance_snapshot_uses_live_runtime_as_provider_source_of_truth(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    snapshot = service.performance_snapshot(refresh=False)
    row = next(r for r in snapshot["rows"] if r["agent_id"] == "teleclaw")

    assert row["provider"] == "zeroclaw"
    assert row["provider_status"] == "warning"
    assert "aligned state away from picoclaw" in row["provider_issue"]
    assert "sudo clawie agent provider set teleclaw picoclaw" in row["provider_remediation"]
    assert row["status"] == "running"
    assert row["pid"] == 4321
    assert row["cpu_percent"] == 1.5
    assert row["mem_percent"] == 2.5
    assert row["rss_kb"] == 4096


def test_local_claw_service_action_updates_local_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    selected = ZeroClawService._preferred_local_linux_user(
        default_user="azicon",
        hint_user="teleclaw",
        cached_user="teleclaw",
    )
    assert selected == "azicon"


def test_parse_systemctl_status_ignores_bus_errors() -> None:
    assert ZeroClawService._parse_systemctl_status("", "Failed to connect to bus: No medium found") == "unknown"
    assert ZeroClawService._parse_systemctl_status("active\n", "") == "running"
    assert ZeroClawService._parse_systemctl_status("inactive\n", "") == "stopped"


def test_systemd_status_prefers_any_running_candidate_over_stopped(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))

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

    monkeypatch.setattr("clawie.service.Path", FakeHomePath)

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


def test_channel_inventory_includes_agent_and_local_channels(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/openclaw")
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
    service = ZeroClawService(StateStore(config_dir=tmp_path))
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
