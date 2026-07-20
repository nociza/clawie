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
from clawie.service import ClawieService, SetupError
from clawie.service_common import AgentNotFoundError
from clawie.store import StateStore


def _service_with_agent(
    tmp_path: Path, *, provider: str = "openclaw", linux_user: str = "alice"
) -> ClawieService:
    service = ClawieService(StateStore(config_dir=tmp_path))
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
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.5"  # fast tier
    # session key is task-scoped
    assert cmd[cmd.index("--session-key") + 1].startswith("agent:alice:clawie:")
    assert any(e["type"] == "delegation.delivered" for e in service.list_events(limit=5))


def test_deliver_to_agent_enforces_manifest_gateway_timeout(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["limits"] = {"gateway_timeout": 15}
    service.store.write_state(state)
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str]) -> str:
        captured["cmd"] = cmd
        return '{"payloads":[{"text":"done"}]}'

    result = service.deliver_to_agent("alice", "x", timeout=300, run=fake_run)

    assert captured["cmd"][captured["cmd"].index("--timeout") + 1] == "15"
    assert result["timeout_seconds"] == 15.0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_deliver_to_agent_rejects_invalid_timeout(tmp_path: Path, timeout: float) -> None:
    service = _service_with_agent(tmp_path)
    with pytest.raises(ValueError, match="positive finite"):
        service.deliver_to_agent("alice", "x", timeout=timeout, run=lambda cmd: "{}")


def test_deliver_to_agent_error_reply(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path)
    result = service.deliver_to_agent("alice", "x", run=lambda cmd: '{"error":"model refused"}')
    assert result["ok"] is False
    assert result["error"] == "model refused"
    assert any(e["type"] == "delegation.delivery_failed" for e in service.list_events(limit=5))


def test_deliver_to_agent_unknown_agent(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path))
    with pytest.raises(AgentNotFoundError):
        service.deliver_to_agent("ghost", "x", run=lambda cmd: "{}")


def test_delegate_task_rejects_missing_endpoints_before_persisting(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path)
    state = service.store.read_state()
    state["agents"]["planner"] = {
        "agent_id": "planner",
        "agent": {"provider": "openclaw", "linux_user": "", "model_tier": "balanced"},
    }
    service.store.write_state(state)

    with pytest.raises(AgentNotFoundError, match="ghost-parent"):
        service.delegate_task("ghost-parent", "alice", {"task": "x"})
    with pytest.raises(AgentNotFoundError, match="ghost-child"):
        service.delegate_task("planner", "ghost-child", {"task": "x"})

    assert service.delegation_tasks() == []


def test_deliver_to_agent_provider_without_adapter(tmp_path: Path) -> None:
    service = _service_with_agent(tmp_path, provider="picoclaw")
    with pytest.raises(AdapterError):
        service.deliver_to_agent("alice", "x", run=lambda cmd: "{}")


def test_delegate_task_delivers_to_managed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_agent(tmp_path)
    state = service.store.read_state()
    state["agents"]["planner"] = {
        "agent_id": "planner",
        "agent": {"provider": "openclaw", "linux_user": "", "model_tier": "balanced"},
    }
    service.store.write_state(state)
    seen: dict[str, object] = {}

    def fake_deliver(agent_id: str, message: str, **kwargs: object) -> dict[str, object]:
        seen.update(agent_id=agent_id, message=message, kwargs=kwargs)
        return {"ok": True, "output": "gateway-computed", "delivery_status": "sent"}

    monkeypatch.setattr(service, "deliver_to_agent", fake_deliver)
    result = service.delegate_task(
        "planner", "alice", {"task": "analyze this"}, timeout=45, model_tier="fast"
    )

    assert result["status"] == "completed"
    assert result["result"]["output"] == "gateway-computed"
    assert seen == {
        "agent_id": "alice",
        "message": "analyze this",
        "kwargs": {"tier": "fast", "timeout": 45},
    }
    tasks = service.delegation_tasks(agent_id="planner", status="completed")
    assert len(tasks) == 1
    assert tasks[0]["result"]["output"] == "gateway-computed"


def test_default_deliver_runner_resolves_agent_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_agent(tmp_path)
    seen: dict[str, object] = {}

    def fake_resolve(provider: str) -> str:
        seen["provider"] = provider
        return f"/opt/{provider}"

    def fake_wrap(argv: list[str], linux_user: str, purpose: str = "") -> list[str]:
        seen["argv"] = argv
        seen["linux_user"] = linux_user
        seen["purpose"] = purpose
        return list(argv)

    monkeypatch.setattr(service, "_resolve_provider_executable", fake_resolve)
    monkeypatch.setattr(
        service,
        "_verify_installed_runtime_version",
        lambda provider, executable: "2026.7.1",
    )
    monkeypatch.setattr(service, "_wrap_user_command", fake_wrap)
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
    assert seen["provider"] == "openclaw"
    assert seen["argv"][0] == "/opt/openclaw"
    assert seen["linux_user"] == "alice"
    assert seen["purpose"] == "agent delegation"
    assert result["runtime_version"] == "2026.7.1"


def test_default_deliver_runner_reports_agent_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_agent(tmp_path)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda provider: f"/opt/{provider}")
    monkeypatch.setattr(
        service,
        "_verify_installed_runtime_version",
        lambda provider, executable: "2026.7.1",
    )
    monkeypatch.setattr(service, "_wrap_user_command", lambda argv, lu, purpose="": list(argv))
    monkeypatch.setattr(service, "_service_env", lambda lu: {})

    class _R:
        returncode = 1
        stdout = ""
        stderr = "gateway unavailable"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _R())

    with pytest.raises(SetupError, match="openclaw agent delivery failed: gateway unavailable"):
        service.deliver_to_agent("alice", "x")


def test_delivery_fails_closed_on_unsupported_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_agent(tmp_path)
    monkeypatch.setattr(service, "_resolve_provider_executable", lambda _provider: "/opt/openclaw")
    monkeypatch.setattr(
        service,
        "_verify_installed_runtime_version",
        lambda _provider, _executable: (_ for _ in ()).throw(
            SetupError("outside the verified delivery range")
        ),
    )

    with pytest.raises(SetupError, match="verified delivery range"):
        service.deliver_to_agent("alice", "x")


def test_cli_delegation_deliver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from clawie.cli import main

    monkeypatch.setattr(
        ClawieService,
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
        ClawieService,
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
