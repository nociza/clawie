"""Local published workspace storage for cross-agent artifact sharing."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from clawie.safe_fs import (
    UnsafePathError,
    append_text_under,
    ensure_directory_under,
    read_bytes_under,
    write_bytes_under,
)


SCHEMA = "clawie.published-workspace.v1"
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_PUBLICATION_BYTES = 2 * 1024 * 1024 * 1024
_READONLY_DIRECTORY_MODE = stat.S_IRUSR | stat.S_IXUSR


class PublishedWorkspaceError(ValueError):
    """Raised for invalid published-workspace operations."""


@dataclass(frozen=True)
class SourceFile:
    source: Path
    relative_path: str
    sha256: str
    size: int
    mode: str


def _now_iso() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return stamp.replace("+00:00", "Z")


def _path_stamp(stamp: str) -> str:
    return stamp.replace("-", "").replace(":", "").replace("+00:00", "Z")


def _safe_token(value: str, *, field_name: str) -> str:
    token = str(value).strip()
    if not token:
        raise PublishedWorkspaceError(f"{field_name} is required")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(ch not in allowed for ch in token):
        raise PublishedWorkspaceError(
            f"{field_name} may only contain letters, numbers, '.', '_', and '-'"
        )
    if token in {".", ".."} or token.startswith("."):
        raise PublishedWorkspaceError(f"{field_name} is not safe for workspace paths")
    return token


def _slug(value: str, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    chars: list[str] = []
    previous_dash = False
    for ch in raw:
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            chars.append(ch)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    token = "".join(chars).strip("-")
    return token or fallback


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(files: Iterable[SourceFile]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda row: row.relative_path):
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _safe_relative_path(path: Path) -> str:
    if path.is_absolute():
        raise PublishedWorkspaceError(f"published path must be relative: {path}")
    parts = path.parts
    if not parts:
        raise PublishedWorkspaceError("published path is empty")
    for part in parts:
        if part in {"", ".", ".."}:
            raise PublishedWorkspaceError(f"published path is not safe: {path}")
    return path.as_posix()


class PublishedWorkspace:
    """Filesystem-backed publication store with a small SQLite catalog."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def catalog_path(self) -> Path:
        return self.root / "catalog.sqlite"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def ensure(self) -> None:
        try:
            root_st = self.root.lstat()
        except FileNotFoundError:
            self.root.mkdir(parents=True, mode=0o700)
            root_st = self.root.lstat()
        if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
            raise PublishedWorkspaceError(
                f"published workspace root must be a real directory: {self.root}"
            )
        os.chmod(self.root, 0o700)
        try:
            for rel in (
                "blobs/sha256",
                "publications",
                "streams",
                "append",
                "views",
                "events/agents",
                "snapshots",
                "tmp",
            ):
                ensure_directory_under(self.root, rel, mode=0o700)
            try:
                workspace_info = read_bytes_under(
                    self.root,
                    "WORKSPACE.json",
                    max_bytes=1024 * 1024,
                )
            except FileNotFoundError:
                workspace_info = (
                    json.dumps(
                        {
                            "schema": SCHEMA,
                            "created_at": _now_iso(),
                            "layout": "local-fs-v1",
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            write_bytes_under(
                self.root,
                "WORKSPACE.json",
                workspace_info,
                mode=0o600,
            )
        except UnsafePathError as exc:
            raise PublishedWorkspaceError(str(exc)) from exc
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS publications (
                    publication_id TEXT PRIMARY KEY,
                    publisher_agent_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tree_sha256 TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_publications_publisher
                    ON publications(publisher_agent_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_publications_created
                    ON publications(created_at);
                CREATE TABLE IF NOT EXISTS agent_aliases (
                    alias_id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_aliases_target
                    ON agent_aliases(target_id);
                """
            )
            conn.commit()

    def add_agent_alias(self, old_agent_id: str, new_agent_id: str) -> None:
        """Preserve immutable-publication access across a logical agent rename."""
        old_id = _safe_token(old_agent_id, field_name="old_agent_id")
        new_id = _safe_token(new_agent_id, field_name="new_agent_id")
        if old_id == new_id:
            raise PublishedWorkspaceError("old and new agent IDs must differ")
        self.ensure()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT target_id FROM agent_aliases WHERE alias_id = ?", (old_id,)
            ).fetchone()
            if existing is not None and str(existing["target_id"]) != new_id:
                raise PublishedWorkspaceError(
                    f"published-workspace alias '{old_id}' already targets "
                    f"'{existing['target_id']}'"
                )
            conn.execute(
                "INSERT OR REPLACE INTO agent_aliases(alias_id, target_id, created_at) "
                "VALUES (?, ?, ?)",
                (old_id, new_id, _now_iso()),
            )
            conn.commit()

    def remove_agent_alias(self, old_agent_id: str, new_agent_id: str) -> None:
        old_id = _safe_token(old_agent_id, field_name="old_agent_id")
        new_id = _safe_token(new_agent_id, field_name="new_agent_id")
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM agent_aliases WHERE alias_id = ? AND target_id = ?",
                (old_id, new_id),
            )
            conn.commit()

    def identity_equivalents(self, agent_id: str) -> set[str]:
        token = _safe_token(agent_id, field_name="agent_id")
        self.ensure()
        equivalents = {token}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alias_id, target_id FROM agent_aliases"
            ).fetchall()
        changed = True
        while changed:
            changed = False
            for row in rows:
                alias_id = str(row["alias_id"])
                target_id = str(row["target_id"])
                if alias_id in equivalents or target_id in equivalents:
                    before = len(equivalents)
                    equivalents.update((alias_id, target_id))
                    changed = changed or len(equivalents) != before
        return equivalents

    def can_view(self, publication_id: str, viewer_agent_id: str) -> bool:
        viewer = _safe_token(viewer_agent_id, field_name="viewer_agent_id")
        result = self.show(publication_id)
        return bool(self.identity_equivalents(viewer) & set(result.get("visible_to", [])))

    def publish(
        self,
        *,
        source_path: Path,
        publisher_agent_id: str,
        visible_to: list[str],
        title: str = "",
        source_workspace: Path | None = None,
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.ensure()
        publisher = _safe_token(publisher_agent_id, field_name="publisher_agent_id")
        viewers = self._normalize_viewers([publisher, *visible_to])
        requested_source = Path(source_path).expanduser()
        if requested_source.is_symlink():
            raise PublishedWorkspaceError(f"cannot publish symlink: {requested_source}")
        source = requested_source.resolve(strict=True)
        if source_workspace is not None:
            workspace = Path(source_workspace).expanduser().resolve(strict=True)
            if not _is_relative_to(source, workspace):
                raise PublishedWorkspaceError(
                    f"source path must be inside the publishing agent workspace: {workspace}"
                )
        staging = Path(
            tempfile.mkdtemp(
                prefix=".capture-",
                suffix=".staging",
                dir=str(self.tmp_dir),
            )
        )
        try:
            files_dir = staging / "files"
            files = self._collect_source_files(source, files_dir)
            tree_sha = _tree_digest(files)
            created_at = _now_iso()
            stamp = _path_stamp(created_at)
            short = tree_sha[:8]
            title_token = _slug(title, source.stem if source.is_file() else source.name)
            publication_id = f"pub_{stamp}_{publisher}_{short}_{uuid.uuid4().hex[:8]}"
            view_name = f"{stamp}-{title_token}-{short}"
            final = self.root / "publications" / publication_id
            for item in files:
                blob_rel = self._blob_relative_path(item.sha256)
                blob_path = self.root / blob_rel
                self._ensure_blob(item.source, blob_path)
                self._make_readonly_file(item.source)

            manifest = {
                "schema": SCHEMA,
                "publication_id": publication_id,
                "publisher_agent_id": publisher,
                "created_at": created_at,
                "title": str(title or "").strip() or source.name,
                "mode": "immutable",
                "view_name": view_name,
                "visibility": {"agents": viewers, "groups": []},
                "source": {
                    "path": str(source),
                    "agent_workspace_relative_path": self._source_relative_path(
                        source,
                        source_workspace,
                    ),
                    "source_digest": f"sha256:{tree_sha}",
                },
                "context": context or {},
                "files": [
                    {
                        "path": item.relative_path,
                        "sha256": item.sha256,
                        "size": item.size,
                        "mode": item.mode,
                        "blob": self._blob_relative_path(item.sha256).as_posix(),
                    }
                    for item in sorted(files, key=lambda row: row.relative_path)
                ],
                "parents": [],
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(staging, final)
            self._make_tree_readonly(final)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO publications(
                        publication_id,
                        publisher_agent_id,
                        title,
                        mode,
                        created_at,
                        tree_sha256,
                        source_path,
                        manifest_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        publication_id,
                        publisher,
                        str(manifest["title"]),
                        "immutable",
                        created_at,
                        tree_sha,
                        str(source),
                        json.dumps(manifest, sort_keys=True),
                    ),
                )
                conn.commit()
            for viewer in viewers:
                self.rebuild_view(viewer)
            event = {
                "event": "published",
                "publication_id": publication_id,
                "publisher": publisher,
                "visible_to": viewers,
                "created_at": created_at,
                "title": manifest["title"],
            }
            self._append_event(self.root / "events" / "global.jsonl", event)
            for viewer in viewers:
                self._append_event(self.root / "events" / "agents" / f"{viewer}.jsonl", event)
            return self.show(publication_id)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def list_publications(
        self,
        *,
        viewer_agent_id: str = "",
        publisher_agent_id: str = "",
    ) -> list[dict[str, Any]]:
        self.ensure()
        viewer = str(viewer_agent_id or "").strip()
        publisher = str(publisher_agent_id or "").strip()
        rows: list[dict[str, Any]] = []
        viewer_ids = self.identity_equivalents(viewer) if viewer else set()
        publisher_ids = self.identity_equivalents(publisher) if publisher else set()
        with self._connect() as conn:
            result = conn.execute(
                "SELECT manifest_json FROM publications ORDER BY created_at DESC"
            ).fetchall()
        for row in result:
            manifest = self._decode_manifest(row["manifest_json"])
            if publisher and str(manifest.get("publisher_agent_id", "")) not in publisher_ids:
                continue
            if viewer and not (viewer_ids & set(self._manifest_viewers(manifest))):
                continue
            rows.append(self._summary(manifest, viewer_agent_id=viewer))
        return rows

    def show(self, publication_id: str, *, viewer_agent_id: str = "") -> dict[str, Any]:
        self.ensure()
        pub_id = _safe_token(publication_id, field_name="publication_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT manifest_json FROM publications WHERE publication_id = ?",
                (pub_id,),
            ).fetchone()
        if row is None:
            raise PublishedWorkspaceError(f"publication not found: {pub_id}")
        manifest = self._decode_manifest(row["manifest_json"])
        return self._summary(manifest, viewer_agent_id=viewer_agent_id, include_manifest=True)

    def rebuild_view(self, viewer_agent_id: str) -> dict[str, Any]:
        self.ensure()
        viewer = _safe_token(viewer_agent_id, field_name="viewer_agent_id")
        rows = self.list_publications(viewer_agent_id=viewer)
        views_root = self.root / "views"
        staging = Path(
            tempfile.mkdtemp(
                prefix=f"{viewer}.",
                suffix=".view",
                dir=str(self.tmp_dir),
            )
        )
        try:
            index_rows: list[dict[str, Any]] = []
            for item in rows:
                publisher = _safe_token(
                    str(item.get("publisher_agent_id", "")),
                    field_name="publisher_agent_id",
                )
                view_name = _safe_token(str(item.get("view_name", "")), field_name="view_name")
                target = Path(str(item.get("path", ""))).resolve(strict=True)
                publication = staging / publisher / view_name
                publication.parent.mkdir(parents=True, exist_ok=True)
                # Views are private, materialized projections.  Symlinks into
                # the manager's 0700 state tree either fail for real agent UIDs
                # or force the canonical store to be exposed too broadly.
                shutil.copytree(target, publication, symlinks=False)
                index_rows.append(
                    {
                        "publication_id": item.get("publication_id", ""),
                        "publisher_agent_id": publisher,
                        "title": item.get("title", ""),
                        "created_at": item.get("created_at", ""),
                        "path": f"{publisher}/{view_name}",
                    }
                )

            (staging / "_index.json").write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "viewer_agent_id": viewer,
                        "generated_at": _now_iso(),
                        "publications": index_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            (staging / "_index.md").write_text(
                self._render_index_md(viewer, index_rows),
                encoding="utf-8",
            )
            final = views_root / viewer
            old = views_root / f".{viewer}.old-{uuid.uuid4().hex[:8]}"
            if final.exists() or final.is_symlink():
                os.replace(final, old)
            os.replace(staging, final)
            if old.exists() or old.is_symlink():
                if old.is_symlink() or old.is_file():
                    old.unlink()
                else:
                    shutil.rmtree(old, ignore_errors=True)
            return {
                "viewer_agent_id": viewer,
                "view_path": str(final),
                "publications": len(index_rows),
            }
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def verify(self, publication_id: str = "") -> dict[str, Any]:
        self.ensure()
        pub_id = str(publication_id or "").strip()
        if pub_id:
            manifests = [self.show(pub_id)["manifest"]]
        else:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT manifest_json FROM publications ORDER BY created_at DESC"
                ).fetchall()
            manifests = [self._decode_manifest(row["manifest_json"]) for row in rows]
        failures: list[dict[str, str]] = []
        checked_files = 0
        for manifest in manifests:
            manifest_id = str(manifest.get("publication_id", ""))
            base = self.root / "publications" / manifest_id / "files"
            for entry in manifest.get("files", []):
                if not isinstance(entry, dict):
                    continue
                checked_files += 1
                rel = _safe_relative_path(Path(str(entry.get("path", ""))))
                expected = str(entry.get("sha256", ""))
                path = base / rel
                if not path.is_file():
                    failures.append(
                        {
                            "publication_id": manifest_id,
                            "path": rel,
                            "reason": "file missing",
                        }
                    )
                    continue
                actual = _sha256_file(path)
                if actual != expected:
                    failures.append(
                        {
                            "publication_id": manifest_id,
                            "path": rel,
                            "reason": "sha256 mismatch",
                        }
                    )
                blob = self.root / str(entry.get("blob", ""))
                if not blob.is_file():
                    failures.append(
                        {
                            "publication_id": manifest_id,
                            "path": rel,
                            "reason": "blob missing",
                        }
                    )
        return {
            "status": "ok" if not failures else "failed",
            "publications": len(manifests),
            "files": checked_files,
            "failures": failures,
        }

    def view_path(self, viewer_agent_id: str) -> Path:
        viewer = _safe_token(viewer_agent_id, field_name="viewer_agent_id")
        return self.root / "views" / viewer

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open the catalog transactionally and close it on every path."""
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink():
            raise PublishedWorkspaceError(
                f"published workspace root must be a real directory: {self.root}"
            )
        os.chmod(self.root, 0o700)
        if self.catalog_path.is_symlink():
            raise PublishedWorkspaceError(
                f"published workspace catalog must not be a symlink: {self.catalog_path}"
            )
        conn = sqlite3.connect(self.catalog_path)
        try:
            os.chmod(self.catalog_path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _collect_source_files(self, source: Path, capture_root: Path) -> list[SourceFile]:
        """Capture source content once through no-follow descriptors.

        All subsequent hashing, blob creation, and publication reads use the
        manager-private capture, so an agent cannot swap a checked path before
        a privileged copy.
        """
        capture_root.mkdir(parents=True, mode=0o700)
        files: list[SourceFile] = []
        total = 0
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        dir_flags = file_flags | getattr(os, "O_DIRECTORY", 0)

        def open_directory_path(path: Path) -> int:
            if not path.is_absolute():
                raise PublishedWorkspaceError(f"source directory must be absolute: {path}")
            current_fd = os.open(os.path.sep, dir_flags)
            try:
                for part in path.parts[1:]:
                    next_fd = os.open(part, dir_flags, dir_fd=current_fd)
                    os.close(current_fd)
                    current_fd = next_fd
                return current_fd
            except Exception:
                os.close(current_fd)
                raise

        def capture_file(
            parent_fd: int,
            name: str,
            rel_path: Path,
            expected: os.stat_result,
        ) -> None:
            nonlocal total
            try:
                source_fd = os.open(name, file_flags, dir_fd=parent_fd)
            except OSError as exc:
                raise PublishedWorkspaceError(
                    f"cannot safely open source file {source / rel_path}: {exc}"
                ) from exc
            target = capture_root / _safe_relative_path(rel_path)
            try:
                st = os.fstat(source_fd)
                if not stat.S_ISREG(st.st_mode):
                    raise PublishedWorkspaceError(f"cannot publish special file: {source / rel_path}")
                if (int(st.st_dev), int(st.st_ino)) != (
                    int(expected.st_dev),
                    int(expected.st_ino),
                ):
                    raise PublishedWorkspaceError(f"source changed while publishing: {source / rel_path}")
                size = int(st.st_size)
                if size > MAX_FILE_BYTES:
                    raise PublishedWorkspaceError(f"file exceeds publish size limit: {source / rel_path}")
                total += size
                if total > MAX_PUBLICATION_BYTES:
                    raise PublishedWorkspaceError("publication exceeds total size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied = 0
                with target.open("xb") as output:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_FILE_BYTES or total - size + copied > MAX_PUBLICATION_BYTES:
                            raise PublishedWorkspaceError("source changed beyond publication size limits")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                final_st = os.fstat(source_fd)
                stable_fields = (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
                if copied != size or any(
                    getattr(st, field) != getattr(final_st, field) for field in stable_fields
                ):
                    raise PublishedWorkspaceError(f"source changed while publishing: {source / rel_path}")
                files.append(
                    SourceFile(
                        source=target,
                        relative_path=_safe_relative_path(rel_path),
                        sha256=digest.hexdigest(),
                        size=copied,
                        mode=f"{stat.S_IMODE(st.st_mode):04o}",
                    )
                )
            finally:
                os.close(source_fd)

        def walk(directory_fd: int, rel_dir: Path) -> None:
            for name in sorted(os.listdir(directory_fd)):
                rel_path = rel_dir / name
                try:
                    st = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise PublishedWorkspaceError(
                        f"cannot inspect source path {source / rel_path}: {exc}"
                    ) from exc
                if stat.S_ISLNK(st.st_mode):
                    raise PublishedWorkspaceError(f"cannot publish symlink: {source / rel_path}")
                if stat.S_ISDIR(st.st_mode):
                    try:
                        child_fd = os.open(name, dir_flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise PublishedWorkspaceError(
                            f"cannot safely open source directory {source / rel_path}: {exc}"
                        ) from exc
                    try:
                        opened_st = os.fstat(child_fd)
                        if (int(opened_st.st_dev), int(opened_st.st_ino)) != (
                            int(st.st_dev),
                            int(st.st_ino),
                        ):
                            raise PublishedWorkspaceError(
                                f"source changed while publishing: {source / rel_path}"
                            )
                        walk(child_fd, rel_path)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(st.st_mode):
                    capture_file(directory_fd, name, rel_path, st)
                else:
                    raise PublishedWorkspaceError(f"cannot publish special file: {source / rel_path}")

        try:
            source_st = source.lstat()
            if stat.S_ISDIR(source_st.st_mode):
                root_fd = open_directory_path(source)
                try:
                    opened_st = os.fstat(root_fd)
                    if (int(opened_st.st_dev), int(opened_st.st_ino)) != (
                        int(source_st.st_dev),
                        int(source_st.st_ino),
                    ):
                        raise PublishedWorkspaceError(f"source changed while publishing: {source}")
                    walk(root_fd, Path())
                finally:
                    os.close(root_fd)
            elif stat.S_ISREG(source_st.st_mode):
                parent_fd = open_directory_path(source.parent)
                try:
                    capture_file(parent_fd, source.name, Path(source.name), source_st)
                finally:
                    os.close(parent_fd)
            else:
                raise PublishedWorkspaceError(
                    f"source path is not a regular file or directory: {source}"
                )
        except OSError as exc:
            raise PublishedWorkspaceError(f"cannot safely open source path {source}: {exc}") from exc
        if not files:
            raise PublishedWorkspaceError("source tree contains no regular files")
        return files

    def _ensure_blob(self, source: Path, blob_path: Path) -> None:
        if blob_path.is_file():
            return
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".blob-", dir=str(blob_path.parent))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copyfile(source, tmp)
            self._make_readonly_file(tmp)
            os.replace(tmp, blob_path)
        finally:
            tmp.unlink(missing_ok=True)

    def _blob_relative_path(self, sha256: str) -> Path:
        return Path("blobs") / "sha256" / sha256[:2] / sha256[2:4] / sha256

    def _source_relative_path(self, source: Path, workspace: Path | None) -> str:
        if workspace is None:
            return ""
        try:
            return source.relative_to(Path(workspace).expanduser().resolve(strict=True)).as_posix()
        except Exception:
            return ""

    @staticmethod
    def _normalize_viewers(viewers: list[str]) -> list[str]:
        rows: list[str] = []
        seen: set[str] = set()
        for item in viewers:
            token = _safe_token(str(item), field_name="viewer_agent_id")
            if token in seen:
                continue
            seen.add(token)
            rows.append(token)
        return rows

    @staticmethod
    def _decode_manifest(payload: str) -> dict[str, Any]:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise PublishedWorkspaceError("catalog manifest is invalid")
        return data

    @staticmethod
    def _manifest_viewers(manifest: dict[str, Any]) -> list[str]:
        visibility = manifest.get("visibility", {})
        if not isinstance(visibility, dict):
            return []
        agents = visibility.get("agents", [])
        if not isinstance(agents, list):
            return []
        return [str(item) for item in agents]

    def _summary(
        self,
        manifest: dict[str, Any],
        *,
        viewer_agent_id: str = "",
        include_manifest: bool = False,
    ) -> dict[str, Any]:
        pub_id = str(manifest.get("publication_id", ""))
        publisher = str(manifest.get("publisher_agent_id", ""))
        view_name = str(manifest.get("view_name", pub_id))
        result = {
            "publication_id": pub_id,
            "publisher_agent_id": publisher,
            "title": str(manifest.get("title", "")),
            "mode": str(manifest.get("mode", "")),
            "created_at": str(manifest.get("created_at", "")),
            "tree_sha256": str(manifest.get("source", {}).get("source_digest", "")),
            "view_name": view_name,
            "visible_to": self._manifest_viewers(manifest),
            "file_count": len(manifest.get("files", [])) if isinstance(manifest.get("files"), list) else 0,
            "path": str(self.root / "publications" / pub_id),
        }
        if viewer_agent_id:
            result["view_path"] = str(self.view_path(viewer_agent_id) / publisher / view_name)
        if include_manifest:
            result["manifest"] = manifest
        return result

    @staticmethod
    def _render_index_md(viewer: str, rows: list[dict[str, Any]]) -> str:
        lines = [
            "# Published workspace",
            "",
            f"Viewer: `{viewer}`",
            "",
            "This directory is generated by clawie. Treat published entries as read-only.",
            "",
        ]
        if not rows:
            lines.append("No publications are visible to this agent yet.")
            lines.append("")
            return "\n".join(lines)
        lines.append("| Publisher | Title | Created | Path |")
        lines.append("|---|---|---|---|")
        for row in rows:
            lines.append(
                "| "
                + str(row.get("publisher_agent_id", ""))
                + " | "
                + str(row.get("title", "")).replace("|", "\\|")
                + " | "
                + str(row.get("created_at", ""))
                + " | `"
                + str(row.get("path", ""))
                + "` |"
            )
        lines.append("")
        return "\n".join(lines)

    def _append_event(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise PublishedWorkspaceError(f"event path escapes workspace root: {path}") from exc
        try:
            append_text_under(
                self.root,
                relative,
                json.dumps(payload, sort_keys=True) + "\n",
                mode=0o600,
                directory_mode=0o700,
            )
        except UnsafePathError as exc:
            raise PublishedWorkspaceError(str(exc)) from exc

    @staticmethod
    def _make_readonly_file(path: Path) -> None:
        try:
            os.chmod(path, 0o400)
        except OSError:
            return

    @classmethod
    def _make_tree_readonly(cls, path: Path) -> None:
        for child in path.rglob("*"):
            if child.is_dir():
                try:
                    os.chmod(child, _READONLY_DIRECTORY_MODE)
                except OSError:
                    pass
            else:
                cls._make_readonly_file(child)
        try:
            os.chmod(path, _READONLY_DIRECTORY_MODE)
        except OSError:
            pass
