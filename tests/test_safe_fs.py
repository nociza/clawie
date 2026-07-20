from __future__ import annotations

from pathlib import Path

import pytest

from clawie.safe_fs import (
    UnsafePathError,
    copy_tree_under,
    read_text_under,
    remove_under,
    write_text_under,
)


def test_write_text_under_rejects_destination_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("original", encoding="utf-8")
    (root / "target").symlink_to(outside)

    with pytest.raises(UnsafePathError, match="symlink or special"):
        write_text_under(root, "target", "overwritten")

    assert outside.read_text(encoding="utf-8") == "original"


def test_write_text_under_rejects_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "parent").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError, match="path component"):
        write_text_under(root, "parent/target", "overwritten")

    assert not (outside / "target").exists()


def test_write_text_under_publishes_regular_file_atomically(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    target = write_text_under(root, "nested/value.txt", "first", mode=0o640)
    write_text_under(root, "nested/value.txt", "second", mode=0o600)

    assert target.read_text(encoding="utf-8") == "second"
    assert target.stat().st_mode & 0o777 == 0o600
    assert read_text_under(root, "nested/value.txt") == "second"


def test_read_text_under_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / "value").symlink_to(outside)

    with pytest.raises(UnsafePathError):
        read_text_under(root, "value")


def test_copy_tree_under_rejects_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (source / "credentials").mkdir()
    (source / "credentials" / "token").symlink_to(outside)

    with pytest.raises(UnsafePathError, match="contains a symlink"):
        copy_tree_under(source, "credentials", target, "copied")

    assert not (target / "copied").exists()


def test_remove_under_unlinks_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    remove_under(root, "link", recursive=True)

    assert not (root / "link").exists()
    assert (outside / "keep").read_text(encoding="utf-8") == "safe"
