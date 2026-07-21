"""Tests for the gateway delivery bridge (clawie service deliver_to_agent).

The bridge is exercised with an injected runner, so no openclaw install or
running gateway is required: we assert the adapter command is built correctly,
the reply is parsed, the event log records the outcome, and unknown
agents/providers fail cleanly.
"""
from __future__ import annotations

import re
import threading
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
    assert cmd[cmd.index("--agent") + 1] == "main"
    assert cmd[cmd.index("--message") + 1] == "do the thing"
    assert cmd[cmd.index("--timeout") + 1] == "60"
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.5"  # fast tier
    # session key is task-scoped
    assert cmd[cmd.index("--session-key") + 1].startswith("agent:main:clawie:")
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
    assert seen["agent_id"] == "alice"
    assert "[Clawie delegation context]" in str(seen["message"])
    assert "analyze this" in str(seen["message"])
    assert f"--parent-task {result['task_id']}" in str(seen["message"])
    assert seen["kwargs"] == {"tier": "fast", "timeout": 45}
    assert result["model_tier"] == "fast"
    assert result["depth"] == 1
    assert result["root_agent_id"] == "planner"
    assert result["context_budget"]["total_budget"] == 4_000
    assert result["context_budget"]["tokens_used"] > 0
    tasks = service.delegation_tasks(agent_id="planner", status="completed")
    assert len(tasks) == 1
    assert tasks[0]["result"]["output"] == "gateway-computed"
    assert tasks[0]["root_agent_id"] == "planner"
    assert tasks[0]["root_task_id"] == result["task_id"]


def _service_with_agents(tmp_path: Path, *agent_ids: str) -> ClawieService:
    service = ClawieService(StateStore(config_dir=tmp_path))
    state = service.store.read_state()
    state["agents"] = {
        agent_id: {
            "agent_id": agent_id,
            "agent": {
                "provider": "openclaw",
                "linux_user": agent_id,
                "model_tier": "balanced",
            },
        }
        for agent_id in agent_ids
    }
    service.store.write_state(state)
    return service


def _task_id_from_managed_message(message: str) -> str:
    match = re.search(r"^task_id: ([0-9a-f]+)$", message, flags=re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_managed_delegation_persists_recursive_lineage_and_auto_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_agents(tmp_path, "planner", "worker", "researcher")
    nested_result: dict[str, object] = {}

    def fake_deliver(agent_id: str, message: str, **kwargs: object) -> dict[str, object]:
        if agent_id == "worker":
            nested_result.update(
                service.delegate_task(
                    "worker",
                    "researcher",
                    {"task": "check status"},
                    parent_task_id=_task_id_from_managed_message(message),
                )
            )
        return {"ok": True, "output": f"done:{agent_id}", "delivery_status": "sent"}

    monkeypatch.setattr(service, "deliver_to_agent", fake_deliver)
    result = service.delegate_task("planner", "worker", {"task": "analyze architecture"})

    assert result["status"] == "completed"
    assert result["model_tier"] == "power"
    assert nested_result["status"] == "completed"
    assert nested_result["model_tier"] == "fast"
    assert nested_result["root_agent_id"] == "planner"
    assert nested_result["root_task_id"] == result["root_task_id"]
    assert nested_result["parent_task_id"] == result["task_id"]
    assert nested_result["depth"] == 2
    tree = service.delegation_tree("planner")
    assert tree["planner"]["children"] == ["worker"]
    assert tree["worker"]["children"] == ["researcher"]
    assert tree["researcher"]["depth"] == 2
    assert tree["planner"]["status"] == "completed"


def test_managed_delegation_rejects_active_recursive_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_agents(tmp_path, "alpha", "beta")
    cycle_error = ""

    def fake_deliver(agent_id: str, message: str, **_kwargs: object) -> dict[str, object]:
        nonlocal cycle_error
        if agent_id == "beta":
            with pytest.raises(ValueError, match="cycle detected") as exc:
                service.delegate_task(
                    "beta",
                    "alpha",
                    {"task": "recurse"},
                    parent_task_id=_task_id_from_managed_message(message),
                )
            cycle_error = str(exc.value)
        return {"ok": True, "output": "stopped", "delivery_status": "sent"}

    monkeypatch.setattr(service, "deliver_to_agent", fake_deliver)
    result = service.delegate_task("alpha", "beta", {"task": "start"})

    assert result["status"] == "completed"
    assert "alpha already in ancestry" in cycle_error
    assert len(service.delegation_tasks()) == 1


def test_managed_delegation_rejects_depth_overflow_from_active_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_agents(tmp_path, "root", "middle", "leaf")
    state = service.store.read_state()
    state["agents"]["root"]["agent"]["limits"] = {"delegation_depth": 2}
    service.store.write_state(state)
    depth_error = ""

    def fake_deliver(agent_id: str, message: str, **_kwargs: object) -> dict[str, object]:
        nonlocal depth_error
        if agent_id == "middle":
            with pytest.raises(ValueError, match="max recursion depth") as exc:
                service.delegate_task(
                    "middle",
                    "leaf",
                    {"task": "too deep"},
                    parent_task_id=_task_id_from_managed_message(message),
                )
            depth_error = str(exc.value)
        return {"ok": True, "output": "done", "delivery_status": "sent"}

    monkeypatch.setattr(service, "deliver_to_agent", fake_deliver)
    result = service.delegate_task("root", "middle", {"task": "start"})

    assert result["status"] == "completed"
    assert "depth=2" in depth_error


def test_managed_delegation_reservation_prevents_concurrent_duplicate_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_agents(tmp_path, "p1", "p2", "worker")
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    first_result: dict[str, object] = {}

    def fake_deliver(_agent_id: str, _message: str, **_kwargs: object) -> dict[str, object]:
        delivery_started.set()
        assert release_delivery.wait(timeout=5)
        return {"ok": True, "output": "done", "delivery_status": "sent"}

    def run_first() -> None:
        first_result.update(service.delegate_task("p1", "worker", {"task": "work"}))

    monkeypatch.setattr(service, "deliver_to_agent", fake_deliver)
    thread = threading.Thread(target=run_first)
    thread.start()
    assert delivery_started.wait(timeout=5)
    try:
        with pytest.raises(ValueError, match="already participates"):
            service.delegate_task("p2", "worker", {"task": "other"})
    finally:
        release_delivery.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert first_result["status"] == "completed"


def test_managed_delegation_emits_persisted_context_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_agents(tmp_path, "planner", "worker")
    monkeypatch.setattr(
        service,
        "deliver_to_agent",
        lambda *_args, **_kwargs: {
            "ok": True,
            "output": "x" * 12_500,
            "delivery_status": "sent",
        },
    )

    result = service.delegate_task(
        "planner", "worker", {"task": "check"}, model_tier="fast"
    )

    assert result["context_budget"]["needs_warning"] is True
    assert result["context_budget"]["needs_compaction"] is False
    tasks = service.delegation_tasks(agent_id="planner")
    assert tasks[0]["context_budget"] == result["context_budget"]
    assert any(
        event["type"] == "delegation.context_warning"
        for event in service.list_events(limit=10)
    )


def test_managed_delegation_rejects_payload_over_selected_budget(tmp_path: Path) -> None:
    service = _service_with_agents(tmp_path, "planner", "worker")

    with pytest.raises(ValueError, match="exceeds the power context budget"):
        service.delegate_task("planner", "worker", {"task": "analyze", "data": "x" * 300_000})

    assert service.delegation_tasks() == []


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
