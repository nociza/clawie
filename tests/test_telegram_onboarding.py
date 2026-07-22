from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from clawie.cli import _read_private_telegram_token_file, main
from clawie.service import ClawieService, SetupError
from clawie.store import StateStore


BOT_TOKEN = "123456:" + "A" * 36
ALT_BOT_TOKEN = "654321:" + "B" * 36


def _service_with_managed_openclaw_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ClawieService, Path]:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="pro",
        workspace="production",
        api_url="https://api.openai.com/v1",
    )
    service.create_agent(
        agent_id="teleclaw",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=[],
        agent_version="1.0.0",
        provider="openclaw",
    )
    state = service.store.read_state()
    info = state["agents"]["teleclaw"]["agent"]
    info["linux_user"] = "teleclaw"
    info["gateway_port"] = 18789
    info["gateway_token"] = "gateway-secret"
    service.store.write_state(state)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(service, "_refresh_managed_agent_provider_alignment", lambda _agent: None)
    monkeypatch.setattr(service, "_can_manage_linux_user", lambda _user: True)
    monkeypatch.setattr(service, "_agent_linux_home", lambda _agent: home)
    return service, home


def test_configure_telegram_uses_private_token_file_and_proves_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "openclaw")
    monkeypatch.setattr(service, "_verify_installed_runtime_version", lambda *_args: "2026.7.1")
    monkeypatch.setattr(
        service,
        "_probe_telegram_bot_token",
        lambda _token: {"bot_id": "123", "bot_username": "teleclaw_bot"},
    )
    monkeypatch.setattr(service, "_provider_process_live", lambda *_args: True)
    monkeypatch.setattr(
        service,
        "_run_managed_provider_service_action",
        lambda **kwargs: {
            "action": kwargs["action"],
            "service_status": "running",
            "service_mode": "systemd",
        },
    )
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    monkeypatch.setattr(
        service,
        "openclaw_telegram_status",
        lambda _agent: {
            "agent_id": "teleclaw",
            "healthy": True,
            "configured": True,
            "running": True,
            "connected": True,
            "probe_ok": True,
            "bot_username": "teleclaw_bot",
        },
    )

    result = service.configure_openclaw_telegram("teleclaw", BOT_TOKEN, wait_seconds=0)

    token_path = home / ".openclaw" / "telegram.token"
    assert token_path.read_text(encoding="utf-8").strip() == BOT_TOKEN
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    config = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    assert config["channels"]["telegram"] == {
        "dmPolicy": "pairing",
        "enabled": True,
        "streaming": {"mode": "off"},
        "tokenFile": str(token_path),
    }
    persisted = service.store.read_state()
    assert BOT_TOKEN not in json.dumps(persisted)
    assert persisted["agents"]["teleclaw"]["agent"]["gateway_port"] == 18789
    assert persisted["agents"]["teleclaw"]["channels"] == [
        {
            "enabled": True,
            "external_id": "teleclaw:telegram:1",
            "kind": "telegram",
            "name": "teleclaw-telegram",
        }
    ]
    assert result["service_action"] == "restart"
    assert result["channel_name"] == "teleclaw-telegram"
    assert result["status"]["healthy"] is True


def _mock_telegram_setup_preflight(
    service: ClawieService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "openclaw")
    monkeypatch.setattr(service, "_verify_installed_runtime_version", lambda *_args: "2026.7.1")
    monkeypatch.setattr(
        service,
        "_probe_telegram_bot_token",
        lambda _token: {"bot_id": "654", "bot_username": "replacement_bot"},
    )
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)


