from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch, raises

from clawie.cli import main
from clawie.daemon import Clawied
from clawie.manifest import AgentManifest, ChannelSpec, CredentialRef
from clawie.service import ClawieService
from clawie.service_common import AgentNotFoundError, SetupError
from clawie.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


def _setup_service(tmp_path: Path) -> ClawieService:
    service = ClawieService(StateStore(config_dir=tmp_path))
    service.setup(
        provider="openclaw",
        api_key="",
        subscription="starter",
        workspace="default",
        api_url="https://api.openclaw.example/v1",
    )
    return service


def _create_agent(
    service: ClawieService,
    agent_id: str,
    *,
    provider: str | None = None,
    channels: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return service.create_agent(
        agent_id=agent_id,
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=channels or [],
        agent_version="",
        provider=provider,
    )


def test_reconcile_agent_manifest_creates_and_converges_agent(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    manifest = AgentManifest(
        id="alice",
        provider="picoclaw",
        model_tier="power",
        display_name="Alice Ops",
        channels=[ChannelSpec("telegram", "ops")],
        credentials=[CredentialRef("provider-auth", "shared")],
    )

    dry_run = service.reconcile_agent_manifest(manifest, dry_run=True)
    assert [row["kind"] for row in dry_run["actions"]] == [
        "ensure_agent",
        "set_provider",
        "set_model_tier",
        "ensure_channel",
        "set_credentials",
    ]
    assert service.observed_agent_manifest_state("alice") is None

    result = service.reconcile_agent_manifest(manifest)

    assert result["converged"] is True
    assert result["errors"] == []
    observed = service.observed_agent_manifest_state("alice")
    assert observed is not None
    assert observed["provider"] == "picoclaw"
    assert observed["model_tier"] == "power"
    assert observed["display_name"] == "Alice Ops"
    assert observed["credential_bundles"] == ["provider-auth"]
    assert observed["channels"] == [{"kind": "telegram", "name": "ops"}]


def test_reconcile_control_role_seeds_control_workspace_prompts(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)

    result = service.reconcile_agent_manifest(
        AgentManifest(id="control", provider="openclaw", role="control")
    )
    agent = service.get_agent("control")

    assert result["converged"] is True
    assert agent["agent"]["role"] == "control"
    assert "clawie-control-tools-begin" in agent["core_prompts"]["TOOLS.md"]
    assert "clawie control request <verb>" in agent["core_prompts"]["TOOLS.md"]
    assert "clawie-control-boot-begin" in agent["core_prompts"]["AGENTS.md"]

    service.reconcile_agent_manifest(
        AgentManifest(id="control", provider="openclaw", role="worker")
    )
    worker = service.get_agent("control")

    assert worker["agent"]["role"] == "worker"
    assert "clawie-control-tools-begin" not in worker["core_prompts"]["TOOLS.md"]
    assert "clawie-control-boot-begin" not in worker["core_prompts"]["AGENTS.md"]


def test_clawied_run_once_reconciles_stored_manifests_and_writes_status(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    service.write_agent_manifest(
        AgentManifest(
            id="bob",
            provider="openclaw",
            channels=[ChannelSpec("telegram", "ops")],
        )
    )
    daemon = Clawied(service, interval_seconds=2)

    result = daemon.run_once()

    assert result["status"] == "ok"
    assert result["manifests"] == 1
    assert result["errors"] == 0
    assert service.observed_agent_manifest_state("bob") is not None
    stored = json.loads((tmp_path / "clawied-status.json").read_text(encoding="utf-8"))
    assert stored["manifests"] == 1
    assert stored["results"][0]["agent_id"] == "bob"


def test_clawied_cli_reconcile_json_reports_plan(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    service.write_agent_manifest(AgentManifest(id="carol", provider="openclaw"))

    code = run_cli(tmp_path, "clawied", "reconcile", "--agent", "carol", "--dry-run", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["agent_id"] == "carol"
    assert payload["dry_run"] is True
    assert payload["actions"][0]["kind"] == "ensure_agent"


def test_clawied_cli_run_once_json_writes_status(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    service.write_agent_manifest(AgentManifest(id="dora", provider="openclaw"))

    code = run_cli(tmp_path, "clawied", "run", "--once", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["cycles"] == 1
    assert payload["last"]["manifests"] == 1
    assert (tmp_path / "clawied-status.json").is_file()


def test_clawied_ipc_status_and_stop(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    status = daemon.request("status", {})
    stop = daemon.request("stop", {})
    thread.join(timeout=5)

    assert status["via"] == "ipc"
    assert status["running"] is True
    assert stop["via"] == "ipc"
    assert stop["stopped"] is True
    assert result_holder["result"]["status"] == "stopped"  # type: ignore[index]


def test_clawied_cli_status_uses_ipc_when_daemon_is_running(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    code = run_cli(tmp_path, "clawied", "status", "--json")
    payload = json.loads(capsys.readouterr().out)
    daemon.request("stop", {})
    thread.join(timeout=5)

    assert code == 0
    assert payload["via"] == "ipc"
    assert payload["running"] is True


def test_clawied_cli_reconcile_uses_ipc_when_daemon_is_running(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    service.write_agent_manifest(AgentManifest(id="erin", provider="openclaw"))
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    code = run_cli(tmp_path, "clawied", "reconcile", "--agent", "erin", "--json")
    payload = json.loads(capsys.readouterr().out)
    daemon.request("stop", {})
    thread.join(timeout=5)

    assert code == 0
    assert payload["via"] == "ipc"
    assert payload["agent_id"] == "erin"


def test_clawied_service_call_rejects_unapproved_method(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        with raises(SetupError, match="unsupported clawied service method"):
            daemon.request("service_call", {"method": "export_state", "kwargs": {"output": "state.json"}})
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)


def test_clawied_cli_credentials_set_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "frank")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "agent", "credentials", "set", "frank", "provider-auth")
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert code == 0
    assert "Selected bundles: provider-auth" in output
    assert service.get_agent("frank")["credential_sync"]["bundles"] == ["provider-auth"]


def test_clawied_cli_agent_create_and_delete_route_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        create_code = run_cli(tmp_path, "agent", "create", "iris", "--model-tier", "fast")
        create_output = capsys.readouterr().out
        created = service.get_agent("iris")
        delete_code = run_cli(tmp_path, "agent", "delete", "iris")
        delete_output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert create_code == 0
    assert "Provisioned agent iris" in create_output
    assert created["agent"]["model_tier"] == "fast"
    assert delete_code == 0
    assert "Deleted agent iris" in delete_output
    with raises(AgentNotFoundError):
        service.get_agent("iris")


def test_clawied_cli_agent_purge_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "jules")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "agent", "purge", "jules", "--yes")
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert code == 0
    assert "Purged agent jules" in output
    with raises(AgentNotFoundError):
        service.get_agent("jules")


def test_clawied_cli_batch_create_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(
        json.dumps(
            [
                {"agent_id": "kira", "display_name": "Kira"},
                {"agent_id": "luis", "display_name": "Luis"},
            ]
        ),
        encoding="utf-8",
    )
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "agent", "create-batch", str(batch_file))
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert code == 0
    assert "created: 2" in output
    assert service.get_agent("kira")["display_name"] == "Kira"
    assert service.get_agent("luis")["display_name"] == "Luis"


def test_clawied_cli_prompt_copy_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "mona")
    _create_agent(service, "nate")
    service.set_agent_core_prompt("mona", "SOUL.md", "source soul", sync_to_disk=False)
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(
            tmp_path,
            "agent",
            "prompt",
            "copy",
            "mona",
            "nate",
            "--no-apply-to-disk",
        )
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    target = service.get_agent("nate")
    assert code == 0
    assert "Cloned core prompts mona -> nate" in output
    assert str(target.get("core_prompts", {}).get("SOUL.md", "")) == "source soul"


def test_clawied_cli_config_set_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(
            tmp_path,
            "config",
            "set",
            "--provider",
            "picoclaw",
            "--subscription",
            "pro",
            "--workspace",
            "ops",
        )
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    config = service.store.read_config()
    assert code == 0
    assert "Clawie config updated" in output
    assert config["provider"] == "picoclaw"
    assert config["subscription"] == "pro"
    assert config["workspace"] == "ops"


def test_clawied_cli_backup_init_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    backup_dir = tmp_path / "backup-repo"
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "backup", "init", str(backup_dir), "--no-auto")
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    config = service.store.read_config()
    assert code == 0
    assert "Backup repo ready" in output or "Created backup repo" in output
    assert config["backup_repo_path"] == str(backup_dir.resolve())
    assert config["backup_enabled"] is False
    assert (backup_dir / ".git").is_dir()


def test_clawied_cli_backup_import_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "otto")
    snapshot = service.export_state(tmp_path / "snapshot.json")
    service.delete_agent("otto")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "backup", "import", str(snapshot), "--merge")
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert code == 0
    assert "State merged" in output
    assert service.get_agent("otto")["agent_id"] == "otto"


def test_clawied_cli_provider_set_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "gina", provider="openclaw")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "agent", "provider", "set", "gina", "picoclaw")
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert code == 0
    assert "Changed provider for gina to picoclaw" in output
    assert service.get_agent("gina")["agent"]["provider"] == "picoclaw"


