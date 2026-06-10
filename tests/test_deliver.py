"""Tests for the gateway delivery bridge (clawie service deliver_to_agent).

The bridge is exercised with an injected runner, so no openclaw install or
running gateway is required: we assert the adapter command is built correctly,
the reply is parsed, the event log records the outcome, and unknown
agents/providers fail cleanly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clawie.adapters import AdapterError
from clawie.service import ZeroClawService
from clawie.service_common import AgentNotFoundError
from clawie.store import StateStore


def _service_with_agent(
    tmp_path: Path, *, provider: str = "openclaw", linux_user: str = "alice"
) -> ZeroClawService:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    state = service.store.read_state()
    state["agents"] = {
        "alice": {
            "agent_id": "alice",
            "agent": {"provider": provider, "linux_user": linux_user, "model_tier": "balanced"},
        }
    }
    service.store.write_state(state)
    return service


def test_deliver_to_agent_success(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path)
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str]) -> str:
        captured["cmd"] = cmd
        return '{"payloads":[{"text":"done"}],"deliveryStatus":{"status":"sent"}}'

    result = service.deliver_to_agent("alice", "do the thing", tier="fast", timeout=60, run=fake_run)

    assert result["ok"] is True
    assert result["output"] == "done"
    assert result["delivery_status"] == "sent"
    cmd = captured["cmd"]
    assert cmd[:2] == ["openclaw", "agent"]
    assert cmd[cmd.index("--agent") + 1] == "alice"
    assert cmd[cmd.index("--message") + 1] == "do the thing"
    assert cmd[cmd.index("--timeout") + 1] == "60"
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.2"  # fast tier
    # session key is task-scoped
    assert cmd[cmd.index("--session-key") + 1].startswith("agent:alice:clawie:")
    assert any(e["type"] == "delegation.delivered" for e in service.list_events(limit=5))


def test_deliver_to_agent_error_reply(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path)
    result = service.deliver_to_agent("alice", "x", run=lambda cmd: '{"error":"model refused"}')
    assert result["ok"] is False
    assert result["error"] == "model refused"
    assert any(e["type"] == "delegation.delivery_failed" for e in service.list_events(limit=5))


def test_deliver_to_agent_unknown_agent(tmp_path: Path) -> None:
    service = ZeroClawService(StateStore(config_dir=tmp_path))
    with pytest.raises(AgentNotFoundError):
        service.deliver_to_agent("ghost", "x", run=lambda cmd: "{}")


def test_deliver_to_agent_provider_without_adapter(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path, provider="picoclaw")
    with pytest.raises(AdapterError):
        service.deliver_to_agent("alice", "x", run=lambda cmd: "{}")


def test_default_deliver_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with_agent(tmp_path)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda provider: "/opt/openclaw")
    monkeypatch.setattr(service, "_wrap_user_command", lambda argv, lu, purpose="": list(argv))
    monkeypatch.setattr(service, "_service_env", lambda lu: {})

    class _R:
        returncode = 0
        stdout = '{"payloads":[{"text":"ok"}]}'
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _R())

    # no run injected -> exercises _default_deliver_runner, which swaps argv[0]
    # for the resolved executable path
    result = service.deliver_to_agent("alice", "x")
    assert result["ok"] is True
    assert result["output"] == "ok"


def test_cli_delegation_deliver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from clawie.cli import main

    monkeypatch.setattr(
        ZeroClawService,
        "deliver_to_agent",
        lambda self, agent_id, message, **kw: {
            "agent_id": agent_id,
            "task_id": "t",
            "ok": True,
            "output": "RESULT",
            "error": "",
            "usage": {},
            "delivery_status": "sent",
        },
    )
    code = main(["--config-dir", str(tmp_path), "delegation", "deliver", "--agent", "alice", "--message", "hi"])
    out = capsys.readouterr().out
    assert code == 0
    assert "RESULT" in out


def test_cli_delegation_deliver_failure_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from clawie.cli import main

    monkeypatch.setattr(
        ZeroClawService,
        "deliver_to_agent",
        lambda self, agent_id, message, **kw: {
            "agent_id": agent_id,
            "task_id": "t",
            "ok": False,
            "output": "",
            "error": "boom",
            "usage": {},
            "delivery_status": "",
        },
    )
    code = main(
        ["--config-dir", str(tmp_path), "delegation", "deliver", "--agent", "alice", "--message", "hi", "--json"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert '"ok": false' in out
