"""Service-level tests for per-agent gateway endpoint provisioning (the bridge
enabler) and the backup redaction that keeps the gateway token out of git.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawie.adapters import get_adapter
from clawie.service import ClawieService
from clawie.store import StateStore


def _service(tmp_path: Path) -> ClawieService:
    return ClawieService(StateStore(config_dir=tmp_path))


def test_home_prep_writes_gateway_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_login_shell_env", lambda _u: {})
    home = tmp_path / "home"
    home.mkdir()

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="alice",
        channels=[],
        live_payloads={},
        auth_mode="api_key",
        api_key="sk-test",
        gateway_port=19011,
        gateway_token="tok-xyz",
    )

    gw = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))["gateway"]
    assert gw["mode"] == "local"
    assert gw["bind"] == "loopback"
    assert gw["port"] == 19011
    assert gw["auth"] == {"mode": "token", "token": "tok-xyz"}


def test_home_prep_without_gateway_args_sets_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_login_shell_env", lambda _u: {})
    home = tmp_path / "home"
    home.mkdir()

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="alice",
        channels=[],
        live_payloads={},
        auth_mode="api_key",
        api_key="sk-test",
    )

    gw = json.loads((home / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))["gateway"]
    assert gw["mode"] == "local"
    assert "port" not in gw
    assert "auth" not in gw


def test_allocate_gateway_port_avoids_collisions(tmp_path: Path) -> None:
    service = _service(tmp_path)
    state = service.store.read_state()
    state["agents"] = {
        "a": {"agent": {"gateway_port": 18789}},
        "b": {"agent": {"gateway_port": 18790}},
        "c": {"agent": {}},  # no port — ignored
    }
    service.store.write_state(state)
    assert service._allocate_gateway_port() == 18791


def test_allocate_gateway_port_default_base(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service._allocate_gateway_port() == 18789


def test_backup_redacts_gateway_token(tmp_path: Path) -> None:
    service = _service(tmp_path)
    state = service.store.read_state()
    state["agents"] = {
        "alice": {"agent": {"provider": "openclaw", "gateway_token": "supersecret"}},
    }
    service.store.write_state(state)

    redacted = service._redacted_backup_state()
    assert redacted["agents"]["alice"]["agent"]["gateway_token"] == "<redacted>"
    # original state is untouched (deep copy)
    assert service.store.read_state()["agents"]["alice"]["agent"]["gateway_token"] == "supersecret"


def test_openclaw_version_gate_supported(tmp_path: Path) -> None:
    service = _service(tmp_path)
    gate = service.openclaw_version_gate(run=lambda cmd: "openclaw 2026.7.1")
    assert gate["runtime"] == "openclaw"
    assert gate["version"] == "2026.7.1"
    assert gate["supported"] is True
    assert gate["degraded"] is False


def test_openclaw_version_gate_unknown_degrades(tmp_path: Path) -> None:
    service = _service(tmp_path)
    gate = service.openclaw_version_gate(run=lambda cmd: "weird output, no version")
    assert gate["version"] == ""
    assert gate["degraded"] is True


def test_cli_runtime_version_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from clawie.cli import main

    monkeypatch.setattr(
        ClawieService,
        "openclaw_version_gate",
        lambda self, run=None: {
            "runtime": "openclaw",
            "version": "2026.7.1",
            "supported": True,
            "degraded": False,
            "message": "ok",
        },
    )
    code = main(["--config-dir", str(tmp_path), "runtime", "version"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2026.7.1" in out


def test_cli_runtime_version_unsupported_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from clawie.cli import main

    monkeypatch.setattr(
        ClawieService,
        "openclaw_version_gate",
        lambda self, run=None: {
            "runtime": "openclaw",
            "version": "2030.1.0",
            "supported": False,
            "degraded": True,
            "message": "too new",
        },
    )
    code = main(["--config-dir", str(tmp_path), "runtime", "version"])
    assert code == 1


def test_production_runtime_contract_executes_version_readiness_and_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    state = service.store.read_state()
    state["agents"] = {
        "alice": {
            "agent_id": "alice",
            "agent": {"provider": "openclaw", "linux_user": ""},
        }
    }
    service.store.write_state(state)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda provider: "/bin/openclaw")
    monkeypatch.setattr(service, "_wrap_user_command", lambda argv, user, purpose="": argv)
    monkeypatch.setattr(service, "_service_env", lambda user: {})

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        if "--version" in argv:
            return Result("openclaw 2026.7.1")
        assert argv[1:4] == ["models", "status", "--json"]
        return Result('{"auth":{"providers":[]}}')

    monkeypatch.setattr("clawie.service.subprocess.run", fake_run)

    def fake_deliver(agent_id: str, message: str, **kwargs: object) -> dict[str, object]:
        marker = message.rsplit(" ", 1)[-1]
        return {"ok": True, "output": marker, "transport": "gateway", "fallback_from": ""}

    monkeypatch.setattr(service, "deliver_to_agent", fake_deliver)
    row = service._production_runtime_adapter_contract_check(
        "openclaw", get_adapter, exercise_delivery=True
    )

    assert row["status"] == "pass"
    assert row["evidence"]["runtime_version"] == "2026.7.1"
    assert row["evidence"]["delivery_challenge_verified"] is True


def test_production_runtime_contract_rejects_static_only_proof(tmp_path: Path) -> None:
    service = _service(tmp_path)
    row = service._production_runtime_adapter_contract_check("openclaw", get_adapter)
    assert row["status"] == "fail"
    assert "--exercise-runtime-delivery" in row["message"]