def test_clawied_cli_channel_apply_routes_through_daemon(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "hank")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        code = run_cli(tmp_path, "channel", "apply", "hank", "--preset", "minimal")
        output = capsys.readouterr().out
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    agent = service.get_agent("hank")
    assert code == 0
    assert "Applied minimal preset for hank (1 channels)" in output
    assert len(agent["channels"]) == 1
    assert agent["channels"][0]["kind"] == "chat"
    assert agent["channels"][0]["name"] == "hank-primary"
    assert agent["channels"][0]["enabled"] is True


def test_clawied_control_request_allows_read_status(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "opal")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        result = daemon.request(
            "control_request",
            {"verb": "status", "args": {"sections": ["agents"]}},
        )
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert result["decision"] == "allow"
    assert result["allowed"] is True
    assert result["tier"] == "read"
    assert result["result"]["agents"]["rows"][0]["agent_id"] == "opal"


def test_clawied_control_cli_request_and_confirm_use_ipc(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "rhea")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        read_code = run_cli(
            tmp_path,
            "control",
            "request",
            "status",
            "--args-json",
            '{"sections":["agents"]}',
            "--json",
        )
        read_payload = json.loads(capsys.readouterr().out)
        pending_code = run_cli(
            tmp_path,
            "control",
            "request",
            "delete_agent",
            "--args-json",
            '{"agent_id":"rhea"}',
            "--json",
        )
        pending = json.loads(capsys.readouterr().out)
        confirm_code = run_cli(
            tmp_path,
            "control",
            "confirm",
            "delete_agent",
            "--nonce",
            pending["nonce"],
            "--confirmer",
            "@op",
            "--args-json",
            '{"agent_id":"rhea"}',
            "--json",
        )
        confirmed = json.loads(capsys.readouterr().out)
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert read_code == 0
    assert read_payload["decision"] == "allow"
    assert read_payload["result"]["agents"]["rows"][0]["agent_id"] == "rhea"
    assert pending_code == 0
    assert pending["decision"] == "pending_confirmation"
    assert pending["tier"] == "destructive"
    assert confirm_code == 0
    assert confirmed["decision"] == "allow"
    assert confirmed["result"] == {"agent_id": "rhea", "deleted": True}
    with raises(AgentNotFoundError):
        service.get_agent("rhea")


