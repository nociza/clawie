from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_codex_auth(source_home: Path) -> dict[str, str]:
    path = source_home / ".codex" / "auth.json"
    payload = _read_json_object(path)
    tokens = payload.get("tokens", {})
    if not isinstance(tokens, dict):
        tokens = {}
    access_token = str(tokens.get("access_token", "")).strip()
    refresh_token = str(tokens.get("refresh_token", "")).strip()
    id_token = str(tokens.get("id_token", "")).strip()
    account_id = str(tokens.get("account_id", payload.get("account_id", ""))).strip()
    if not any((access_token, refresh_token, id_token)):
        raise ValueError(f"codex auth is missing tokens: {path}")
    updated_at = str(payload.get("last_refresh", "")).strip()
    access_expires_at = _jwt_expiry(access_token)
    id_expires_at = _jwt_expiry(id_token)
    expires_at = access_expires_at or id_expires_at
    return {
        "source": "codex",
        "upstream_provider": "openai-codex",
        "profile_name": "default",
        "profile_id": "openai-codex:default",
        "kind": "oauth",
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_at": expires_at,
        "updated_at": updated_at,
    }


def load_claude_auth(source_home: Path) -> dict[str, str]:
    credentials_path = source_home / ".claude" / ".credentials.json"
    state_path = source_home / ".claude.json"
    payload = _read_json_object(credentials_path)
    oauth = payload.get("claudeAiOauth", {})
    if not isinstance(oauth, dict):
        oauth = {}
    access_token = str(oauth.get("accessToken", "")).strip()
    refresh_token = str(oauth.get("refreshToken", "")).strip()
    expires_at = str(oauth.get("expiresAt", "")).strip()
    if not any((access_token, refresh_token)):
        raise ValueError(f"claude auth is missing tokens: {credentials_path}")

    state = _read_json_object(state_path, allow_missing=True)
    account = ""
    if state:
        oauth_account = state.get("oauthAccount", {})
        if isinstance(oauth_account, dict):
            account = str(oauth_account.get("accountUuid", oauth_account.get("emailAddress", ""))).strip()
        if not account:
            account = str(state.get("userID", "")).strip()
    updated_at = _mtime_iso(credentials_path) or _mtime_iso(state_path)
    return {
        "source": "claude",
        "upstream_provider": "anthropic-claude",
        "profile_name": "default",
        "profile_id": "anthropic-claude:default",
        "kind": "oauth",
        "account_id": account,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": "",
        "expires_at": expires_at,
        "updated_at": updated_at,
    }


def merge_provider_auth_profile(existing: dict[str, Any], imported: dict[str, str]) -> dict[str, Any]:
    payload = dict(existing) if isinstance(existing, dict) else {}
    payload["version"] = int(payload.get("version", 1) or 1)
    active_profiles = payload.get("active_profiles", {})
    if not isinstance(active_profiles, dict):
        active_profiles = {}
    order = payload.get("order", {})
    if not isinstance(order, dict):
        order = {}
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}

    upstream_provider = str(imported.get("upstream_provider", "")).strip()
    profile_id = str(imported.get("profile_id", "")).strip()
    if not upstream_provider or not profile_id:
        raise ValueError("imported auth profile is incomplete")

    profile_payload = {
        "profile_name": str(imported.get("profile_name", "default")).strip() or "default",
        "provider": upstream_provider,
        "account_id": str(imported.get("account_id", "")).strip(),
        "accountId": str(imported.get("account_id", "")).strip(),
        "kind": str(imported.get("kind", "oauth")).strip() or "oauth",
        "type": "oauth" if str(imported.get("kind", "oauth")).strip().lower() == "oauth" else "token",
        "access_token": str(imported.get("access_token", "")).strip(),
        "refresh_token": str(imported.get("refresh_token", "")).strip(),
        "updated_at": str(imported.get("updated_at", "")).strip(),
    }
    if profile_payload["access_token"]:
        profile_payload["access"] = profile_payload["access_token"]
    if profile_payload["refresh_token"]:
        profile_payload["refresh"] = profile_payload["refresh_token"]
    id_token = str(imported.get("id_token", "")).strip()
    expires_at = str(imported.get("expires_at", "")).strip()
    if id_token:
        profile_payload["id_token"] = id_token
    if expires_at:
        profile_payload["expires_at"] = expires_at
        expires_ms = _iso_to_epoch_millis(expires_at)
        if expires_ms > 0:
            profile_payload["expires"] = expires_ms

    active_profiles[upstream_provider] = profile_id
    existing_order = order.get(upstream_provider, [])
    if not isinstance(existing_order, list):
        existing_order = []
    order[upstream_provider] = [
        profile_id,
        *[
            str(item).strip()
            for item in existing_order
            if str(item).strip() and str(item).strip() != profile_id
        ],
    ]
    profiles[profile_id] = profile_payload
    payload["active_profiles"] = active_profiles
    payload["profiles"] = profiles
    payload["order"] = order
    payload["updated_at"] = profile_payload["updated_at"]
    return payload


