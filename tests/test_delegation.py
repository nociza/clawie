"""Tests for the recursive agent delegation system."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from clawie.cli import main
from clawie.delegation import (
    DELEGATION_DIR,
    MAX_RECURSION_DEPTH,
    AgentREPL,
    DelegationBus,
    DelegationCoordinator,
    DelegationTree,
    FileMailbox,
    Message,
    SessionAgentManager,
    cleanup_stale_sockets,
    list_active_agents,
    recv_message,
    render_tree_ascii,
    send_message,
)
from clawie.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------


class TestMessage:
    def test_round_trip(self) -> None:
        msg = Message(
            msg_type="task_submit",
            task_id="t1",
            parent_agent_id="planner",
            child_agent_id="worker",
            depth=2,
            payload={"key": "value"},
        )
        raw = msg.encode()
        # Decode: skip 4-byte header
        decoded = Message.decode(raw[4:])
        assert decoded.msg_type == "task_submit"
        assert decoded.task_id == "t1"
        assert decoded.parent_agent_id == "planner"
        assert decoded.child_agent_id == "worker"
        assert decoded.depth == 2
        assert decoded.payload == {"key": "value"}

    def test_auto_fields(self) -> None:
        msg = Message(msg_type="heartbeat")
        assert msg.msg_id  # auto-generated
        assert msg.timestamp > 0

    def test_encode_decode_empty_payload(self) -> None:
        msg = Message(msg_type="shutdown")
        decoded = Message.decode(msg.encode()[4:])
        assert decoded.msg_type == "shutdown"
        assert decoded.payload == {}


# ---------------------------------------------------------------------------
# DelegationTree
# ---------------------------------------------------------------------------


class TestDelegationTree:
    def test_register_and_get(self) -> None:
        tree = DelegationTree()
        node = tree.register("worker", "planner", "task1", depth=0)
        assert node.agent_id == "worker"
        assert node.parent_id == "planner"
        fetched = tree.get_node("worker")
        assert fetched is not None
        assert fetched.task_id == "task1"

    def test_depth_limit(self) -> None:
        tree = DelegationTree()
        with pytest.raises(ValueError, match="max recursion depth"):
            tree.register("deep", "parent", "t", depth=MAX_RECURSION_DEPTH)

    def test_cycle_detection(self) -> None:
        tree = DelegationTree()
        tree.register("B", "A", "t1", depth=0)
        tree.register("C", "B", "t2", depth=1)
        with pytest.raises(ValueError, match="delegation cycle"):
            tree.register("A", "C", "t3", depth=2)

    def test_max_children(self) -> None:
        tree = DelegationTree()
        tree.register("parent", "", "root", depth=0)
        from clawie.delegation import MAX_CHILDREN_PER_AGENT
        for i in range(MAX_CHILDREN_PER_AGENT):
            tree.register(f"child-{i}", "parent", f"t-{i}", depth=1)
        with pytest.raises(ValueError, match="max children"):
            tree.register("one-too-many", "parent", "tx", depth=1)

    def test_subtree(self) -> None:
        tree = DelegationTree()
        tree.register("root", "", "t0", depth=0)
        tree.register("a", "root", "t1", depth=1)
        tree.register("b", "root", "t2", depth=1)
        subtree = tree.get_subtree("root")
        assert subtree["agent_id"] == "root"
        assert len(subtree["children"]) == 2

    def test_to_from_dict(self) -> None:
        tree = DelegationTree()
        tree.register("root", "", "t0", depth=0)
        tree.register("child", "root", "t1", depth=1)
        data = tree.to_dict()
        restored = DelegationTree.from_dict(data)
        assert restored.get_node("root") is not None
        assert restored.get_node("child") is not None

    def test_remove(self) -> None:
        tree = DelegationTree()
        tree.register("root", "", "t0", depth=0)
        tree.register("child", "root", "t1", depth=1)
        tree.remove("child")
        assert tree.get_node("child") is None
        root = tree.get_node("root")
        assert root is not None
        assert "child" not in root.children

    def test_update_status(self) -> None:
        tree = DelegationTree()
        tree.register("a", "", "t", depth=0)
        tree.update_status("a", "running")
        assert tree.get_node("a").status == "running"


# ---------------------------------------------------------------------------
# DelegationBus -- Unix socket IPC
# ---------------------------------------------------------------------------


class TestDelegationBus:
    def test_listen_and_connect(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        server_bus = DelegationBus("server")
        client_bus = DelegationBus("client")
        try:
            server_bus.listen()
            assert (tmp_path / "server.sock").exists()

            # Connect from client in a thread
            results: list[Message] = []

            def _client() -> None:
                sock = client_bus.connect("server")
                msg = Message(msg_type="heartbeat", parent_agent_id="client")
                send_message(sock, msg)
                reply = recv_message(sock, timeout=5.0)
                results.append(reply)

            # Server accept+respond
            def _server() -> None:
                conn = None
                for _ in range(50):  # 5 seconds max
                    conn = server_bus.accept(timeout=0.1)
                    if conn:
                        break
                assert conn is not None
                incoming = recv_message(conn, timeout=5.0)
                ack = Message(msg_type="heartbeat_ack", task_id=incoming.task_id)
                send_message(conn, ack)
                conn.close()

            st = threading.Thread(target=_server, daemon=True)
            ct = threading.Thread(target=_client, daemon=True)
            st.start()
            ct.start()
            st.join(timeout=10)
            ct.join(timeout=10)

            assert len(results) == 1
            assert results[0].msg_type == "heartbeat_ack"
        finally:
            server_bus.close()
            client_bus.close()


# ---------------------------------------------------------------------------
# FileMailbox
# ---------------------------------------------------------------------------


class TestFileMailbox:
    def test_send_and_poll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        sender = FileMailbox("sender")
        receiver = FileMailbox("receiver")
        receiver.ensure()

        msg = Message(msg_type="task_submit", task_id="t1", payload={"data": 42})
        sender.send("receiver", msg)

        messages = receiver.poll()
        assert len(messages) == 1
        assert messages[0].task_id == "t1"
        assert messages[0].payload == {"data": 42}

        # Should be empty after poll (consumed)
        assert receiver.poll() == []

    def test_poll_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mbox = FileMailbox("agent")
        mbox.ensure()
        assert mbox.poll() == []


# ---------------------------------------------------------------------------
# AgentREPL
# ---------------------------------------------------------------------------


class TestAgentREPL:
    def test_echo_handler(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        repl = AgentREPL("echo-agent")
        repl.start_background()
        time.sleep(0.2)  # Let REPL spin up

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(tmp_path / "echo-agent.sock"))

            msg = Message(
                msg_type="task_submit",
                task_id="test-task",
                parent_agent_id="caller",
                child_agent_id="echo-agent",
                payload={"echo": "hello"},
            )
            send_message(sock, msg)

            # Read acceptance
            accepted = recv_message(sock, timeout=5.0)
            assert accepted.msg_type == "task_accepted"

            # Read result
            result = recv_message(sock, timeout=5.0)
            assert result.msg_type == "task_result"
            assert result.payload == {"echo": "hello"}

            sock.close()
        finally:
            repl.stop()

    def test_handler_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)

        def failing_handler(msg: Message, repl: AgentREPL) -> dict:
            raise RuntimeError("boom")

        repl = AgentREPL("fail-agent", handler=failing_handler)
        repl.start_background()
        time.sleep(0.2)

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(tmp_path / "fail-agent.sock"))

            msg = Message(
                msg_type="task_submit",
                task_id="fail-task",
                parent_agent_id="caller",
                child_agent_id="fail-agent",
            )
            send_message(sock, msg)

            accepted = recv_message(sock, timeout=5.0)
            assert accepted.msg_type == "task_accepted"

            result = recv_message(sock, timeout=5.0)
            assert result.msg_type == "task_error"
            assert "boom" in result.payload.get("error", "")

            sock.close()
        finally:
            repl.stop()

    def test_shutdown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        repl = AgentREPL("shutdown-agent")
        repl.start_background()
        time.sleep(0.2)

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(tmp_path / "shutdown-agent.sock"))

            msg = Message(msg_type="shutdown", parent_agent_id="caller")
            send_message(sock, msg)
            ack = recv_message(sock, timeout=5.0)
            assert ack.msg_type == "shutdown"
            sock.close()
        finally:
            repl.stop()


# ---------------------------------------------------------------------------
# Full delegation flow
# ---------------------------------------------------------------------------


class TestDelegationFlow:
    def test_parent_child_delegation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)

        # Start child REPL
        child_repl = AgentREPL("child-worker")
        child_repl.start_background()
        time.sleep(0.2)

        try:
            coord = DelegationCoordinator("parent-planner")
            result = coord.delegate("child-worker", {"task": "analyze"}, timeout=5.0)
            assert result == {"task": "analyze"}  # echo handler
        finally:
            child_repl.stop()
            coord.bus.close()

    def test_recursive_3_levels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test 3-level recursive delegation: root -> mid -> leaf."""
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)

        # Leaf: echo handler
        leaf_repl = AgentREPL("leaf")
        leaf_repl.start_background()
        time.sleep(0.15)

        # Mid: delegates to leaf
        def mid_handler(msg: Message, repl: AgentREPL) -> dict:
            result = repl.delegate("leaf", {"from": "mid"}, depth=2, timeout=5.0)
            return {"mid_got": result}

        mid_repl = AgentREPL("mid", handler=mid_handler)
        mid_repl.start_background()
        time.sleep(0.15)

        try:
            coord = DelegationCoordinator("root")
            result = coord.delegate("mid", {"from": "root"}, depth=1, timeout=10.0)
            assert result == {"mid_got": {"from": "mid"}}
        finally:
            leaf_repl.stop()
            mid_repl.stop()
            coord.bus.close()

    def test_timeout_handling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)

        def slow_handler(msg: Message, repl: AgentREPL) -> dict:
            time.sleep(10)
            return {}

        repl = AgentREPL("slow-agent", handler=slow_handler, timeout=0.5)
        repl.start_background()
        time.sleep(0.2)

        try:
            coord = DelegationCoordinator("impatient")
            result = coord.delegate("slow-agent", {}, timeout=1.0)
            assert result.get("error") or "timed out" in str(result)
        finally:
            repl.stop()
            coord.bus.close()

    def test_delegate_many(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)

        workers = []
        for i in range(3):
            repl = AgentREPL(f"worker-{i}")
            repl.start_background()
            workers.append(repl)
        time.sleep(0.3)

        try:
            coord = DelegationCoordinator("multi-parent")
            tasks = [
                {"child_id": f"worker-{i}", "payload": {"i": i}}
                for i in range(3)
            ]
            results = coord.delegate_many(tasks, timeout=5.0)
            assert len(results) == 3
            for i, r in enumerate(results):
                assert r.get("i") == i
        finally:
            for w in workers:
                w.stop()
            coord.bus.close()


