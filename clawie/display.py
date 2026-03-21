"""Virtual display addon: Xvfb + Openbox + x11vnc + noVNC management."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


# ── APT package installation ────────────────────────────────────────

def install_display_packages(apt_packages: tuple[str, ...]) -> dict[str, Any]:
    """Install system packages required for the virtual display stack."""
    env_result = subprocess.run(
        ["apt-get", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if env_result.returncode != 0:
        raise RuntimeError("apt-get is not available on this system")

    subprocess.run(
        ["apt-get", "update", "-qq"],
        capture_output=True,
        text=True,
        check=False,
    )

    cmd = ["apt-get", "install", "-y", "--no-install-recommends"] + list(apt_packages)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = "\n".join(part for part in [result.stdout, result.stderr] if str(part).strip()).strip()
    if result.returncode != 0:
        raise RuntimeError(f"apt-get install failed: {output or f'exit {result.returncode}'}")
    return {
        "packages": list(apt_packages),
        "output": output,
    }


def check_display_installed(check_executables: tuple[str, ...]) -> bool:
    """Return True if the key display executables are present."""
    for exe in check_executables:
        if not shutil.which(exe):
            return False
    return True


# ── Display number / port allocation ────────────────────────────────

def allocate_display_number(
    existing_display_numbers: list[int],
    offset: int = 101,
) -> int:
    """Pick the next free display number starting at *offset*."""
    used = set(existing_display_numbers)
    candidate = offset
    while candidate in used:
        candidate += 1
    return candidate


def vnc_port_for_display(display_num: int, vnc_offset: int = 5900) -> int:
    return vnc_offset + display_num


def novnc_port_for_display(display_num: int, novnc_offset: int = 6080) -> int:
    return novnc_offset + display_num


# ── Systemd unit management ─────────────────────────────────────────

UNIT_DIR = Path("/etc/systemd/system")

_XVFB_UNIT = """\
[Unit]
Description=Clawie Xvfb display :{display_num}
After=network.target

[Service]
Type=simple
User={linux_user}
Environment=DISPLAY=:{display_num}
ExecStart=/usr/bin/Xvfb :{display_num} -screen 0 {resolution} -ac +extension GLX +render -noreset
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

_OPENBOX_UNIT = """\
[Unit]
Description=Clawie Openbox WM on display :{display_num}
Requires=clawie-xvfb-{display_num}.service
After=clawie-xvfb-{display_num}.service

[Service]
Type=simple
User={linux_user}
Environment=DISPLAY=:{display_num}
ExecStartPre=/bin/sleep 1
ExecStart=/usr/bin/openbox
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

_X11VNC_UNIT = """\
[Unit]
Description=Clawie x11vnc on display :{display_num}
Requires=clawie-xvfb-{display_num}.service
After=clawie-xvfb-{display_num}.service

[Service]
Type=simple
User={linux_user}
Environment=DISPLAY=:{display_num}
ExecStart=/usr/bin/x11vnc -display :{display_num} -nopw -listen 0.0.0.0 -xkb -ncache 10 -forever -rfbport {vnc_port}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

_NOVNC_UNIT = """\
[Unit]
Description=Clawie noVNC on display :{display_num}
Requires=clawie-x11vnc-{display_num}.service
After=clawie-x11vnc-{display_num}.service

[Service]
Type=simple
User={linux_user}
Environment=DISPLAY=:{display_num}
ExecStart=/usr/bin/websockify --web {novnc_web_dir} {novnc_port} localhost:{vnc_port}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

_UNIT_TEMPLATES = {
    "clawie-xvfb-{display_num}.service": _XVFB_UNIT,
    "clawie-openbox-{display_num}.service": _OPENBOX_UNIT,
    "clawie-x11vnc-{display_num}.service": _X11VNC_UNIT,
    "clawie-novnc-{display_num}.service": _NOVNC_UNIT,
}

_SERVICE_ORDER = (
    "clawie-xvfb-{display_num}.service",
    "clawie-openbox-{display_num}.service",
    "clawie-x11vnc-{display_num}.service",
    "clawie-novnc-{display_num}.service",
)


def _resolve_novnc_web_dir() -> str:
    """Find the noVNC web root on the filesystem."""
    candidates = [
        Path("/usr/share/novnc"),
        Path("/usr/share/noVNC"),
        Path("/snap/novnc/current/utils/websockify"),
    ]
    for path in candidates:
        if path.is_dir():
            return str(path)
    return "/usr/share/novnc"


def write_systemd_units(
    display_num: int,
    linux_user: str,
    resolution: str,
    vnc_port: int,
    novnc_port: int,
) -> list[str]:
    """Write systemd unit files for a display stack. Returns list of written unit paths."""
    novnc_web_dir = _resolve_novnc_web_dir()
    ctx = {
        "display_num": str(display_num),
        "linux_user": linux_user,
        "resolution": resolution,
        "vnc_port": str(vnc_port),
        "novnc_port": str(novnc_port),
        "novnc_web_dir": novnc_web_dir,
    }
    written: list[str] = []
    for name_tpl, content_tpl in _UNIT_TEMPLATES.items():
        unit_name = name_tpl.format(**ctx)
        unit_content = content_tpl.format(**ctx)
        unit_path = UNIT_DIR / unit_name
        unit_path.write_text(unit_content, encoding="utf-8")
        written.append(str(unit_path))
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)
    return written


def start_display_services(display_num: int) -> list[str]:
    """Enable and start all display services for the given display number."""
    started: list[str] = []
    for name_tpl in _SERVICE_ORDER:
        unit = name_tpl.format(display_num=display_num)
        subprocess.run(
            ["systemctl", "enable", "--now", unit],
            capture_output=True,
            text=True,
            check=False,
        )
        started.append(unit)
    return started


def stop_display_services(display_num: int) -> list[str]:
    """Stop and disable all display services for the given display number."""
    stopped: list[str] = []
    for name_tpl in reversed(_SERVICE_ORDER):
        unit = name_tpl.format(display_num=display_num)
        subprocess.run(
            ["systemctl", "disable", "--now", unit],
            capture_output=True,
            text=True,
            check=False,
        )
        stopped.append(unit)
    return stopped


def remove_systemd_units(display_num: int) -> list[str]:
    """Remove systemd unit files for a display stack."""
    removed: list[str] = []
    for name_tpl in _UNIT_TEMPLATES:
        unit_name = name_tpl.format(display_num=display_num)
        unit_path = UNIT_DIR / unit_name
        if unit_path.exists():
            unit_path.unlink()
            removed.append(str(unit_path))
    if removed:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)
    return removed


def display_service_status(display_num: int) -> dict[str, str]:
    """Check systemd service status for each component of the display stack."""
    statuses: dict[str, str] = {}
    for name_tpl in _SERVICE_ORDER:
        unit = name_tpl.format(display_num=display_num)
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            check=False,
        )
        statuses[unit] = str(result.stdout).strip() or "unknown"
    return statuses


def display_status(
    display_num: int,
    vnc_port: int,
    novnc_port: int,
) -> dict[str, Any]:
    """Return a combined status dict for a display stack."""
    services = display_service_status(display_num)
    all_active = all(s == "active" for s in services.values())
    any_active = any(s == "active" for s in services.values())
    if all_active:
        overall = "running"
    elif any_active:
        overall = "partial"
    else:
        overall = "stopped"
    return {
        "display_number": display_num,
        "vnc_port": vnc_port,
        "novnc_port": novnc_port,
        "status": overall,
        "services": services,
    }
