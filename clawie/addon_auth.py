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

    if _is_nonempty_json_object(credentials):
        return {
            "auth_status": "ready",
            "login_required": False,
            "source": "file:credentials.json",
            "detail": "plaintext credentials",
            "config_dir": str(root),
            "credentials_path": str(credentials),
            "client_secret_path": str(client_secret if client_secret.exists() else ""),
            "client_secret_present": client_secret.exists(),
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
        }

    if client_secret.exists():
        return {
            "auth_status": "missing",
            "login_required": True,
            "source": "file:client_secret.json",
            "detail": "OAuth client is configured; run login to create shared credentials",
            "config_dir": str(root),
            "credentials_path": str(credentials),
            "client_secret_path": str(client_secret),
            "client_secret_present": True,
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
