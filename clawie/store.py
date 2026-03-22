from __future__ import annotations

import copy
import json
import os
import pwd
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "api_url": "https://api.openclaw.example/v1",
    "api_key": "",
    "provider": "openclaw",
    "auth_mode": "none",
    "schema_version": 1,
    "provider_credentials": {},
    "local_service_state": {},
    "channel_pool": [],
    "spawn_password_hash": "",
    "subscription": "starter",
    "workspace": "default",
    "runtime_installed": False,
    "created_at": "",
    "updated_at": "",
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


class StateStore:
    SCHEMA_VERSION = 1
    LEGACY_DEFAULT_CHANNELS: tuple[tuple[str, str], ...] = (
        ("chat", "support"),
        ("email", "inbox"),
    )

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._allow_tmp_fallback = config_dir is None and "CLAWIE_HOME" not in os.environ
        if config_dir is None:
            root = self._default_root()
        else:
            root = Path(config_dir).expanduser()
        self._set_root(root)

    def ensure(self) -> None:
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
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
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
                    timeout_seconds REAL DEFAULT 300.0
                );
                CREATE TABLE IF NOT EXISTS delegation_trees (
                    root_agent_id TEXT PRIMARY KEY,
                    tree_data TEXT DEFAULT '{}',
                    updated_at TEXT
                );
                """
            )
            conn.commit()
        self._seed_defaults()
        self._migrate_legacy_json()
        self._migrate_schema()
        self._migrate_delegation_schema()

    def read_config(self) -> dict[str, Any]:
        self.ensure()
        config = copy.deepcopy(DEFAULT_CONFIG)
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM config").fetchall()
        for row in rows:
            key = str(row["key"])
            if key in config:
                config[key] = self._decode_value(str(row["value"]))
        return config

    def write_config(self, payload: dict[str, Any]) -> None:
        self.ensure()
        normalized = copy.deepcopy(DEFAULT_CONFIG)
        normalized.update(payload)
        with self._connect() as conn:
            for key, value in normalized.items():
                conn.execute(
                    "INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)",
                    (key, self._encode_value(value)),
                )
            conn.commit()

    def read_state(self) -> dict[str, Any]:
        self.ensure()
        state = copy.deepcopy(DEFAULT_STATE)
        with self._connect() as conn:
            template_rows = conn.execute("SELECT name, payload FROM templates").fetchall()
            user_rows = conn.execute("SELECT user_id, payload FROM users").fetchall()
            event_rows = conn.execute(
                "SELECT timestamp, type, message, context FROM events ORDER BY id ASC"
            ).fetchall()

        state["templates"] = {}
        for row in template_rows:
            state["templates"][str(row["name"])] = self._decode_json_obj(str(row["payload"]))
        if not state["templates"]:
            state["templates"] = copy.deepcopy(DEFAULT_STATE["templates"])

        state["agents"] = {}
        for row in user_rows:
            state["agents"][str(row["user_id"])] = self._decode_json_obj(str(row["payload"]))
        state["users"] = state["agents"]

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
        return state

    def write_state(self, payload: dict[str, Any]) -> None:
        self.ensure()
        templates = payload.get("templates", {})
        agents = payload.get("agents", payload.get("users", {}))
        events = payload.get("events", [])
        if not isinstance(templates, dict) or not isinstance(agents, dict) or not isinstance(events, list):
            raise ValueError("state payload must include templates/agents/events")

        with self._connect() as conn:
            conn.execute("DELETE FROM templates")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM events")

            for name, template in templates.items():
                conn.execute(
                    "INSERT INTO templates(name, payload) VALUES (?, ?)",
                    (str(name), json.dumps(template, sort_keys=True)),
                )

            for agent_id, agent in agents.items():
                conn.execute(
                    "INSERT INTO users(user_id, payload) VALUES (?, ?)",
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
            conn.commit()

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
        """Add model_tier and context_budget columns if missing."""
        with self._connect() as conn:
            cols = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(delegation_tasks)").fetchall()
            }
            if "model_tier" not in cols:
                conn.execute(
                    "ALTER TABLE delegation_tasks ADD COLUMN model_tier TEXT DEFAULT ''"
                )
            if "context_budget" not in cols:
                conn.execute(
                    "ALTER TABLE delegation_tasks ADD COLUMN context_budget TEXT DEFAULT '{}'"
                )
            conn.commit()

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
    ) -> None:
        self.ensure()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO delegation_tasks
                (task_id, parent_agent_id, child_agent_id, depth, status,
                 payload, result, error, created_at, completed_at, timeout_seconds,
                 model_tier, context_budget)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

    def read_delegation_tasks(
        self,
        parent_agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.ensure()
        clauses: list[str] = []
        params: list[Any] = []
        if parent_agent_id:
            clauses.append("parent_agent_id = ?")
            params.append(parent_agent_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM delegation_tasks{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
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
                        "INSERT INTO templates(name, payload) VALUES (?, ?)",
                        (name, json.dumps(payload, sort_keys=True)),
                    )

            config_count = int(conn.execute("SELECT COUNT(*) FROM config").fetchone()[0])
            if config_count == 0:
                for key, value in DEFAULT_CONFIG.items():
                    conn.execute(
                        "INSERT INTO config(key, value) VALUES (?, ?)",
                        (key, self._encode_value(value)),
                    )
            conn.commit()

    def _migrate_legacy_json(self) -> None:
        if not self.db_path.exists():
            return
        with self._connect() as conn:
            has_users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]) > 0
            has_events = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]) > 0

        if has_users or has_events:
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
        if current != self._stored_schema_version():
            self._write_schema_version(current)

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
            user_rows = conn.execute("SELECT user_id, payload FROM users").fetchall()

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

            for row in user_rows:
                agent_id = str(row["user_id"])
                payload = self._decode_json_obj(str(row["payload"]))
                channels = payload.get("channels", [])
                if not isinstance(channels, list):
                    continue
                filtered = [item for item in channels if not self._is_legacy_default_channel(item, agent_id)]
                if len(filtered) == len(channels):
                    continue
                payload["channels"] = filtered
                conn.execute(
                    "UPDATE users SET payload = ? WHERE user_id = ?",
                    (json.dumps(payload, sort_keys=True), agent_id),
                )

            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        self._ensure_root_dir()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _set_root(self, root: Path) -> None:
        self.root = root.expanduser()
        self.db_path = self.root / "clawie.db"
        self.config_path = self.root / "config.json"  # legacy migration input
        self.state_path = self.root / "state.json"  # legacy migration input

    def _ensure_root_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            if not self._allow_tmp_fallback:
                raise
            fallback = self._fallback_root()
            self._set_root(fallback)
            self.root.mkdir(parents=True, exist_ok=True)

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
