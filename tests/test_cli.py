from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture

from clawctl.cli import main
from clawctl.store import StateStore


def run_cli(config_dir: Path, *args: str) -> int:
    return main(["--config-dir", str(config_dir), *args])


def test_setup_init_requires_api_key(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    code = run_cli(tmp_path, "setup", "init")
    output = capsys.readouterr().out
    assert code == 1
    assert "API key is required" in output


def test_clone_user_and_dashboard(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert (
        run_cli(
            tmp_path,
            "setup",
            "init",
            "--api-key",
            "zc_live_1234",
            "--subscription",
            "pro",
            "--workspace",
            "prod",
        )
        == 0
    )
    capsys.readouterr()

    assert (
        run_cli(
            tmp_path,
            "users",
            "create",
            "--user-id",
            "alice",
            "--template",
            "baseline",
            "--channel-strategy",
            "new",
        )
        == 0
    )
    capsys.readouterr()

    assert (
        run_cli(
            tmp_path,
            "users",
            "clone",
            "--from-user",
            "alice",
            "--user-id",
            "bob",
            "--channel-strategy",
            "migrate",
        )
        == 0
    )
    capsys.readouterr()

    code = run_cli(tmp_path, "dashboard")
    output = capsys.readouterr().out
    assert code == 0
    assert "Dashboard" in output
    assert "alice" in output
    assert "bob" in output


def test_create_user_with_channel_args(tmp_path: Path) -> None:
    assert (
        run_cli(
            tmp_path,
            "setup",
            "init",
            "--api-key",
            "zc_live_1234",
            "--subscription",
            "pro",
            "--workspace",
            "prod",
        )
        == 0
    )
    assert (
        run_cli(
            tmp_path,
            "users",
            "create",
            "--user-id",
            "sam",
            "--channel-strategy",
            "new",
            "--channel",
            "chat:ops",
            "--channel",
            "email:inbox",
        )
        == 0
    )

    state = StateStore(config_dir=tmp_path).read_state()
    channels = state["users"]["sam"]["channels"]
    names = {channel["name"] for channel in channels}
    assert "sam-ops" in names
    assert "sam-inbox" in names


def test_batch_create_returns_nonzero_on_errors(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    assert (
        run_cli(
            tmp_path,
            "setup",
            "init",
            "--api-key",
            "zc_live_1234",
            "--subscription",
            "pro",
            "--workspace",
            "prod",
        )
        == 0
    )
    capsys.readouterr()

    batch_file = tmp_path / "users.json"
    batch_file.write_text(
        json.dumps(
            [
                {"user_id": "maria", "display_name": "Maria"},
                {"display_name": "MissingId"},
            ]
        ),
        encoding="utf-8",
    )

    code = run_cli(tmp_path, "users", "batch-create", "--file", str(batch_file))
    output = capsys.readouterr().out
    assert code == 1
    assert "created: 1" in output
    assert "errors: 1" in output
