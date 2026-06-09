"""Tests for the git-backed knowledge backup, credential porting, and related hardening."""
from __future__ import annotations

import builtins
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pytest import CaptureFixture, MonkeyPatch, raises

from clawie.auth_sources import (
    extract_picoclaw_credentials,
    extract_provider_auth_profiles,
)
from clawie.cli import main
from clawie.service import AgentNotFoundError, SetupError, ZeroClawService
from clawie.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


def make_service(tmp_path: Path, provider: str = "openclaw", api_key: str = "") -> ZeroClawService:
    service = ZeroClawService(StateStore(config_dir=tmp_path / "clawie"))
    service.setup(
        provider=provider,
        api_key=api_key,
        auth_mode="api_key" if api_key else None,
        subscription="starter",
        workspace="default",
        api_url=f"https://api.{provider}.example/v1",
    )
    return service


def make_agent(service: ZeroClawService, agent_id: str = "alice", provider: str | None = None) -> dict[str, Any]:
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


def test_backup_init_requires_git(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    service = make_service(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with raises(SetupError, match="git is required"):
        service.backup_init(tmp_path / "repo")


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
    assert "backup_last_run_at" not in snapshot["config"]
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
    (workspace / "huge.md").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    (home / ".openclaw" / "auth-profiles.json").write_text("{}", encoding="utf-8")
    (workspace / "auth-link.md").symlink_to(home / ".openclaw" / "auth-profiles.json")
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: home)

    repo = tmp_path / "repo"
    service.backup_init(repo)
    result = service.backup_run()
    assert result["changed"] is True

    backed = repo / "agents" / "alice" / "workspace"
    assert (backed / "NOTES.md").exists()
    assert (backed / "memory" / "2026-06-09").exists()
    assert not (backed / "data.json").exists()
    assert not (backed / "my-token.md").exists()
    assert not (backed / "huge.md").exists()
    assert not (backed / "auth-link.md").exists()
    # The provider auth store sits outside the workspace and must never appear.
    committed = git_output(repo, "ls-files")
    assert "auth-profiles.json" not in committed


def test_backup_run_push_failure_is_nonfatal(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    repo = tmp_path / "repo"
    service.backup_init(repo, remote=str(tmp_path / "missing-remote.git"))
    result = service.backup_run()
    assert result["changed"] is True
    assert result["pushed"] is False
    assert result["push_error"]


def test_backup_run_pushes_to_local_remote(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    repo = tmp_path / "repo"
    service.backup_init(repo, remote=str(remote))
    result = service.backup_run()
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
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: home)

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
    monkeypatch.setattr(ZeroClawService, "_agent_linux_home", lambda self, _agent: home)

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


def test_backup_restore_skips_agents_missing_from_state(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    make_agent(service, agent_id="bob")
    service.backup_init(tmp_path / "repo")
    service.backup_run()

    service.delete_agent("bob")
    result = service.backup_restore()
    assert "alice" in result["restored"]
    assert any(row["agent_id"] == "bob" for row in result["skipped"])


def test_backup_restore_requires_backup_data(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with raises(SetupError, match="backup repo has no agents"):
        service.backup_restore()


# ── maintenance integration ─────────────────────────────────────────────────


def test_maintenance_run_includes_backup_when_enabled(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Keep the auth-refresh phase inside the tmp dir even when running as root.
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
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
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    make_agent(service)
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice"
    state["agents"]["alice"]["credential_sync"] = {"bundles": ["provider-auth"]}
    service.store.write_state(state)

    def fake_sync(self: ZeroClawService, agent_id: str, **_kwargs: object) -> dict[str, object]:
        inner = self.store.read_state()
        inner["agents"][agent_id].setdefault("credential_sync", {})["last_synced_at"] = "MARKER"
        self.store.write_state(inner)
        return {"agent_id": agent_id, "bundles": ["provider-auth"], "copied_paths": []}

    monkeypatch.setattr(ZeroClawService, "sync_agent_credentials", fake_sync)
    monkeypatch.setattr(
        ZeroClawService,
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

    other = ZeroClawService(StateStore(config_dir=tmp_path / "other"))
    other.import_state(target)
    assert "alice" in other.store.read_state()["agents"]
    assert other.store.read_config()["api_key"] == "zc_live_9999"


def test_export_import_merge_keeps_existing_agents(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    make_agent(service)
    target = service.export_state(tmp_path / "snap.json")

    other = make_service(tmp_path / "second")
    make_agent(other, agent_id="bob")
    other.import_state(target, merge=True)
    agents = other.store.read_state()["agents"]
    assert {"alice", "bob"} <= set(agents)


# ── auth porting between claws ──────────────────────────────────────────────


def seed_openclaw_shared_auth(service: ZeroClawService, source_home: Path) -> None:
    (source_home / ".codex").mkdir(parents=True)
    (source_home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "acc-tok",
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
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", shared)
    service = make_service(tmp_path)
    seed_openclaw_shared_auth(service, tmp_path / "source-home")

    result = service.port_shared_auth("openclaw", "picoclaw")
    assert result["profiles"] == ["openai-codex:default"]

    native = json.loads((shared / ".picoclaw" / "auth.json").read_text(encoding="utf-8"))
    assert native["credentials"]["openai"]["access_token"] == "acc-tok"
    assert native["credentials"]["openai"]["refresh_token"] == "ref-tok"
    profiles = json.loads((shared / ".picoclaw" / "auth-profiles.json").read_text(encoding="utf-8"))
    assert profiles["active_profiles"]["openai-codex"] == "openai-codex:default"
    assert profiles["profiles"]["openai-codex:default"]["access_token"] == "acc-tok"

    events = service.store.read_state()["events"]
    assert any(event["type"] == "auth.ported" for event in events)


def test_auth_port_picoclaw_to_openclaw(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    shared = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", shared)
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
    profiles = json.loads((shared / ".openclaw" / "auth-profiles.json").read_text(encoding="utf-8"))
    profile = profiles["profiles"]["anthropic-claude:default"]
    assert profile["access_token"] == "claude-tok"
    assert profile["refresh_token"] == "claude-ref"
    assert profiles["active_profiles"]["anthropic-claude"] == "anthropic-claude:default"


def test_auth_port_same_provider_rejected(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    with raises(ValueError, match="must differ"):
        service.port_shared_auth("openclaw", "openclaw")


def test_auth_port_without_source_sessions_errors(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", tmp_path / "shared")
    service = make_service(tmp_path)
    with raises(SetupError, match="no shared zeroclaw auth sessions"):
        service.port_shared_auth("zeroclaw", "openclaw")


def test_auth_port_cli(tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch) -> None:
    shared = tmp_path / "shared-provider-auth"
    monkeypatch.setattr(ZeroClawService, "SHARED_PROVIDER_AUTH_DIR", shared)
    config_dir = tmp_path / "clawie"
    assert run_cli(config_dir, "config", "set") == 0
    service = ZeroClawService(StateStore(config_dir=config_dir))
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
    output = capsys.readouterr().out
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
    output = capsys.readouterr().out
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