# ---------------------------------------------------------------------------
# Store delegation CRUD
# ---------------------------------------------------------------------------


class TestStoreDelegation:
    def test_write_and_read_task(self, tmp_path: Path) -> None:
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        store.write_delegation_task(
            task_id="t1",
            parent_agent_id="planner",
            child_agent_id="worker",
            depth=1,
            status="completed",
            payload={"data": 1},
            result={"out": 2},
            created_at="2025-01-01T00:00:00Z",
            completed_at="2025-01-01T00:00:01Z",
        )
        tasks = store.read_delegation_tasks(parent_agent_id="planner")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "t1"
        assert tasks[0]["payload"] == {"data": 1}
        assert tasks[0]["result"] == {"out": 2}

    def test_read_filter_by_status(self, tmp_path: Path) -> None:
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        store.write_delegation_task(
            task_id="a", parent_agent_id="p", child_agent_id="c",
            status="pending", created_at="2025-01-01T00:00:00Z",
        )
        store.write_delegation_task(
            task_id="b", parent_agent_id="p", child_agent_id="c",
            status="completed", created_at="2025-01-01T00:00:01Z",
        )
        pending = store.read_delegation_tasks(status="pending")
        assert len(pending) == 1
        assert pending[0]["task_id"] == "a"

    def test_write_and_read_tree(self, tmp_path: Path) -> None:
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        tree_data = {"root": {"agent_id": "root", "children": ["a", "b"]}}
        store.write_delegation_tree("root", tree_data)
        loaded = store.read_delegation_tree("root")
        assert loaded == tree_data

    def test_delete_tree(self, tmp_path: Path) -> None:
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        store.write_delegation_tree("x", {"data": True})
        store.delete_delegation_tree("x")
        assert store.read_delegation_tree("x") == {}

    def test_read_nonexistent_tree(self, tmp_path: Path) -> None:
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        assert store.read_delegation_tree("nobody") == {}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


