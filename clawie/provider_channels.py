from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


class ProviderChannelAdapter(Protocol):
    def discover_channels(self, provider_root: Path) -> list[dict[str, str]]:
        ...

    def connect_commands(self, executable: str, kind: str, name: str) -> list[list[str]]:
        ...


class ZeroClawChannelAdapter:
    def discover_channels(self, provider_root: Path) -> list[dict[str, str]]:
        config_path = provider_root / "config.toml"
        if tomllib is None or not _path_exists(config_path):
            return []
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        channels_cfg = payload.get("channels_config", {})
        if not isinstance(channels_cfg, dict):
            return []

        channels: list[dict[str, str]] = []
        if bool(channels_cfg.get("cli")):
            channels.append({"kind": "cli", "name": "local"})

        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "cli" or not kind:
                continue
            if not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue

            token_keys = ("bot_token", "token", "api_key", "webhook_url")
            has_secret = any(str(value.get(token, "")).strip() for token in token_keys)
            has_config = bool(value.get("allowed_users")) or bool(value.get("channels"))
            if not has_secret and not has_config:
                continue
            channel_name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            channels.append({"kind": kind, "name": channel_name})
        return dedupe_channels(channels)

    def connect_commands(self, executable: str, kind: str, name: str) -> list[list[str]]:
        commands: list[list[str]] = []
        if kind == "telegram" and _looks_like_sender_identity(name):
            commands.append([executable, "channel", "bind-telegram", name])
        commands.append([executable, "channel", "doctor"])
        commands.append([executable, "onboard", "--channels-only"])
        return commands


class PicoClawChannelAdapter:
    def discover_channels(self, provider_root: Path) -> list[dict[str, str]]:
        config_path = provider_root / "config.json"
        if not _path_exists(config_path):
            return []
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return []

        channels: list[dict[str, str]] = []
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if not kind:
                continue
            enabled = True
            if isinstance(value, dict):
                enabled = bool(value.get("enabled", True))
                channel_name = str(value.get("name", kind)).strip().lower()
            else:
                channel_name = kind
            if not enabled:
                continue
            channels.append({"kind": kind, "name": channel_name or kind})
        return dedupe_channels(channels)

    def connect_commands(self, executable: str, kind: str, name: str) -> list[list[str]]:
        _ = (executable, kind, name)
        return []


class OpenClawChannelAdapter:
    def discover_channels(self, provider_root: Path) -> list[dict[str, str]]:
        config_path = provider_root / "openclaw.json"
        if not _path_exists(config_path):
            return []
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        channels: list[dict[str, str]] = []
        channels_cfg = payload.get("channels", {})
        if isinstance(channels_cfg, dict):
            for key, value in channels_cfg.items():
                kind = str(key).strip().lower()
                if not kind or kind == "defaults":
                    continue
                if isinstance(value, dict):
                    if not bool(value.get("enabled", True)):
                        continue
                    channel_name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
                else:
                    channel_name = kind
                channels.append({"kind": kind, "name": channel_name})

        bindings = payload.get("bindings", [])
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                match = binding.get("match", {})
                if not isinstance(match, dict):
                    continue
                kind = str(match.get("channel", "")).strip().lower()
                account = str(match.get("accountId", "")).strip()
                if not kind or not account:
                    continue
                channel_name = re.sub(r"[^a-z0-9._-]+", "-", account.lower()).strip("-") or kind
                channels.append({"kind": kind, "name": channel_name})
        return dedupe_channels(channels)

    def connect_commands(self, executable: str, kind: str, name: str) -> list[list[str]]:
        return [
            [executable, "channels", "add", "--channel", kind, "--name", name],
            [executable, "channels", "login", kind],
            [executable, "channels", "login"],
        ]


class GenericChannelAdapter:
    def discover_channels(self, provider_root: Path) -> list[dict[str, str]]:
        _ = provider_root
        return []

    def connect_commands(self, executable: str, kind: str, name: str) -> list[list[str]]:
        return [
            [executable, "channel", "add", kind, name],
            [executable, "channels", "add", "--channel", kind, "--name", name],
            [executable, "channel", "login", kind],
            [executable, "channels", "login", kind],
        ]


def get_channel_adapter(provider: str) -> ProviderChannelAdapter:
    name = str(provider).strip().lower()
    if name == "zeroclaw":
        return ZeroClawChannelAdapter()
    if name == "picoclaw":
        return PicoClawChannelAdapter()
    if name == "openclaw":
        return OpenClawChannelAdapter()
    return GenericChannelAdapter()


def dedupe_channels(channels: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for channel in channels:
        kind = str(channel.get("kind", "")).strip().lower()
        name = str(channel.get("name", "")).strip()
        if not kind or not name:
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"kind": kind, "name": name})
    return deduped


def _looks_like_sender_identity(value: str) -> bool:
    token = str(value).strip()
    if not token:
        return False
    if token in {"telegram", "default", "primary"}:
        return False
    if token.startswith("@") and len(token) > 1:
        return True
    return token.isdigit()


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False
