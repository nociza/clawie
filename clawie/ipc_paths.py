"""Stable paths shared by clawied and managed provider service units."""
from __future__ import annotations

import hashlib
from pathlib import Path


CONTROL_SOCKET_ROOT = Path("/run/clawie/control")


def control_socket_path(state_root: str | Path, uid: int | str) -> Path:
    """Return the request-only socket path for one manager and control UID."""
    canonical_root = str(Path(state_root).expanduser().absolute())
    manager_id = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()[:16]
    return CONTROL_SOCKET_ROOT / f"{uid}-{manager_id}.sock"


def delegation_socket_path(state_root: str | Path, uid: int | str) -> Path:
    """Return the request-only delegation socket for one manager and agent UID."""
    canonical_root = str(Path(state_root).expanduser().absolute())
    manager_id = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()[:16]
    return CONTROL_SOCKET_ROOT / f"delegation-{uid}-{manager_id}.sock"