def test_clawied_control_request_runs_safe_heal_reconcile(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    service.write_agent_manifest(AgentManifest(id="pax", provider="openclaw"))
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        result = daemon.request(
            "control_request",
            {"verb": "reconcile", "args": {"agent": "pax"}},
        )
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert result["decision"] == "allow"
    assert result["tier"] == "safe_heal"
    assert result["result"]["agent_id"] == "pax"
    assert service.observed_agent_manifest_state("pax") is not None


def test_clawied_control_destructive_requires_matching_confirmation(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    _create_agent(service, "quinn")
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    try:
        pending = daemon.request(
            "control_request",
            {"verb": "delete_agent", "args": {"agent_id": "quinn"}},
        )
        denied = daemon.request(
            "control_confirm",
            {
                "nonce": pending["nonce"],
                "confirmer": "@op",
                "verb": "delete_agent",
                "args": {"agent_id": "other"},
            },
        )
        still_present = service.get_agent("quinn")
        pending_again = daemon.request(
            "control_request",
            {"verb": "delete_agent", "args": {"agent_id": "quinn"}},
        )
        confirmed = daemon.request(
            "control_confirm",
            {
                "nonce": pending_again["nonce"],
                "confirmer": "@op",
                "verb": "delete_agent",
                "args": {"agent_id": "quinn"},
            },
        )
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert pending["decision"] == "pending_confirmation"
    assert pending["allowed"] is False
    assert pending["tier"] == "destructive"
    assert denied["decision"] == "deny"
    assert still_present["agent_id"] == "quinn"
    assert confirmed["decision"] == "allow"
    assert confirmed["result"] == {"agent_id": "quinn", "deleted": True}
    with raises(AgentNotFoundError):
        service.get_agent("quinn")


def test_clawied_control_open_issue_requires_confirmation_and_uses_github_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = _setup_service(tmp_path)
    token_path = tmp_path / "github-token"
    token_path.write_text("ghp_testtoken\n", encoding="utf-8")
    token_path.chmod(0o600)
    service.configure_control_escalation(
        github_repo="octo/example",
        github_token_path=str(token_path),
        operators=["@op"],
        issue_labels=["clawie-control", "bug"],
        rate_limit_seconds=0,
    )
    calls: list[dict[str, object]] = []

    def fake_github_request(
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        calls.append({"method": method, "url": url, "token": token, "payload": payload})
        return {"number": 17, "html_url": "https://github.com/octo/example/issues/17"}

    monkeypatch.setattr(
        ClawieService,
        "_github_json_request",
        staticmethod(fake_github_request),
    )
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    args = {
        "title": "Provider drift detected",
        "body": "openclaw version changed",
        "dedupe_key": "provider-drift-openclaw",
    }
    try:
        pending = daemon.request("control_request", {"verb": "open_issue", "args": args})
        confirmed = daemon.request(
            "control_confirm",
            {
                "nonce": pending["nonce"],
                "confirmer": "@op",
                "verb": "open_issue",
                "args": args,
            },
        )
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert pending["decision"] == "pending_confirmation"
    assert pending["tier"] == "outward"
    assert confirmed["decision"] == "allow"
    assert confirmed["result"]["created"] is True
    assert confirmed["result"]["number"] == 17
    assert calls == [
        {
            "method": "POST",
            "url": "https://api.github.com/repos/octo/example/issues",
            "token": "ghp_testtoken",
            "payload": {
                "title": "Provider drift detected",
                "body": "openclaw version changed",
                "labels": ["clawie-control", "bug"],
            },
        }
    ]
    events = service.store.read_state()["events"]
    assert any(event["type"] == "control.github_issue_opened" for event in events)


def test_clawied_control_open_pr_requires_confirmation_and_uses_existing_branch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = _setup_service(tmp_path)
    token_path = tmp_path / "github-token"
    token_path.write_text("ghp_testtoken\n", encoding="utf-8")
    token_path.chmod(0o600)
    service.configure_control_escalation(
        github_repo="https://github.com/octo/example",
        github_token_path=str(token_path),
        operators=["@op"],
        rate_limit_seconds=0,
    )
    calls: list[dict[str, object]] = []

    def fake_github_request(
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        calls.append({"method": method, "url": url, "token": token, "payload": payload})
        return {"number": 22, "html_url": "https://github.com/octo/example/pull/22"}

    monkeypatch.setattr(
        ClawieService,
        "_github_json_request",
        staticmethod(fake_github_request),
    )
    daemon = Clawied(service, interval_seconds=30)
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", daemon.run_forever()),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(daemon.socket_path)

    args = {
        "title": "Repair provider drift",
        "body": "Reconciles the generated manifest.",
        "head": "control/provider-drift",
        "base": "main",
        "draft": True,
        "dedupe_key": "provider-drift-pr",
    }
    try:
        pending = daemon.request("control_request", {"verb": "open_pr", "args": args})
        confirmed = daemon.request(
            "control_confirm",
            {
                "nonce": pending["nonce"],
                "confirmer": "@op",
                "verb": "open_pr",
                "args": args,
            },
        )
    finally:
        daemon.request("stop", {})
        thread.join(timeout=5)

    assert pending["decision"] == "pending_confirmation"
    assert pending["tier"] == "outward"
    assert confirmed["decision"] == "allow"
    assert confirmed["result"]["created"] is True
    assert confirmed["result"]["number"] == 22
    assert confirmed["result"]["draft"] is True
    assert calls == [
        {
            "method": "POST",
            "url": "https://api.github.com/repos/octo/example/pulls",
            "token": "ghp_testtoken",
            "payload": {
                "title": "Repair provider drift",
                "body": "Reconciles the generated manifest.",
                "head": "control/provider-drift",
                "base": "main",
                "draft": True,
                "maintainer_can_modify": False,
            },
        }
    ]
    events = service.store.read_state()["events"]
    assert any(event["type"] == "control.github_pr_opened" for event in events)


def test_control_github_token_file_must_be_private(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    token_path = tmp_path / "github-token"
    token_path.write_text("ghp_testtoken\n", encoding="utf-8")
    token_path.chmod(0o644)
    service.configure_control_escalation(
        github_repo="octo/example",
        github_token_path=str(token_path),
        rate_limit_seconds=0,
    )

    with raises(SetupError, match="must not be group/world accessible"):
        service.open_control_issue(title="unsafe token")


def _wait_for_socket(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"socket did not appear: {path}")
