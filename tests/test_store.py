from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from pytest import MonkeyPatch, raises

from clawie.store import ConcurrentStateWriteError, StateStore


def test_store_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    store.ensure()

    with store._connect() as conn:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])

    assert journal_mode == "wal"
    assert busy_timeout_ms == 30000
    with raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


def test_store_hardens_state_root_and_database_permissions(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    os.chmod(state_root, 0o777)

    store = StateStore(config_dir=state_root)
    store.ensure()

    assert (state_root.stat().st_mode & 0o777) == 0o700
    assert (store.db_path.stat().st_mode & 0o777) == 0o600


def test_store_repairs_existing_clawie_state_root_permissions(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "clawied-status.json").write_text("{}", encoding="utf-8")
    os.chmod(state_root, 0o755)

    store = StateStore(config_dir=state_root)
    store.ensure()

    assert (state_root.stat().st_mode & 0o777) == 0o700
    assert store.db_path.exists()


def test_store_rejects_non_clawie_config_dir_without_chmod(tmp_path: Path) -> None:
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    (shared_dir / "unrelated.txt").write_text("do not manage this directory\n", encoding="utf-8")
    os.chmod(shared_dir, 0o755)

    with raises(PermissionError, match="refusing to change permissions"):
        StateStore(config_dir=shared_dir).ensure()

    assert (shared_dir.stat().st_mode & 0o777) == 0o755


def test_store_rejects_non_clawie_sudo_dir_without_chown(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home" / "alice"
    shared_dir = home / "shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "unrelated.txt").write_text("do not manage this directory\n", encoding="utf-8")
    os.chmod(shared_dir, 0o755)
    chown_calls: list[tuple[Path, int, int]] = []

    class UserInfo:
        pw_dir = str(home)
        pw_uid = 501
        pw_gid = 20

    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr("clawie.store.os.geteuid", lambda: 0)
    monkeypatch.setattr("clawie.store.pwd.getpwnam", lambda user: UserInfo())
    monkeypatch.setattr(
        "clawie.store.os.chown",
        lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
    )

    with raises(PermissionError, match="refusing to change permissions"):
        StateStore(config_dir=shared_dir).ensure()

    assert chown_calls == []
    assert (shared_dir.stat().st_mode & 0o777) == 0o755


def test_store_rejects_symlink_state_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "state-link"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)

    with raises(PermissionError, match="state root must not be a symlink"):
        StateStore(config_dir=link).ensure()


def test_store_rejects_symlink_database(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_text("", encoding="utf-8")
    db_link = tmp_path / "clawie.db"
    db_link.symlink_to(target)

    with raises(PermissionError, match="database must not be a symlink"):
        StateStore(config_dir=tmp_path).ensure()


def test_store_preserves_sudo_user_ownership_for_state_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    chown_calls: list[tuple[Path, int, int]] = []

    class UserInfo:
        pw_dir = str(home)
        pw_uid = 501
        pw_gid = 20

    def fake_chown(path: object, uid: int, gid: int) -> None:
        chown_calls.append((Path(path), uid, gid))

    monkeypatch.delenv("CLAWIE_HOME", raising=False)
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr("clawie.store.os.geteuid", lambda: 0)
    monkeypatch.setattr("clawie.store.pwd.getpwnam", lambda user: UserInfo())
    monkeypatch.setattr("clawie.store.os.chown", fake_chown)

    store = StateStore()
    store.ensure()

    assert store.root == home / ".clawie"
    assert (store.root, 501, 20) in chown_calls
    assert (store.db_path, 501, 20) in chown_calls


def test_store_migrates_legacy_users_table_to_agents(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "clawie.db"
    legacy_agent = {"agent_id": "alice", "agent": {"provider": "openclaw"}}
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE templates (name TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE users (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                message TEXT NOT NULL,
                context TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO config(key, value) VALUES (?, ?)",
            ("schema_version", json.dumps(1)),
        )
        conn.execute(
            "INSERT INTO users(user_id, payload) VALUES (?, ?)",
            ("alice", json.dumps(legacy_agent, sort_keys=True)),
        )
        conn.commit()
    conn.close()

    store = StateStore(config_dir=tmp_path)
    state = store.read_state()

    assert state["agents"]["alice"] == legacy_agent
    assert "users" not in state
    assert store.read_config()["schema_version"] == 3
    with store._connect() as conn:
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "agents" in tables
    assert "users" not in tables


def test_store_migrates_legacy_backup_auto_push_to_opt_in(tmp_path: Path) -> None:
    store = StateStore(config_dir=tmp_path)
    store.ensure()
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
            ("schema_version", json.dumps(2)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
            ("backup_auto_push", json.dumps(True)),
        )
        conn.commit()

    migrated = StateStore(config_dir=tmp_path)
    config = migrated.read_config()

    assert config["schema_version"] == 3
    assert config["backup_auto_push"] is False


def test_store_add_column_tolerates_concurrent_duplicate_column_race() -> None:
    class FakeCursor:
        def fetchall(self) -> list[dict[str, str]]:
            return [{"name": "task_id"}]

    class FakeConnection:
        def execute(self, sql: str) -> FakeCursor:
            if sql.startswith("PRAGMA table_info"):
                return FakeCursor()
            if sql.startswith("ALTER TABLE"):
                raise sqlite3.OperationalError("duplicate column name: model_tier")
            raise AssertionError(f"unexpected SQL: {sql}")

    StateStore._add_column_if_missing(
        FakeConnection(),  # type: ignore[arg-type]
        "delegation_tasks",
        "model_tier",
        "TEXT DEFAULT ''",
    )


def test_store_rejects_stale_state_snapshot_instead_of_losing_update(tmp_path: Path) -> None:
    first = StateStore(config_dir=tmp_path)
    second = StateStore(config_dir=tmp_path)
    stale = first.read_state()
    fresh = second.read_state()

    fresh["agents"]["new"] = {"agent_id": "new", "agent": {"provider": "openclaw"}}
    second.write_state(fresh)
    stale["events"].append(
        {"timestamp": "now", "type": "stale", "message": "stale", "context": {}}
    )

    with raises(ConcurrentStateWriteError, match="changed concurrently"):
        first.write_state(stale)
    assert "new" in first.read_state()["agents"]


def test_store_rejects_stale_config_snapshot_instead_of_losing_update(tmp_path: Path) -> None:
    first = StateStore(config_dir=tmp_path)
    second = StateStore(config_dir=tmp_path)
    stale = first.read_config()
    fresh = second.read_config()

    fresh["workspace"] = "new-workspace"
    second.write_config(fresh)
    stale["subscription"] = "pro"

    with raises(ConcurrentStateWriteError, match="changed concurrently"):
        first.write_config(stale)
    assert first.read_config()["workspace"] == "new-workspace"
