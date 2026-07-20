from __future__ import annotations

import io
import shutil
import socket
import struct
import tarfile
import tempfile
from pathlib import Path

import pytest

from clawie.service import ClawieService
from clawie.service_common import SetupError
from clawie.store import StateStore
import clawie.delegation as delegation


def test_prompt_write_rejects_agent_controlled_symlink(tmp_path: Path) -> None:
    service = ClawieService(StateStore(config_dir=tmp_path / "state"))
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.write_text("original", encoding="utf-8")
    prompt = service._core_prompt_path("openclaw", home, "SOUL.md")
    prompt.parent.mkdir(parents=True)
    prompt.symlink_to(outside)

    with pytest.raises(PermissionError, match="symlink or special"):
        service._write_core_prompt_file("openclaw", home, "SOUL.md", "overwritten")

    assert outside.read_text(encoding="utf-8") == "original"


def test_tar_extractor_rejects_symlink_escape(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.tar.gz"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    with tarfile.open(archive_path, "w:gz") as archive:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        archive.addfile(link)
        body = b"escaped"
        item = tarfile.TarInfo("link/pwned")
        item.size = len(body)
        archive.addfile(item, io.BytesIO(body))

    with pytest.raises(SetupError, match="contained a link"):
        ClawieService._extract_tarball_safe(archive_path, target)

    assert not (outside / "pwned").exists()


def test_tar_extractor_preserves_regular_executable(tmp_path: Path) -> None:
    archive_path = tmp_path / "good.tar.gz"
    target = tmp_path / "target"
    target.mkdir()
    with tarfile.open(archive_path, "w:gz") as archive:
        body = b"#!/bin/sh\nexit 0\n"
        item = tarfile.TarInfo("tool/bin/run")
        item.mode = 0o755
        item.size = len(body)
        archive.addfile(item, io.BytesIO(body))

    ClawieService._extract_tarball_safe(archive_path, target)

    installed = target / "tool" / "bin" / "run"
    assert installed.read_bytes() == body
    assert installed.stat().st_mode & 0o777 == 0o755


def test_delegation_socket_is_private_and_ids_are_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="clawie-sec-")) / "delegation"
    monkeypatch.setattr(delegation, "DELEGATION_DIR", root)
    bus = delegation.DelegationBus("worker")
    try:
        bus.listen()
        assert root.stat().st_mode & 0o777 == 0o700
        assert bus.socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        bus.close()
        shutil.rmtree(root.parent, ignore_errors=True)

    with pytest.raises(ValueError, match="agent id"):
        delegation.DelegationBus("../victim")


def test_delegation_rejects_oversized_frame_before_reading_body() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(struct.pack("!I", delegation.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(ValueError, match="invalid delegation message length"):
            delegation.recv_message(receiver, timeout=0.1)
    finally:
        sender.close()
        receiver.close()
