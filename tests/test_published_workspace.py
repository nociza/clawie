from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from clawie.cli import main
from clawie.published_workspace import PublishedWorkspace, PublishedWorkspaceError
from clawie.service import ClawieService
from clawie.store import StateStore


def test_catalog_connections_close_after_context_exit(tmp_path: Path) -> None:
    workspace = PublishedWorkspace(tmp_path / "published")
    workspace.ensure()

    with workspace._connect() as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


def test_catalog_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    victim = tmp_path / "victim.db"
    victim.write_text("keep", encoding="utf-8")
    (root / "catalog.sqlite").symlink_to(victim)
    workspace = PublishedWorkspace(root)

    with pytest.raises(PublishedWorkspaceError, match="catalog must not be a symlink"):
        workspace.ensure()

    assert victim.read_text(encoding="utf-8") == "keep"


def test_workspace_initialization_rejects_child_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    victim.chmod(0o755)
    (root / "views").symlink_to(victim)

    with pytest.raises(PublishedWorkspaceError, match="unsafe or non-directory"):
        PublishedWorkspace(root).ensure()

    assert victim.stat().st_mode & 0o777 == 0o755


def _service_with_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ClawieService, dict[str, Path]]:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="",
    )
    service.create_agent(
        agent_id="alice",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    service.create_agent(
        agent_id="bob",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    homes = {
        "alice-user": tmp_path / "alice-home",
        "bob-user": tmp_path / "bob-home",
    }
    state = service.store.read_state()
    state["agents"]["alice"]["agent"]["linux_user"] = "alice-user"
    state["agents"]["bob"]["agent"]["linux_user"] = "bob-user"
    service.store.write_state(state)
    for home in homes.values():
        (home / ".openclaw" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(service, "_linux_home_for_user", lambda user: homes.get(user))
    monkeypatch.setattr(service, "_can_manage_linux_user", lambda _user: True)
    return service, homes


def test_workspace_publish_creates_publication_and_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, homes = _service_with_agents(tmp_path, monkeypatch)
    source = homes["alice-user"] / ".openclaw" / "workspace" / "report.md"
    source.write_text("published notes\n", encoding="utf-8")

    result = service.workspace_publish(
        source,
        agent_id="alice",
        visible_to=["bob"],
        title="Research Notes",
    )

    assert result["publisher_agent_id"] == "alice"
    assert result["visible_to"] == ["alice", "bob"]
    publication = Path(result["path"])
    assert (publication / "manifest.json").is_file()
    assert (publication / "files" / "report.md").read_text(encoding="utf-8") == "published notes\n"
    bob_publications = service.workspace_list(agent_id="bob")
    assert [row["publication_id"] for row in bob_publications] == [result["publication_id"]]
    bob_mount = homes["bob-user"] / ".openclaw" / "workspace" / "published"
    assert bob_mount.is_dir()
    assert not bob_mount.is_symlink()
    assert (bob_mount / "_index.json").is_file()
    assert (bob_mount / "alice" / result["view_name"] / "files" / "report.md").read_text(
        encoding="utf-8"
    ) == "published notes\n"
    assert service.workspace_verify(result["publication_id"])["status"] == "ok"
    assert service.workspace_verify()["status"] == "ok"
    assert service._published_workspace_root().stat().st_mode & 0o777 == 0o700
    assert publication.stat().st_mode & 0o777 == 0o500
    assert (publication / "files" / "report.md").stat().st_mode & 0o777 == 0o400
    assert bob_mount.stat().st_mode & 0o777 == 0o700
    assert (bob_mount / "alice" / result["view_name"] / "files" / "report.md").stat().st_mode & 0o777 == 0o600


def test_workspace_mount_replaces_symlink_without_touching_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, homes = _service_with_agents(tmp_path, monkeypatch)
    source = homes["alice-user"] / ".openclaw" / "workspace" / "report.md"
    source.write_text("published notes\n", encoding="utf-8")
    result = service.workspace_publish(source, agent_id="alice", visible_to=["bob"])

    mount = homes["bob-user"] / ".openclaw" / "workspace" / "published"
    shutil.rmtree(mount)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched\n", encoding="utf-8")
    os.symlink(outside, mount, target_is_directory=True)

    mounted = service.workspace_mount(agent_id="bob")

    assert mounted["mounted"][0]["status"] == "materialized"
    assert mount.is_dir()
    assert not mount.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    assert (mount / "alice" / result["view_name"] / "files" / "report.md").is_file()


def test_workspace_publish_rejects_source_outside_agent_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _homes = _service_with_agents(tmp_path, monkeypatch)
    source = tmp_path / "outside.md"
    source.write_text("secret\n", encoding="utf-8")

    with pytest.raises(ValueError, match="inside the publishing agent workspace"):
        service.workspace_publish(source, agent_id="alice", visible_to=["bob"])


def test_workspace_publish_rejects_symlink_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, homes = _service_with_agents(tmp_path, monkeypatch)
    source_dir = homes["alice-user"] / ".openclaw" / "workspace" / "bundle"
    source_dir.mkdir()
    (source_dir / "notes.md").write_text("notes\n", encoding="utf-8")
    os.symlink(source_dir / "notes.md", source_dir / "alias.md")

    with pytest.raises(ValueError, match="cannot publish symlink"):
        service.workspace_publish(source_dir, agent_id="alice", visible_to=["bob"])


def test_workspace_publish_uses_captured_file_when_source_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, homes = _service_with_agents(tmp_path, monkeypatch)
    source = homes["alice-user"] / ".openclaw" / "workspace" / "report.md"
    source.write_text("safe report\n", encoding="utf-8")
    outside = tmp_path / "manager-secret"
    outside.write_text("must not leak\n", encoding="utf-8")
    original = PublishedWorkspace._ensure_blob
    swapped = False

    def swap_after_capture(self: PublishedWorkspace, captured: Path, blob: Path) -> None:
        nonlocal swapped
        if not swapped:
            source.unlink()
            source.symlink_to(outside)
            swapped = True
        original(self, captured, blob)

    monkeypatch.setattr(PublishedWorkspace, "_ensure_blob", swap_after_capture)

    result = service.workspace_publish(source, agent_id="alice", visible_to=["bob"])

    published = Path(result["path"]) / "files" / "report.md"
    assert published.read_text(encoding="utf-8") == "safe report\n"
    assert "must not leak" not in published.read_text(encoding="utf-8")


def test_workspace_publish_rejects_file_modified_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, homes = _service_with_agents(tmp_path, monkeypatch)
    source = homes["alice-user"] / ".openclaw" / "workspace" / "report.md"
    source.write_text("safe report\n", encoding="utf-8")
    source_inode = source.stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_after_source_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(fd, size)
        if chunk and not mutated and os.fstat(fd).st_ino == source_inode:
            source.write_text("evil report\n", encoding="utf-8")
            mutated = True
        return chunk

    monkeypatch.setattr("clawie.published_workspace.os.read", mutate_after_source_read)

    with pytest.raises(ValueError, match="source changed while publishing"):
        service.workspace_publish(source, agent_id="alice", visible_to=["bob"])


def test_workspace_publish_infers_default_named_runtime_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    service.setup(
        provider="openclaw",
        api_key="",
        auth_mode="none",
        subscription="starter",
        workspace="default",
        api_url="",
    )
    service.create_agent(
        agent_id="Abulafia",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    service.create_agent(
        agent_id="bob",
        display_name=None,
        template="baseline",
        clone_from=None,
        channel_strategy="new",
        channels=None,
        agent_version="1.0.0",
    )
    homes = {
        "abulafia": tmp_path / "abulafia-home",
        "bob-user": tmp_path / "bob-home",
    }
    state = service.store.read_state()
    state["agents"]["Abulafia"]["agent"]["linux_user"] = "abulafia"
    state["agents"]["bob"]["agent"]["linux_user"] = "bob-user"
    service.store.write_state(state)
    for home in homes.values():
        (home / ".openclaw" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(service, "_linux_home_for_user", lambda user: homes.get(user))
    monkeypatch.setattr(service, "_can_manage_linux_user", lambda _user: True)
    monkeypatch.setattr(service, "_current_linux_user", lambda: "abulafia")
    source = homes["abulafia"] / ".openclaw" / "workspace" / "notes.md"
    source.write_text("default-name notes\n", encoding="utf-8")

    result = service.workspace_publish(source, visible_to=["bob"])

    assert result["publisher_agent_id"] == "Abulafia"
    assert "bob" in result["visible_to"]
    published = homes["bob-user"] / ".openclaw" / "workspace" / "published"
    assert published.is_dir()
    assert not published.is_symlink()


def test_cli_workspace_publish_and_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = tmp_path / "state"
    service, homes = _service_with_agents(tmp_path, monkeypatch)
    assert service.store.root == config_dir

    def fake_home_for_user(_self: ClawieService, user: str) -> Path | None:
        return homes.get(user)

    monkeypatch.setattr(ClawieService, "_linux_home_for_user", fake_home_for_user)
    monkeypatch.setattr(ClawieService, "_can_manage_linux_user", lambda _self, _user: True)
    source = homes["alice-user"] / ".openclaw" / "workspace" / "report.md"
    source.write_text("cli notes\n", encoding="utf-8")

    code = main(
        [
            "--config-dir",
            str(config_dir),
            "workspace",
            "publish",
            str(source),
            "--agent",
            "alice",
            "--to",
            "bob",
            "--title",
            "CLI Notes",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "Published pub_" in output
    assert "visible_to: alice, bob" in output

    list_code = main(["--config-dir", str(config_dir), "workspace", "list", "--agent", "bob", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert list_code == 0
    assert payload[0]["publisher_agent_id"] == "alice"
    assert payload[0]["title"] == "CLI Notes"


def test_openclaw_home_prep_mounts_empty_published_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    monkeypatch.setattr(service, "_login_shell_env", lambda _linux_user: {})
    home = tmp_path / "alice-home"
    home.mkdir()

    service._ensure_openclaw_home_prepared(
        home=home,
        linux_user="alice-user",
        channels=[],
        live_payloads={},
        auth_mode="none",
        api_key="",
        agent_id="alice",
    )

    target = home / ".openclaw" / "workspace" / "published"
    assert target.is_dir()
    assert not target.is_symlink()
    assert (target / "_index.json").is_file()
