from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clawie.providers import get_provider


def empty_auth_payload(provider: str, auth_mode: str) -> dict[str, Any]:
    return {
        "provider": str(provider).strip().lower(),
        "auth_mode": str(auth_mode).strip().lower(),
        "auth_status": "unknown",
        "auth_profile": "",
        "account": "",
        "expires_at": "",
        "last_refresh": "",
        "login_required": False,
        "can_login": str(auth_mode).strip().lower() == "linked",
        "source": "config",
        "detail": "",
    }


def login_required(auth_status: str) -> bool:
    return str(auth_status).strip().lower() in {"expired", "missing"}


def parse_provider_auth_status_output(output: str) -> dict[str, Any]:
    text = str(output or "").strip()
    if not text:
        return {}
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "not logged in",
            "login required",
            "no auth profiles",
            "no profiles found",
            "no active profile",
        )
    ):
        return {"auth_status": "missing", "detail": text.splitlines()[0].strip()}

    candidate = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "kind=" in line or "expires=" in line or "account=" in line:
            candidate = line
            break
    if not candidate:
        if "expired" in lowered:
            return {"auth_status": "expired", "detail": text.splitlines()[0].strip()}
        return {}

    profile_match = re.match(r"^\*?\s*([^\s]+)", candidate)
    kind_match = re.search(r"\bkind=([^\s]+)", candidate, flags=re.IGNORECASE)
    account_match = re.search(r"\baccount=([^\s]+)", candidate)
    expired_match = re.search(r"\bexpires=expired at ([^\s]+)", candidate, flags=re.IGNORECASE)
    expires_match = re.search(r"\bexpires=([^\s]+)", candidate, flags=re.IGNORECASE)

    expires_at = ""
    auth_status = "ready"
    if expired_match:
        expires_at = expired_match.group(1).strip()
        auth_status = "expired"
    elif expires_match:
        token = expires_match.group(1).strip()
        if token.lower() == "never":
            auth_status = "ready"
        elif token.lower() == "expired":
            auth_status = "expired"
        else:
            expires_at = token
            auth_status = auth_status_from_expiry(token, has_token=True)

    return {
        "auth_status": auth_status,
        "auth_profile": profile_match.group(1) if profile_match else "",
        "account": account_match.group(1).strip() if account_match else "",
        "expires_at": expires_at,
        "detail": kind_match.group(1).strip() if kind_match else "",
    }


def inspect_auth_files(provider: str, home: Path | None) -> dict[str, Any]:
    if not home:
        return {}
    spec = get_provider(provider)
    profiles_path = home / spec.state_dir / "auth-profiles.json"
    if _path_exists(profiles_path):
        parsed = auth_status_from_profiles_json(profiles_path)
        if parsed:
            return parsed
    codex_path = home / ".codex" / "auth.json"
    if _path_exists(codex_path):
        parsed = auth_status_from_codex_auth_json(codex_path)
        if parsed:
            return parsed
    return {}


def auth_status_from_profiles_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return {"auth_status": "missing", "detail": "no auth profiles found", "source": f"file:{path.name}"}

    ordered_keys: list[str] = []
    active_profiles = payload.get("active_profiles", {})
    if isinstance(active_profiles, dict):
        for value in active_profiles.values():
            token = str(value).strip()
            if token and token in profiles and token not in ordered_keys:
                ordered_keys.append(token)
    for key in profiles:
        token = str(key).strip()
        if token and token not in ordered_keys:
            ordered_keys.append(token)

    selected_key = ""
    selected: dict[str, Any] = {}
    for key in ordered_keys:
        item = profiles.get(key)
        if isinstance(item, dict):
            selected_key = key
            selected = item
            break
    if not selected:
        return {}

    expires_at = str(selected.get("expires_at", "")).strip()
    has_token = any(
        str(selected.get(name, "")).strip()
        for name in ("access_token", "refresh_token", "token", "id_token")
    )
    return {
        "auth_status": auth_status_from_expiry(expires_at, has_token=has_token),
        "auth_profile": str(selected.get("profile_name", "")).strip() or selected_key,
        "account": str(selected.get("account_id", "")).strip(),
        "expires_at": expires_at,
        "last_refresh": str(selected.get("updated_at", payload.get("updated_at", ""))).strip(),
        "detail": str(selected.get("kind", "")).strip(),
        "source": f"file:{path.name}",
    }


def auth_status_from_codex_auth_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    token_map = payload.get("tokens", {})
    if not isinstance(token_map, dict):
        token_map = {}
    has_token = any(
        str(token_map.get(name, "")).strip()
        for name in ("access_token", "refresh_token", "id_token")
    )
    auth_mode = str(payload.get("auth_mode", "")).strip().lower()
    api_key = str(payload.get("OPENAI_API_KEY", "")).strip()
    status = "missing"
    if has_token:
        status = "ready"
    elif api_key and auth_mode not in {"chatgpt", "oauth", "linked"}:
        status = "ready"
    return {
        "auth_status": status,
        "auth_profile": "default",
        "account": str(token_map.get("account_id", payload.get("account_id", ""))).strip(),
        "expires_at": "",
        "last_refresh": str(payload.get("last_refresh", "")).strip(),
        "detail": auth_mode or "codex-auth",
        "source": f"file:{path.name}",
    }


def parse_iso_timestamp(value: str) -> datetime | None:
    token = str(value or "").strip()
    if not token:
        return None
    match = re.match(r"^(?P<head>.+?)(?:\.(?P<frac>\d+))?(?P<tz>Z|[+-]\d\d:\d\d)?$", token)
    if not match:
        return None
    head = str(match.group("head") or "")
    frac = str(match.group("frac") or "")
    zone = str(match.group("tz") or "")
    if zone == "Z":
        zone = "+00:00"
    normalized = head
    if frac:
        normalized += "." + frac[:6]
    if zone:
        normalized += zone
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def auth_status_from_expiry(expires_at: str, *, has_token: bool) -> str:
    token = str(expires_at).strip()
    if not token:
        return "ready" if has_token else "missing"
    parsed = parse_iso_timestamp(token)
    if parsed is None:
        lowered = token.lower()
        if lowered == "expired":
            return "expired"
        return "ready" if has_token else "unknown"
    return "expired" if parsed <= datetime.now(timezone.utc) else "ready"


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False
