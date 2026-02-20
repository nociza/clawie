from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "api_url": "https://api.zeroclaw.example/v1",
    "api_key": "",
    "provider": "zeroclaw",
    "subscription": "starter",
    "workspace": "default",
    "runtime_installed": False,
    "created_at": "",
    "updated_at": "",
}

DEFAULT_STATE: dict[str, Any] = {
    "templates": {
        "baseline": {
            "channels": [
                {"kind": "chat", "name": "support"},
                {"kind": "email", "name": "inbox"},
            ],
            "agent_defaults": {
                "runtime": "zeroclaw-agent",
                "autostart": True,
                "heartbeat_seconds": 30,
            },
        }
    },
    "agents": {},
    "events": [],
}


class StateStore:
    def __init__(self, config_dir: str | Path | None = None) -> None:
        if config_dir is None:
            self.root = Path(os.environ.get("CLAWIE_HOME", str(Path.home() / ".clawie")))
        else:
            self.root = Path(config_dir).expanduser()
        self.db_path = self.root / "clawie.db"
        self.config_path = self.root / "config.json"  # legacy migration input
        self.state_path = self.root / "state.json"  # legacy migration input

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
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
                """
            )
            conn.commit()
        self._seed_defaults()
        self._migrate_legacy_json()

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

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
