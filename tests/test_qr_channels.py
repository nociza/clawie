from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clawie.cli import main
from clawie.service import ClawieService, SetupError
from clawie.store import StateStore


def _managed_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ClawieService, Path]:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(provider="openclaw", subscription="pro", workspace="production")
    service.create_agent(
        agent_id="whatsclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    state = service.store.read_state()
    info = state["agents"]["whatsclaw"]["agent"]
    info.update({"linux_user": "whatsclaw", "gateway_port": 18791, "gateway_token": "secret"})
    service.store.write_state(state)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(service, "_can_manage_linux_user", lambda _user: True)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)
    return service, home


def test_qr_setup_refuses_noninteractive_terminal_before_install_or_state_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _home = _managed_agent(tmp_path, monkeypatch)
    state_before = json.dumps(service.store.read_state(), sort_keys=True)
    monkeypatch.setattr("clawie._service_channels.os.isatty", lambda _fd: False)
    monkeypatch.setattr(
        service,
        "_ensure_openclaw_qr_plugin",
        lambda *_args, **_kwargs: pytest.fail("plugin install must not run without a TTY"),
    )

    with pytest.raises(SetupError, match="interactive terminal"):
        service.setup_openclaw_qr_channel("whatsclaw", "whatsapp")

    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_qr_setup_commits_channel_only_after_live_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _home = _managed_agent(tmp_path, monkeypatch)
    monkeypatch.setattr("clawie._service_channels.os.isatty", lambda _fd: True)
    monkeypatch.setattr(
        service,
        "_openclaw_qr_plugin_status",
        lambda *_args, **_kwargs: {
            "installed": False,
            "enabled": False,
            "version": "",
        },
    )
    monkeypatch.setattr(
        service,
        "_ensure_openclaw_qr_plugin",
        lambda *_args, **_kwargs: {
            "installed": True,
            "enabled": True,
            "version": "2026.7.1",
            "installed_now": True,
        },
    )
    calls: list[list[str]] = []

    def fake_command(
        _agent: str,
        _channel: str,
        arguments: list[str],
        **_kwargs: Any,
    ) -> None:
        calls.append(arguments)
        return None

    monkeypatch.setattr(service, "_run_openclaw_qr_command", fake_command)
    monkeypatch.setattr(service, "_provider_process_live", lambda *_args: True)
    monkeypatch.setattr(
        service,
        "_run_managed_provider_service_action",
        lambda **_kwargs: {"service_status": "running", "service_mode": "systemd"},
    )
    monkeypatch.setattr(
        service,
        "openclaw_qr_channel_status",
        lambda *_args: {
            "healthy": True,
            "configured": True,
            "running": True,
            "connected": True,
            "probe_ok": True,
            "account_count": 1,
        },
    )

    result = service.setup_openclaw_qr_channel(
        "whatsclaw",
        "whatsapp",
        wait_seconds=0,
    )

    assert calls == [["channels", "login", "--channel", "whatsapp"]]
    assert result["status"]["healthy"] is True
    channels = service.store.read_state()["agents"]["whatsclaw"]["channels"]
    assert channels == [
        {
            "enabled": True,
            "external_id": "whatsclaw:whatsapp:default",
            "kind": "whatsapp",
            "name": "whatsapp",
        }
    ]


def test_qr_setup_restores_running_gateway_after_login_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _home = _managed_agent(tmp_path, monkeypatch)
    monkeypatch.setattr("clawie._service_channels.os.isatty", lambda _fd: True)
    monkeypatch.setattr(
        service,
        "_openclaw_qr_plugin_status",
        lambda *_args, **_kwargs: {
            "installed": False,
            "enabled": False,
            "version": "",
        },
    )
    monkeypatch.setattr(
        service,
        "_ensure_openclaw_qr_plugin",
        lambda *_args, **_kwargs: {
            "installed": True,
            "enabled": True,
            "version": "2026.7.1",
            "installed_now": True,
        },
    )
    monkeypatch.setattr(service, "_provider_process_live", lambda *_args: True)

    def failed_login(
        _agent: str,
        _channel: str,
        _arguments: list[str],
        **_kwargs: Any,
    ) -> None:
        raise SetupError("simulated login failure")

    monkeypatch.setattr(service, "_run_openclaw_qr_command", failed_login)
    service_actions: list[str] = []

    def service_action(**kwargs: Any) -> dict[str, str]:
        service_actions.append(str(kwargs["action"]))
        return {"service_status": "running", "service_mode": "systemd"}

    monkeypatch.setattr(service, "_run_managed_provider_service_action", service_action)

    with pytest.raises(SetupError, match="simulated login failure"):
        service.setup_openclaw_qr_channel("whatsclaw", "whatsapp")

    assert service_actions == ["restart"]
    assert service.store.read_state()["agents"]["whatsclaw"]["channels"] == []


