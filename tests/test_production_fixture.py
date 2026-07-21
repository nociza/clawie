from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.production_verify_fixture import _trusted_wrapper_dir


def test_production_wrapper_is_created_below_trusted_effective_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_home = tmp_path / "root-home"
    trusted_home.mkdir(mode=0o700)
    monkeypatch.setattr(
        "scripts.production_verify_fixture.pwd.getpwuid",
        lambda _uid: type("PwdRow", (), {"pw_dir": str(trusted_home)})(),
    )

    wrapper_dir = _trusted_wrapper_dir("018")

    assert wrapper_dir.parent == trusted_home.resolve()
    assert wrapper_dir.stat().st_uid == os.geteuid()
    assert wrapper_dir.stat().st_mode & 0o077 == 0


def test_production_wrapper_rejects_writable_effective_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_home = tmp_path / "unsafe-root-home"
    unsafe_home.mkdir(mode=0o700)
    unsafe_home.chmod(0o777)
    monkeypatch.setattr(
        "scripts.production_verify_fixture.pwd.getpwuid",
        lambda _uid: type("PwdRow", (), {"pw_dir": str(unsafe_home)})(),
    )

    with pytest.raises(RuntimeError, match="not group/world-writable"):
        _trusted_wrapper_dir("018")