class TestUtilities:
    def test_cleanup_stale_sockets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        # Create a fake stale socket file
        sock_path = tmp_path / "stale.sock"
        sock_path.touch()
        # Set mtime to old
        old_time = time.time() - 700
        os.utime(str(sock_path), (old_time, old_time))

        removed = cleanup_stale_sockets(max_age_seconds=600.0)
        assert str(sock_path) in removed
        assert not sock_path.exists()

    def test_cleanup_fresh_sockets_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        sock_path = tmp_path / "fresh.sock"
        sock_path.touch()
        removed = cleanup_stale_sockets(max_age_seconds=600.0)
        assert removed == []
        assert sock_path.exists()

    def test_list_active_agents_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        assert list_active_agents() == []


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


class TestDelegationCLI:
    def test_cleanup_command(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = run_cli(tmp_path, "delegation", "cleanup")
        output = capsys.readouterr().out
        assert code == 0
        assert "active REPL agents" in output or "stale socket" in output

    def test_tasks_command_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli(tmp_path, "delegation", "tasks")
        output = capsys.readouterr().out
        assert code == 0
        assert "No delegation tasks" in output

    def test_status_command(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli(tmp_path, "delegation", "status")
        output = capsys.readouterr().out
        assert code == 0
        assert "No active" in output

    def test_tree_command_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli(tmp_path, "delegation", "tree", "--agent-id", "nobody")
        output = capsys.readouterr().out
        assert code == 0
        assert "No delegation tree" in output

    def test_tasks_with_data(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        store.write_delegation_task(
            task_id="cli-test",
            parent_agent_id="p",
            child_agent_id="c",
            status="completed",
            created_at="2025-01-01T00:00:00Z",
        )
        code = run_cli(tmp_path, "delegation", "tasks")
        output = capsys.readouterr().out
        assert code == 0
        assert "cli-test" in output


# ---------------------------------------------------------------------------
# Delegation skill auto-loading
# ---------------------------------------------------------------------------


class TestDelegationSkill:
    def _setup_and_create(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra_args: list[str] | None = None
    ) -> dict:
        """Setup provider and create agent, return agent state from store."""
        from clawie.service import ZeroClawService

        assert run_cli(tmp_path, "config", "set") == 0
        capsys.readouterr()
        args = ["agent", "create", "skill-test"]
        if extra_args:
            args.extend(extra_args)
        assert run_cli(tmp_path, *args) == 0
        capsys.readouterr()
        store = StateStore(config_dir=tmp_path)
        svc = ZeroClawService(store)
        return svc.get_agent("skill-test")

    def test_delegation_skill_auto_loaded_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        agent = self._setup_and_create(tmp_path, capsys)
        plugins = agent.get("agent", {}).get("plugins", {})
        assert plugins.get("delegation") is True
        prompts = agent.get("core_prompts", {})
        content = prompts.get("DELEGATION.md", "")
        assert "# Delegation Skill" in content
        assert "clawie delegation submit" in content
        assert "clawie delegation repl" in content

    def test_delegation_skill_disabled_with_no_delegation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        agent = self._setup_and_create(tmp_path, capsys, extra_args=["--no-delegation"])
        plugins = agent.get("agent", {}).get("plugins", {})
        assert plugins.get("delegation") is False
        prompts = agent.get("core_prompts", {})
        content = prompts.get("DELEGATION.md", "")
        assert content == ""

    def test_skill_content_includes_key_sections(self) -> None:
        from clawie.delegation import DELEGATION_SKILL_CONTENT
        assert "## 1. Core Concepts" in DELEGATION_SKILL_CONTENT
        assert "## 2. CLI Commands" in DELEGATION_SKILL_CONTENT
        assert "## 3. Using the REPL Loop" in DELEGATION_SKILL_CONTENT
        assert "## 4. Parallel Delegation" in DELEGATION_SKILL_CONTENT
        assert "## 5. Error Handling" in DELEGATION_SKILL_CONTENT
        assert "## 6. Quick-Start Checklist" in DELEGATION_SKILL_CONTENT
        assert "## 9. Disabling This Skill" in DELEGATION_SKILL_CONTENT

    def test_delegation_md_in_provider_core_prompts(self) -> None:
        from clawie.providers import get_provider
        for name in ("zeroclaw", "picoclaw", "openclaw"):
            spec = get_provider(name)
            assert "DELEGATION.md" in spec.core_prompt_files

    def test_plugin_toggle_clears_delegation_prompt(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Toggling delegation off clears the prompt on next hydrate."""
        from clawie.service import ZeroClawService

        agent = self._setup_and_create(tmp_path, capsys)
        assert agent["core_prompts"].get("DELEGATION.md", "") != ""

        store = StateStore(config_dir=tmp_path)
        svc = ZeroClawService(store)
        svc.toggle_agent_plugin("skill-test", "delegation")

        updated = svc.get_agent("skill-test")
        assert updated["agent"]["plugins"]["delegation"] is False
        assert updated["core_prompts"].get("DELEGATION.md", "") == ""

    def test_hydration_seeds_skill_for_existing_agent(
        self, tmp_path: Path
    ) -> None:
        """Agents created before the skill existed get it on hydration."""
        from clawie.service import ZeroClawService

        store = StateStore(config_dir=tmp_path)
        svc = ZeroClawService(store)
        svc.setup(
            provider="zeroclaw", api_key="", subscription="starter",
            workspace="default", api_url="https://example.com",
        )
        # Create agent without the delegation plugin key (simulating old agent)
        agent = svc.create_agent(
            agent_id="legacy",
            display_name="legacy",
            template="baseline",
            clone_from=None,
            channel_strategy="new",
            channels=None,
            agent_version="1.0.0",
        )
        # The agent should have delegation enabled by default and the skill seeded
        loaded = svc.get_agent("legacy")
        assert loaded["agent"]["plugins"].get("delegation") is True
        assert "# Delegation Skill" in loaded["core_prompts"].get("DELEGATION.md", "")

    def test_skill_includes_session_agent_docs(self) -> None:
        from clawie.delegation import DELEGATION_SKILL_CONTENT
        assert "## 7. Session Sub-Agents" in DELEGATION_SKILL_CONTENT
        assert "spawn-session" in DELEGATION_SKILL_CONTENT
        assert "SessionAgentManager" in DELEGATION_SKILL_CONTENT
        assert "## 8. Dashboard Delegation View" in DELEGATION_SKILL_CONTENT


# ---------------------------------------------------------------------------
# ASCII tree rendering
# ---------------------------------------------------------------------------


class TestTreeRendering:
    def test_empty_tree(self) -> None:
        lines = render_tree_ascii({})
        assert lines == ["(empty tree)"]

    def test_nested_format(self) -> None:
        nested = {
            "agent_id": "root",
            "status": "running",
            "depth": 0,
            "children": [
                {
                    "agent_id": "child-a",
                    "status": "completed",
                    "depth": 1,
                    "children": [],
                },
                {
                    "agent_id": "child-b",
                    "status": "pending",
                    "depth": 1,
                    "children": [],
                },
            ],
        }
        lines = render_tree_ascii(nested)
        assert any("root" in l for l in lines)
        assert any("child-a" in l for l in lines)
        assert any("child-b" in l for l in lines)
        # Check status icons
        assert any("\u25cf" in l for l in lines)  # ● running
        assert any("\u2713" in l for l in lines)   # ✓ completed
        assert any("\u25cb" in l for l in lines)   # ○ pending

    def test_flat_format(self) -> None:
        tree = DelegationTree()
        tree.register("root", "", "t0", depth=0)
        tree.register("a", "root", "t1", depth=1)
        tree.register("b", "root", "t2", depth=1)
        tree.update_status("root", "running")
        tree.update_status("a", "completed")

        lines = render_tree_ascii(tree.to_dict(), root_id="root")
        assert any("root" in l for l in lines)
        assert any("a" in l and "\u2713" in l for l in lines)  # ✓
        assert len(lines) == 3  # root + 2 children

    def test_flat_format_auto_root(self) -> None:
        tree = DelegationTree()
        tree.register("root", "", "t0", depth=0)
        tree.register("child", "root", "t1", depth=1)
        lines = render_tree_ascii(tree.to_dict())
        assert any("root" in l for l in lines)
        assert any("child" in l for l in lines)


# ---------------------------------------------------------------------------
# SessionAgentManager
# ---------------------------------------------------------------------------


class TestSessionAgentManager:
    def test_spawn_and_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mgr = SessionAgentManager("parent")
        try:
            info = mgr.spawn("child-1")
            assert info["agent_id"] == "child-1"
            assert info["status"] == "running"
            assert info["session"] is True

            agents = mgr.list_agents()
            assert len(agents) == 1
            assert agents[0]["agent_id"] == "child-1"
        finally:
            mgr.stop_all()

    def test_spawn_duplicate_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mgr = SessionAgentManager("parent")
        try:
            mgr.spawn("dup")
            with pytest.raises(ValueError, match="already exists"):
                mgr.spawn("dup")
        finally:
            mgr.stop_all()

    def test_delegate_to_session_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mgr = SessionAgentManager("parent")
        try:
            mgr.spawn("echo-worker")
            time.sleep(0.2)  # Let REPL spin up
            result = mgr.delegate("echo-worker", {"msg": "hi"}, timeout=5.0)
            assert result == {"msg": "hi"}  # echo handler
        finally:
            mgr.stop_all()

    def test_tree_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mgr = SessionAgentManager("root-agent")
        try:
            mgr.spawn("sub-1")
            mgr.spawn("sub-2")
            time.sleep(0.2)  # Let REPLs spin up
            lines = mgr.tree_lines()
            assert any("sub-1" in l for l in lines)
            assert any("sub-2" in l for l in lines)
        finally:
            mgr.stop_all()

    def test_stop_single_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mgr = SessionAgentManager("parent")
        try:
            mgr.spawn("w1")
            mgr.spawn("w2")
            mgr.stop_agent("w1")
            agents = mgr.list_agents()
            assert len(agents) == 1
            assert agents[0]["agent_id"] == "w2"
        finally:
            mgr.stop_all()

    def test_delegate_to_nonexistent_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("clawie.delegation.DELEGATION_DIR", tmp_path)
        mgr = SessionAgentManager("parent")
        try:
            with pytest.raises(ValueError, match="not found"):
                mgr.delegate("ghost", {"x": 1})
        finally:
            mgr.stop_all()


# ---------------------------------------------------------------------------
# CLI session commands
# ---------------------------------------------------------------------------


class TestSessionCLI:
    def test_session_agents_no_parent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli(tmp_path, "delegation", "session-agents", "--parent", "nobody")
        output = capsys.readouterr().out
        assert code == 0
        assert "No session agents" in output

    def test_tree_uses_ascii_art(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When tree exists from task history, show ASCII art not JSON."""
        store = StateStore(config_dir=tmp_path)
        store.ensure()
        store.write_delegation_task(
            task_id="tree-task",
            parent_agent_id="parent",
            child_agent_id="child",
            status="completed",
            created_at="2025-01-01T00:00:00Z",
        )
        # Also write a delegation tree so the tree command finds it
        tree_data = {
            "parent": {
                "agent_id": "parent", "parent_id": "", "task_id": "root",
                "depth": 0, "status": "running", "children": ["child"],
            },
            "child": {
                "agent_id": "child", "parent_id": "parent", "task_id": "tree-task",
                "depth": 1, "status": "completed", "children": [],
            },
        }
        store.write_delegation_tree("parent", tree_data)
        code = run_cli(tmp_path, "delegation", "tree", "--agent-id", "parent")
        output = capsys.readouterr().out
        assert code == 0
        assert "parent" in output
        assert "child" in output
        # Should NOT be raw JSON
        assert '"agent_id"' not in output
