from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "api_url": "https://api.zeroclaw.example/v1",
    "api_key": "",
    "subscription": "starter",
    "workspace": "default",
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
    "users": {},
    "events": [],
}


class StateStore:
    def __init__(self, config_dir: str | Path | None = None) -> None:
        if config_dir is None:
            base = Path(
                os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            )
            self.root = base / "clawie"
        else:
            self.root = Path(config_dir).expanduser()
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def read_config(self) -> dict[str, Any]:
        self.ensure()
        if not self.config_path.exists():
            return copy.deepcopy(DEFAULT_CONFIG)
        return self._read_json(self.config_path, copy.deepcopy(DEFAULT_CONFIG))

    def write_config(self, payload: dict[str, Any]) -> None:
        self.ensure()
        self._write_json_atomic(self.config_path, payload)

    def read_state(self) -> dict[str, Any]:
        self.ensure()
        if not self.state_path.exists():
            return copy.deepcopy(DEFAULT_STATE)
        state = self._read_json(self.state_path, copy.deepcopy(DEFAULT_STATE))
        state.setdefault("templates", copy.deepcopy(DEFAULT_STATE["templates"]))
        state.setdefault("users", {})
        state.setdefault("events", [])
        return state

    def write_state(self, payload: dict[str, Any]) -> None:
        self.ensure()
        self._write_json_atomic(self.state_path, payload)

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
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=path.parent, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            tmp_name = handle.name
        Path(tmp_name).replace(path)
