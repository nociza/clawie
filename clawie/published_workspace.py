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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "clawie.published-workspace.v1"
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_PUBLICATION_BYTES = 2 * 1024 * 1024 * 1024


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
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        workspace_info = self.root / "WORKSPACE.json"
        if not workspace_info.exists():
            workspace_info.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "created_at": _now_iso(),
                        "layout": "local-fs-v1",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
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
                """
            )
            conn.commit()

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
        source = Path(source_path).expanduser().resolve(strict=True)
        if source_workspace is not None:
            workspace = Path(source_workspace).expanduser().resolve(strict=True)
            if not _is_relative_to(source, workspace):
                raise PublishedWorkspaceError(
                    f"source path must be inside the publishing agent workspace: {workspace}"
                )
        files = self._collect_source_files(source)
        tree_sha = _tree_digest(files)
        created_at = _now_iso()
        stamp = _path_stamp(created_at)
        short = tree_sha[:8]
        title_token = _slug(title, source.stem if source.is_file() else source.name)
        publication_id = f"pub_{stamp}_{publisher}_{short}_{uuid.uuid4().hex[:8]}"
        view_name = f"{stamp}-{title_token}-{short}"
        staging = Path(
            tempfile.mkdtemp(
                prefix=f"{publication_id}.",
                suffix=".staging",
                dir=str(self.tmp_dir),
            )
        )
        final = self.root / "publications" / publication_id
        try:
            files_dir = staging / "files"
            for item in files:
                blob_rel = self._blob_relative_path(item.sha256)
                blob_path = self.root / blob_rel
                self._ensure_blob(item.source, blob_path)
                target = files_dir / item.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.source, target)
                self._make_readonly_file(target)

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
        with self._connect() as conn:
            if publisher:
                result = conn.execute(
                    """
                    SELECT manifest_json FROM publications
                    WHERE publisher_agent_id = ?
                    ORDER BY created_at DESC
                    """,
                    (publisher,),
                ).fetchall()
            else:
                result = conn.execute(
                    "SELECT manifest_json FROM publications ORDER BY created_at DESC"
                ).fetchall()
        for row in result:
            manifest = self._decode_manifest(row["manifest_json"])
            if viewer and viewer not in self._manifest_viewers(manifest):
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
                link = staging / publisher / view_name
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, link)
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

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.catalog_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _collect_source_files(self, source: Path) -> list[SourceFile]:
        if source.is_symlink():
            raise PublishedWorkspaceError(f"cannot publish symlink: {source}")
        if source.is_file():
            candidates = [(source, Path(source.name))]
        elif source.is_dir():
            candidates = []
            for entry in sorted(source.rglob("*"), key=lambda item: str(item)):
                if entry.is_symlink():
                    raise PublishedWorkspaceError(f"cannot publish symlink: {entry}")
                rel = entry.relative_to(source)
                try:
                    st = entry.lstat()
                except OSError as exc:
                    raise PublishedWorkspaceError(f"cannot inspect source path {entry}: {exc}") from exc
                if stat.S_ISDIR(st.st_mode):
                    continue
                if not stat.S_ISREG(st.st_mode):
                    raise PublishedWorkspaceError(f"cannot publish special file: {entry}")
                candidates.append((entry, rel))
        else:
            raise PublishedWorkspaceError(f"source path is not a regular file or directory: {source}")

        files: list[SourceFile] = []
        total = 0
        for path, rel_path in candidates:
            rel = _safe_relative_path(rel_path)
            st = path.stat()
            size = int(st.st_size)
            if size > MAX_FILE_BYTES:
                raise PublishedWorkspaceError(f"file exceeds publish size limit: {path}")
            total += size
            if total > MAX_PUBLICATION_BYTES:
                raise PublishedWorkspaceError("publication exceeds total size limit")
            files.append(
                SourceFile(
                    source=path,
                    relative_path=rel,
                    sha256=_sha256_file(path),
                    size=size,
                    mode=f"{stat.S_IMODE(st.st_mode):04o}",
                )
            )
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

    @staticmethod
    def _append_event(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _make_readonly_file(path: Path) -> None:
        try:
            os.chmod(path, 0o444)
        except OSError:
            return

    @classmethod
    def _make_tree_readonly(cls, path: Path) -> None:
        for child in path.rglob("*"):
            if child.is_dir():
                try:
                    os.chmod(child, 0o555)
                except OSError:
                    pass
            else:
                cls._make_readonly_file(child)
        try:
            os.chmod(path, 0o555)
        except OSError:
            pass