def _write_existing_telegram_setup(home: Path) -> tuple[Path, Path, str]:
    root = home / ".openclaw"
    root.mkdir(exist_ok=True)
    token_path = root / "telegram.token"
    token_path.write_text(BOT_TOKEN + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    config_path = root / "openclaw.json"
    config_content = json.dumps(
        {
            "gateway": {"mode": "local"},
            "channels": {
                "telegram": {
                    "enabled": True,
                    "tokenFile": str(token_path),
                    "dmPolicy": "pairing",
                }
            },
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    config_path.write_text(config_content, encoding="utf-8")
    config_path.chmod(0o600)
    return token_path, config_path, config_content


def test_existing_bot_requires_explicit_replace_before_probe_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    token_path, config_path, config_before = _write_existing_telegram_setup(home)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "openclaw")
    monkeypatch.setattr(service, "_verify_installed_runtime_version", lambda *_args: "2026.7.1")
    probe_calls: list[str] = []
    monkeypatch.setattr(
        service,
        "_probe_telegram_bot_token",
        lambda token: probe_calls.append(token),
    )
    state_before = json.dumps(service.store.read_state(), sort_keys=True)

    with pytest.raises(Exception, match="--replace"):
        service.configure_openclaw_telegram("teleclaw", ALT_BOT_TOKEN, wait_seconds=0)

    assert probe_calls == []
    assert token_path.read_text(encoding="utf-8") == BOT_TOKEN + "\n"
    assert config_path.read_text(encoding="utf-8") == config_before
    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_concurrent_setup_fails_before_probe_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service,
        "_probe_telegram_bot_token",
        lambda _token: pytest.fail("a contending setup must not reach token preflight"),
    )
    state_before = json.dumps(service.store.read_state(), sort_keys=True)

    with service._openclaw_telegram_setup_lock(home, "teleclaw"):
        with pytest.raises(SetupError, match="already running"):
            service.configure_openclaw_telegram("teleclaw", BOT_TOKEN, wait_seconds=0)

    assert not (home / ".openclaw").exists()
    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_rejected_preflight_changes_nothing_and_never_touches_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "openclaw")
    monkeypatch.setattr(service, "_verify_installed_runtime_version", lambda *_args: "2026.7.1")
    monkeypatch.setattr(
        service,
        "_probe_telegram_bot_token",
        lambda _token: (_ for _ in ()).throw(Exception("rejected safely")),
    )
    monkeypatch.setattr(
        service,
        "_run_managed_provider_service_action",
        lambda **_kwargs: pytest.fail("service must not be touched before token preflight"),
    )
    state_before = json.dumps(service.store.read_state(), sort_keys=True)

    with pytest.raises(Exception, match="rejected safely"):
        service.configure_openclaw_telegram("teleclaw", BOT_TOKEN, wait_seconds=0)

    assert not (home / ".openclaw" / "telegram.token").exists()
    assert not (home / ".openclaw" / "openclaw.json").exists()
    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_token_probe_never_surfaces_secret_from_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        @staticmethod
        def request(_method: str, path: str, **_kwargs: Any) -> None:
            raise RuntimeError(f"request failed at {path}")

        @staticmethod
        def close() -> None:
            pass

    monkeypatch.setattr("clawie._service_channels.http.client.HTTPSConnection", FailingConnection)

    with pytest.raises(SetupError) as error:
        ClawieService._probe_telegram_bot_token(BOT_TOKEN)

    assert BOT_TOKEN not in str(error.value)
    assert error.value.__cause__ is None
    assert "no files, services, or agent state were changed" in str(error.value)


def test_failed_replacement_restores_exact_files_service_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    token_path, config_path, config_before = _write_existing_telegram_setup(home)
    _mock_telegram_setup_preflight(service, monkeypatch)
    monkeypatch.setattr(service, "_provider_process_live", lambda *_args: True)
    actions: list[str] = []

    def fake_service_action(**kwargs: Any) -> dict[str, Any]:
        actions.append(kwargs["action"])
        if len(actions) == 1:
            raise RuntimeError("simulated start failure")
        return {"action": kwargs["action"], "service_status": "running"}

    monkeypatch.setattr(service, "_run_managed_provider_service_action", fake_service_action)
    state_before = json.dumps(service.store.read_state(), sort_keys=True)

    with pytest.raises(Exception, match="previous files and service state were restored"):
        service.configure_openclaw_telegram(
            "teleclaw",
            ALT_BOT_TOKEN,
            wait_seconds=0,
            replace=True,
        )

    assert actions == ["restart", "restart"]
    assert token_path.read_text(encoding="utf-8") == BOT_TOKEN + "\n"
    assert config_path.read_text(encoding="utf-8") == config_before
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_failed_fresh_setup_removes_partial_files_and_restores_stopped_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    _mock_telegram_setup_preflight(service, monkeypatch)
    monkeypatch.setattr(service, "_provider_process_live", lambda *_args: False)
    actions: list[str] = []

    def fake_service_action(**kwargs: Any) -> dict[str, Any]:
        actions.append(kwargs["action"])
        if len(actions) == 1:
            raise RuntimeError("simulated start failure")
        return {"action": kwargs["action"], "service_status": "stopped"}

    monkeypatch.setattr(service, "_run_managed_provider_service_action", fake_service_action)
    state_before = json.dumps(service.store.read_state(), sort_keys=True)

    with pytest.raises(Exception, match="previous files and service state were restored"):
        service.configure_openclaw_telegram("teleclaw", BOT_TOKEN, wait_seconds=0)

    assert actions == ["start", "stop"]
    assert not (home / ".openclaw" / "telegram.token").exists()
    assert not (home / ".openclaw" / "openclaw.json").exists()
    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_state_commit_failure_rolls_back_live_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, home = _service_with_managed_openclaw_agent(tmp_path, monkeypatch)
    token_path, config_path, config_before = _write_existing_telegram_setup(home)
    _mock_telegram_setup_preflight(service, monkeypatch)
    monkeypatch.setattr(service, "_provider_process_live", lambda *_args: True)
    actions: list[str] = []

    def fake_service_action(**kwargs: Any) -> dict[str, Any]:
        actions.append(kwargs["action"])
        return {
            "action": kwargs["action"],
            "service_status": "running",
            "service_mode": "systemd",
        }

    monkeypatch.setattr(service, "_run_managed_provider_service_action", fake_service_action)
    monkeypatch.setattr(service, "_assert_provider_postflight_ready", lambda **_kwargs: None)
    monkeypatch.setattr(
        service,
        "openclaw_telegram_status",
        lambda _agent: {"healthy": True, "bot_username": "replacement_bot"},
    )
    state_before = json.dumps(service.store.read_state(), sort_keys=True)
    monkeypatch.setattr(
        service.store,
        "write_state",
        lambda _state: (_ for _ in ()).throw(RuntimeError("simulated commit race")),
    )

    with pytest.raises(Exception, match="previous files and service state were restored"):
        service.configure_openclaw_telegram(
            "teleclaw",
            ALT_BOT_TOKEN,
            wait_seconds=0,
            replace=True,
        )

    assert actions == ["restart", "restart"]
    assert token_path.read_text(encoding="utf-8") == BOT_TOKEN + "\n"
    assert config_path.read_text(encoding="utf-8") == config_before
    assert json.dumps(service.store.read_state(), sort_keys=True) == state_before


