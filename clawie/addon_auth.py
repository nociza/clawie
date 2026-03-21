from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clawie.addons import get_addon


def inspect_addon_auth(addon: str, config_dir: Path) -> dict[str, Any]:
    spec = get_addon(addon)
    if spec.name == "gws":
        return inspect_gws_auth(config_dir)
    raise ValueError(f"unsupported addon auth inspection: {spec.name}")


def inspect_gws_auth(config_dir: Path) -> dict[str, Any]:
    root = Path(config_dir).expanduser()
    credentials = root / "credentials.json"
    client_secret = root / "client_secret.json"
    encrypted = sorted(root.glob("credentials*.enc"))
    credentials_payload = _load_json_object(credentials)
    client_secret_payload = _load_json_object(client_secret)
    client_error = _gws_client_secret_error(client_secret_payload) if client_secret.exists() else ""

    if _has_gws_refresh_token(credentials_payload) and not client_error:
        return {
            "auth_status": "ready",
            "login_required": False,
            "source": "file:credentials.json",
            "detail": "plaintext credentials",
            "config_dir": str(root),
            "credentials_path": str(credentials),
            "client_secret_path": str(client_secret if client_secret.exists() else ""),
            "client_secret_present": client_secret.exists(),
            "client_config_error": "",
        }

    if encrypted:
        return {
            "auth_status": "missing",
            "login_required": True,
            "source": f"file:{encrypted[0].name}",
            "detail": "encrypted credentials exist but no portable shared credentials export is present",
            "config_dir": str(root),
            "credentials_path": str(credentials),
            "client_secret_path": str(client_secret if client_secret.exists() else ""),
            "client_secret_present": client_secret.exists(),
            "client_config_error": client_error,
        }

    if _has_gws_refresh_token(credentials_payload):
        return {
            "auth_status": "missing",
            "login_required": True,
            "source": "file:credentials.json",
            "detail": "plaintext credentials exist but OAuth client config is invalid",
            "config_dir": str(root),
            "credentials_path": str(credentials),
            "client_secret_path": str(client_secret if client_secret.exists() else ""),
            "client_secret_present": client_secret.exists(),
            "client_config_error": client_error or "invalid client_secret.json format",
        }

    if client_secret.exists():
        return {
            "auth_status": "missing",
            "login_required": True,
            "source": "file:client_secret.json",
            "detail": "OAuth client is configured; run login to create shared credentials"
            if not client_error
            else "OAuth client config is invalid; run setup to recreate it",
            "config_dir": str(root),
            "credentials_path": str(credentials),
            "client_secret_path": str(client_secret),
            "client_secret_present": True,
            "client_config_error": client_error,
        }

    return {
        "auth_status": "missing",
        "login_required": True,
        "source": "none",
        "detail": "no addon credentials configured",
        "config_dir": str(root),
        "credentials_path": str(credentials),
        "client_secret_path": "",
        "client_secret_present": False,
        "client_config_error": "",
    }


def parse_gws_status_output(raw: str, *, config_dir: Path | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"gws auth status did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("gws auth status did not return an object")

    client_path = str(payload.get("client_config", ""))
    credentials_path = str(payload.get("plain_credentials", ""))
    client_exists = bool(payload.get("client_config_exists", False))
    client_error = str(payload.get("client_config_error", "")).strip()
    has_refresh_token = bool(payload.get("has_refresh_token", False))
    credential_source = str(payload.get("credential_source", "")).strip().lower()
    storage = str(payload.get("storage", "")).strip().lower()
    root = str(config_dir or Path(credentials_path).parent if credentials_path else "")
    ready = client_exists and not client_error and has_refresh_token and credential_source != "none"

    if ready:
        detail = "plaintext credentials" if storage == "plaintext" else "oauth credentials"
        auth_status = "ready"
        login_required = False
    elif client_error:
        detail = f"OAuth client config is invalid: {client_error}"
        auth_status = "missing"
        login_required = True
    elif client_exists:
        detail = "OAuth client is configured; run login to create shared credentials"
        auth_status = "missing"
        login_required = True
    else:
        detail = "no addon credentials configured"
        auth_status = "missing"
        login_required = True

    return {
        "auth_status": auth_status,
        "login_required": login_required,
        "source": "command:auth status",
        "detail": detail,
        "config_dir": root,
        "credentials_path": credentials_path,
        "client_secret_path": client_path,
        "client_secret_present": client_exists,
        "client_config_error": client_error,
    }


def parse_gws_exported_credentials(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"gws auth export did not return JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("gws auth export returned an empty credentials payload")
    return payload


def _is_nonempty_json_object(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return isinstance(payload, dict) and bool(payload)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _has_gws_refresh_token(payload: dict[str, Any]) -> bool:
    return bool(str(payload.get("refresh_token", "")).strip())


def _gws_client_secret_error(payload: dict[str, Any]) -> str:
    if not payload:
        return "client_secret.json is not valid JSON"
    root = payload.get("installed")
    if not isinstance(root, dict):
        root = payload.get("web")
    if not isinstance(root, dict):
        return "client_secret.json must contain an 'installed' or 'web' object"
    if not str(root.get("client_id", "")).strip():
        return "client_secret.json is missing client_id"
    if not str(root.get("client_secret", "")).strip():
        return "client_secret.json is missing client_secret"
    return ""
