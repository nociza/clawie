from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clawie.cli import main
from clawie.service import ClawieService
from clawie.store import StateStore


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
    assert bob_mount.is_symlink()
    assert (bob_mount / "_index.json").is_file()
    assert (bob_mount / "alice" / result["view_name"] / "files" / "report.md").read_text(
        encoding="utf-8"
    ) == "published notes\n"
    assert service.workspace_verify(result["publication_id"])["status"] == "ok"
    assert service.workspace_verify()["status"] == "ok"


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
    assert (homes["bob-user"] / ".openclaw" / "workspace" / "published").is_symlink()


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
    assert target.is_symlink()
    assert (target / "_index.json").is_file()
