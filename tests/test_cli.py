from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from clawie.cli import main
from clawie.dashboard import DashboardState, _handle_detail_key
from clawie.providers import credential_paths_for_providers
from clawie.service import ZeroClawService
from clawie.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


def test_setup_defaults_to_linked_auth_for_zeroclaw(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    code = run_cli(tmp_path, "setup")
    output = capsys.readouterr().out
    assert code == 0
    assert "provider: zeroclaw" in output
    assert "auth_mode: linked" in output


def test_setup_openclaw_without_api_key(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    code = run_cli(
        tmp_path,
        "setup",
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
    assert "spawn_password_default: not set" in output
    assert "runtime_installed: True" in output

    status = run_cli(tmp_path, "setup", "--status")
    status_output = capsys.readouterr().out
    assert status == 0
    assert "configured: True" in status_output


def test_setup_api_key_mode_requires_api_key(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    code = run_cli(tmp_path, "setup", "--provider", "picoclaw", "--auth-mode", "api_key")
    output = capsys.readouterr().out
    assert code == 1
    assert "API key is required when --auth-mode api_key is selected" in output


def test_create_agent_with_provider_override(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "setup", "--provider", "openclaw") == 0
    capsys.readouterr()
    code = run_cli(
        tmp_path,
        "agents",
        "create",
        "--agent-id",
        "pico",
        "--provider",
        "picoclaw",
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "provider 'picoclaw' is not configured" in output

    assert run_cli(tmp_path, "setup", "--provider", "picoclaw") == 0
    capsys.readouterr()
    code = run_cli(
        tmp_path,
        "agents",
        "create",
        "--agent-id",
        "pico",
        "--provider",
        "picoclaw",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "provider: picoclaw" in output


def test_create_agent_and_monitor_snapshot(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "setup", "--api-key", "zc_live_1234", "--workspace", "prod") == 0
    capsys.readouterr()
    assert (
        run_cli(
            tmp_path,
            "agents",
            "create",
            "--agent-id",
            "alice",
            "--template",
            "baseline",
            "--channel-strategy",
            "new",
        )
        == 0
    )
    capsys.readouterr()

    code = run_cli(tmp_path, "monitor")
    output = capsys.readouterr().out
    assert code == 0
    assert "Clawie Monitor" in output
    assert "alice" in output
    assert "cpu%" in output


def test_list_alias_matches_agents_list(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "setup", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agents", "create", "--agent-id", "alice") == 0
    capsys.readouterr()

    code = run_cli(tmp_path, "list")
    output = capsys.readouterr().out
    assert code == 0
    assert "agent_id" in output
    assert "status" in output
    assert "alice" in output


def test_spawn_requires_root(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "setup", "--api-key", "zc_live_1234") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "spawn", "--agent-id", "sam")
    output = capsys.readouterr().out
    assert code == 1
    assert "requires root privileges" in output


def test_spawn_success_with_mocks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "setup", "--api-key", "zc_live_1234") == 0
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

    code = run_cli(
        tmp_path,
        "spawn",
        "--agent-id",
        "sam",
        "--linux-user",
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
        "setup",
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
        "setup",
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

    code = run_cli(
        tmp_path,
        "spawn",
        "--agent-id",
        "sam",
        "--linux-user",
        "sam",
        "--skip-config-copy",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Spawned linux user sam" in output
    assert any(cmd[:2] == ["usermod", "-p"] for cmd in calls)


def test_spawn_uses_per_agent_plaintext_password(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "setup", "--provider", "openclaw") == 0
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

    code = run_cli(
        tmp_path,
        "spawn",
        "--agent-id",
        "sam2",
        "--linux-user",
        "sam2",
        "--skip-config-copy",
        "--password",
        "LocalPass123!",
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Spawned linux user sam2" in output
    assert any(cmd == ["chpasswd"] and input_data == "sam2:LocalPass123!\n" for cmd, input_data in calls)


def test_purge_removes_agent_and_linux_user_with_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "setup", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agents", "create", "--agent-id", "teleclaw") == 0
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

    code = run_cli(tmp_path, "purge", "--agent-id", "teleclaw", "--yes")
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
    assert run_cli(tmp_path, "setup", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agents", "create", "--agent-id", "teleclaw") == 0
    capsys.readouterr()

    store = StateStore(config_dir=tmp_path)
    state = store.read_state()
    state["agents"]["teleclaw"]["agent"]["linux_user"] = "teleclaw"
    store.write_state(state)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    code = run_cli(tmp_path, "purge", "--agent-id", "teleclaw", "--yes")
    output = capsys.readouterr().out
    assert code == 1
    assert "purge requires root privileges" in output


def test_purge_accepts_positional_agent_id(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "setup", "--provider", "openclaw") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "agents", "create", "--agent-id", "teleclaw") == 0
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    code = run_cli(tmp_path, "purge", "teleclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "Purged agent teleclaw" in output


def test_spawn_imports_channels_from_zeroclaw_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    assert run_cli(tmp_path, "setup", "--provider", "zeroclaw") == 0
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

    code = run_cli(
        tmp_path,
        "spawn",
        "--agent-id",
        "teleclaw",
        "--linux-user",
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


def test_store_creates_sqlite_db(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    store.ensure()
    assert store.db_path.exists()


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
    assert run_cli(tmp_path, "setup", "--api-key", "zc_live_1234") == 0
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

    code = run_cli(tmp_path, "agents", "batch-create", "--file", str(batch_file))
    output = capsys.readouterr().out
    assert code == 1
    assert "created: 1" in output
    assert "errors: 1" in output


def test_provider_credential_path_registry_contains_codex_and_openai() -> None:
    paths = credential_paths_for_providers(["zeroclaw", "openclaw"])
    assert ".codex" in paths
    assert ".config/openai" in paths


def test_detect_installed_claws_command(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    source_home = tmp_path / "home"
    source_home.mkdir(parents=True)
    zeroclaw = source_home / ".zeroclaw"
    zeroclaw.mkdir(parents=True)
    (zeroclaw / "config.toml").write_text("default_provider='openai-codex'\n", encoding="utf-8")
    (zeroclaw / "auth-profiles.json").write_text("{}", encoding="utf-8")

    code = run_cli(tmp_path, "claws", "detect", "--source-home", str(source_home))
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
        channels=None,
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
    assert any(len(cmd) >= 2 and cmd[1] == "onboard" for cmd in commands)
    assert not any(len(cmd) >= 2 and cmd[1] == "channel" for cmd in commands)
