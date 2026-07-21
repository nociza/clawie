from __future__ import annotations

import copy
import fcntl
import json
import os
import pwd
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "api_url": "",
    "api_key": "",
    "provider": "openclaw",
    "auth_mode": "none",
    "schema_version": 3,
    "provider_credentials": {},
    "local_service_state": {},
    "channel_pool": [],
    "spawn_password_hash": "",
    "subscription": "starter",
    "workspace": "default",
    "runtime_installed": False,
    "created_at": "",
    "updated_at": "",
    "maintenance_cron_enabled": False,
    "maintenance_cron_interval_hours": 4,
    "backup_enabled": False,
    "backup_repo_path": "",
    "backup_remote": "",
    "backup_auto_push": False,
    "backup_last_attempt_at": "",
    "backup_last_run_at": "",
    "backup_last_commit": "",
    "backup_last_status": "never",
    "backup_last_error": "",
    "published_workspace_root": "",
    "control_operator_allowlist": [],
    "control_github_repo": "",
    "control_github_token_path": "",
    "control_github_issue_labels": ["clawie-control"],
    "control_github_rate_limit_seconds": 3600,
    "control_watchdog_enabled": False,
    "control_watchdog_interval_seconds": 60,
    "control_watchdog_notify_command": "",
}

DEFAULT_STATE: dict[str, Any] = {
    "templates": {
        "baseline": {
            "channels": [],
            "agent_defaults": {
                "runtime": "openclaw-agent",
                "autostart": True,
                "heartbeat_seconds": 30,
            },
        }
    },
    "agents": {},
    "events": [],
}


class ConcurrentStateWriteError(RuntimeError):
    """Raised instead of silently overwriting a newer state/config snapshot."""