def merge_picoclaw_auth_store(existing: dict[str, Any], imported: dict[str, str]) -> dict[str, Any]:
    payload = dict(existing) if isinstance(existing, dict) else {}
    credentials = payload.get("credentials", {})
    if not isinstance(credentials, dict):
        credentials = {}

    provider = _picoclaw_provider_name(imported)
    if not provider:
        raise ValueError("imported auth profile cannot be mapped to picoclaw provider auth")

    credential: dict[str, Any] = {
        "access_token": str(imported.get("access_token", "")).strip(),
        "provider": provider,
        "auth_method": "oauth" if str(imported.get("kind", "oauth")).strip().lower() == "oauth" else "token",
    }
    refresh_token = str(imported.get("refresh_token", "")).strip()
    if refresh_token:
        credential["refresh_token"] = refresh_token
    account_id = str(imported.get("account_id", "")).strip()
    if account_id:
        credential["account_id"] = account_id
    expires_at = str(imported.get("expires_at", "")).strip()
    if expires_at:
        credential["expires_at"] = expires_at

    credentials[provider] = credential
    payload["credentials"] = credentials
    return payload


def extract_provider_auth_profiles(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Convert an ``auth-profiles.json`` payload into normalized imported-auth dicts.

    Returns one dict per usable profile, in merge order: inactive profiles
    first, the active profile for each upstream provider last, so replaying
    them through :func:`merge_provider_auth_profile` preserves which profile
    is active. Profiles without any token material are skipped.
    """
    if not isinstance(payload, dict):
        return []
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        return []
    active_profiles = payload.get("active_profiles", {})
    if not isinstance(active_profiles, dict):
        active_profiles = {}
    active_ids = {str(value).strip() for value in active_profiles.values() if str(value).strip()}

    rows: list[dict[str, str]] = []
    for profile_id, raw in profiles.items():
        if not isinstance(raw, dict):
            continue
        token = str(profile_id).strip()
        upstream = str(raw.get("provider", "")).strip()
        if not token or not upstream:
            continue
        access_token = str(raw.get("access_token", raw.get("access", "")) or "").strip()
        refresh_token = str(raw.get("refresh_token", raw.get("refresh", "")) or "").strip()
        id_token = str(raw.get("id_token", "") or "").strip()
        if not any((access_token, refresh_token, id_token)):
            continue
        kind = str(raw.get("kind", raw.get("type", "oauth")) or "oauth").strip().lower() or "oauth"
        expires_at = str(raw.get("expires_at", "") or "").strip()
        if not expires_at:
            expires_at = _epoch_millis_to_iso(raw.get("expires"))
        rows.append(
            {
                "source": "port",
                "upstream_provider": upstream,
                "profile_name": str(raw.get("profile_name", "default") or "default").strip() or "default",
                "profile_id": token,
                "kind": kind,
                "account_id": str(raw.get("account_id", raw.get("accountId", "")) or "").strip(),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expires_at": expires_at,
                "updated_at": str(raw.get("updated_at", "") or "").strip(),
            }
        )
    rows.sort(key=lambda row: (row["profile_id"] in active_ids, row["profile_id"]))
    return rows


def extract_picoclaw_credentials(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Convert a picoclaw ``auth.json`` payload into normalized imported-auth dicts."""
    if not isinstance(payload, dict):
        return []
    credentials = payload.get("credentials", {})
    if not isinstance(credentials, dict):
        return []

    rows: list[dict[str, str]] = []
    for provider, raw in sorted(credentials.items()):
        if not isinstance(raw, dict):
            continue
        upstream = _upstream_provider_for_picoclaw(str(provider))
        if not upstream:
            continue
        access_token = str(raw.get("access_token", "") or "").strip()
        refresh_token = str(raw.get("refresh_token", "") or "").strip()
        if not any((access_token, refresh_token)):
            continue
        method = str(raw.get("auth_method", "oauth") or "oauth").strip().lower()
        rows.append(
            {
                "source": "port",
                "upstream_provider": upstream,
                "profile_name": "default",
                "profile_id": f"{upstream}:default",
                "kind": "oauth" if method == "oauth" else "token",
                "account_id": str(raw.get("account_id", "") or "").strip(),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": "",
                "expires_at": str(raw.get("expires_at", "") or "").strip(),
                "updated_at": "",
            }
        )
    return rows


def _read_json_object(path: Path, *, allow_missing: bool = False) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if allow_missing:
            return {}
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"failed reading auth source {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"auth source must be a JSON object: {path}")
    return payload


def _jwt_expiry(token: str) -> str:
    value = str(token).strip()
    if not value or value.count(".") < 2:
        return ""
    segment = value.split(".", 2)[1]
    padding = "=" * ((4 - (len(segment) % 4)) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment + padding)
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    exp = payload.get("exp")
    try:
        stamp = datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_to_epoch_millis(value: str) -> int:
    token = str(value or "").strip()
    if not token:
        return 0
    try:
        stamp = datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp() * 1000)


def _mtime_iso(path: Path) -> str:
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return ""
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _picoclaw_provider_name(imported: dict[str, str]) -> str:
    upstream = str(imported.get("upstream_provider", "")).strip().lower()
    if upstream in {"openai", "openai-codex"}:
        return "openai"
    if upstream in {"anthropic", "anthropic-claude"}:
        return "anthropic"
    if upstream in {"google-antigravity", "antigravity"}:
        return "google-antigravity"
    return ""


def _upstream_provider_for_picoclaw(provider: str) -> str:
    token = str(provider).strip().lower()
    if token == "openai":
        return "openai-codex"
    if token == "anthropic":
        return "anthropic-claude"
    if token == "google-antigravity":
        return "google-antigravity"
    return ""


def _epoch_millis_to_iso(value: Any) -> str:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ""
    if millis <= 0:
        return ""
    try:
        stamp = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
