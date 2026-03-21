from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass(frozen=True)
class AddonSpec:
    name: str
    label: str
    description: str
    executable: str
    install_method: str
    install_package: str
    shared_config_dir: str
    target_config_rel: str
    auth_files: tuple[str, ...]
    auth_status_command: tuple[str, ...] = ()
    auth_login_command: tuple[str, ...] = ()
    auth_setup_command: tuple[str, ...] = ()
    auth_export_command: tuple[str, ...] = ()
    config_dir_env: str = ""
    tools_snippet: str = ""
    env_exports: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ServiceAddonSpec:
    """Addon spec for system-service addons (installed via apt, managed as systemd services)."""

    name: str
    label: str
    description: str
    install_method: str  # "apt"
    apt_packages: tuple[str, ...] = ()
    check_executables: tuple[str, ...] = ()
    default_resolution: str = "1280x720x24"
    default_display_offset: int = 101
    default_vnc_port_offset: int = 5900
    default_novnc_port_offset: int = 6080
    tools_snippet: str = ""
    env_exports: tuple[tuple[str, str], ...] = ()


# ── Tools snippet templates ──────────────────────────────────────────

_DISPLAY_TOOLS_TEMPLATE = """\
## Virtual Display & Browser Automation

You have a virtual display running at **DISPLAY=:{display_number}** ({resolution}).
The `DISPLAY` environment variable is already set in your shell.

### Available tools

| Tool | Usage | Description |
|------|-------|-------------|
| **Chromium** | `chromium-browser --no-sandbox --disable-gpu` | Full browser for web automation |
| **xdotool** | `xdotool key Return`, `xdotool type "text"` | Simulate keyboard/mouse input |
| **scrot** | `scrot /tmp/screenshot.png` | Take screenshots of the display |
| **xterm** | `xterm -display :{display_number} &` | Open a terminal on the display |
| **ImageMagick** | `convert`, `identify` | Image processing and conversion |

### Browser automation patterns

```bash
# Launch Chromium to a URL
chromium-browser --no-sandbox --disable-gpu --disable-software-rasterizer \\
  --window-size=1280,720 "https://example.com" &

# Wait for window, then screenshot
sleep 3
scrot /tmp/page.png

# Click at coordinates
xdotool mousemove 640 360 click 1

# Type text into focused field
xdotool type --delay 50 "search query"
xdotool key Return
```

### Ports

- VNC: `localhost:{vnc_port}` (for VNC clients)
- noVNC: `http://localhost:{novnc_port}/vnc.html` (browser-based viewer)"""

_GWS_TOOLS_TEMPLATE = """\
## Google Workspace CLI

You have the `gws` CLI tool available for interacting with Google Workspace APIs.

### Available services

| Service | Command prefix | Description |
|---------|---------------|-------------|
| **Gmail** | `gws gmail` | Read, send, search, and manage emails |
| **Drive** | `gws drive` | List, upload, download, and manage files |
| **Sheets** | `gws sheets` | Read and write spreadsheet data |
| **Calendar** | `gws calendar` | List, create, and manage calendar events |
| **Chat** | `gws chat` | Send and read Google Chat messages |
| **Docs** | `gws docs` | Create and read Google Docs |

### Common usage patterns

```bash
# List recent emails
gws gmail messages list --max-results 10

# Send an email
gws gmail messages send --to "user@example.com" --subject "Hello" --body "Message body"

# Search emails
gws gmail messages list --query "from:boss@company.com is:unread"

# List files in Drive
gws drive files list --max-results 20

# Download a file
gws drive files download --file-id FILE_ID --output /tmp/file.pdf

# Read spreadsheet data
gws sheets values get --spreadsheet-id SHEET_ID --range "Sheet1!A1:D10"

# List upcoming calendar events
gws calendar events list --max-results 5

# Create a calendar event
gws calendar events create --summary "Meeting" --start "2024-01-15T10:00:00" --end "2024-01-15T11:00:00"
```

### Authentication

Authentication is pre-configured. Use `gws auth status` to verify."""


# ── Addon registry ───────────────────────────────────────────────────

ADDONS: dict[str, Union[AddonSpec, ServiceAddonSpec]] = {
    "gws": AddonSpec(
        name="gws",
        label="Google Workspace CLI",
        description="Google Workspace API CLI for Gmail, Drive, Sheets, Chat, and more",
        executable="gws",
        install_method="npm",
        install_package="@googleworkspace/cli",
        shared_config_dir="gws",
        target_config_rel=".config/gws",
        auth_files=("credentials.json", "client_secret.json"),
        auth_status_command=("auth", "status"),
        auth_login_command=("auth", "login"),
        auth_setup_command=("auth", "setup"),
        auth_export_command=("auth", "export", "--unmasked"),
        config_dir_env="GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
        tools_snippet=_GWS_TOOLS_TEMPLATE,
        env_exports=(("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "{config_dir}"),),
    ),
    "display": ServiceAddonSpec(
        name="display",
        label="Virtual Display",
        description="Xvfb + Openbox + x11vnc + noVNC virtual display for browser automation",
        install_method="apt",
        apt_packages=(
            "xvfb",
            "x11vnc",
            "novnc",
            "websockify",
            "openbox",
            "xdotool",
            "scrot",
            "imagemagick",
            "chromium-browser",
            "fonts-liberation",
            "xterm",
        ),
        check_executables=("Xvfb", "x11vnc", "openbox"),
        default_resolution="1280x720x24",
        default_display_offset=101,
        default_vnc_port_offset=5900,
        default_novnc_port_offset=6080,
        tools_snippet=_DISPLAY_TOOLS_TEMPLATE,
        env_exports=(("DISPLAY", ":{display_number}"),),
    ),
}


def addon_names() -> list[str]:
    return sorted(ADDONS)


def get_addon(name: str) -> Union[AddonSpec, ServiceAddonSpec]:
    token = str(name or "").strip().lower()
    if token not in ADDONS:
        choices = ", ".join(addon_names())
        raise ValueError(f"addon must be one of: {choices}")
    return ADDONS[token]


def is_service_addon(name: str) -> bool:
    token = str(name or "").strip().lower()
    spec = ADDONS.get(token)
    return isinstance(spec, ServiceAddonSpec)