def test_failed_qr_plugin_install_attempts_scoped_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _home = _managed_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service,
        "_openclaw_qr_plugin_status",
        lambda *_args: {"installed": False, "enabled": False, "status": "", "version": ""},
    )
    calls: list[list[str]] = []

    def fake_command(
        _agent: str,
        _channel: str,
        arguments: list[str],
        **_kwargs: Any,
    ) -> None:
        calls.append(arguments)
        if arguments[:2] == ["plugins", "install"]:
            raise SetupError("simulated install failure")
        return None

    monkeypatch.setattr(service, "_run_openclaw_qr_command", fake_command)

    with pytest.raises(SetupError, match="simulated install failure"):
        service._ensure_openclaw_qr_plugin("whatsclaw", "whatsapp", install=True)

    assert calls == [
        ["plugins", "install", "--pin", "@openclaw/whatsapp"],
        ["plugins", "uninstall", "--force", "whatsapp"],
    ]


def test_qr_status_contract_is_secret_free_and_requires_full_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(
        service,
        "_openclaw_qr_plugin_status",
        lambda *_args: {
            "installed": True,
            "enabled": True,
            "status": "loaded",
            "version": "2.4.6",
        },
    )
    payload = {
        "channels": {
            "openclaw-weixin": {
                "configured": True,
                "running": True,
                "lastError": "historical reconnect failure",
            }
        },
        "channelAccounts": {
            "openclaw-weixin": [
                {
                    "accountId": "private-account-id",
                    "configured": True,
                    "running": True,
                }
            ]
        },
    }
    monkeypatch.setattr(service, "_run_openclaw_qr_command", lambda *_args, **_kwargs: payload)

    result = service.openclaw_qr_channel_status("bidao", "wechat")

    assert result["healthy"] is True
    assert result["last_error"] == "historical reconnect failure"
    assert result["connected_inferred"] is True
    assert result["probe_inferred"] is True
    assert result["channel"] == "openclaw-weixin"
    assert result["account_count"] == 1
    assert "private-account-id" not in json.dumps(result)


def test_qr_status_honors_explicit_disconnected_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    monkeypatch.setattr(
        service,
        "_openclaw_qr_plugin_status",
        lambda *_args: {
            "installed": True,
            "enabled": True,
            "status": "loaded",
            "version": "2026.7.1",
        },
    )
    monkeypatch.setattr(
        service,
        "_run_openclaw_qr_command",
        lambda *_args, **_kwargs: {
            "channels": {
                "whatsapp": {
                    "configured": True,
                    "running": True,
                    "lastError": "not linked",
                }
            },
            "channelAccounts": {
                "whatsapp": [
                    {
                        "configured": True,
                        "running": True,
                        "connected": False,
                    }
                ]
            },
        },
    )

    result = service.openclaw_qr_channel_status("whatsclaw", "whatsapp")

    assert result["healthy"] is False
    assert result["connected"] is False
    assert result["connected_inferred"] is False
    assert result["login_required"] is True
    assert "channel whatsapp setup whatsclaw" in result["remediation"]


def test_qr_setup_recovers_healthy_saved_login_without_tty_or_relogin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _home = _managed_agent(tmp_path, monkeypatch)
    monkeypatch.setattr("clawie._service_channels.os.isatty", lambda _fd: False)
    plugin = {
        "installed": True,
        "enabled": True,
        "status": "loaded",
        "version": "2.4.6",
    }
    status = {
        "healthy": True,
        "configured": True,
        "running": True,
        "connected": True,
        "probe_ok": True,
        "account_count": 1,
    }
    monkeypatch.setattr(service, "_openclaw_qr_plugin_status", lambda *_args: plugin)
    monkeypatch.setattr(service, "openclaw_qr_channel_status", lambda *_args: status)
    monkeypatch.setattr(
        service,
        "_ensure_openclaw_qr_plugin",
        lambda *_args, **_kwargs: pytest.fail("healthy recovery must not reinstall the plugin"),
    )
    monkeypatch.setattr(
        service,
        "_run_openclaw_qr_command",
        lambda *_args, **_kwargs: pytest.fail("healthy recovery must not rerun QR login"),
    )

    result = service.setup_openclaw_qr_channel("whatsclaw", "whatsapp")

    assert result["resumed_existing_login"] is True
    assert service.store.read_state()["agents"]["whatsclaw"]["channels"] == [
        {
            "enabled": True,
            "external_id": "whatsclaw:whatsapp:default",
            "kind": "whatsapp",
            "name": "whatsapp",
        }
    ]


def test_qr_cli_exposes_actionable_status_and_pairing_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "openclaw_qr_channel_status",
        lambda _service, agent_id, channel: {
            "agent_id": agent_id,
            "channel": "whatsapp",
            "label": "WhatsApp",
            "healthy": False,
            "installed": True,
            "enabled": True,
            "plugin_version": "2026.7.1",
            "configured": True,
            "running": True,
            "connected": False,
            "probe_ok": False,
            "account_count": 1,
            "last_error": "",
            "remediation": f"Run 'sudo clawie channel {channel} setup {agent_id}'",
        },
    )

    code = main(
        [
            "--config-dir",
            str(tmp_path),
            "--no-color",
            "channel",
            "whatsapp",
            "status",
            "whatsclaw",
        ]
    )
    output = capsys.readouterr()

    assert code == 1
    assert "healthy: no" in output.out
    assert "channel whatsapp setup whatsclaw" in output.err
