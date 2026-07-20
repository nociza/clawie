"""Tests for the git-backed knowledge backup, credential porting, and related hardening."""
from __future__ import annotations

import base64
import builtins
import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any

from pytest import CaptureFixture, MonkeyPatch, raises

from clawie.auth_sources import (
    extract_picoclaw_credentials,
    extract_provider_auth_profiles,
)
from clawie.cli import main
from clawie.manifest import AgentManifest
from clawie.service import AgentNotFoundError, SetupError, ClawieService
from clawie.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


def _fake_jwt(payload: dict[str, object]) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode("utf-8")).decode("utf-8").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{header}.{body}.sig"


def _read_openclaw_native_profiles(home: Path) -> dict[str, Any]:
    db_path = home / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT store_json FROM auth_profile_store WHERE store_key = ?",
            ("primary",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(str(row[0]))["profiles"]


def make_service(tmp_path: Path, provider: str = "openclaw", api_key: str = "") -> ClawieService:
    service = ClawieService(StateStore(config_dir=tmp_path / "clawie"))
    service.setup(
        provider=provider,
        api_key=api_key,
        auth_mode="api_key" if api_key else None,
        subscription="starter",
        workspace="default",
        api_url=f"https://api.{provider}.example/v1",
    )
    return service


def make_agent(service: ClawieService, agent_id: str = "alice", provider: str | None = None) -> dict[str, Any]:
    return service.create_agent(
        agent_id=agent_id,
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
        provider=provider,
    )


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ── backup init ─────────────────────────────────────────────────────────────


def test_backup_status_reports_uninitialized(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "backup", "status")
    output = capsys.readouterr().out
    assert code == 1
    assert "initialized: False" in output
    assert "backup init" in output


def test_backup_init_creates_repo_and_enables(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo = tmp_path / "repo"
    result = service.backup_init(repo, remote="https://example.invalid/backup.git")

    assert result["created"] is True
    assert (repo / ".git").is_dir()
    assert (repo / "README.md").exists()
    assert "auth-profiles.json" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert git_output(repo, "remote", "get-url", "origin") == "https://example.invalid/backup.git"

    config = service.store.read_config()
    assert config["backup_enabled"] is True
    assert config["backup_repo_path"] == str(repo)
    assert config["backup_remote"] == "https://example.invalid/backup.git"

    # Re-init with a new remote updates origin instead of failing.
    again = service.backup_init(repo, remote="https://example.invalid/other.git")
    assert again["created"] is False
    assert git_output(repo, "remote", "get-url", "origin") == "https://example.invalid/other.git"


def test_backup_init_no_auto_keeps_disabled(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.backup_init(tmp_path / "repo", enable=False)
    assert service.store.read_config()["backup_enabled"] is False


def test_backup_init_auto_push_requires_explicit_opt_in(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo = tmp_path / "repo"

    enabled = service.backup_init(repo, auto_push=True)
    preserved = service.backup_init(repo)
    disabled = service.backup_init(repo, auto_push=False)

    assert enabled["auto_push"] is True
    assert preserved["auto_push"] is True
    assert disabled["auto_push"] is False
    assert service.store.read_config()["backup_auto_push"] is False


def test_backup_init_requires_git(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = make_service(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with raises(SetupError, match="git is required"):
        service.backup_init(tmp_path / "repo")


def test_backup_git_drops_root_privileges_and_disables_hooks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    captured: list[str] = []

    class OwnedRepo:
        def __str__(self) -> str:
            return str(tmp_path / "repo")

        @staticmethod
        def stat() -> object:
            return type("Stat", (), {"st_uid": 12345})()

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> Result:
        captured.extend(command)
        return Result()

    monkeypatch.setattr("clawie._service_backup.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "clawie._service_backup.pwd.getpwuid",
        lambda _uid: type("User", (), {"pw_name": "manager"})(),
    )
    monkeypatch.setattr("clawie._service_backup.subprocess.run", fake_run)

    service._run_backup_git(OwnedRepo(), "status")  # type: ignore[arg-type]

    assert captured[:6] == ["sudo", "-u", "manager", "-H", "--", "git"]
    assert "core.hooksPath=/dev/null" in captured
    assert "protocol.ext.allow=never" in captured


def test_backup_init_refuses_unowned_nonempty_directory(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo = tmp_path / "existing"
    repo.mkdir()
    important = repo / "important.txt"
    important.write_text("do not delete", encoding="utf-8")

    with raises(SetupError, match="refusing to adopt non-empty directory"):
        service.backup_init(repo)

    assert important.read_text(encoding="utf-8") == "do not delete"
    assert not (repo / ".git").exists()


def test_backup_init_rejects_remote_with_embedded_secret(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with raises(SetupError, match="embedded credentials"):
        service.backup_init(
            tmp_path / "repo",
            remote="https://oauth2:supersecret@example.com/fleet.git",
        )
    assert not (tmp_path / "repo").exists()


# ── backup run ──────────────────────────────────────────────────────────────


def test_backup_run_commits_redacted_snapshot_and_prompts(tmp_path: Path) -> None:
    service = make_service(tmp_path, api_key="zc_live_supersecret_1234")
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["channels"] = [
        {"kind": "telegram", "name": "123456:" + "a" * 35, "enabled": True}
    ]
    service.store.write_state(state)
    config = service.store.read_config()
    config["spawn_password_hash"] = "$6$saltsalt$hashhashhash"
    service.store.write_config(config)

    repo = tmp_path / "repo"
    service.backup_init(repo)
    result = service.backup_run()

    assert result["changed"] is True
    assert result["commit"]
    assert result["agents"] == ["alice"]

    raw = (repo / "state" / "snapshot.json").read_text(encoding="utf-8")
    assert "zc_live_supersecret_1234" not in raw
    assert "$6$saltsalt$hashhashhash" not in raw
    assert "123456:" + "a" * 35 not in raw
    snapshot = json.loads(raw)
    assert "events" not in snapshot["state"]
    assert "users" not in snapshot["state"]
    assert "backup_last_run_at" not in snapshot["config"]
    manifest = AgentManifest.read(repo / "agents" / "alice" / "manifest.json")
    assert manifest.id == "alice"
    assert manifest.provider == "openclaw"
    assert manifest.channels == []
    assert (repo / "agents" / "alice" / "prompts" / "SOUL.md").exists()

    # No knowledge changed: the second run must not create a commit.
    again = service.backup_run()
    assert again["changed"] is False
    assert again["commit"] == result["commit"]

    # A knowledge change is picked up by the next run.
    service.set_agent_core_prompt("alice", "MEMORY.md", "remember the milk", sync_to_disk=False)
    third = service.backup_run()
    assert third["changed"] is True
    assert third["commit"] != result["commit"]
    assert (
        repo / "agents" / "alice" / "prompts" / "MEMORY.md"
    ).read_text(encoding="utf-8") == "remember the milk"


def test_backup_run_auto_initializes_default_repo(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    result = service.backup_run()
    repo = Path(result["repo"])
    assert repo == service.store.root / "backup"
    assert (repo / ".git").is_dir()
    assert result["changed"] is True
    # Auto-init must not silently flip on automatic backups.
    assert service.store.read_config()["backup_enabled"] is False


def test_backup_run_stages_only_clawie_managed_paths(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo = tmp_path / "repo"
    service.backup_init(repo)
    first = service.backup_run()
    unrelated = repo / "operator-notes.txt"
    unrelated.write_text("private operator notes", encoding="utf-8")

    second = service.backup_run()

    assert first["changed"] is True
    assert second["changed"] is False
    assert "operator-notes.txt" not in git_output(repo, "ls-files")
    assert unrelated.read_text(encoding="utf-8") == "private operator notes"


def test_backup_run_skips_prompt_with_secret_material(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    service.set_agent_core_prompt(
        "alice",
        "MEMORY.md",
        "Temporary credential: api_key=sk-sensitive-production-value-123456",
        sync_to_disk=False,
    )
    repo = tmp_path / "repo"
    service.backup_init(repo)

    result = service.backup_run()

    assert not (repo / "agents" / "alice" / "prompts" / "MEMORY.md").exists()
    assert any("contains secret-like content" in row["reason"] for row in result["skipped"])
    assert "sk-sensitive-production-value" not in git_output(repo, "show", "HEAD")


def test_backup_run_collects_workspace_knowledge_and_skips_secrets(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    service.store.write_state(state)

    home = tmp_path / "alice-home"
    workspace = home / ".openclaw" / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "NOTES.md").write_text("project notes", encoding="utf-8")
    (workspace / "memory" / "2026-06-09").write_text("met bob", encoding="utf-8")
    (workspace / "data.json").write_text("{}", encoding="utf-8")  # not a knowledge suffix
    (workspace / "my-token.md").write_text("secret-ish", encoding="utf-8")
    (workspace / "innocent-name.md").write_text(
        "API_KEY=sk-this-value-must-never-be-committed", encoding="utf-8"
    )
    (home / ".openclaw" / "auth-profiles.json").write_text("{}", encoding="utf-8")
    (workspace / "auth-link.md").symlink_to(home / ".openclaw" / "auth-profiles.json")
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)

    repo = tmp_path / "repo"
    service.backup_init(repo)
    result = service.backup_run()
    assert result["changed"] is True

    backed = repo / "agents" / "alice" / "workspace"
    assert (backed / "NOTES.md").exists()
    assert (backed / "memory" / "2026-06-09").exists()
    assert not (backed / "data.json").exists()
    assert not (backed / "my-token.md").exists()
    assert not (backed / "innocent-name.md").exists()
    assert not (backed / "auth-link.md").exists()
    # The provider auth store sits outside the workspace and must never appear.
    committed = git_output(repo, "ls-files")
    assert "auth-profiles.json" not in committed


def test_backup_run_preserves_last_complete_snapshot_when_collection_is_incomplete(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    service.store.write_state(state)
    home = tmp_path / "alice-home"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    notes = workspace / "NOTES.md"
    notes.write_text("last complete notes", encoding="utf-8")
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)

    repo = tmp_path / "repo"
    service.backup_init(repo)
    complete = service.backup_run()
    head = complete["commit"]
    notes.write_text("new notes from an incomplete pass", encoding="utf-8")
    (workspace / "huge.md").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")

    degraded = service.backup_run()

    assert degraded["status"] == "degraded"
    assert degraded["changed"] is False
    assert degraded["commit"] == head
    assert degraded["incomplete"]
    assert git_output(repo, "rev-parse", "HEAD") == head
    assert (repo / "agents" / "alice" / "workspace" / "NOTES.md").read_text(
        encoding="utf-8"
    ) == "last complete notes"
    assert service.backup_settings()["last_status"] == "degraded"


def test_backup_run_recovers_an_interrupted_tree_swap_before_collecting(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    repo = tmp_path / "repo"
    service.backup_init(repo)
    first = service.backup_run()
    transaction = "c" * 32
    previous_state = repo / f".clawie-backup-previous-{transaction}-state"
    (repo / "state").rename(previous_state)
    (repo / "state").mkdir()
    (repo / "state" / "partial.txt").write_text("incomplete", encoding="utf-8")
    abandoned_stage = repo / ".clawie-backup-stage-abandoned"
    abandoned_stage.mkdir()
    (repo / ".clawie-backup-transaction.json").write_text(
        json.dumps(
            {
                "transaction": transaction,
                "phase": "installing",
                "existed": {"state": True, "agents": True},
            }
        ),
        encoding="utf-8",
    )

    status = service.backup_status()
    assert status["interrupted_transaction"] is True
    assert "interrupted" in status["validation_error"]
    with raises(SetupError, match="interrupted snapshot transaction"):
        service.backup_restore(apply_to_disk=False)

    recovered = service.backup_run()

    assert recovered["status"] == "completed"
    assert recovered["commit"] == first["commit"]
    assert (repo / "state" / "snapshot.json").is_file()
    assert not (repo / "state" / "partial.txt").exists()
    assert not (repo / ".clawie-backup-transaction.json").exists()
    assert not previous_state.exists()
    assert not abandoned_stage.exists()


def test_backup_recovery_rejects_a_tampered_previous_tree(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    repo = tmp_path / "repo"
    service.backup_init(repo)
    service.backup_run()
    transaction = "d" * 32
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    (repo / f".clawie-backup-previous-{transaction}-state").symlink_to(
        victim,
        target_is_directory=True,
    )
    (repo / ".clawie-backup-transaction.json").write_text(
        json.dumps(
            {
                "transaction": transaction,
                "phase": "installed",
                "existed": {"state": True, "agents": True},
            }
        ),
        encoding="utf-8",
    )

    with raises(SetupError, match="backup recovery path is unsafe"):
        service.backup_run()

    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (repo / ".clawie-backup-transaction.json").is_file()


def test_backup_run_records_unexpected_collection_failure_without_replacing_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    repo = tmp_path / "repo"
    service.backup_init(repo)
    complete = service.backup_run()
    snapshot = (repo / "state" / "snapshot.json").read_bytes()
    monkeypatch.setattr(
        service,
        "_write_backup_tree",
        lambda _repo: (_ for _ in ()).throw(OSError("disk read failed")),
    )

    with raises(OSError, match="disk read failed"):
        service.backup_run()

    assert git_output(repo, "rev-parse", "HEAD") == complete["commit"]
    assert (repo / "state" / "snapshot.json").read_bytes() == snapshot
    settings = service.backup_settings()
    assert settings["last_status"] == "failed"
    assert "disk read failed" in settings["last_error"]
    assert service.list_events(limit=1)[0]["type"] == "backup.failed"


def test_concurrent_backup_runs_are_serialized(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    first = make_service(tmp_path)
    make_agent(first)
    repo = tmp_path / "repo"
    first.backup_init(repo)
    second = ClawieService(StateStore(config_dir=first.store.root))
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    first_write = first._write_backup_tree
    second_write = second._write_backup_tree

    def delayed_first(repo_path: Path) -> dict[str, Any]:
        first_entered.set()
        if not release_first.wait(timeout=5):
            raise TimeoutError("test did not release the first backup")
        return first_write(repo_path)

    def observed_second(repo_path: Path) -> dict[str, Any]:
        second_entered.set()
        return second_write(repo_path)

    monkeypatch.setattr(first, "_write_backup_tree", delayed_first)
    monkeypatch.setattr(second, "_write_backup_tree", observed_second)

    def run(service: ClawieService) -> None:
        try:
            results.append(service.backup_run())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below.
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(first,))
    second_thread = threading.Thread(target=run, args=(second,))
    first_thread.start()
    assert first_entered.wait(timeout=2)
    second_thread.start()
    assert not second_entered.wait(timeout=0.2)
    release_first.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors
    assert second_entered.is_set()
    assert [row["status"] for row in results] == ["completed", "completed"]


def test_backup_secret_scanner_catches_common_cloud_and_registry_tokens() -> None:
    samples = (
        b"AWS key: AKIAIOSFODNN7EXAMPLE",
        b"AWS_SECRET_ACCESS_KEY=" + (b"a" * 40),
        b"Google key: AIzaSyD-ExampleKeyMaterial123456789",
        b"npm token: npm_abcdefghijklmnopqrstuvwxyz1234567890",
        b"GitLab token: glpat-abcdefghijklmnopqrstuvwxyz1234",
    )
    assert all(ClawieService._contains_secret_material(sample) for sample in samples)


def test_backup_remote_push_is_opt_in(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    service.backup_init(tmp_path / "repo", remote=str(remote))

    result = service.backup_run()

    assert result["pushed"] is False
    assert service.backup_settings()["auto_push"] is False


def test_backup_run_push_failure_is_reported_as_degraded(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo = tmp_path / "repo"
    service.backup_init(repo, remote=str(tmp_path / "missing-remote.git"))
    result = service.backup_run(push=True)
    assert result["changed"] is True
    assert result["pushed"] is False
    assert result["push_error"]
    assert result["status"] == "degraded"
    assert service.backup_settings()["last_status"] == "degraded"


def test_backup_cli_returns_nonzero_when_explicit_push_fails(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    service = make_service(tmp_path)
    service.backup_init(tmp_path / "repo", remote=str(tmp_path / "missing-remote.git"))

    code = run_cli(tmp_path / "clawie", "backup", "run", "--push")
    captured = capsys.readouterr()

    assert code == 1
    assert "remote durability was not achieved" in captured.out


def test_backup_run_pushes_to_local_remote(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    repo = tmp_path / "repo"
    service.backup_init(repo, remote=str(remote))
    result = service.backup_run(push=True)
    assert result["pushed"] is True
    assert git_output(remote, "rev-parse", "HEAD") == result["commit"]

    # --no-push (push=False) skips the remote entirely.
    config = service.store.read_config()
    config["workspace"] = "renamed"
    service.store.write_config(config)
    offline = service.backup_run(push=False)
    assert offline["changed"] is True
    assert offline["pushed"] is False
    assert offline["push_error"] == ""


# ── backup restore ──────────────────────────────────────────────────────────


def test_backup_restore_prompts_and_workspace(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    service.store.write_state(state)

    home = tmp_path / "alice-home"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "NOTES.md").write_text("important notes", encoding="utf-8")
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)

    repo = tmp_path / "repo"
    service.backup_init(repo)
    service.backup_run()

    # Simulate knowledge loss: corrupt the state prompt and delete the note.
    service.set_agent_core_prompt("alice", "SOUL.md", "CORRUPTED", sync_to_disk=False)
    (workspace / "NOTES.md").unlink()

    result = service.backup_restore("alice")
    assert result["restored"]["alice"]["prompts"] >= 1
    assert result["restored"]["alice"]["workspace_files"] == 1
    assert (workspace / "NOTES.md").read_text(encoding="utf-8") == "important notes"
    restored = service.get_agent_core_prompt("alice", "SOUL.md")
    assert "CORRUPTED" not in restored["content"]
    # apply_to_disk also rewrites prompt files into the workspace
    assert (workspace / "SOUL.md").exists()


def test_backup_restore_live_workspace_knowledge_wins(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Regression: a workspace file that shadows a core prompt (e.g. a
    self-edited MEMORY.md) must not be clobbered by the control-plane copy."""
    service = make_service(tmp_path)
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    service.store.write_state(state)

    home = tmp_path / "alice-home"
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("learned: deploys happen on fridays", encoding="utf-8")
    monkeypatch.setattr(ClawieService, "_agent_linux_home", lambda self, _agent: home)

    service.backup_init(tmp_path / "repo")
    service.backup_run()

    shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    service.backup_restore("alice")
    assert (
        workspace / "MEMORY.md"
    ).read_text(encoding="utf-8") == "learned: deploys happen on fridays"


def test_backup_restore_unknown_agent_errors(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    service.backup_init(tmp_path / "repo")
    service.backup_run()
    with raises(AgentNotFoundError, match="not found in backup repo"):
        service.backup_restore("ghost")


def test_backup_restore_recreates_agents_from_manifest(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    make_agent(service, agent_id="bob")
    state = service.store.read_state()
    state["agents"]["bob"]["channels"] = [{"kind": "chat", "name": "ops", "enabled": True}]
    state["agents"]["bob"]["credential_sync"] = {"bundles": ["git"], "shared_provider_auth": False}
    state["agents"]["bob"]["agent"]["model_tier"] = "fast"
    service.store.write_state(state)
    service.set_agent_core_prompt("bob", "MEMORY.md", "bob knows the restore path", sync_to_disk=False)

    service.backup_init(tmp_path / "repo")
    service.backup_run()

    service.delete_agent("bob")
    result = service.backup_restore(apply_to_disk=False)
    assert "alice" in result["restored"]
    assert result["restored"]["bob"]["prompts"] >= 1
    assert not any(row["agent_id"] == "bob" for row in result["skipped"])

    restored = service.get_agent("bob")
    assert restored["agent"]["provider"] == "openclaw"
    assert restored["agent"]["model_tier"] == "fast"
    assert restored["channels"] == [
        {"kind": "chat", "name": "ops", "enabled": True, "external_id": "bob:chat:1"}
    ]
    assert restored["credential_sync"]["bundles"] == ["git"]
    assert service.get_agent_core_prompt("bob", "MEMORY.md")["content"] == "bob knows the restore path"
    assert (service.agent_manifest_path("bob")).is_file()


def test_backup_restore_skips_legacy_agents_missing_from_state(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    make_agent(service, agent_id="bob")
    repo = tmp_path / "repo"
    service.backup_init(repo)
    service.backup_run()

    (repo / "agents" / "bob" / "manifest.json").unlink()
    service.delete_agent("bob")
    result = service.backup_restore()
    assert "alice" in result["restored"]
    assert any(
        row["agent_id"] == "bob" and row["reason"] == "not in local state and backup has no manifest"
        for row in result["skipped"]
    )


def test_backup_restore_requires_backup_data(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with raises(SetupError, match="backup repository does not exist"):
        service.backup_restore()


def test_backup_restore_rejects_path_traversal_agent_id(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.backup_init(tmp_path / "repo")
    service.backup_run()
    with raises(ValueError, match="unsafe"):
        service.backup_restore("../outside")


# ── maintenance integration ─────────────────────────────────────────────────


def test_maintenance_run_includes_backup_when_enabled(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Keep the auth-refresh phase inside the tmp dir even when running as root.
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    service.backup_init(tmp_path / "repo")

    result = service.maintenance_run()
    assert result["backup"].startswith("ok")
    repo = tmp_path / "repo"
    assert (repo / "state" / "snapshot.json").exists()

    # When disabled, maintenance reports it and leaves the repo alone.
    config = service.store.read_config()
    config["backup_enabled"] = False
    service.store.write_config(config)
    head_before = git_output(repo, "rev-parse", "HEAD")
    result = service.maintenance_run()
    assert result["backup"] == "disabled"
    assert git_output(repo, "rev-parse", "HEAD") == head_before


def test_maintenance_run_preserves_inner_state_writes(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Regression: the final maintenance event must not clobber state written
    by per-agent operations during the run."""
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    state["agents"]["alice"]["credential_sync"] = {"bundles": ["provider-auth"]}
    service.store.write_state(state)

    def fake_sync(self: ClawieService, agent_id: str, **_kwargs: object) -> dict[str, object]:
        inner = self.store.read_state()
        inner["agents"][agent_id].setdefault("credential_sync", {})["last_synced_at"] = "MARKER"
        self.store.write_state(inner)
        return {"agent_id": agent_id, "bundles": ["provider-auth"], "copied_paths": []}

    monkeypatch.setattr(ClawieService, "sync_agent_credentials", fake_sync)
    monkeypatch.setattr(
        ClawieService,
        "apply_staged_prompts",
        lambda self, agent_id: {"agent_id": agent_id, "applied": [], "remaining": []},
    )

    result = service.maintenance_run()
    assert result["results"]["alice"]["credentials"] == "ok"
    final = service.store.read_state()
    assert final["agents"]["alice"]["credential_sync"]["last_synced_at"] == "MARKER"
    # The summary event itself must also be present.
    assert any(event["type"] == "maintenance.run" for event in final["events"])


def test_status_snapshot_includes_backup_section(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    snapshot = service.status_snapshot()
    assert "backup" in snapshot
    assert snapshot["backup"]["initialized"] is False

    snapshot = service.status_snapshot(sections=["backup"])
    assert set(snapshot) == {"generated_at", "backup"}


def test_status_backup_section_via_cli(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "status", "backup", "--json")
    output = capsys.readouterr().out
    assert code == 0
    payload = json.loads(output)
    assert "backup" in payload


# ── export/import snapshots ─────────────────────────────────────────────────


def test_export_import_roundtrip(tmp_path: Path) -> None:
    service = make_service(tmp_path, api_key="zc_live_9999")
    make_agent(service)
    target = service.export_state(tmp_path / "snap.json")
    assert (target.stat().st_mode & 0o777) == 0o600

    other = ClawieService(StateStore(config_dir=tmp_path / "other"))
    other.import_state(target)
    assert "alice" in other.store.read_state()["agents"]
    assert other.store.read_config()["api_key"] == "zc_live_9999"


def test_export_state_refuses_symlink_target(tmp_path: Path) -> None:
    service = make_service(tmp_path / "source", api_key="super-secret-api-key")
    victim = tmp_path / "victim.txt"
    victim.write_text("leave me alone\n", encoding="utf-8")
    target = tmp_path / "snapshot.json"
    target.symlink_to(victim)

    with raises(PermissionError, match="symlink|special file"):
        service.export_state(target)

    assert victim.read_text(encoding="utf-8") == "leave me alone\n"


def test_export_refuses_snapshot_that_cannot_be_imported_under_size_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    service = make_service(tmp_path / "source")
    target = tmp_path / "missing-parent" / "snapshot.json"
    monkeypatch.setattr("clawie.service._MAX_STATE_SNAPSHOT_BYTES", 128)

    with raises(ValueError, match="round-trip import limit"):
        service.export_state(target)

    assert not target.parent.exists()


def test_export_import_merge_keeps_existing_agents(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    target = service.export_state(tmp_path / "snap.json")

    other = make_service(tmp_path / "second")
    make_agent(other, agent_id="bob")
    other.import_state(target, merge=True)
    agents = other.store.read_state()["agents"]
    assert {"alice", "bob"} <= set(agents)


def test_import_rejects_malformed_snapshot_before_changing_live_state(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    before_config = dict(service.store.read_config())
    before_state = dict(service.store.read_state())
    snapshot = tmp_path / "malformed.json"
    snapshot.write_text(
        json.dumps(
            {
                "config": {**before_config, "workspace": "must-not-apply"},
                "state": {
                    "templates": before_state["templates"],
                    "agents": before_state["agents"],
                    "events": [{"type": "bad", "context": ["not", "an", "object"]}],
                },
            }
        ),
        encoding="utf-8",
    )

    with raises(ValueError, match="events"):
        service.import_state(snapshot)

    assert service.store.read_config()["workspace"] == before_config["workspace"]
    assert service.store.read_state()["agents"] == before_state["agents"]


def test_import_rejects_an_unbounded_event_history(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    before = service.store.read_state()
    snapshot = tmp_path / "too-many-events.json"
    events = [
        {"timestamp": "", "type": "audit", "message": "", "context": {}}
        for _ in range(service.EVENT_LIMIT + 1)
    ]
    snapshot.write_text(
        json.dumps(
            {
                "config": dict(service.store.read_config()),
                "state": {
                    "templates": before["templates"],
                    "agents": before["agents"],
                    "events": events,
                },
            }
        ),
        encoding="utf-8",
    )

    with raises(ValueError, match="maximum"):
        service.import_state(snapshot)

    assert service.store.read_state()["events"] == before["events"]


def test_import_refuses_to_orphan_an_existing_managed_runtime(tmp_path: Path) -> None:
    service = make_service(tmp_path / "live")
    agent = make_agent(service, "worker")
    agent["agent"].update(
        {
            "linux_user": "clawie-worker",
            "linux_home": "/home/clawie-worker",
            "linux_user_managed": True,
            "managed_user_operation_id": "d" * 32,
        }
    )
    state = service.store.read_state()
    state["agents"]["worker"] = agent
    service.store.write_state(state)
    source = make_service(tmp_path / "source")
    snapshot = source.export_state(tmp_path / "replacement.json")

    with raises(SetupError, match="would orphan or remap managed runtime"):
        service.import_state(snapshot)

    preserved = service.store.read_state()["agents"]["worker"]["agent"]
    assert preserved["linux_user"] == "clawie-worker"
    assert preserved["managed_user_operation_id"] == "d" * 32


def test_import_refuses_to_introduce_an_unprovisioned_linux_runtime(tmp_path: Path) -> None:
    source = make_service(tmp_path / "source")
    agent = make_agent(source, "worker")
    agent["agent"].update(
        {
            "linux_user": "root",
            "linux_uid": 0,
            "linux_home": "/root",
            "linux_user_managed": True,
            "managed_user_operation_id": "e" * 32,
        }
    )
    state = source.store.read_state()
    state["agents"]["worker"] = agent
    source.store.write_state(state)
    snapshot = source.export_state(tmp_path / "unprovisioned-runtime.json")
    destination = make_service(tmp_path / "destination")

    with raises(SetupError, match="without local provisioning proof"):
        destination.import_state(snapshot)

    assert "worker" not in destination.store.read_state()["agents"]


def test_cli_import_requires_confirmation_before_replacement(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    source = make_service(tmp_path / "source")
    make_agent(source, "alice")
    snapshot = source.export_state(tmp_path / "snapshot.json")
    destination = make_service(tmp_path / "destination")
    make_agent(destination, "bob")

    monkeypatch.setattr(
        builtins,
        "input",
        lambda _prompt="": (_ for _ in ()).throw(EOFError()),
    )
    code = run_cli(tmp_path / "destination" / "clawie", "backup", "import", str(snapshot))
    captured = capsys.readouterr()

    assert code == 1
    assert "use --yes" in captured.err
    assert "bob" in destination.store.read_state()["agents"]


# ── auth porting between claws ──────────────────────────────────────────────


def seed_openclaw_shared_auth(service: ClawieService, source_home: Path) -> None:
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _fake_jwt({"exp": 1893456000}),
                    "refresh_token": "ref-tok",
                    "id_token": "",
                    "account_id": "acct-9",
                },
                "last_refresh": "2026-06-01T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    service.import_shared_auth("openclaw", source="codex", source_home=source_home)


def test_auth_port_openclaw_to_picoclaw(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    shared = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared)
    service = make_service(tmp_path)
    seed_openclaw_shared_auth(service, tmp_path / "source-home")

    result = service.port_shared_auth("openclaw", "picoclaw")
    assert result["profiles"] == ["openai:default"]

    native = json.loads((shared / ".picoclaw" / "auth.json").read_text(encoding="utf-8"))
    assert native["credentials"]["openai"]["access_token"] == _fake_jwt({"exp": 1893456000})
    assert native["credentials"]["openai"]["refresh_token"] == "ref-tok"
    profiles = json.loads((shared / ".picoclaw" / "auth-profiles.json").read_text(encoding="utf-8"))
    assert profiles["active_profiles"]["openai"] == "openai:default"
    assert profiles["profiles"]["openai:default"]["access_token"] == _fake_jwt({"exp": 1893456000})

    events = service.store.read_state()["events"]
    assert any(event["type"] == "auth.ported" for event in events)


def test_auth_port_picoclaw_to_openclaw(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    shared = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared)
    service = make_service(tmp_path, provider="picoclaw")
    (shared / ".picoclaw").mkdir(parents=True)
    (shared / ".picoclaw" / "auth.json").write_text(
        json.dumps(
            {
                "credentials": {
                    "anthropic": {
                        "access_token": "claude-tok",
                        "refresh_token": "claude-ref",
                        "provider": "anthropic",
                        "auth_method": "oauth",
                        "expires_at": "2026-07-01T00:00:00Z",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = service.port_shared_auth("picoclaw", "openclaw")
    assert result["profiles"] == ["anthropic-claude:default"]
    profiles = _read_openclaw_native_profiles(shared)
    profile = profiles["anthropic:default"]
    assert profile["access"] == "claude-tok"
    assert profile["refresh"] == "claude-ref"
    assert profile["provider"] == "anthropic"
    assert profile["type"] == "oauth"
    assert not (shared / ".openclaw" / "auth-profiles.json").exists()


def test_auth_port_same_provider_rejected(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    with raises(ValueError, match="must differ"):
        service.port_shared_auth("openclaw", "openclaw")


def test_auth_port_without_source_sessions_errors(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    with raises(SetupError, match="no shared zeroclaw auth sessions"):
        service.port_shared_auth("zeroclaw", "openclaw")


def test_auth_port_cli(tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch) -> None:
    shared = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ClawieService, "SHARED_PROVIDER_AUTH_DIR", shared)
    config_dir = tmp_path / "clawie"
    assert run_cli(config_dir, "config", "set") == 0
    service = ClawieService(StateStore(config_dir=config_dir))
    seed_openclaw_shared_auth(service, tmp_path / "source-home")
    capsys.readouterr()

    code = run_cli(config_dir, "auth", "port", "--from", "openclaw", "--to", "picoclaw")
    output = capsys.readouterr().out
    assert code == 0
    assert "Ported 1 auth profile(s) from openclaw to picoclaw" in output
    assert (shared / ".picoclaw" / "auth.json").exists()


def test_extract_provider_auth_profiles_normalizes_aliases() -> None:
    payload = {
        "version": 1,
        "active_profiles": {"openai-codex": "openai-codex:work"},
        "profiles": {
            "openai-codex:work": {
                "provider": "openai-codex",
                "profile_name": "work",
                "access": "tok-work",
                "refresh": "ref-work",
                "accountId": "acct-1",
                "expires": 1781000000000,
            },
            "openai-codex:old": {
                "provider": "openai-codex",
                "access_token": "tok-old",
            },
            "broken": {"provider": "x"},  # no tokens: skipped
        },
    }
    rows = extract_provider_auth_profiles(payload)
    assert [row["profile_id"] for row in rows] == ["openai-codex:old", "openai-codex:work"]
    active = rows[-1]
    assert active["access_token"] == "tok-work"
    assert active["refresh_token"] == "ref-work"
    assert active["account_id"] == "acct-1"
    assert active["expires_at"].endswith("Z")


def test_extract_picoclaw_credentials_maps_upstream_providers() -> None:
    payload = {
        "credentials": {
            "openai": {"access_token": "a", "auth_method": "oauth"},
            "anthropic": {"access_token": "b", "refresh_token": "rb"},
            "mystery": {"access_token": "c"},
            "empty": {},
        }
    }
    rows = extract_picoclaw_credentials(payload)
    ids = {row["profile_id"] for row in rows}
    assert ids == {"openai-codex:default", "anthropic-claude:default"}


# ── CLI hardening ───────────────────────────────────────────────────────────


def test_agent_id_rejects_path_unsafe_values(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    service = make_service(tmp_path)
    for bad in ("../evil", "a/b", "@local:openclaw", "-dash", ".hidden", "a" * 65):
        with raises(ValueError):
            make_agent(service, agent_id=bad)
    # Dots, dashes, underscores within the name remain valid.
    agent = make_agent(service, agent_id="team.bot-v2_x")
    assert agent["agent_id"] == "team.bot-v2_x"

    capsys.readouterr()
    code = run_cli(tmp_path / "clawie", "agent", "create", "../evil")
    output = capsys.readouterr().err
    assert code == 1
    assert "agent_id must start with" in output


def test_agent_purge_without_stdin_cancels_cleanly(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    capsys.readouterr()

    def no_stdin(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", no_stdin)
    code = run_cli(tmp_path, "agent", "purge", "ghost")
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == 1
    assert "purge cancelled" in output
    assert "--yes" in output


def test_interactive_config_eof_is_clean_interrupt(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    def no_stdin(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", no_stdin)
    code = run_cli(tmp_path, "config", "set", "--interactive")
    output = capsys.readouterr().out
    assert code == 130
    assert "Interrupted" in output


def test_backup_export_cli_warns_about_secrets(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    assert run_cli(tmp_path, "config", "set") == 0
    capsys.readouterr()
    code = run_cli(tmp_path, "backup", "export", str(tmp_path / "snap.json"))
    output = capsys.readouterr().out
    assert code == 0
    assert "unredacted credentials" in output