class _RevisionedDict(dict[str, Any]):
    def __init__(self, *args: Any, revision: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._clawie_revision = int(revision)


class StateStore:
    SCHEMA_VERSION = 3
    LEGACY_DEFAULT_CHANNELS: tuple[tuple[str, str], ...] = (
        ("chat", "support"),
        ("email", "inbox"),
    )

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._read_only_depth = 0
        self._explicit_config_dir = config_dir is not None
        self._allow_tmp_fallback = config_dir is None and "CLAWIE_HOME" not in os.environ
        if config_dir is None:
            root = self._default_root()
        else:
            root = Path(config_dir).expanduser()
        self._set_root(root)

    def ensure(self) -> None:
        if self._read_only_depth:
            # Observational commands must never initialize, migrate, chmod, or
            # otherwise mutate the live store.  Existing schemas are queried as
            # they are; a missing database is represented by the in-memory
            # defaults returned by the read methods below.
            if self.root.exists() or self.root.is_symlink():
                root_st = self.root.lstat()
                if self.root.is_symlink() or not self.root.is_dir():
                    raise PermissionError(f"clawie state root must be a real directory: {self.root}")
                if self.db_path.exists() or self.db_path.is_symlink():
                    db_st = self.db_path.lstat()
                    if self.db_path.is_symlink() or not stat.S_ISREG(db_st.st_mode):
                        raise PermissionError(
                            f"clawie database must be a regular non-symlink file: {self.db_path}"
                        )
                    if stat.S_IMODE(root_st.st_mode) & 0o077 or stat.S_IMODE(db_st.st_mode) & 0o077:
                        raise PermissionError(
                            "clawie state permissions are not private; run a non-status clawie "
                            "command as the state owner to repair them"
                        )
            return
        self._ensure_root_dir()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS templates (
                    name TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    context TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    cpu_percent REAL NOT NULL,
                    mem_percent REAL NOT NULL,
                    rss_kb INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delegation_tasks (
                    task_id TEXT PRIMARY KEY,
                    parent_agent_id TEXT NOT NULL,
                    child_agent_id TEXT NOT NULL,
                    depth INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    payload TEXT DEFAULT '{}',
                    result TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at TEXT,
                    completed_at TEXT,
                    timeout_seconds REAL DEFAULT 300.0,
                    model_tier TEXT DEFAULT '',
                    context_budget TEXT DEFAULT '{}',
                    root_agent_id TEXT DEFAULT '',
                    root_task_id TEXT DEFAULT '',
                    parent_task_id TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS delegation_trees (
                    root_agent_id TEXT PRIMARY KEY,
                    tree_data TEXT DEFAULT '{}',
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS session_agents (
                    parent_agent_id TEXT NOT NULL,
                    child_agent_id TEXT NOT NULL,
                    pid INTEGER DEFAULT 0,
                    depth INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'running',
                    model_tier TEXT DEFAULT '',
                    socket_path TEXT DEFAULT '',
                    log_path TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    PRIMARY KEY(parent_agent_id, child_agent_id)
                );
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO store_metadata(key, value) VALUES ('config_revision', 0);
                INSERT OR IGNORE INTO store_metadata(key, value) VALUES ('state_revision', 0);
                """
            )
            conn.commit()
        self._migrate_users_table_to_agents()
        self._seed_defaults()
        self._migrate_legacy_json()
        self._migrate_schema()
        self._migrate_delegation_schema()

    def read_config(self) -> dict[str, Any]:
        self.ensure()
        config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        if self._read_only_depth and not self.db_path.exists():
            return _RevisionedDict(config, revision=0)
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
            revision = self._read_revision(conn, "config_revision")
        for row in rows:
            key = str(row["key"])
            if key in config:
                config[key] = self._decode_value(str(row["value"]))
        return _RevisionedDict(config, revision=revision)

    def write_config(self, payload: dict[str, Any]) -> None:
        self.ensure()
        normalized = copy.deepcopy(DEFAULT_CONFIG)
        normalized.update(payload)
        expected_revision = getattr(payload, "_clawie_revision", None)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_revision = self._read_revision(conn, "config_revision")
            if expected_revision is not None and int(expected_revision) != current_revision:
                conn.rollback()
                raise ConcurrentStateWriteError(
                    "configuration changed concurrently; retry the operation against a fresh snapshot"
                )
            for key, value in normalized.items():
                conn.execute(
                    "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
                    (key, self._encode_value(value)),
                )
            self._increment_revision(conn, "config_revision", current_revision)
            conn.commit()

    def read_state(self) -> dict[str, Any]:
        self.ensure()
        state: dict[str, Any] = copy.deepcopy(DEFAULT_STATE)
        if self._read_only_depth and not self.db_path.exists():
            return _RevisionedDict(state, revision=0)
        with self._connect() as conn:
            template_rows = conn.execute("SELECT name, payload FROM templates").fetchall()
            agent_rows = conn.execute("SELECT agent_id, payload FROM agents").fetchall()
            event_rows = conn.execute(
                "SELECT timestamp, type, message, context FROM events ORDER BY id ASC"
            ).fetchall()
            revision = self._read_revision(conn, "state_revision")

        state["templates"] = {}
        for row in template_rows:
            state["templates"][str(row["name"])] = self._decode_json_obj(str(row["payload"]))
        if not state["templates"]:
            state["templates"] = copy.deepcopy(DEFAULT_STATE["templates"])

        state["agents"] = {}
        for row in agent_rows:
            state["agents"][str(row["agent_id"])] = self._decode_json_obj(str(row["payload"]))

        state["events"] = []
        for row in event_rows:
            context = self._decode_json_obj(str(row["context"]))
            state["events"].append(
                {
                    "timestamp": str(row["timestamp"]),
                    "type": str(row["type"]),
                    "message": str(row["message"]),
                    "context": context,
                }
            )
        return _RevisionedDict(state, revision=revision)

    def write_state(self, payload: dict[str, Any]) -> None:
        self.ensure()
        templates = payload.get("templates", {})
        agents = payload.get("agents", {})
        events = payload.get("events", [])
        if not isinstance(templates, dict) or not isinstance(agents, dict) or not isinstance(events, list):
            raise ValueError("state payload must include templates/agents/events")

        expected_revision = getattr(payload, "_clawie_revision", None)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_revision = self._read_revision(conn, "state_revision")
            if expected_revision is not None and int(expected_revision) != current_revision:
                conn.rollback()
                raise ConcurrentStateWriteError(
                    "state changed concurrently; retry the operation against a fresh snapshot"
                )
            conn.execute("DELETE FROM templates")
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM events")

            for name, template in templates.items():
                conn.execute(
                    "INSERT INTO templates(name, payload) VALUES (?, ?)",
                    (str(name), json.dumps(template, sort_keys=True)),
                )

            for agent_id, agent in agents.items():
                conn.execute(
                    "INSERT INTO agents(agent_id, payload) VALUES (?, ?)",
                    (str(agent_id), json.dumps(agent, sort_keys=True)),
                )

            for event in events:
                conn.execute(
                    "INSERT INTO events(timestamp, type, message, context) VALUES (?, ?, ?, ?)",
                    (
                        str(event.get("timestamp", "")),
                        str(event.get("type", "")),
                        str(event.get("message", "")),
                        json.dumps(event.get("context", {}), sort_keys=True),
                    ),
                )
            self._increment_revision(conn, "state_revision", current_revision)
            conn.commit()

    def write_snapshot(self, config: dict[str, Any], state: dict[str, Any]) -> None:
        """Replace configuration and state in one all-or-nothing transaction.

        All serialization and structural validation happens before the write
        transaction begins, so a malformed imported snapshot cannot leave the
        configuration and fleet state out of sync.
        """
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise ValueError("snapshot config and state must be JSON objects")
        templates = state.get("templates", {})
        agents = state.get("agents", {})
        events = state.get("events", [])
        if not isinstance(templates, dict) or not isinstance(agents, dict) or not isinstance(events, list):
            raise ValueError("state payload must include templates/agents/events")
        if any(not isinstance(value, dict) for value in templates.values()):
            raise ValueError("every imported template must be a JSON object")
        if any(not isinstance(value, dict) for value in agents.values()):
            raise ValueError("every imported agent must be a JSON object")
        if any(not isinstance(value, dict) for value in events):
            raise ValueError("every imported event must be a JSON object")

        normalized_config = copy.deepcopy(DEFAULT_CONFIG)
        normalized_config.update({key: value for key, value in config.items() if key in DEFAULT_CONFIG})
        encoded_config = {
            key: self._encode_value(value) for key, value in normalized_config.items()
        }
        encoded_templates = {
            str(name): json.dumps(value, sort_keys=True) for name, value in templates.items()
        }
        encoded_agents = {
            str(agent_id): json.dumps(value, sort_keys=True) for agent_id, value in agents.items()
        }
        encoded_events = [
            (
                str(event.get("timestamp", "")),
                str(event.get("type", "")),
                str(event.get("message", "")),
                json.dumps(event.get("context", {}), sort_keys=True),
            )
            for event in events
        ]

        self.ensure()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            config_revision = self._read_revision(conn, "config_revision")
            state_revision = self._read_revision(conn, "state_revision")
            conn.execute("DELETE FROM config")
            conn.execute("DELETE FROM templates")
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM events")
            conn.executemany(
                "INSERT INTO config(key, value) VALUES (?, ?)",
                encoded_config.items(),
            )
            conn.executemany(
                "INSERT INTO templates(name, payload) VALUES (?, ?)",
                encoded_templates.items(),
            )
            conn.executemany(
                "INSERT INTO agents(agent_id, payload) VALUES (?, ?)",
                encoded_agents.items(),
            )
            conn.executemany(
                "INSERT INTO events(timestamp, type, message, context) VALUES (?, ?, ?, ?)",
                encoded_events,
            )
            self._increment_revision(conn, "config_revision", config_revision)
            self._increment_revision(conn, "state_revision", state_revision)
            conn.commit()

    @staticmethod
    def _read_revision(conn: sqlite3.Connection, key: str) -> int:
        row = conn.execute("SELECT value FROM store_metadata WHERE key = ?", (key,)).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _increment_revision(conn: sqlite3.Connection, key: str, current: int) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO store_metadata(key, value) VALUES (?, ?)",
            (key, int(current) + 1),
        )

    def write_metric(
        self,
        timestamp: str,
        user_id: str,
        cpu_percent: float,
        mem_percent: float,
        rss_kb: int,
        status: str,
    ) -> None:
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO metrics(timestamp, user_id, cpu_percent, mem_percent, rss_kb, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (timestamp, user_id, cpu_percent, mem_percent, rss_kb, status),
            )
            conn.commit()

    def latest_metrics(self, limit_per_user: int = 1) -> dict[str, list[dict[str, Any]]]:
        self.ensure()
        if self._read_only_depth and not self.db_path.exists():
            return {}
        limit = max(1, int(limit_per_user))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, user_id, cpu_percent, mem_percent, rss_kb, status
                FROM metrics
                ORDER BY id DESC
                """
            ).fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            user_id = str(row["user_id"])
            bucket = grouped.setdefault(user_id, [])
            if len(bucket) >= limit:
                continue
            bucket.append(
                {
                    "timestamp": str(row["timestamp"]),
                    "cpu_percent": float(row["cpu_percent"]),
                    "mem_percent": float(row["mem_percent"]),
                    "rss_kb": int(row["rss_kb"]),
                    "status": str(row["status"]),
                }
            )
        return grouped

    def _migrate_delegation_schema(self) -> None:
        """Add delegation columns and indexes introduced after the base schema."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._add_column_if_missing(
                    conn,
                    "delegation_tasks",
                    "model_tier",
                    "TEXT DEFAULT ''",
                )
                self._add_column_if_missing(
                    conn,
                    "delegation_tasks",
                    "context_budget",
                    "TEXT DEFAULT '{}'",
                )
                self._add_column_if_missing(
                    conn,
                    "delegation_tasks",
                    "root_agent_id",
                    "TEXT DEFAULT ''",
                )
                self._add_column_if_missing(
                    conn,
                    "delegation_tasks",
                    "root_task_id",
                    "TEXT DEFAULT ''",
                )
                self._add_column_if_missing(
                    conn,
                    "delegation_tasks",
                    "parent_task_id",
                    "TEXT DEFAULT ''",
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS delegation_tasks_status_parent "
                    "ON delegation_tasks(status, parent_agent_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS delegation_tasks_status_child "
                    "ON delegation_tasks(status, child_agent_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS delegation_tasks_root "
                    "ON delegation_tasks(root_task_id, created_at)"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _add_column_if_missing(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        cols = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in cols:
            return
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                return
            raise

    # ------------------------------------------------------------------
    # Delegation task CRUD
    # ------------------------------------------------------------------

    def write_delegation_task(
        self,
        task_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        depth: int = 0,
        status: str = "pending",
        payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str = "",
        created_at: str = "",
        completed_at: str = "",
        timeout_seconds: float = 300.0,
        model_tier: str = "",
        context_budget: dict[str, Any] | None = None,
        root_agent_id: str = "",
        root_task_id: str = "",
        parent_task_id: str = "",
    ) -> None:
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO delegation_tasks
                (task_id, parent_agent_id, child_agent_id, depth, status,
                 payload, result, error, created_at, completed_at, timeout_seconds,
                 model_tier, context_budget, root_agent_id, root_task_id, parent_task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    parent_agent_id,
                    child_agent_id,
                    depth,
                    status,
                    json.dumps(payload or {}, sort_keys=True),
                    json.dumps(result or {}, sort_keys=True),
                    error,
                    created_at,
                    completed_at,
                    timeout_seconds,
                    model_tier,
                    json.dumps(context_budget or {}, sort_keys=True),
                    root_agent_id,
                    root_task_id,
                    parent_task_id,
                ),
            )
            conn.commit()

    def reserve_delegation_task(
        self,
        *,
        task_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        payload: dict[str, Any],
        created_at: str,
        timeout_seconds: float,
        model_tier: str,
        context_budget: dict[str, Any],
        depth_limits: dict[str, int],
        max_depth: int,
        max_children: int,
        parent_task_id: str = "",
    ) -> dict[str, Any]:
        """Atomically validate and reserve one edge in the active task graph.

        The state-directory lock and ``BEGIN IMMEDIATE`` make graph validation
        race-free across CLI and daemon processes.  Only active tasks constrain
        a new independent tree, while every node already recorded in the same
        active tree remains protected from cycles and duplicate placement.
        """
        self.ensure()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale_rows = conn.execute(
                """
                SELECT * FROM delegation_tasks
                 WHERE status IN ('pending', 'running')
                   AND COALESCE(created_at, '') <> ''
                   AND (julianday(?) - julianday(created_at)) * 86400.0
                       > MAX(timeout_seconds, 0.0) + 30.0
                """,
                (created_at,),
            ).fetchall()
            conn.execute(
                """
                UPDATE delegation_tasks
                   SET status = 'timeout',
                       completed_at = ?,
                       error = CASE WHEN error = ''
                           THEN 'delegation lease expired before completion'
                           ELSE error END
                 WHERE status IN ('pending', 'running')
                   AND COALESCE(created_at, '') <> ''
                   AND (julianday(?) - julianday(created_at)) * 86400.0
                       > MAX(timeout_seconds, 0.0) + 30.0
                """,
                (created_at, created_at),
            )
            stale_generations = {
                (
                    str(row["root_agent_id"] or row["parent_agent_id"]),
                    str(row["root_task_id"] or row["task_id"]),
                )
                for row in stale_rows
            }
            for stale_root_agent, stale_root_task in stale_generations:
                stale_tree_row = conn.execute(
                    "SELECT tree_data FROM delegation_trees WHERE root_agent_id = ?",
                    (stale_root_agent,),
                ).fetchone()
                if stale_tree_row is None:
                    continue
                stale_tree = self._decode_json_obj(str(stale_tree_row["tree_data"] or "{}"))
                root_node = stale_tree.get(stale_root_agent)
                if not isinstance(root_node, dict) or str(root_node.get("task_id", "")) != (
                    f"{stale_root_task}:root"
                ):
                    continue
                for stale_row in stale_rows:
                    row_root_agent = str(
                        stale_row["root_agent_id"] or stale_row["parent_agent_id"]
                    )
                    row_root_task = str(stale_row["root_task_id"] or stale_row["task_id"])
                    if (row_root_agent, row_root_task) != (
                        stale_root_agent,
                        stale_root_task,
                    ):
                        continue
                    node = stale_tree.get(str(stale_row["child_agent_id"]))
                    if isinstance(node, dict) and str(node.get("task_id", "")) == str(
                        stale_row["task_id"]
                    ):
                        node["status"] = "timeout"
                active_in_generation = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM delegation_tasks "
                        "WHERE root_task_id = ? AND status IN ('pending', 'running')",
                        (stale_root_task,),
                    ).fetchone()[0]
                )
                if active_in_generation == 0:
                    root_node["status"] = "failed"
                conn.execute(
                    """
                    UPDATE delegation_trees SET tree_data = ?, updated_at = ?
                    WHERE root_agent_id = ?
                    """,
                    (json.dumps(stale_tree, sort_keys=True), created_at, stale_root_agent),
                )
            active = conn.execute(
                "SELECT * FROM delegation_tasks "
                "WHERE status IN ('pending', 'running') ORDER BY created_at, task_id"
            ).fetchall()
            active_by_task = {str(row["task_id"]): row for row in active}
            incoming = [row for row in active if str(row["child_agent_id"]) == parent_agent_id]
            requested_parent_task = str(parent_task_id or "").strip()

            lineage_row: sqlite3.Row | None = None
            if requested_parent_task:
                lineage_row = active_by_task.get(requested_parent_task)
                if lineage_row is None:
                    raise ValueError(
                        f"parent task is not active: {requested_parent_task}"
                    )
                if str(lineage_row["child_agent_id"]) != parent_agent_id:
                    raise ValueError(
                        f"parent task {requested_parent_task} belongs to "
                        f"{lineage_row['child_agent_id']}, not {parent_agent_id}"
                    )
            elif len(incoming) == 1:
                lineage_row = incoming[0]
            elif len(incoming) > 1:
                raise ValueError(
                    f"agent {parent_agent_id} has multiple active parent tasks; "
                    "specify --parent-task"
                )

            root_agent_id = parent_agent_id
            root_task_id = task_id
            effective_parent_task = ""
            tree_data: dict[str, Any] = {}
            if lineage_row is not None:
                root_agent_id = str(lineage_row["root_agent_id"] or "") or str(
                    lineage_row["parent_agent_id"]
                )
                root_task_id = str(lineage_row["root_task_id"] or "") or str(
                    lineage_row["task_id"]
                )
                effective_parent_task = str(lineage_row["task_id"])
            else:
                direct_roots = {
                    str(row["root_task_id"] or row["task_id"])
                    for row in active
                    if str(row["parent_agent_id"]) == parent_agent_id
                    and (str(row["root_agent_id"] or "") or parent_agent_id)
                    == parent_agent_id
                    and not str(row["parent_task_id"] or "")
                }
                if len(direct_roots) > 1:
                    raise ValueError(
                        f"agent {parent_agent_id} has multiple active root trees; "
                        "wait for one to finish"
                    )
                if direct_roots:
                    root_task_id = next(iter(direct_roots))

            tree_row = conn.execute(
                "SELECT tree_data FROM delegation_trees WHERE root_agent_id = ?",
                (root_agent_id,),
            ).fetchone()
            if tree_row is not None:
                decoded = self._decode_json_obj(str(tree_row["tree_data"] or "{}"))
                root_node = decoded.get(root_agent_id)
                expected_root_task = f"{root_task_id}:root"
                if isinstance(root_node, dict) and str(root_node.get("task_id", "")) == expected_root_task:
                    tree_data = decoded

            if not tree_data:
                if lineage_row is not None:
                    raise ValueError(
                        f"active delegation lineage for {parent_agent_id} has no durable tree"
                    )
                tree_data = {
                    root_agent_id: {
                        "agent_id": root_agent_id,
                        "parent_id": "",
                        "task_id": f"{root_task_id}:root",
                        "depth": 0,
                        "status": "running",
                        "children": [],
                        "model_tier": model_tier,
                    }
                }

            parent_node = tree_data.get(parent_agent_id)
            if not isinstance(parent_node, dict):
                raise ValueError(
                    f"parent {parent_agent_id} is not present in active delegation tree "
                    f"rooted at {root_agent_id}"
                )
            ancestry: set[str] = set()
            current = parent_agent_id
            while current:
                if current in ancestry:
                    raise ValueError(f"active delegation tree already contains a cycle at {current}")
                ancestry.add(current)
                current_node = tree_data.get(current)
                if not isinstance(current_node, dict):
                    raise ValueError(f"active delegation tree is missing node {current}")
                current = str(current_node.get("parent_id", ""))
            if child_agent_id in ancestry:
                raise ValueError(
                    f"delegation cycle detected: {child_agent_id} already in ancestry"
                )

            active_participants = {
                str(value)
                for row in active
                for value in (row["parent_agent_id"], row["child_agent_id"])
            }
            if child_agent_id in active_participants:
                raise ValueError(
                    f"child {child_agent_id} already participates in an active delegation tree"
                )
            if child_agent_id in tree_data:
                raise ValueError(
                    f"child {child_agent_id} already appears in the current delegation tree"
                )

            parent_depth = int(parent_node.get("depth", 0))
            depth = parent_depth + 1
            depth_limit = int(depth_limits.get(root_agent_id, max_depth))
            if depth >= depth_limit:
                raise ValueError(
                    f"max recursion depth ({depth_limit}) exceeded at depth={depth}"
                )
            children = parent_node.get("children", [])
            if not isinstance(children, list):
                raise ValueError(f"active delegation tree has invalid children for {parent_agent_id}")
            if len(children) >= max_children:
                raise ValueError(
                    f"max children ({max_children}) exceeded for {parent_agent_id}"
                )

            parent_node["status"] = "running"
            children.append(child_agent_id)
            tree_data[child_agent_id] = {
                "agent_id": child_agent_id,
                "parent_id": parent_agent_id,
                "task_id": task_id,
                "depth": depth,
                "status": "running",
                "children": [],
                "model_tier": model_tier,
            }
            conn.execute(
                """
                INSERT INTO delegation_tasks
                (task_id, parent_agent_id, child_agent_id, depth, status,
                 payload, result, error, created_at, completed_at, timeout_seconds,
                 model_tier, context_budget, root_agent_id, root_task_id, parent_task_id)
                VALUES (?, ?, ?, ?, 'running', ?, '{}', '', ?, '', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    parent_agent_id,
                    child_agent_id,
                    depth,
                    json.dumps(payload, sort_keys=True),
                    created_at,
                    timeout_seconds,
                    model_tier,
                    json.dumps(context_budget, sort_keys=True),
                    root_agent_id,
                    root_task_id,
                    effective_parent_task,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO delegation_trees(root_agent_id, tree_data, updated_at)
                VALUES (?, ?, ?)
                """,
                (root_agent_id, json.dumps(tree_data, sort_keys=True), created_at),
            )
            conn.commit()
        return {
            "task_id": task_id,
            "root_agent_id": root_agent_id,
            "root_task_id": root_task_id,
            "parent_task_id": effective_parent_task,
            "depth": depth,
        }

    def complete_delegation_task(
        self,
        *,
        task_id: str,
        status: str,
        result: dict[str, Any],
        error: str,
        completed_at: str,
        context_budget: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete a reserved task and its durable tree node atomically."""
        if status not in {"completed", "failed", "timeout"}:
            raise ValueError("delegation completion status must be completed, failed, or timeout")
        self.ensure()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM delegation_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"delegation task not found: {task_id}")
            current_status = str(row["status"])
            if current_status not in {"pending", "running"}:
                raise ValueError(
                    f"delegation task is no longer active: {task_id} ({current_status})"
                )
            conn.execute(
                """
                UPDATE delegation_tasks
                   SET status = ?, result = ?, error = ?, completed_at = ?, context_budget = ?
                 WHERE task_id = ?
                """,
                (
                    status,
                    json.dumps(result, sort_keys=True),
                    error,
                    completed_at,
                    json.dumps(context_budget, sort_keys=True),
                    task_id,
                ),
            )
            root_agent_id = str(row["root_agent_id"] or row["parent_agent_id"])
            root_task_id = str(row["root_task_id"] or row["task_id"])
            tree_row = conn.execute(
                "SELECT tree_data FROM delegation_trees WHERE root_agent_id = ?",
                (root_agent_id,),
            ).fetchone()
            if tree_row is not None:
                tree_data = self._decode_json_obj(str(tree_row["tree_data"] or "{}"))
                child_id = str(row["child_agent_id"])
                child_node = tree_data.get(child_id)
                if isinstance(child_node, dict) and str(child_node.get("task_id", "")) == task_id:
                    child_node["status"] = status
                active_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM delegation_tasks "
                        "WHERE root_task_id = ? AND status IN ('pending', 'running')",
                        (root_task_id,),
                    ).fetchone()[0]
                )
                if active_count == 0:
                    failed_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM delegation_tasks "
                            "WHERE root_task_id = ? AND status IN ('failed', 'timeout')",
                            (root_task_id,),
                        ).fetchone()[0]
                    )
                    root_node = tree_data.get(root_agent_id)
                    if isinstance(root_node, dict):
                        root_node["status"] = "failed" if failed_count else "completed"
                conn.execute(
                    """
                    UPDATE delegation_trees SET tree_data = ?, updated_at = ?
                    WHERE root_agent_id = ?
                    """,
                    (json.dumps(tree_data, sort_keys=True), completed_at, root_agent_id),
                )
            conn.commit()
        return {
            "task_id": task_id,
            "parent_agent_id": str(row["parent_agent_id"]),
            "child_agent_id": str(row["child_agent_id"]),
            "root_agent_id": root_agent_id,
            "root_task_id": root_task_id,
            "parent_task_id": str(row["parent_task_id"] or ""),
            "depth": int(row["depth"]),
            "model_tier": str(row["model_tier"] or ""),
        }

    def read_delegation_tasks(
        self,
        parent_agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.ensure()
        if self._read_only_depth and not self.db_path.exists():
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if parent_agent_id:
            clauses.append("parent_agent_id = ?")
            params.append(parent_agent_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, limit))
        if len(clauses) == 2:
            query = (
                "SELECT * FROM delegation_tasks "
                "WHERE parent_agent_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
        elif parent_agent_id:
            query = (
                "SELECT * FROM delegation_tasks WHERE parent_agent_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
        elif status:
            query = (
                "SELECT * FROM delegation_tasks WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
        else:
            query = "SELECT * FROM delegation_tasks ORDER BY created_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            d: dict[str, Any] = {
                "task_id": str(row["task_id"]),
                "parent_agent_id": str(row["parent_agent_id"]),
                "child_agent_id": str(row["child_agent_id"]),
                "depth": int(row["depth"]),
                "status": str(row["status"]),
                "payload": self._decode_json_obj(str(row["payload"])),
                "result": self._decode_json_obj(str(row["result"])),
                "error": str(row["error"]),
                "created_at": str(row["created_at"]),
                "completed_at": str(row["completed_at"]),
                "timeout_seconds": float(row["timeout_seconds"]),
            }
            # Safe accessor for columns added by migration
            try:
                d["model_tier"] = str(row["model_tier"] or "")
            except (IndexError, KeyError):
                d["model_tier"] = ""
            try:
                d["context_budget"] = self._decode_json_obj(str(row["context_budget"] or "{}"))
            except (IndexError, KeyError):
                d["context_budget"] = {}
            for column in ("root_agent_id", "root_task_id", "parent_task_id"):
                try:
                    d[column] = str(row[column] or "")
                except (IndexError, KeyError):
                    d[column] = ""
            results.append(d)
        return results

    def write_delegation_tree(
        self, root_agent_id: str, tree_data: dict[str, Any]
    ) -> None:
        self.ensure()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO delegation_trees
                (root_agent_id, tree_data, updated_at) VALUES (?, ?, ?)
                """,
                (root_agent_id, json.dumps(tree_data, sort_keys=True), now),
            )
            conn.commit()

    def read_delegation_tree(self, root_agent_id: str) -> dict[str, Any]:
        self.ensure()
        if self._read_only_depth and not self.db_path.exists():
            return {}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tree_data FROM delegation_trees WHERE root_agent_id = ?",
                (root_agent_id,),
            ).fetchone()
        if row:
            return self._decode_json_obj(str(row["tree_data"]))
        return {}

    def delete_delegation_tree(self, root_agent_id: str) -> None:
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM delegation_trees WHERE root_agent_id = ?",
                (root_agent_id,),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Session agent metadata
    # ------------------------------------------------------------------

    def write_session_agent(
        self,
        *,
        parent_agent_id: str,
        child_agent_id: str,
        pid: int = 0,
        depth: int = 1,
        status: str = "running",
        model_tier: str = "",
        socket_path: str = "",
        log_path: str = "",
        created_at: str = "",
        updated_at: str = "",
    ) -> None:
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO session_agents
                (parent_agent_id, child_agent_id, pid, depth, status, model_tier,
                 socket_path, log_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parent_agent_id,
                    child_agent_id,
                    int(pid),
                    int(depth),
                    status,
                    model_tier,
                    socket_path,
                    log_path,
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()

    def read_session_agents(
        self, parent_agent_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.ensure()
        if self._read_only_depth and not self.db_path.exists():
            return []
        params: list[Any] = []
        if parent_agent_id:
            params.append(parent_agent_id)
            query = (
                "SELECT * FROM session_agents WHERE parent_agent_id = ? "
                "ORDER BY created_at ASC"
            )
        else:
            query = "SELECT * FROM session_agents ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._session_agent_row(row) for row in rows]

    def read_session_agent(
        self, parent_agent_id: str, child_agent_id: str
    ) -> dict[str, Any] | None:
        self.ensure()
        if self._read_only_depth and not self.db_path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM session_agents
                WHERE parent_agent_id = ? AND child_agent_id = ?
                """,
                (parent_agent_id, child_agent_id),
            ).fetchone()
        if row is None:
            return None
        return self._session_agent_row(row)

    def delete_session_agent(self, parent_agent_id: str, child_agent_id: str) -> None:
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM session_agents
                WHERE parent_agent_id = ? AND child_agent_id = ?
                """,
                (parent_agent_id, child_agent_id),
            )
            conn.commit()

    @staticmethod
    def _session_agent_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "parent_agent_id": str(row["parent_agent_id"]),
            "child_agent_id": str(row["child_agent_id"]),
            "pid": int(row["pid"]),
            "depth": int(row["depth"]),
            "status": str(row["status"]),
            "model_tier": str(row["model_tier"] or ""),
            "socket_path": str(row["socket_path"] or ""),
            "log_path": str(row["log_path"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    @staticmethod
    def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return fallback
            return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return fallback

    @staticmethod
    def _decode_json_obj(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {}

    @staticmethod
    def _encode_value(value: Any) -> str:
        return json.dumps(value)

    @staticmethod
    def _decode_value(raw: str) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _seed_defaults(self) -> None:
        with self._connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0])
            if count == 0:
                for name, payload in DEFAULT_STATE["templates"].items():
                    conn.execute(
                        "INSERT OR IGNORE INTO templates(name, payload) VALUES (?, ?)",
                        (name, json.dumps(payload, sort_keys=True)),
                    )

            config_count = int(conn.execute("SELECT COUNT(*) FROM config").fetchone()[0])
            if config_count == 0:
                for key, value in DEFAULT_CONFIG.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)",
                        (key, self._encode_value(value)),
                    )
            conn.commit()

    def _migrate_legacy_json(self) -> None:
        if not self.db_path.exists():
            return
        with self._connect() as conn:
            has_agents = int(conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]) > 0
            has_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]) > 0

        if has_agents or has_events:
            return

        if self.config_path.exists():
            config = self._read_json(self.config_path, copy.deepcopy(DEFAULT_CONFIG))
            self.write_config(config)

        if self.state_path.exists():
            state = self._read_json(self.state_path, copy.deepcopy(DEFAULT_STATE))
            self.write_state(state)

    def _migrate_schema(self) -> None:
        current = self._stored_schema_version()
        if current >= self.SCHEMA_VERSION:
            return
        if current < 1:
            self._migrate_remove_legacy_default_channels()
            current = 1
        if current < 2:
            current = 2
        if current < 3:
            self._migrate_disable_legacy_backup_auto_push()
            current = 3
        if current != self._stored_schema_version():
            self._write_schema_version(current)

    def _migrate_users_table_to_agents(self) -> None:
        with self._connect() as conn:
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "users" not in tables:
                return
            rows = conn.execute("SELECT user_id, payload FROM users").fetchall()
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO agents(agent_id, payload) VALUES (?, ?)",
                    (str(row["user_id"]), str(row["payload"])),
                )
            conn.execute("DROP TABLE users")
            conn.commit()

    def _stored_schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM config WHERE key = ?",
                ("schema_version",),
            ).fetchone()
        if row is None:
            return 0
        try:
            return int(self._decode_value(str(row["value"])))
        except (TypeError, ValueError):
            return 0

    def _write_schema_version(self, version: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
                ("schema_version", self._encode_value(int(version))),
            )
            conn.commit()

    @classmethod
    def _is_legacy_default_channel(cls, channel: dict[str, Any], agent_id: str = "") -> bool:
        if not isinstance(channel, dict):
            return False
        kind = str(channel.get("kind", "")).strip().lower()
        name = str(channel.get("name", "")).strip()
        if not kind or not name:
            return False
        for expected_kind, base_name in cls.LEGACY_DEFAULT_CHANNELS:
            if kind != expected_kind:
                continue
            if name == base_name:
                return True
            if agent_id and name == f"{agent_id}-{base_name}":
                return True
        return False

    def _migrate_remove_legacy_default_channels(self) -> None:
        with self._connect() as conn:
            template_rows = conn.execute("SELECT name, payload FROM templates").fetchall()
            agent_rows = conn.execute("SELECT agent_id, payload FROM agents").fetchall()

            for row in template_rows:
                name = str(row["name"])
                if name != "baseline":
                    continue
                payload = self._decode_json_obj(str(row["payload"]))
                channels = payload.get("channels", [])
                if not isinstance(channels, list):
                    continue
                filtered = [item for item in channels if not self._is_legacy_default_channel(item)]
                if len(filtered) == len(channels):
                    continue
                payload["channels"] = filtered
                conn.execute(
                    "UPDATE templates SET payload = ? WHERE name = ?",
                    (json.dumps(payload, sort_keys=True), name),
                )

            for row in agent_rows:
                agent_id = str(row["agent_id"])
                payload = self._decode_json_obj(str(row["payload"]))
                channels = payload.get("channels", [])
                if not isinstance(channels, list):
                    continue
                filtered = [item for item in channels if not self._is_legacy_default_channel(item, agent_id)]
                if len(filtered) == len(channels):
                    continue
                payload["channels"] = filtered
                conn.execute(
                    "UPDATE agents SET payload = ? WHERE agent_id = ?",
                    (json.dumps(payload, sort_keys=True), agent_id),
                )

            conn.commit()

    def _migrate_disable_legacy_backup_auto_push(self) -> None:
        """Replace the pre-0.1.8 fail-open backup push default.

        Earlier releases persisted ``true`` for every newly created state root,
        so the value cannot reliably represent an operator opt-in. Operators
        can explicitly enable it again after reviewing the backup repository.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
                ("backup_auto_push", self._encode_value(False)),
            )
            conn.commit()

    @contextmanager
    def read_only(self) -> Iterator[None]:
        """Prevent persistent store mutations for an observational operation."""
        self._read_only_depth += 1
        try:
            yield
        finally:
            self._read_only_depth -= 1

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a transactional connection and always release its resources.

        ``sqlite3.Connection`` commits or rolls back when used as a context
        manager, but it does not close itself.  Keeping the close here makes
        every existing ``with self._connect()`` call safe on Python versions
        that report unclosed database handles as ``ResourceWarning``.
        """
        if self._read_only_depth:
            if self.db_path.is_symlink():
                raise PermissionError(f"clawie database must not be a symlink: {self.db_path}")
            if not self.db_path.exists():
                raise FileNotFoundError(self.db_path)
            with self._database_lock(exclusive=False):
                self._assert_safe_database_sidecars(require_checkpointed=True)
                database_uri = self.db_path.resolve().as_uri() + "?mode=ro&immutable=1"
                conn = sqlite3.connect(database_uri, uri=True, timeout=30.0)
                try:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA query_only = ON")
                    conn.execute("PRAGMA busy_timeout = 30000")
                    yield conn
                finally:
                    conn.close()
            return

        self._ensure_root_dir()
        if self.db_path.is_symlink():
            raise PermissionError(f"clawie database must not be a symlink: {self.db_path}")
        with self._database_lock(exclusive=True):
            self._assert_safe_database_sidecars(require_checkpointed=False)
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 30000")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                self._harden_db_files()
                with conn:
                    yield conn
            finally:
                conn.close()

    @contextmanager
    def _database_lock(self, *, exclusive: bool) -> Iterator[None]:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.root, flags)
        except OSError as exc:
            raise PermissionError(f"cannot safely lock clawie state root {self.root}: {exc}") from exc
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + 30.0
        try:
            lock_st = os.fstat(fd)
            if not stat.S_ISDIR(lock_st.st_mode):
                raise PermissionError(f"clawie state lock is not a directory: {self.root}")
            while True:
                try:
                    fcntl.flock(fd, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for the clawie database lock") from None
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _assert_safe_database_sidecars(self, *, require_checkpointed: bool) -> None:
        for suffix in ("-wal", "-shm"):
            path = self.db_path.with_name(self.db_path.name + suffix)
            try:
                path_st = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(path_st.st_mode) or not stat.S_ISREG(path_st.st_mode):
                raise PermissionError(f"clawie database sidecar must be a regular file: {path}")
            if stat.S_IMODE(path_st.st_mode) & 0o077:
                raise PermissionError(f"clawie database sidecar permissions are not private: {path}")
            if require_checkpointed and suffix == "-wal" and int(path_st.st_size) > 0:
                raise PermissionError(
                    "read-only status cannot inspect an uncheckpointed clawie WAL; "
                    "retry after the active writer finishes or run a normal clawie command"
                )

    def _set_root(self, root: Path) -> None:
        self.root = root.expanduser()
        self.db_path = self.root / "clawie.db"
        self.config_path = self.root / "config.json"  # legacy migration input
        self.state_path = self.root / "state.json"  # legacy migration input

    def _ensure_root_dir(self) -> None:
        existed = self.root.exists() or self.root.is_symlink()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            if not self._allow_tmp_fallback:
                raise
            fallback = self._fallback_root()
            self._set_root(fallback)
            existed = self.root.exists() or self.root.is_symlink()
            self.root.mkdir(parents=True, exist_ok=True)
        self._harden_root_permissions(created=not existed)

    def _harden_root_permissions(self, *, created: bool) -> None:
        if self.root.is_symlink():
            raise PermissionError(f"clawie state root must not be a symlink: {self.root}")
        try:
            current_mode = int(self.root.stat().st_mode) & 0o777
        except OSError:
            return
        if current_mode != 0o700:
            if self._explicit_config_dir and not created and not self._is_repairable_state_root():
                raise PermissionError(
                    "refusing to change permissions on non-clawie state directory: "
                    f"{self.root}. Use a dedicated empty directory, CLAWIE_HOME, or --config-dir."
                )
        owner = self._owner_for_managed_path(self.root)
        if owner is not None:
            try:
                os.chown(self.root, owner[0], owner[1])
            except OSError:
                pass
        if current_mode != 0o700:
            os.chmod(self.root, 0o700)

    def _is_repairable_state_root(self) -> bool:
        try:
            resolved = self.root.resolve()
        except OSError:
            return False
        reserved = {Path("/").resolve(), Path(tempfile.gettempdir()).resolve()}
        if resolved in reserved:
            return False
        markers = {
            "clawie.db",
            "clawie.db-wal",
            "clawie.db-shm",
            "config.json",
            "state.json",
            "clawied-status.json",
            "clawied.pid",
            "clawied.lock",
            "clawied.sock",
            "manifests",
            "shared-provider-auth",
            "shared-addon-auth",
            "shared-toolchain",
        }
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return False
        if not entries:
            return True
        return any(entry.name in markers for entry in entries)

    def _harden_db_files(self) -> None:
        for path in (
            self.db_path,
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            try:
                if path.exists() and not path.is_symlink():
                    owner = self._owner_for_managed_path(path)
                    if owner is not None:
                        try:
                            os.chown(path, owner[0], owner[1])
                        except OSError:
                            pass
                    os.chmod(path, 0o600)
            except OSError:
                continue

    @staticmethod
    def _sudo_user_owner_for_path(path: Path) -> tuple[int, int] | None:
        if os.geteuid() != 0:
            return None
        sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
        if not sudo_user or sudo_user == "root":
            return None
        try:
            row = pwd.getpwnam(sudo_user)
        except KeyError:
            return None
        home = Path(row.pw_dir).expanduser()
        try:
            resolved_home = home.resolve()
            resolved_path = path.resolve()
        except OSError:
            return None
        try:
            resolved_path.relative_to(resolved_home)
        except ValueError:
            return None
        return int(row.pw_uid), int(row.pw_gid)

    def _owner_for_managed_path(self, path: Path) -> tuple[int, int] | None:
        sudo_owner = self._sudo_user_owner_for_path(path)
        if sudo_owner is not None:
            return sudo_owner
        if os.geteuid() != 0:
            return None
        try:
            root_st = self.root.stat()
            resolved_root = self.root.resolve()
            resolved_path = path.resolve()
            resolved_path.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        if int(root_st.st_uid) == 0:
            return None
        return int(root_st.st_uid), int(root_st.st_gid)

    @staticmethod
    def _fallback_root() -> Path:
        uid_fn = getattr(os, "getuid", None)
        if callable(uid_fn):
            suffix = str(uid_fn())
        else:
            suffix = str(os.environ.get("USERNAME", "user"))
        return Path(tempfile.gettempdir()) / f"clawie-{suffix}"

    @staticmethod
    def _default_root() -> Path:
        explicit = str(os.environ.get("CLAWIE_HOME", "")).strip()
        if explicit:
            return Path(explicit).expanduser()
        sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
        if os.geteuid() == 0 and sudo_user and sudo_user != "root":
            try:
                home = Path(pwd.getpwnam(sudo_user).pw_dir)
                return home / ".clawie"
            except KeyError:
                pass
        return Path.home() / ".clawie"