def test_telegram_status_is_stable_actionable_and_redacts_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    raw = {
        "channels": {
            "telegram": {
                "configured": True,
                "running": True,
                "mode": "polling",
                "tokenSource": "tokenFile",
                "probe": {
                    "ok": False,
                    "error": f"request failed for bot {BOT_TOKEN}",
                    "botInfo": {"username": "teleclaw_bot"},
                },
            }
        },
        "channelAccounts": {
            "telegram": [
                {
                    "connected": False,
                    "tokenStatus": "configured",
                }
            ]
        },
    }
    monkeypatch.setattr(service, "_run_openclaw_agent_command", lambda *_args, **_kwargs: raw)

    result = service.openclaw_telegram_status("teleclaw")

    assert result["healthy"] is False
    assert result["configured"] is True
    assert result["running"] is True
    assert result["probe_ok"] is False
    assert result["bot_username"] == "teleclaw_bot"
    assert result["token_source"] == "tokenFile"
    assert "BotFather" in result["remediation"]
    assert BOT_TOKEN not in json.dumps(result)
    assert "[redacted]" in result["last_error"]


def test_telegram_pairing_contract_sanitizes_requests_and_validates_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    calls: list[list[str]] = []

    def fake_run(_agent: str, arguments: list[str], **kwargs: Any) -> dict[str, Any] | None:
        calls.append(arguments)
        if kwargs.get("expect_json"):
            return {
                "requests": [
                    {
                        "code": "PAIR-1234",
                        "id": "42",
                        "createdAt": "2026-07-21T12:00:00Z",
                        "lastSeenAt": "2026-07-21T12:01:00Z",
                        "meta": {
                            "username": "new_user",
                            "firstName": "New",
                            "lastName": "User",
                        },
                    }
                ]
            }
        return None

    monkeypatch.setattr(service, "_run_openclaw_agent_command", fake_run)

    pairings = service.list_openclaw_telegram_pairings("teleclaw")
    assert pairings["requests"] == [
        {
            "code": "PAIR-1234",
            "sender_id": "42",
            "username": "new_user",
            "display_name": "New User",
            "requested_at": "2026-07-21T12:00:00Z",
        }
    ]
    assert calls[-1] == ["pairing", "list", "telegram", "--json"]

    approved = service.approve_openclaw_telegram_pairing("teleclaw", "PAIR-1234")
    assert approved["status"] == "approved"
    assert calls[-1] == ["pairing", "approve", "telegram", "PAIR-1234"]
    with pytest.raises(ValueError, match="pairing code"):
        service.approve_openclaw_telegram_pairing("teleclaw", "bad code")


