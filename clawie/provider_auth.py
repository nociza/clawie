from __future__ import annotations

import json
import re
import sqlite3
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


def parse_openclaw_models_status_output(output: str) -> dict[str, Any]:
    """Parse ``openclaw models status --json`` output into clawie's auth shape.

    The openclaw JSON status surface has changed across prerelease builds, so
    this parser accepts a few equivalent shapes while staying conservative: it
    only returns a payload when it finds an OpenAI provider record or an auth
    record with explicit status/login fields.
    """
    text = str(output or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    candidates = _openclaw_auth_candidates(payload)
    if not candidates:
        return {}

    # Prefer an explicit OpenAI record over a generic auth object.
    candidates.sort(key=lambda item: 0 if _record_names_openai(item) else 1)
    for record in candidates:
        parsed = _parse_openclaw_auth_record(record)
        if parsed:
            return parsed
    return {}


def _openclaw_auth_candidates(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def visit(item: Any, *, parent_key: str = "") -> None:
        if isinstance(item, dict):
            if _record_names_openai(item) or _record_has_auth_status_signal(item):
                rows.append(dict(item))
            for key, child in item.items():
                if str(key).strip().lower() == "openai" and isinstance(child, dict):
                    row = dict(child)
                    row.setdefault("provider", "openai")
                    rows.append(row)
                if str(key).strip().lower() == "auth" and isinstance(child, dict):
                    merged = dict(item)
                    merged.update(child)
                    rows.append(merged)
                visit(child, parent_key=str(key))
        elif isinstance(item, list):
            for child in item:
                visit(child, parent_key=parent_key)

    visit(value)
    return rows


def _record_names_openai(record: dict[str, Any]) -> bool:
    for key in ("provider", "name", "id", "modelProvider", "model_provider"):
        token = str(record.get(key, "") or "").strip().lower()
        if token in {"openai", "openai-codex"} or token.startswith("openai/"):
            return True
    return False


def _record_has_auth_status_signal(record: dict[str, Any]) -> bool:
    keys = {str(key).strip() for key in record}
    return bool(
        keys
        & {
            "auth_status",
            "authStatus",
            "auth_state",
            "authState",
            "login_required",
            "loginRequired",
            "authenticated",
            "configured",
            "ready",
        }
    )


def _parse_openclaw_auth_record(record: dict[str, Any]) -> dict[str, Any]:
    status = ""
    for key in ("auth_status", "authStatus", "auth_state", "authState", "status", "state"):
        if key in record:
            status = str(record.get(key, "") or "").strip().lower()
            break

    login_required_value = _coerce_bool(record.get("login_required", record.get("loginRequired")))
    authenticated_value = _coerce_bool(record.get("authenticated"))
    configured_value = _coerce_bool(record.get("configured", record.get("ready")))

    auth_status = ""
    if status in {"ready", "ok", "valid", "configured", "authenticated", "active"}:
        auth_status = "ready"
    elif status in {"expired"}:
        auth_status = "expired"
    elif status in {"missing", "not_required", "not-required"}:
        auth_status = "missing" if status == "missing" else "not_required"
    elif status in {"unauthenticated", "not_authenticated", "not-authenticated", "login_required"}:
        auth_status = "missing"

    if not auth_status:
        if login_required_value is True:
            auth_status = "missing"
        elif authenticated_value is True or configured_value is True:
            auth_status = "ready"
        elif authenticated_value is False or configured_value is False:
            auth_status = "missing"

    if not auth_status:
        return {}

    expires_at = str(
        record.get("expires_at", record.get("expiresAt", record.get("expires", ""))) or ""
    ).strip()
    return {
        "auth_status": auth_status,
        "auth_profile": str(
            record.get("auth_profile", record.get("profile", record.get("profileId", record.get("profile_id", ""))))
            or ""
        ).strip(),
        "account": str(
            record.get("account", record.get("accountId", record.get("account_id", record.get("email", ""))))
            or ""
        ).strip(),
        "expires_at": expires_at,
        "detail": str(record.get("detail", record.get("message", "")) or "").strip(),
    }


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = str(value if value is not None else "").strip().lower()
    if token in {"true", "1", "yes", "ready", "ok"}:
        return True
    if token in {"false", "0", "no", "missing", "unauthenticated"}:
        return False
    return None


def inspect_auth_files(provider: str, home: Path | None) -> dict[str, Any]:
    if not home:
        return {}
    spec = get_provider(provider)
    native_path = home / spec.state_dir / "auth.json"
    if spec.name == "picoclaw" and _path_exists(native_path):
        parsed = auth_status_from_picoclaw_auth_json(native_path)
        if parsed:
            return parsed
    if spec.name == "openclaw":
        sqlite_path = home / spec.state_dir / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
        if _path_exists(sqlite_path):
            parsed = auth_status_from_openclaw_sqlite(sqlite_path)
            if parsed:
                return parsed
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


def auth_status_from_openclaw_sqlite(path: Path) -> dict[str, Any]:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT store_json FROM auth_profile_store WHERE store_key = ?",
            ("primary",),
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()
    if not row or not row[0]:
        return {"auth_status": "missing", "detail": "no auth profiles found", "source": f"file:{path.name}"}
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return {}
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return {"auth_status": "missing", "detail": "no auth profiles found", "source": f"file:{path.name}"}

    selected_key = ""
    selected: dict[str, Any] = {}
    for key, item in profiles.items():
        if isinstance(item, dict):
            selected_key = str(key)
            selected = item
            break
    if not selected:
        return {}

    expires_at = _profile_expiry_for_display(selected)
    has_token = any(
        str(selected.get(name, "")).strip()
        for name in ("access", "refresh", "token", "idToken", "key")
    )
    return {
        "auth_status": auth_status_from_expiry(expires_at, has_token=has_token),
        "auth_profile": str(selected.get("displayName", "")).strip() or selected_key,
        "account": str(selected.get("accountId", "")).strip(),
        "expires_at": expires_at,
        "last_refresh": "",
        "detail": str(selected.get("type", "")).strip(),
        "source": f"file:{path.name}",
    }


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

    expires_at = _profile_expiry_for_display(selected)
    has_token = any(
        str(selected.get(name, "")).strip()
        for name in ("access_token", "refresh_token", "token", "id_token", "access", "refresh", "key")
    )
    return {
        "auth_status": auth_status_from_expiry(expires_at, has_token=has_token),
        "auth_profile": str(selected.get("profile_name", "")).strip() or selected_key,
        "account": str(selected.get("account_id", selected.get("accountId", ""))).strip(),
        "expires_at": expires_at,
        "last_refresh": str(selected.get("updated_at", payload.get("updated_at", ""))).strip(),
        "detail": str(selected.get("kind", selected.get("type", ""))).strip(),
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


def auth_status_from_picoclaw_auth_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    credentials = payload.get("credentials", {})
    if not isinstance(credentials, dict) or not credentials:
        return {"auth_status": "missing", "detail": "no picoclaw auth credentials found", "source": f"file:{path.name}"}

    ordered = []
    for name in ("openai", "anthropic", "google-antigravity"):
        if name in credentials:
            ordered.append(name)
    for name in credentials:
        token = str(name).strip()
        if token and token not in ordered:
            ordered.append(token)

    selected_key = ""
    selected: dict[str, Any] = {}
    for key in ordered:
        item = credentials.get(key)
        if isinstance(item, dict):
            selected_key = key
            selected = item
            break
    if not selected:
        return {}

    expires_at = str(selected.get("expires_at", "")).strip()
    has_token = any(
        str(selected.get(name, "")).strip()
        for name in ("access_token", "refresh_token")
    )
    detail = str(selected.get("auth_method", "")).strip() or "oauth"
    return {
        "auth_status": auth_status_from_expiry(expires_at, has_token=has_token),
        "auth_profile": selected_key or "default",
        "account": str(selected.get("account_id", "")).strip(),
        "expires_at": expires_at,
        "last_refresh": "",
        "detail": detail,
        "source": f"file:{path.name}",
    }


def parse_iso_timestamp(value: str) -> datetime | None:
    token = str(value or "").strip()
    if not token:
        return None
    if token.isdigit():
        try:
            stamp = datetime.fromtimestamp(int(token) / 1000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
        return stamp.astimezone(timezone.utc)
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


def _profile_expiry_for_display(profile: dict[str, Any]) -> str:
    expires_at = str(profile.get("expires_at", "")).strip()
    if expires_at:
        return expires_at
    raw = profile.get("expires")
    if isinstance(raw, (int, float)):
        stamp = datetime.fromtimestamp(float(raw) / 1000, tz=timezone.utc)
        return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    token = str(raw or "").strip()
    if token.isdigit():
        stamp = datetime.fromtimestamp(int(token) / 1000, tz=timezone.utc)
        return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return token
