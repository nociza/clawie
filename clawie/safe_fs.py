"""Symlink-safe filesystem helpers for privileged service operations.

Clawie frequently runs as root while writing into directories owned by managed
agents.  Path-based ``open``/``write_text``/``copyfile`` calls are unsafe at
that boundary because an agent can replace any component with a symlink between
checks.  These helpers walk relative paths with directory file descriptors and
``O_NOFOLLOW`` and publish files with an atomic, same-directory rename.
"""
from __future__ import annotations

import os
import pwd
import secrets
import stat
from pathlib import Path
from typing import TypeAlias


Owner: TypeAlias = tuple[int, int] | None


class UnsafePathError(PermissionError):
    """Raised when a privileged path contains a symlink or special file."""


def owner_for_username(username: str) -> Owner:
    """Resolve *username* to uid/gid, returning ``None`` for an empty name."""
    token = str(username).strip()
    if not token:
        return None
    try:
        row = pwd.getpwnam(token)
    except KeyError as exc:
        raise UnsafePathError(f"unknown file owner: {token}") from exc
    return int(row.pw_uid), int(row.pw_gid)


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    path = Path(relative)
    if path.is_absolute():
        raise UnsafePathError(f"path must be relative: {relative}")
    parts = tuple(path.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError(f"path contains an unsafe component: {relative}")
    return parts


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _file_flags(base: int) -> int:
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_root(root: Path) -> int:
    try:
        st = root.lstat()
    except FileNotFoundError as exc:
        raise UnsafePathError(f"trusted root does not exist: {root}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(f"trusted root must be a real directory: {root}")
    try:
        return os.open(root, _directory_flags())
    except OSError as exc:
        raise UnsafePathError(f"could not safely open trusted root {root}: {exc}") from exc


def _apply_owner(fd: int, owner: Owner) -> None:
    if owner is None:
        return
    os.fchown(fd, int(owner[0]), int(owner[1]))


def _walk_directory(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    mode: int,
    owner: Owner,
) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=mode, dir_fd=current)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise UnsafePathError(f"unsafe or non-directory path component: {part}") from exc
            os.close(current)
            current = child
            if create:
                os.fchmod(current, mode)
                _apply_owner(current, owner)
        return current
    except Exception:
        os.close(current)
        raise


def ensure_directory_under(
    root: str | Path,
    relative: str | Path,
    *,
    mode: int = 0o700,
    owner: Owner = None,
) -> Path:
    """Create a real directory tree under *root* without following symlinks."""
    root_path = Path(root)
    parts = _relative_parts(relative)
    root_fd = _open_root(root_path)
    try:
        directory_fd = _walk_directory(
            root_fd,
            parts,
            create=True,
            mode=mode,
            owner=owner,
        )
        os.close(directory_fd)
    finally:
        os.close(root_fd)
    return root_path.joinpath(*parts)


def write_bytes_under(
    root: str | Path,
    relative: str | Path,
    data: bytes,
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
    owner: Owner = None,
) -> Path:
    """Atomically write a regular file below *root* without following symlinks."""
    root_path = Path(root)
    parts = _relative_parts(relative)
    parent_parts, name = parts[:-1], parts[-1]
    root_fd = _open_root(root_path)
    parent_fd: int | None = None
    temp_name = f".clawie-tmp-{os.getpid()}-{secrets.token_hex(8)}"
    temp_fd: int | None = None
    try:
        parent_fd = _walk_directory(
            root_fd,
            parent_parts,
            create=True,
            mode=directory_mode,
            owner=owner,
        )
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafePathError(f"refusing to replace symlink or special file: {root_path / Path(*parts)}")

        flags = _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        temp_fd = os.open(temp_name, flags, mode, dir_fd=parent_fd)
        view = memoryview(data)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError("short write while publishing file")
            view = view[written:]
        os.fchmod(temp_fd, mode)
        _apply_owner(temp_fd, owner)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError:
            # Some platforms/filesystems do not support fsync on directories.
            pass
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        os.close(root_fd)
    return root_path.joinpath(*parts)


def write_text_under(
    root: str | Path,
    relative: str | Path,
    text: str,
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
    owner: Owner = None,
) -> Path:
    return write_bytes_under(
        root,
        relative,
        str(text).encode("utf-8"),
        mode=mode,
        directory_mode=directory_mode,
        owner=owner,
    )


def append_bytes_under(
    root: str | Path,
    relative: str | Path,
    data: bytes,
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
    owner: Owner = None,
) -> Path:
    """Append bytes below *root* without following symlinks."""
    root_path = Path(root)
    parts = _relative_parts(relative)
    root_fd = _open_root(root_path)
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        parent_fd = _walk_directory(
            root_fd,
            parts[:-1],
            create=True,
            mode=directory_mode,
            owner=owner,
        )
        try:
            file_fd = os.open(
                parts[-1],
                _file_flags(os.O_WRONLY | os.O_CREAT | os.O_APPEND),
                mode,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise UnsafePathError(
                f"could not safely open append target: {root_path / Path(*parts)}"
            ) from exc
        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(
                f"refusing to append to non-regular file: {root_path / Path(*parts)}"
            )
        os.fchmod(file_fd, mode)
        _apply_owner(file_fd, owner)
        view = memoryview(data)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)
    return root_path.joinpath(*parts)


def append_text_under(
    root: str | Path,
    relative: str | Path,
    text: str,
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
    owner: Owner = None,
) -> Path:
    return append_bytes_under(
        root,
        relative,
        str(text).encode("utf-8"),
        mode=mode,
        directory_mode=directory_mode,
        owner=owner,
    )


def read_bytes_under(
    root: str | Path,
    relative: str | Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read a regular file below *root* without following any symlinks."""
    root_path = Path(root)
    parts = _relative_parts(relative)
    root_fd = _open_root(root_path)
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        parent_fd = _walk_directory(
            root_fd,
            parts[:-1],
            create=False,
            mode=0o700,
            owner=None,
        )
        try:
            file_fd = os.open(parts[-1], _file_flags(os.O_RDONLY), dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafePathError(f"could not safely open file: {root_path / Path(*parts)}") from exc
        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(f"refusing to read non-regular file: {root_path / Path(*parts)}")
        if max_bytes is not None and st.st_size > max_bytes:
            raise ValueError(f"file exceeds maximum size of {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"file exceeds maximum size of {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def read_text_under(
    root: str | Path,
    relative: str | Path,
    *,
    max_bytes: int | None = None,
) -> str:
    return read_bytes_under(root, relative, max_bytes=max_bytes).decode("utf-8")


def _remove_entry(parent_fd: int, name: str, *, recursive: bool) -> None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        if not recursive:
            raise UnsafePathError(f"refusing to remove directory without recursive=True: {name}")
        try:
            directory_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise UnsafePathError(f"could not safely open directory for removal: {name}") from exc
        try:
            for child in os.listdir(directory_fd):
                _remove_entry(directory_fd, child, recursive=True)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)
        return
    os.unlink(name, dir_fd=parent_fd)


def remove_under(
    root: str | Path,
    relative: str | Path,
    *,
    recursive: bool = False,
) -> None:
    """Remove an entry below *root* without following symlinks."""
    root_path = Path(root)
    parts = _relative_parts(relative)
    root_fd = _open_root(root_path)
    parent_fd: int | None = None
    try:
        parent_fd = _walk_directory(
            root_fd,
            parts[:-1],
            create=False,
            mode=0o700,
            owner=None,
        )
        _remove_entry(parent_fd, parts[-1], recursive=recursive)
    except FileNotFoundError:
        return
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def copy_file_under(
    source_root: str | Path,
    source_relative: str | Path,
    target_root: str | Path,
    target_relative: str | Path,
    *,
    mode: int = 0o600,
    directory_mode: int = 0o700,
    owner: Owner = None,
    max_bytes: int | None = None,
) -> Path:
    data = read_bytes_under(source_root, source_relative, max_bytes=max_bytes)
    return write_bytes_under(
        target_root,
        target_relative,
        data,
        mode=mode,
        directory_mode=directory_mode,
        owner=owner,
    )


def copy_tree_under(
    source_root: str | Path,
    source_relative: str | Path,
    target_root: str | Path,
    target_relative: str | Path,
    *,
    file_mode: int = 0o600,
    directory_mode: int = 0o700,
    owner: Owner = None,
    max_file_bytes: int | None = None,
) -> Path:
    """Copy a tree after rejecting every symlink and special source entry."""
    source_base = Path(source_root)
    source_parts = _relative_parts(source_relative)
    source_path = source_base.joinpath(*source_parts)
    try:
        base_st = source_path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(base_st.st_mode) or not stat.S_ISDIR(base_st.st_mode):
        raise UnsafePathError(f"source tree must be a real directory: {source_path}")

    directories: list[Path] = []
    files: list[Path] = []
    for entry in source_path.rglob("*"):
        rel = entry.relative_to(source_path)
        st = entry.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePathError(f"source tree contains a symlink: {entry}")
        if stat.S_ISDIR(st.st_mode):
            directories.append(rel)
        elif stat.S_ISREG(st.st_mode):
            if max_file_bytes is not None and st.st_size > max_file_bytes:
                raise ValueError(f"source file exceeds maximum size: {entry}")
            files.append(rel)
        else:
            raise UnsafePathError(f"source tree contains a special file: {entry}")

    target_base = Path(target_root)
    target_parts = _relative_parts(target_relative)
    remove_under(target_base, Path(*target_parts), recursive=True)
    ensure_directory_under(
        target_base,
        Path(*target_parts),
        mode=directory_mode,
        owner=owner,
    )
    for rel in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        ensure_directory_under(
            target_base,
            Path(*target_parts) / rel,
            mode=directory_mode,
            owner=owner,
        )
    for rel in sorted(files, key=str):
        copy_file_under(
            source_base,
            Path(*source_parts) / rel,
            target_base,
            Path(*target_parts) / rel,
            mode=file_mode,
            directory_mode=directory_mode,
            owner=owner,
            max_bytes=max_file_bytes,
        )
    return target_base.joinpath(*target_parts)