def test_telegram_setup_cli_never_prints_or_accepts_token_on_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = tmp_path / "telegram.token"
    token_file.write_text(BOT_TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    captured: dict[str, str] = {}

    def fake_configure(
        _service: ClawieService,
        agent_id: str,
        token: str,
        *,
        wait_seconds: float,
        replace: bool,
    ) -> dict[str, Any]:
        captured.update(
            agent_id=agent_id,
            token=token,
            wait_seconds=str(wait_seconds),
            replace=str(replace),
        )
        return {
            "agent_id": agent_id,
            "status": {"healthy": True, "bot_username": "teleclaw_bot"},
            "service_action": "restart",
            "token_file": "/home/teleclaw/.openclaw/telegram.token",
            "dm_policy": "pairing",
        }

    monkeypatch.setattr(ClawieService, "configure_openclaw_telegram", fake_configure)

    code = main(
        [
            "--config-dir",
            str(tmp_path / "state"),
            "--no-color",
            "channel",
            "telegram",
            "setup",
            "teleclaw",
            "--token-file",
            str(token_file),
        ]
    )

    output = capsys.readouterr()
    assert code == 0
    assert captured["token"] == BOT_TOKEN
    assert captured["replace"] == "False"
    assert BOT_TOKEN not in output.out
    assert BOT_TOKEN not in output.err
    assert "pairing-list teleclaw" in output.out
    assert "pairing-approve teleclaw CODE" in output.out


def test_private_token_source_rejects_weak_permissions_and_symlinks(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram.token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        _read_private_telegram_token_file(str(token_file))

    token_file.chmod(0o600)
    symlink = tmp_path / "telegram-link"
    symlink.symlink_to(token_file)
    with pytest.raises(ValueError, match="safely open"):
        _read_private_telegram_token_file(str(symlink))


def test_telegram_status_cli_is_a_monitoring_gate_with_recovery_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "openclaw_telegram_status",
        lambda _service, agent_id: {
            "agent_id": agent_id,
            "healthy": False,
            "configured": True,
            "running": True,
            "connected": False,
            "probe_ok": True,
            "bot_username": "teleclaw_bot",
            "mode": "polling",
            "token_source": "tokenFile",
            "last_error": "",
            "remediation": f"Run 'sudo clawie agent service restart {agent_id}'",
        },
    )

    code = main(
        [
            "--config-dir",
            str(tmp_path),
            "--no-color",
            "channel",
            "telegram",
            "status",
            "teleclaw",
        ]
    )

    output = capsys.readouterr()
    assert code == 1
    assert "healthy: no" in output.out
    assert "agent service restart teleclaw" in output.err


def test_healthy_status_surfaces_pending_pairing_and_exact_approval_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ClawieService,
        "openclaw_telegram_status",
        lambda _service, agent_id: {
            "agent_id": agent_id,
            "healthy": True,
            "configured": True,
            "running": True,
            "connected": True,
            "probe_ok": True,
            "bot_username": "teleclaw_bot",
            "mode": "polling",
            "token_source": "tokenFile",
            "last_error": "",
            "pending_pairing_count": 1,
            "pending_pairings": [
                {
                    "code": "PAIR-1234",
                    "sender_id": "42",
                    "username": "new_user",
                    "display_name": "New User",
                }
            ],
            "pairing_check_error": "",
            "remediation": (
                "Approve the pending sender with 'sudo clawie channel telegram "
                f"pairing-approve {agent_id} CODE'"
            ),
        },
    )

    code = main(
        [
            "--config-dir",
            str(tmp_path),
            "--no-color",
            "channel",
            "telegram",
            "status",
            "teleclaw",
        ]
    )

    output = capsys.readouterr()
    assert code == 0
    assert "pending pairings: 1" in output.out
    assert "PAIR-1234" in output.out
    assert "pairing-approve teleclaw CODE" in output.out


def test_telegram_setup_without_tty_requires_explicit_safe_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class NonInteractiveInput:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr("clawie.cli.sys.stdin", NonInteractiveInput())
    code = main(
        [
            "--config-dir",
            str(tmp_path),
            "--no-color",
            "channel",
            "telegram",
            "setup",
            "teleclaw",
        ]
    )
    assert code == 1
    assert "--token-file PATH or --token-stdin" in capsys.readouterr().err


def test_token_file_mode_is_exactly_private_after_setup(tmp_path: Path) -> None:
    source = tmp_path / "token"
    source.write_text(BOT_TOKEN, encoding="utf-8")
    os.chmod(source, 0o600)
    assert _read_private_telegram_token_file(str(source)) == BOT_TOKEN
