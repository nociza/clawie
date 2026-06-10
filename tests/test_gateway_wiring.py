"""Service-level tests for per-agent gateway endpoint provisioning (the bridge
enabler) and the backup redaction that keeps the gateway token out of git.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawie.service import ClawieService
from clawie.store import StateStore


def _service(tmp_path: Path) -> ClawieService:
    return ClawieService(StateStore(config_dir=tmp_path))


def test_home_prep_writes_gateway_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_login_shell_env", lambda _u: {})
    home = tmp_path / "home"

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
