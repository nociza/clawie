from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clawctl.store import StateStore


class SetupError(RuntimeError):
    pass


class UserExistsError(RuntimeError):
    pass


class UserNotFoundError(RuntimeError):
    pass


def now_iso() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return stamp.replace("+00:00", "Z")


def redact(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


class ZeroClawService:
    EVENT_LIMIT = 2000

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def setup(
        self,
        api_key: str,
        subscription: str,
        workspace: str,
        api_url: str,
    ) -> dict[str, Any]:
        config = self.store.read_config()
        config["api_key"] = api_key.strip()
        config["subscription"] = subscription.strip()
        config["workspace"] = workspace.strip()
        config["api_url"] = api_url.strip()
        created = config.get("created_at") or now_iso()
        config["created_at"] = created
        config["updated_at"] = now_iso()
        self.store.write_config(config)

        state = self.store.read_state()
        self._event(
            state,
            "setup.initialized",
            "ZeroClaw configuration initialized",
            {
                "workspace": config["workspace"],
                "subscription": config["subscription"],
            },
        )
        self.store.write_state(state)
        return config

    def setup_status(self) -> dict[str, Any]:
        config = self.store.read_config()
        configured = bool(config.get("api_key"))
        return {
            "configured": configured,
            "api_url": config.get("api_url", ""),
            "workspace": config.get("workspace", ""),
            "subscription": config.get("subscription", ""),
            "api_key": redact(config.get("api_key", "")),
            "updated_at": config.get("updated_at", ""),
        }

    def create_user(
        self,
        user_id: str,
        display_name: str | None,
        template: str,
        clone_from: str | None,
        channel_strategy: str,
        channels: list[dict[str, str]] | None,
        agent_version: str,
    ) -> dict[str, Any]:
        self._require_setup()

        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user_id is required")

        if channel_strategy not in {"new", "migrate"}:
            raise ValueError("channel_strategy must be one of: new, migrate")

        state = self.store.read_state()
        if user_id in state["users"]:
            raise UserExistsError(f"user already exists: {user_id}")

        base_channels: list[dict[str, str]] = []
        source_template = template
        source_agent_defaults: dict[str, Any] = {}

        if clone_from:
            source = state["users"].get(clone_from)
            if not source:
                raise UserNotFoundError(f"clone source user not found: {clone_from}")
            base_channels = copy.deepcopy(source.get("channels", []))
            source_template = source.get("source_template") or template
            source_agent_defaults = copy.deepcopy(source.get("agent", {}))
        else:
            template_data = state["templates"].get(template)
            if not template_data:
                raise ValueError(f"template not found: {template}")
            base_channels = copy.deepcopy(template_data.get("channels", []))
            source_agent_defaults = copy.deepcopy(template_data.get("agent_defaults", {}))

        if channels:
            base_channels = copy.deepcopy(channels)

        if channel_strategy == "new":
            final_channels = self._mint_channels(user_id, base_channels)
        else:
            if not clone_from:
                raise ValueError(
                    "channel strategy 'migrate' requires --clone-from to copy channels"
                )
            final_channels = copy.deepcopy(base_channels)
            for channel in final_channels:
                channel["migrated_from"] = clone_from

        display = display_name.strip() if display_name else user_id
        agent = {
            "status": "ready",
            "version": agent_version,
            "last_sync": now_iso(),
            "runtime": source_agent_defaults.get("runtime", "zeroclaw-agent"),
            "autostart": bool(source_agent_defaults.get("autostart", True)),
            "heartbeat_seconds": int(source_agent_defaults.get("heartbeat_seconds", 30)),
        }

        user = {
            "user_id": user_id,
            "display_name": display,
            "created_at": now_iso(),
            "source_template": source_template,
            "clone_from": clone_from,
            "channel_strategy": channel_strategy,
            "channels": final_channels,
            "agent": agent,
        }
        state["users"][user_id] = user

        self._event(
            state,
            "users.created",
            f"Provisioned user {user_id}",
            {
                "user_id": user_id,
                "channel_strategy": channel_strategy,
                "channel_count": len(final_channels),
                "clone_from": clone_from or "",
            },
        )
        self.store.write_state(state)
        return user

    def list_users(self) -> list[dict[str, Any]]:
        users = list(self.store.read_state()["users"].values())
        return sorted(users, key=lambda row: (row.get("created_at", ""), row["user_id"]))

    def get_user(self, user_id: str) -> dict[str, Any]:
        state = self.store.read_state()
        user = state["users"].get(user_id)
        if not user:
            raise UserNotFoundError(f"user not found: {user_id}")
        return user

    def delete_user(self, user_id: str) -> None:
        self._require_setup()
        state = self.store.read_state()
        if user_id not in state["users"]:
            raise UserNotFoundError(f"user not found: {user_id}")
        del state["users"][user_id]
        self._event(
            state,
            "users.deleted",
            f"Deleted user {user_id}",
            {"user_id": user_id},
        )
        self.store.write_state(state)

    def migrate_channels(
        self,
        from_user: str,
        to_user: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()

        source = state["users"].get(from_user)
        target = state["users"].get(to_user)
        if not source:
            raise UserNotFoundError(f"source user not found: {from_user}")
        if not target:
            raise UserNotFoundError(f"target user not found: {to_user}")

        source_channels = copy.deepcopy(source.get("channels", []))
        for channel in source_channels:
            channel["migrated_from"] = from_user

        if replace:
            target_channels = source_channels
        else:
            target_channels = copy.deepcopy(target.get("channels", []))
            existing = {(row.get("kind", ""), row.get("name", "")) for row in target_channels}
            for channel in source_channels:
                key = (channel.get("kind", ""), channel.get("name", ""))
                if key not in existing:
                    target_channels.append(channel)
                    existing.add(key)

        target["channels"] = target_channels
        target["channel_strategy"] = "migrate"
        target["agent"]["status"] = "syncing"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.migrated",
            f"Migrated channels from {from_user} to {to_user}",
            {
                "from_user": from_user,
                "to_user": to_user,
                "replace": replace,
                "channel_count": len(target_channels),
            },
        )
        self.store.write_state(state)
        return target

    def bootstrap_channels(
        self,
        user_id: str,
        preset: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        presets = {
            "minimal": [{"kind": "chat", "name": "primary"}],
            "growth": [
                {"kind": "chat", "name": "support"},
                {"kind": "email", "name": "inbox"},
                {"kind": "social", "name": "community"},
            ],
            "enterprise": [
                {"kind": "chat", "name": "ops"},
                {"kind": "email", "name": "queue"},
                {"kind": "voice", "name": "contact-center"},
                {"kind": "ticketing", "name": "service-desk"},
            ],
        }
        if preset not in presets:
            raise ValueError("preset must be one of: minimal, growth, enterprise")

        state = self.store.read_state()
        target = state["users"].get(user_id)
        if not target:
            raise UserNotFoundError(f"user not found: {user_id}")

        generated = self._mint_channels(user_id, presets[preset])
        if replace:
            target_channels = generated
        else:
            target_channels = copy.deepcopy(target.get("channels", []))
            existing = {(row.get("kind", ""), row.get("name", "")) for row in target_channels}
            for channel in generated:
                key = (channel.get("kind", ""), channel.get("name", ""))
                if key not in existing:
                    target_channels.append(channel)
                    existing.add(key)

        target["channels"] = target_channels
        target["agent"]["status"] = "ready"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.bootstrapped",
            f"Applied {preset} channel preset for {user_id}",
            {
                "user_id": user_id,
                "preset": preset,
                "replace": replace,
                "channel_count": len(target_channels),
            },
        )
        self.store.write_state(state)
        return target

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        config = self.store.read_config()
        state = self.store.read_state()

        if config.get("api_key"):
            checks.append({"status": "pass", "message": "API key is configured"})
        else:
            checks.append({
                "status": "fail",
                "message": "API key is missing. Run setup init.",
            })

        if config.get("workspace"):
            checks.append({"status": "pass", "message": "Workspace is configured"})
        else:
            checks.append({
                "status": "warn",
                "message": "Workspace is empty; default will be used.",
            })

        if state.get("templates"):
            checks.append({"status": "pass", "message": "At least one template exists"})
        else:
            checks.append({"status": "fail", "message": "No templates available"})

        users = state.get("users", {})
        if users:
            checks.append({"status": "pass", "message": f"{len(users)} user(s) provisioned"})
        else:
            checks.append({"status": "warn", "message": "No users provisioned yet"})

        no_channels = [uid for uid, row in users.items() if not row.get("channels")]
        if no_channels:
            checks.append({
                "status": "warn",
                "message": "Users without channels: " + ", ".join(no_channels),
            })

        overall = "healthy"
        if any(check["status"] == "fail" for check in checks):
            overall = "unhealthy"
        elif any(check["status"] == "warn" for check in checks):
            overall = "degraded"

        return {"status": overall, "checks": checks}

    def list_events(self, limit: int = 20) -> list[dict[str, Any]]:
        state = self.store.read_state()
        events = state.get("events", [])
        return list(reversed(events[-limit:]))

    def dashboard_snapshot(self, user_id: str | None = None) -> dict[str, Any]:
        state = self.store.read_state()
        users = list(state["users"].values())
        if user_id:
            users = [row for row in users if row.get("user_id") == user_id]

        rows: list[dict[str, Any]] = []
        channel_total = 0
        migrated_total = 0
        for user in sorted(users, key=lambda row: row["user_id"]):
            channels = user.get("channels", [])
            migrated_count = sum(1 for row in channels if row.get("migrated_from"))
            channel_total += len(channels)
            migrated_total += migrated_count
            rows.append(
                {
                    "user_id": user.get("user_id", ""),
                    "display_name": user.get("display_name", ""),
                    "status": user.get("agent", {}).get("status", "unknown"),
                    "version": user.get("agent", {}).get("version", ""),
                    "strategy": user.get("channel_strategy", ""),
                    "channels": len(channels),
                    "migrated": migrated_count,
                    "last_sync": user.get("agent", {}).get("last_sync", ""),
                }
            )

        return {
            "generated_at": now_iso(),
            "workspace": self.store.read_config().get("workspace", ""),
            "totals": {
                "users": len(rows),
                "channels": channel_total,
                "migrated_channels": migrated_total,
            },
            "rows": rows,
            "events": self.list_events(limit=8),
        }

    def batch_create_users(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        results = {"created": [], "errors": []}
        for entry in entries:
            user_id = str(entry.get("user_id", "")).strip()
            if not user_id:
                results["errors"].append({
                    "user_id": "",
                    "error": "entry missing user_id",
                })
                continue
            try:
                user = self.create_user(
                    user_id=user_id,
                    display_name=entry.get("display_name"),
                    template=str(entry.get("template", "baseline")),
                    clone_from=entry.get("clone_from"),
                    channel_strategy=str(entry.get("channel_strategy", "new")),
                    channels=entry.get("channels"),
                    agent_version=str(entry.get("agent_version", "1.0.0")),
                )
                results["created"].append(user["user_id"])
            except Exception as exc:  # noqa: BLE001
                results["errors"].append({"user_id": user_id, "error": str(exc)})
        return results

    def export_state(self, output_path: str | Path) -> Path:
        snapshot = {
            "exported_at": now_iso(),
            "config": self.store.read_config(),
            "state": self.store.read_state(),
        }
        target = Path(output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return target

    def import_state(self, input_path: str | Path, merge: bool = False) -> None:
        source = Path(input_path).expanduser()
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if not isinstance(payload, dict):
            raise ValueError("snapshot must be a JSON object")
        config = payload.get("config")
        state = payload.get("state")
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise ValueError("snapshot must include object fields: config, state")

        if merge:
            current_config = self.store.read_config()
            current_state = self.store.read_state()

            merged_config = copy.deepcopy(current_config)
            merged_config.update(config)

            merged_state = copy.deepcopy(current_state)
            merged_state.setdefault("templates", {})
            merged_state.setdefault("users", {})
            merged_state.setdefault("events", [])
            merged_state["templates"].update(state.get("templates", {}))
            merged_state["users"].update(state.get("users", {}))
            merged_state["events"] = (
                merged_state["events"] + state.get("events", [])
            )[-self.EVENT_LIMIT :]

            self.store.write_config(merged_config)
            self.store.write_state(merged_state)
            return

        self.store.write_config(config)
        self.store.write_state(state)

    def _require_setup(self) -> None:
        config = self.store.read_config()
        if not config.get("api_key"):
            raise SetupError("setup is incomplete. Run 'clawctl setup init'.")

    def _mint_channels(
        self,
        user_id: str,
        base_channels: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        items = base_channels or [{"kind": "chat", "name": "primary"}]
        minted: list[dict[str, str]] = []
        for idx, channel in enumerate(items, start=1):
            kind = str(channel.get("kind", "chat"))
            raw_name = str(channel.get("name", f"channel-{idx}"))
            if raw_name.startswith(f"{user_id}-"):
                full_name = raw_name
            else:
                full_name = f"{user_id}-{raw_name}"
            minted.append(
                {
                    "kind": kind,
                    "name": full_name,
                    "external_id": f"{user_id}:{kind}:{idx}",
                }
            )
        return minted

    def _event(
        self,
        state: dict[str, Any],
        event_type: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        events = state.setdefault("events", [])
        events.append(
            {
                "timestamp": now_iso(),
                "type": event_type,
                "message": message,
                "context": context,
            }
        )
        if len(events) > self.EVENT_LIMIT:
            state["events"] = events[-self.EVENT_LIMIT :]
