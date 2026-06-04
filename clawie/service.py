from __future__ import annotations

import copy
import json
import os
import platform
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import crypt  # removed from the stdlib in Python 3.13 (PEP 594)
except ModuleNotFoundError:  # pragma: no cover
    crypt = None  # type: ignore[assignment]

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]  # Python 3.10 fallback
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

from clawie.auth_sources import (
    load_claude_auth,
    load_codex_auth,
    merge_picoclaw_auth_store,
    merge_provider_auth_profile,
)
from clawie.addon_auth import inspect_addon_auth, parse_gws_exported_credentials, parse_gws_status_output
from clawie.addon_integration import (
    inject_addon_env_block,
    inject_addon_tools_snippet,
    remove_addon_env_block,
    remove_addon_tools_snippet,
    render_addon_env_block,
)
from clawie.addons import AddonSpec, ServiceAddonSpec, ToolAddonSpec, addon_names, get_addon, is_service_addon
from clawie.display import (
    allocate_display_number,
    check_display_installed,
    display_status as _display_stack_status,
    install_display_packages,
    novnc_port_for_display,
    remove_systemd_units,
    start_display_services,
    stop_display_services,
    vnc_port_for_display,
    write_systemd_units,
)
from clawie.provider_auth import (
    empty_auth_payload,
    inspect_auth_files,
    login_required,
    parse_iso_timestamp,
    parse_provider_auth_status_output,
)
from clawie.provider_channels import dedupe_channels, get_channel_adapter
from clawie.providers import (
    detect_installed_providers,
    get_provider,
    provider_names,
    shared_auth_paths_for_providers,
)
from clawie.store import StateStore


class SetupError(RuntimeError):
    pass


class AgentExistsError(RuntimeError):
    pass


class AgentNotFoundError(RuntimeError):
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


_LEGACY_HEARTBEAT_PROMPT = (
    "Surface status changes, blockers, and long-running work clearly so the control plane can monitor "
    "progress.\n"
)


def _default_core_prompt_content(prompt_name: str, agent_id: str = "", display_name: str = "") -> str:
    name = str(prompt_name).strip()
    agent_token = str(agent_id).strip()
    display_token = str(display_name).strip() or agent_token or "this agent"
    identity_line = f"Your agent ID is `{agent_token}`.\n" if agent_token else ""
    prompts = {
        "SOUL.md": (
            f"You are {display_token}, a managed AI agent in clawie.\n"
            f"{identity_line}"
            "Work as one node in a local multi-agent system: be accurate, execution-focused, and willing to "
            "delegate when another agent is a better fit.\n"
        ),
        "IDENTITY.md": (
            "clawie is a local control plane for provisioning, orchestrating, and monitoring a fleet of AI "
            "agents.\n"
            "It manages agents across providers from one CLI and supports recursive delegation, Linux-user "
            "isolation, credential management, and a terminal dashboard.\n"
            "You are operating inside that system, not as an isolated standalone assistant.\n"
        ),
        "AGENTS.md": (
            "Other clawie agents may exist with different channels, providers, and model tiers.\n"
            "Coordinate through clawie's delegation system for fan-out, specialization, or recursive sub-tasks.\n"
            "Do not invent remote or network delegation paths; clawie delegation is local and explicit.\n"
        ),
        "TOOLS.md": (
            "Use the tools and runtime available in this environment first.\n"
            "If a task depends on clawie orchestration or local agent state, prefer clawie commands and report "
            "missing permissions or runtime access explicitly.\n"
        ),
        "MEMORY.md": (
            "Assume long-term memory is limited.\n"
            "Preserve only durable facts that matter for future work, and prefer concise summaries over raw "
            "transcripts.\n"
        ),
        "HEARTBEAT.md": (
            "Heartbeat handling:\n"
            "- Only reply `HEARTBEAT_OK` when the current user message is an OpenClaw heartbeat poll, such as "
            "a message that explicitly tells you to read HEARTBEAT.md and says to reply `HEARTBEAT_OK` if "
            "nothing needs attention.\n"
            "- Never reply `HEARTBEAT_OK` to normal user or channel messages, including short status checks "
            "like \"what about now\". Answer the user's actual message instead.\n"
            "- For true heartbeat polls, surface status changes, blockers, and long-running work clearly so "
            "the control plane can monitor progress.\n"
        ),
        "BOOTSTRAP.md": (
            "On startup, ground yourself in the prompt files and current workspace before answering.\n"
            "If the work can be split safely, identify delegation candidates early and keep returned context "
            "compact.\n"
        ),
        "USER.md": (
            "Default interaction style: concise, factual, and execution-focused.\n"
            "State blockers, assumptions, and required follow-up actions plainly.\n"
        ),
    }
    return prompts.get(name, "")


def _is_legacy_core_prompt_default(prompt_name: str, content: str) -> bool:
    name = str(prompt_name).strip().upper()
    if name == "HEARTBEAT.MD":
        return str(content).strip() == _LEGACY_HEARTBEAT_PROMPT.strip()
    return False


class ZeroClawService:
    EVENT_LIMIT = 2000
    SSHD_DENY_USERS_FILE = Path("/etc/ssh/sshd_config.d/99-clawie-deny-users.conf")
    HOMEBREW_PREFIX = Path("/home/linuxbrew/.linuxbrew")
    GLOBAL_PROFILE_DIR = Path("/etc/profile.d")
    GLOBAL_HOMEBREW_PROFILE_FILE = GLOBAL_PROFILE_DIR / "00-homebrew.sh"
    GLOBAL_FNM_PROFILE_FILE = GLOBAL_PROFILE_DIR / "zz-fnm.sh"
    GLOBAL_CLAUDE_PROFILE_FILE = GLOBAL_PROFILE_DIR / "20-claude-shared.sh"
    SHARED_CLAUDE_DIR = Path("/var/lib/clawie/claude-shared")
    SHARED_PROVIDER_AUTH_DIR = Path("/var/lib/clawie/provider-auth")
    SHARED_TOOLCHAIN_DIR = Path("/var/lib/clawie/toolchain")
    MAINTENANCE_CRON_FILE = Path("/etc/cron.d/clawie-maintenance")
    MAINTENANCE_LOG_FILE = Path("/var/log/clawie-maintenance.log")
    SHARED_CLAUDE_SUBDIRS = (
        "backups",
        "cache",
        "debug",
        "session-env",
        "projects",
        "plans",
        "tasks",
        "file-history",
        "paste-cache",
        "plugins",
        "shell-snapshots",
        "telemetry",
        "todos",
    )
    GLOBAL_HOMEBREW_PROFILE_CONTENT = "\n".join(
        [
            "# Managed by clawie: shared runtime path for all users.",
            'export HOMEBREW_PREFIX="/home/linuxbrew/.linuxbrew"',
            'export PNPM_HOME="$HOMEBREW_PREFIX/bin"',
            'case ":$PATH:" in *":$HOMEBREW_PREFIX/bin:"*) ;; *) PATH="$HOMEBREW_PREFIX/bin:$PATH" ;; esac',
            'case ":$PATH:" in *":$HOMEBREW_PREFIX/sbin:"*) ;; *) PATH="$HOMEBREW_PREFIX/sbin:$PATH" ;; esac',
            'case ":$PATH:" in *":$PNPM_HOME:"*) ;; *) PATH="$PNPM_HOME:$PATH" ;; esac',
            "export PATH",
            "",
        ]
    )
    GLOBAL_FNM_PROFILE_CONTENT = "\n".join(
        [
            "# Managed by clawie: fnm activation for interactive bash shells.",
            'if [ -n "${BASH_VERSION:-}" ] && command -v fnm >/dev/null 2>&1; then',
            "  # `su` can inherit another user's XDG_RUNTIME_DIR and break fnm.",
            '  if [ -n "${XDG_RUNTIME_DIR:-}" ]; then',
            '    if [ ! -d "$XDG_RUNTIME_DIR" ] || [ ! -w "$XDG_RUNTIME_DIR" ]; then',
            "      unset XDG_RUNTIME_DIR",
            "    fi",
            "  fi",
            '  export FNM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/fnm"',
            '  eval "$(fnm env --use-on-cd --shell bash)"',
            "fi",
            "",
        ]
    )
    GLOBAL_CLAUDE_PROFILE_CONTENT = "\n".join(
        [
            "# Managed by clawie: shared Claude Code auth/config directory.",
            'export CLAUDE_CONFIG_DIR="/var/lib/clawie/claude-shared"',
            "",
        ]
    )
    SHARED_ADDON_AUTH_DIR = Path("/var/lib/clawie/addon-auth")
    DEFAULT_AGENT_PLUGINS: dict[str, bool] = {
        "scheduler": True,
        "gateway": True,
        "memory": True,
        "web_search": True,
        "delegation": True,
    }
    CREDENTIAL_BUNDLE_SPECS: tuple[dict[str, Any], ...] = (
        {
            "id": "provider-auth",
            "label": "provider auth sessions (.codex/auth.json + auth-profiles.json)",
            "default": True,
            "kind": "provider",
        },
        {
            "id": "git",
            "label": "git auth (.gitconfig/.git-credentials/.config/gh/.ssh)",
            "default": False,
            "kind": "paths",
            "paths": (".gitconfig", ".git-credentials", ".config/gh", ".ssh"),
        },
    )
    CREDENTIAL_BUNDLE_ALIASES: dict[str, str] = {
        "provider": "provider-auth",
        "providers": "provider-auth",
        "provider-auth": "provider-auth",
        "provider_auth": "provider-auth",
        "git": "git",
    }
    DEFAULT_CREDENTIAL_BUNDLES: tuple[str, ...] = ("provider-auth",)
    ADDON_ALIASES: dict[str, str] = {
        "googleworkspace": "gws",
        "google-workspace": "gws",
        "googleworkspace-cli": "gws",
        "google-workspace-cli": "gws",
        "gws": "gws",
        "display": "display",
        "virtual-display": "display",
        "vnc": "display",
        "novnc": "display",
        "xvfb": "display",
    }
    SHARED_TOOLCHAIN_BEGIN = "# >>> clawie-shared-toolchain >>>"
    SHARED_TOOLCHAIN_END = "# <<< clawie-shared-toolchain <<<"
    SHARED_TOOLCHAIN_BLOCK = "\n".join(
        [
            SHARED_TOOLCHAIN_BEGIN,
            "# Shared runtime tools for all spawned users (pnpm/fnm/uv/codex, etc.).",
            'export HOMEBREW_PREFIX="/home/linuxbrew/.linuxbrew"',
            'if [ -d "$HOMEBREW_PREFIX/bin" ]; then',
            '  case ":$PATH:" in',
            '    *":$HOMEBREW_PREFIX/bin:"*) ;;',
            '    *) export PATH="$HOMEBREW_PREFIX/bin:$PATH" ;;',
            "  esac",
            "fi",
            'if [ -d "$HOMEBREW_PREFIX/sbin" ]; then',
            '  case ":$PATH:" in',
            '    *":$HOMEBREW_PREFIX/sbin:"*) ;;',
            '    *) export PATH="$HOMEBREW_PREFIX/sbin:$PATH" ;;',
            "  esac",
            "fi",
            'export PNPM_HOME="$HOMEBREW_PREFIX/bin"',
            'export CLAUDE_CONFIG_DIR="/var/lib/clawie/claude-shared"',
            'export CLAWIE_SHARED_TOOLCHAIN="/var/lib/clawie/toolchain"',
            'if [ -d "$CLAWIE_SHARED_TOOLCHAIN/bin" ]; then',
            '  case ":$PATH:" in',
            '    *":$CLAWIE_SHARED_TOOLCHAIN/bin:"*) ;;',
            '    *) export PATH="$CLAWIE_SHARED_TOOLCHAIN/bin:$PATH" ;;',
            "  esac",
            "fi",
            'if [ -d "$CLAWIE_SHARED_TOOLCHAIN/google-cloud-sdk/bin" ]; then',
            '  case ":$PATH:" in',
            '    *":$CLAWIE_SHARED_TOOLCHAIN/google-cloud-sdk/bin:"*) ;;',
            '    *) export PATH="$CLAWIE_SHARED_TOOLCHAIN/google-cloud-sdk/bin:$PATH" ;;',
            "  esac",
            "fi",
            'case ":$PATH:" in',
            '  *":$PNPM_HOME:"*) ;;',
            '  *) export PATH="$PNPM_HOME:$PATH" ;;',
            "esac",
            'if [ -n "${BASH_VERSION:-}" ] && command -v fnm >/dev/null 2>&1; then',
            '  if [ -n "${XDG_RUNTIME_DIR:-}" ]; then',
            '    if [ ! -d "$XDG_RUNTIME_DIR" ] || [ ! -w "$XDG_RUNTIME_DIR" ]; then',
            "      unset XDG_RUNTIME_DIR",
            "    fi",
            "  fi",
            '  export FNM_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/fnm"',
            '  eval "$(fnm env --use-on-cd --shell bash)"',
            "fi",
            SHARED_TOOLCHAIN_END,
            "",
        ]
    )

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @classmethod
    def credential_bundle_options(cls) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in cls.CREDENTIAL_BUNDLE_SPECS:
            rows.append(
                {
                    "id": str(spec.get("id", "")),
                    "label": str(spec.get("label", "")),
                    "default": bool(spec.get("default", False)),
                }
            )
        return rows

    @classmethod
    def addon_options(cls) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for addon_id in addon_names():
            spec = get_addon(addon_id)
            rows.append(
                {
                    "id": spec.name,
                    "label": spec.label,
                    "description": spec.description,
                }
            )
        return rows

    @classmethod
    def _credential_bundle_spec_map(cls) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for spec in cls.CREDENTIAL_BUNDLE_SPECS:
            token = str(spec.get("id", "")).strip().lower()
            if token:
                rows[token] = dict(spec)
        return rows

    def _canonical_credential_bundle(self, bundle: str) -> str:
        token = str(bundle).strip().lower().replace("_", "-")
        if not token:
            return ""
        return str(self.CREDENTIAL_BUNDLE_ALIASES.get(token, token))

    def _canonical_addon(self, addon: str) -> str:
        token = str(addon).strip().lower().replace("_", "-")
        if not token:
            return ""
        return str(self.ADDON_ALIASES.get(token, token))

    def _normalize_agent_addons(self, payload: Any) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if not isinstance(payload, dict):
            return rows
        for raw_key, raw_value in payload.items():
            token = self._canonical_addon(str(raw_key))
            if not token:
                continue
            try:
                get_addon(token)
            except ValueError:
                continue
            if isinstance(raw_value, dict):
                enabled = bool(raw_value.get("enabled", True))
                last_paths = self._normalized_string_list(raw_value.get("last_applied_paths", []))
                _STANDARD_KEYS = {"enabled", "credential_mode", "last_applied_at", "last_applied_paths", "last_revoked_at", "last_source"}
                row: dict[str, Any] = {
                    "enabled": enabled,
                    "credential_mode": str(raw_value.get("credential_mode", "shared")).strip().lower() or "shared",
                    "last_applied_at": str(raw_value.get("last_applied_at", "")),
                    "last_applied_paths": last_paths,
                    "last_revoked_at": str(raw_value.get("last_revoked_at", "")),
                    "last_source": str(raw_value.get("last_source", "")),
                }
                for key, val in raw_value.items():
                    if key not in _STANDARD_KEYS:
                        row[key] = val
                rows[token] = row
                continue
            rows[token] = {
                "enabled": bool(raw_value),
                "credential_mode": "shared",
                "last_applied_at": "",
                "last_applied_paths": [],
                "last_revoked_at": "",
                "last_source": "",
            }
        return rows

    def _enabled_agent_addons(self, agent_state: dict[str, Any]) -> list[str]:
        addons = self._normalize_agent_addons(agent_state.get("addons"))
        return sorted(token for token, data in addons.items() if bool(data.get("enabled", False)))

    def _normalize_credential_bundles(
        self,
        bundles: list[str] | tuple[str, ...] | None,
        *,
        include_defaults: bool,
    ) -> list[str]:
        allowed = self._credential_bundle_spec_map()
        seeded: list[str] = []
        if include_defaults:
            seeded.extend(self.DEFAULT_CREDENTIAL_BUNDLES)
        if bundles:
            seeded.extend(str(item) for item in bundles)

        selected: list[str] = []
        seen: set[str] = set()
        invalid: list[str] = []
        for raw in seeded:
            token = self._canonical_credential_bundle(raw)
            if not token:
                continue
            if token not in allowed:
                invalid.append(str(raw))
                continue
            if token in seen:
                continue
            seen.add(token)
            selected.append(token)
        if invalid:
            choices = ", ".join(sorted(allowed))
            raise ValueError(f"unknown credential bundle(s): {', '.join(invalid)} (supported: {choices})")
        return selected

    def _ordered_credential_bundles(self, bundles: list[str]) -> list[str]:
        order = {
            str(spec.get("id", "")).strip().lower(): idx
            for idx, spec in enumerate(self.CREDENTIAL_BUNDLE_SPECS)
        }
        rows = self._normalize_credential_bundles(bundles, include_defaults=False)
        return sorted(rows, key=lambda token: order.get(token, 10_000))

    @staticmethod
    def _normalized_string_list(payload: Any) -> list[str]:
        rows: list[str] = []
        if not isinstance(payload, list):
            return rows
        for item in payload:
            token = str(item).strip()
            if token:
                rows.append(token)
        return rows

    def _normalize_credential_sync_state(self, payload: Any, *, default_when_missing: bool) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        raw_bundles = payload.get("bundles")
        include_defaults = default_when_missing and not isinstance(raw_bundles, list)
        try:
            bundles = self._normalize_credential_bundles(
                raw_bundles if isinstance(raw_bundles, list) else [],
                include_defaults=include_defaults,
            )
        except ValueError:
            bundles = self._normalize_credential_bundles([], include_defaults=default_when_missing)
        return {
            "bundles": self._ordered_credential_bundles(bundles),
            "last_synced_at": str(payload.get("last_synced_at", "")),
            "last_source_home": str(payload.get("last_source_home", "")),
            "last_synced_paths": self._normalized_string_list(payload.get("last_synced_paths", [])),
            "last_revoked_at": str(payload.get("last_revoked_at", "")),
            "last_revoked_paths": self._normalized_string_list(payload.get("last_revoked_paths", [])),
            "shared_provider_auth": bool(payload.get("shared_provider_auth", False)),
        }

    @staticmethod
    def _dedupe_paths(paths: list[str]) -> list[str]:
        seen: set[str] = set()
        rows: list[str] = []
        for item in paths:
            token = str(item).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            rows.append(token)
        return rows

    @staticmethod
    def _current_linux_user() -> str:
        try:
            return str(pwd.getpwuid(os.geteuid()).pw_name)
        except KeyError:
            return ""

    def _assert_linux_user_manageable(self, linux_user: str, action: str) -> None:
        token = str(linux_user).strip()
        if not token:
            raise SetupError(f"{action} requires an agent linux_user")
        if os.geteuid() == 0:
            return
        if token != self._current_linux_user():
            raise SetupError(
                f"{action} requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )

    @staticmethod
    def _default_source_home() -> Path:
        sudo_user = os.environ.get("SUDO_USER", "").strip()
        if sudo_user:
            return Path("/home") / sudo_user
        return Path.home()

    def _shared_provider_auth_home(self) -> Path:
        preferred = self.SHARED_PROVIDER_AUTH_DIR
        if preferred.exists():
            return preferred
        parent = preferred.parent
        if os.geteuid() == 0 or os.access(parent, os.W_OK | os.X_OK):
            return preferred
        return self.store.root / "shared-provider-auth"

    def _shared_provider_auth_scope(self) -> str:
        return "system" if self._ensure_shared_provider_auth_root() == self.SHARED_PROVIDER_AUTH_DIR else "local"

    def _ensure_shared_provider_auth_root(self) -> Path:
        root = self._shared_provider_auth_home()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            root = self.store.root / "shared-provider-auth"
            root.mkdir(parents=True, exist_ok=True)
        self._relax_shared_path_permissions(root)
        for rel in shared_auth_paths_for_providers(provider_names()):
            parent = (root / rel).parent
            parent.mkdir(parents=True, exist_ok=True)
            self._relax_shared_path_permissions(parent)
        return root

    def _relax_shared_provider_auth_permissions(self) -> None:
        root = self._shared_provider_auth_home()
        if not root.exists():
            return
        self._relax_shared_path_permissions(root)
        for rel in shared_auth_paths_for_providers(provider_names()):
            path = root / rel
            if path.parent.exists():
                self._relax_shared_path_permissions(path.parent)
            if path.exists():
                self._relax_shared_path_permissions(path)

    def _shared_toolchain_home(self) -> Path:
        preferred = self.SHARED_TOOLCHAIN_DIR
        if preferred.exists():
            return preferred
        parent = preferred.parent
        if os.geteuid() == 0 or os.access(parent, os.W_OK | os.X_OK):
            return preferred
        return self.store.root / "shared-toolchain"

    def _shared_toolchain_scope(self) -> str:
        return "system" if self._ensure_shared_toolchain_root() == self.SHARED_TOOLCHAIN_DIR else "local"

    def _ensure_shared_toolchain_root(self) -> Path:
        root = self._shared_toolchain_home()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            root = self.store.root / "shared-toolchain"
            root.mkdir(parents=True, exist_ok=True)
        (root / "bin").mkdir(parents=True, exist_ok=True)
        self._relax_path_tree_permissions(root)
        return root

    def _shared_toolchain_path_entries(self) -> list[str]:
        root = self._shared_toolchain_home()
        return [str(root / "bin"), str(root / "google-cloud-sdk" / "bin")]

    def _shared_addon_auth_home(self) -> Path:
        preferred = self.SHARED_ADDON_AUTH_DIR
        if preferred.exists():
            return preferred
        parent = preferred.parent
        if os.geteuid() == 0 or os.access(parent, os.W_OK | os.X_OK):
            return preferred
        return self.store.root / "shared-addon-auth"

    def _shared_addon_auth_scope(self) -> str:
        return "system" if self._ensure_shared_addon_auth_root() == self.SHARED_ADDON_AUTH_DIR else "local"

    def _ensure_shared_addon_auth_root(self) -> Path:
        root = self._shared_addon_auth_home()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            root = self.store.root / "shared-addon-auth"
            root.mkdir(parents=True, exist_ok=True)
        self._relax_path_tree_permissions(root)
        return root

    def _shared_addon_config_dir(self, addon: str) -> Path:
        spec = get_addon(addon)
        return self._ensure_shared_addon_auth_root() / spec.shared_config_dir

    def _ensure_shared_addon_config_dir(self, addon: str) -> Path:
        root = self._shared_addon_config_dir(addon)
        root.mkdir(parents=True, exist_ok=True)
        self._relax_path_tree_permissions(root)
        return root

    def _relax_shared_addon_permissions(self, addon: str | None = None) -> None:
        root = self._shared_addon_auth_home()
        if addon:
            root = self._shared_addon_config_dir(addon)
        if not root.exists():
            return
        self._relax_path_tree_permissions(root)

    @staticmethod
    def _relax_shared_path_permissions(path: Path) -> None:
        try:
            current_mode = int(path.stat().st_mode) & 0o777
            if path.is_dir():
                target_mode = 0o777
            else:
                # Preserve execute bits for toolchain binaries/scripts while still relaxing readability/writability.
                target_mode = 0o666 | (current_mode & 0o111)
            if current_mode != target_mode:
                os.chmod(path, target_mode)
        except OSError:
            return

    @classmethod
    def _relax_path_tree_permissions(cls, path: Path) -> None:
        if not path.exists():
            return
        cls._relax_shared_path_permissions(path)
        if not path.is_dir():
            return
        for child in path.rglob("*"):
            cls._relax_shared_path_permissions(child)

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _write_replaceable_json_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        try:
            path.write_text(content, encoding="utf-8")
            return
        except PermissionError:
            if path.exists() and os.access(path.parent, os.W_OK | os.X_OK):
                path.unlink()
                path.write_text(content, encoding="utf-8")
                return
            raise

    @staticmethod
    def _chown_path(path: Path, username: str) -> None:
        token = str(username).strip()
        if not token or os.geteuid() != 0:
            return
        cmd = ["chown", f"{token}:{token}", str(path)]
        try:
            if path.is_symlink():
                cmd = ["chown", "-h", f"{token}:{token}", str(path)]
        except OSError:
            cmd = ["chown", "-h", f"{token}:{token}", str(path)]
        subprocess.run(cmd, check=False, capture_output=True, text=True)

    @classmethod
    def _chown_tree(cls, path: Path, username: str) -> None:
        token = str(username).strip()
        if not token or os.geteuid() != 0 or not path.exists():
            return
        cls._chown_path(path, token)
        if not path.is_dir():
            return
        for child in path.rglob("*"):
            cls._chown_path(child, token)

    def _copy_if_present(self, src: Path, dst: Path) -> bool:
        if not src.exists():
            return False
        if src.resolve() == dst.resolve():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.copy2(src, dst)
        self._relax_shared_path_permissions(dst.parent)
        self._relax_shared_path_permissions(dst)
        return True

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    def _write_provider_auth_profile(
        self,
        provider: str,
        imported: dict[str, str],
    ) -> list[str]:
        shared_home = self._ensure_shared_provider_auth_root()
        target = shared_home / get_provider(provider).state_dir / "auth-profiles.json"
        existing = self._read_json_file(target)
        payload = merge_provider_auth_profile(existing, imported)
        self._write_replaceable_json_file(target, payload)
        self._relax_shared_path_permissions(target.parent)
        self._relax_shared_path_permissions(target)
        return [str(target)]

    def _write_picoclaw_auth_store(self, imported: dict[str, str]) -> list[str]:
        shared_home = self._ensure_shared_provider_auth_root()
        target = shared_home / ".picoclaw" / "auth.json"
        existing = self._read_json_file(target)
        payload = merge_picoclaw_auth_store(existing, imported)
        self._write_replaceable_json_file(target, payload)
        self._relax_shared_path_permissions(target.parent)
        self._relax_shared_path_permissions(target)
        return [str(target)]

    def _write_provider_auth_profiles(
        self,
        providers: list[str],
        imported: dict[str, str],
    ) -> list[str]:
        updated: list[str] = []
        seen: set[str] = set()
        for item in providers:
            token = str(item or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            if token == "picoclaw":
                updated.extend(self._write_picoclaw_auth_store(imported))
            updated.extend(self._write_provider_auth_profile(token, imported))
        return self._dedupe_paths(updated)

    def _seed_shared_provider_auth_from_home(
        self,
        *,
        source_home: Path,
        requested_provider: str | None,
    ) -> list[str]:
        shared_home = self._ensure_shared_provider_auth_root()
        providers: list[str] = []
        if requested_provider:
            providers.append(str(requested_provider).strip().lower())
        else:
            config = self.store.read_config()
            providers.append(str(config.get("provider", "openclaw")).strip().lower())

        updated: list[str] = []
        for rel in shared_auth_paths_for_providers(providers):
            src = source_home / rel
            dst = shared_home / rel
            if self._copy_if_present(src, dst):
                updated.append(str(dst))
        return self._dedupe_paths(updated)

    def _ensure_shared_provider_auth_links(self, target_home: Path, username: str) -> list[str]:
        if not target_home.exists():
            return []
        shared_home = self._ensure_shared_provider_auth_root()
        updated: list[str] = []
        for rel in shared_auth_paths_for_providers(provider_names()):
            src = shared_home / rel
            if not self._path_exists(src):
                continue
            dst = target_home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._chown_tree(dst.parent, username)
            if dst.is_symlink():
                try:
                    if dst.resolve() == src.resolve():
                        continue
                except OSError:
                    pass
                dst.unlink(missing_ok=True)
            elif dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.symlink_to(src)
            subprocess.run(["chown", "-h", f"{username}:{username}", str(dst)], check=False)
            updated.append(str(dst))
        return updated

    def _ensure_picoclaw_native_auth(
        self,
        *,
        home: Path,
        linux_user: str,
        use_shared_auth: bool,
    ) -> None:
        native_target = home / ".picoclaw" / "auth.json"
        if self._path_exists(native_target):
            return

        source_homes: list[Path] = []
        if use_shared_auth:
            shared_home = self._ensure_shared_provider_auth_root()
            shared_native = shared_home / ".picoclaw" / "auth.json"
            if not self._path_exists(shared_native):
                shared_codex = shared_home / ".codex" / "auth.json"
                if self._path_exists(shared_codex):
                    imported = load_codex_auth(shared_home)
                    self._write_picoclaw_auth_store(imported)
            source_homes.append(shared_home)
        source_homes.append(home)

        for source_home in source_homes:
            codex_path = source_home / ".codex" / "auth.json"
            if not self._path_exists(codex_path):
                continue
            imported = load_codex_auth(source_home)
            if source_home == self._ensure_shared_provider_auth_root():
                self._write_picoclaw_auth_store(imported)
                if use_shared_auth:
                    self._ensure_shared_provider_auth_links(target_home=home, username=linux_user)
                    if self._path_exists(native_target):
                        return
                continue
            if native_target.is_symlink() and not self._path_exists(native_target):
                native_target.unlink(missing_ok=True)
            payload = merge_picoclaw_auth_store(self._read_json_file(native_target), imported)
            self._write_json_file(native_target, payload)
            self._chown_tree(home / ".picoclaw", linux_user)
            return

    def _ensure_openclaw_agent_auth_link(self, *, home: Path, linux_user: str) -> None:
        root = home / ".openclaw"
        source = root / "auth-profiles.json"
        if not self._path_exists(source):
            return
        self._repair_openclaw_auth_store(source)
        agent_dir = root / "agents" / "main" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        self._chown_tree(agent_dir, linux_user)
        target = agent_dir / "auth-profiles.json"
        if target.is_symlink():
            target.unlink(missing_ok=True)
        elif target.exists():
            if os.geteuid() != 0 and not os.access(target, os.W_OK):
                return
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copy2(source, target)
        self._chown_tree(target, linux_user)

    def _repair_openclaw_auth_store(self, path: Path) -> bool:
        if not self._path_exists(path):
            return False
        payload = self._read_json_file(path)
        profiles = payload.get("profiles", {})
        if not isinstance(profiles, dict):
            return False

        changed = False
        payload["version"] = int(payload.get("version", 1) or 1)
        order = payload.get("order", {})
        if not isinstance(order, dict):
            order = {}
            payload["order"] = order
            changed = True
        active_profiles = payload.get("active_profiles", {})
        if not isinstance(active_profiles, dict):
            active_profiles = {}
            payload["active_profiles"] = active_profiles
            changed = True

        for profile_id, raw_profile in profiles.items():
            if not isinstance(raw_profile, dict):
                continue
            profile = dict(raw_profile)
            kind = str(profile.get("kind", profile.get("type", ""))).strip().lower()
            provider = str(profile.get("provider", "")).strip()
            if kind and "type" not in profile:
                profile["type"] = "oauth" if kind == "oauth" else kind
            if provider and not order.get(provider):
                order[provider] = [str(profile_id).strip()]
                changed = True
            if provider and not active_profiles.get(provider):
                active_profiles[provider] = str(profile_id).strip()
                changed = True

            access_token = str(profile.get("access_token", "")).strip()
            refresh_token = str(profile.get("refresh_token", "")).strip()
            account_id = str(profile.get("account_id", "")).strip()
            expires_at = str(profile.get("expires_at", "")).strip()
            if access_token and not str(profile.get("access", "")).strip():
                profile["access"] = access_token
            if refresh_token and not str(profile.get("refresh", "")).strip():
                profile["refresh"] = refresh_token
            if account_id and not str(profile.get("accountId", "")).strip():
                profile["accountId"] = account_id
            if expires_at and "expires" not in profile:
                parsed = parse_iso_timestamp(expires_at)
                if parsed is not None:
                    profile["expires"] = int(parsed.timestamp() * 1000)

            if profile != raw_profile:
                profiles[profile_id] = profile
                changed = True

        if changed:
            self._write_json_file(path, payload)
        return changed

    def setup(
        self,
        provider: str,
        api_key: str,
        subscription: str,
        workspace: str,
        api_url: str,
        auth_mode: str | None = None,
        spawn_password: str | None = None,
        clear_spawn_password: bool = False,
        install_runtime: bool = False,
    ) -> dict[str, Any]:
        provider = provider.strip().lower() or "openclaw"
        provider_spec = get_provider(provider)
        api_key_value = api_key.strip()
        mode = self._resolve_auth_mode(provider_spec.name, api_key_value, auth_mode)
        install_result: dict[str, Any] | None = None
        if install_runtime:
            install_result = self.install_provider_runtime(provider_spec.name)
        config = self.store.read_config()
        config["provider"] = provider_spec.name
        config["auth_mode"] = mode
        config["subscription"] = subscription.strip()
        config["workspace"] = workspace.strip()
        config["api_url"] = api_url.strip()
        credentials = self._normalized_provider_credentials(config)
        provider_creds = {"auth_mode": mode}
        if mode == "api_key":
            provider_creds["api_key"] = api_key_value
        credentials[provider_spec.name] = provider_creds
        config["provider_credentials"] = credentials
        config["api_key"] = provider_creds.get("api_key", "")
        if clear_spawn_password:
            config["spawn_password_hash"] = ""
        elif spawn_password is not None:
            config["spawn_password_hash"] = self._hash_password(spawn_password)
        if install_runtime:
            self._mark_runtime_installed(config, provider_spec.name)
        created = config.get("created_at") or now_iso()
        config["created_at"] = created
        config["updated_at"] = now_iso()
        self.store.write_config(config)

        state = self.store.read_state()
        self._event(
            state,
            "setup.initialized",
            "Clawie configuration initialized",
            {
                "provider": config["provider"],
                "workspace": config["workspace"],
                "subscription": config["subscription"],
                "auth_mode": config["auth_mode"],
                "spawn_password_configured": bool(config.get("spawn_password_hash")),
                "runtime_installed": self._is_runtime_marked_installed(config, provider_spec.name),
                "runtime_install_method": str((install_result or {}).get("method", "")),
                "runtime_install_package": str((install_result or {}).get("package", "")),
            },
        )
        self.store.write_state(state)
        return config

    def setup_status(self) -> dict[str, Any]:
        config = self.store.read_config()
        provider = str(config.get("provider", "openclaw")).strip().lower() or "openclaw"
        provider_spec = get_provider(provider)
        credentials = self._provider_auth(provider)
        auth_mode = credentials.get("auth_mode", provider_spec.default_auth_mode)
        configured = self._is_provider_configured(provider, credentials)
        return {
            "configured": configured,
            "provider": provider,
            "auth_mode": auth_mode,
            "api_url": config.get("api_url", ""),
            "workspace": config.get("workspace", ""),
            "subscription": config.get("subscription", ""),
            "api_key": redact(str(credentials.get("api_key", ""))),
            "spawn_password_configured": bool(str(config.get("spawn_password_hash", "")).strip()),
            "runtime_installed": self._is_runtime_marked_installed(config, provider_spec.name),
            "updated_at": config.get("updated_at", ""),
        }

    @staticmethod
    def _installed_runtime_names(config: dict[str, Any]) -> set[str]:
        names = {
            str(item).strip().lower()
            for item in config.get("installed_runtimes", [])
            if str(item).strip()
        }
        if bool(config.get("runtime_installed", False)):
            provider = str(config.get("provider", "")).strip().lower()
            if provider:
                names.add(provider)
        return names

    def _is_runtime_marked_installed(self, config: dict[str, Any], provider: str) -> bool:
        return str(provider).strip().lower() in self._installed_runtime_names(config)

    def _mark_runtime_installed(self, config: dict[str, Any], provider: str) -> None:
        names = sorted(self._installed_runtime_names(config) | {str(provider).strip().lower()})
        config["installed_runtimes"] = names
        config["runtime_installed"] = bool(names)

    def install_provider_runtime(self, provider: str) -> dict[str, Any]:
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        executable = self._resolve_executable_in_service_env(spec.name)
        if executable:
            config = self.store.read_config()
            self._mark_runtime_installed(config, spec.name)
            config["updated_at"] = now_iso()
            self.store.write_config(config)
            return {
                "provider": spec.name,
                "installed": False,
                "already_present": True,
                "method": spec.install_method,
                "package": spec.install_package or spec.name,
                "executable": executable,
            }

        if spec.install_method == "brew":
            brew = self._resolve_executable_in_service_env("brew")
            if not brew:
                raise SetupError("Homebrew is required to install provider runtimes but was not found in PATH.")
            cmd = [brew, "install", spec.install_package or spec.name]
        elif spec.install_method == "pnpm":
            pnpm = self._resolve_executable_in_service_env("pnpm")
            if not pnpm:
                raise SetupError("pnpm is required to install provider runtimes but was not found in PATH.")
            cmd = [pnpm, "add", "-g", spec.install_package or spec.name]
        else:
            raise SetupError(f"provider '{spec.name}' does not define an install method")

        env = self._service_env("")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        output = "\n".join(part for part in [result.stdout, result.stderr] if str(part).strip()).strip()
        if result.returncode != 0:
            raise SetupError(
                f"failed to install runtime for {spec.name} via {spec.install_method}: {output or f'exit {result.returncode}'}"
            )

        executable = self._resolve_provider_executable(spec.name)
        config = self.store.read_config()
        self._mark_runtime_installed(config, spec.name)
        config["updated_at"] = now_iso()
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(
            state,
            "runtime.installed",
            f"Installed runtime for {spec.name}",
            {
                "provider": spec.name,
                "method": spec.install_method,
                "package": spec.install_package or spec.name,
                "executable": executable,
            },
        )
        self.store.write_state(state)
        return {
            "provider": spec.name,
            "installed": True,
            "already_present": False,
            "method": spec.install_method,
            "package": spec.install_package or spec.name,
            "executable": executable,
            "output": output,
        }

    def ensure_provider_runtime(self, provider: str) -> dict[str, Any]:
        try:
            executable = self._resolve_provider_executable(provider)
        except SetupError:
            return self.install_provider_runtime(provider)
        config = self.store.read_config()
        self._mark_runtime_installed(config, provider)
        config["updated_at"] = now_iso()
        self.store.write_config(config)
        return {
            "provider": str(provider).strip().lower(),
            "installed": False,
            "already_present": True,
            "method": get_provider(provider).install_method,
            "package": get_provider(provider).install_package or str(provider).strip().lower(),
            "executable": executable,
        }

    def install_addon(self, addon: str) -> dict[str, Any]:
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)

        if isinstance(spec, ServiceAddonSpec):
            return self._install_service_addon(spec)

        executable = self._resolve_executable_in_service_env(spec.executable)
        if executable:
            return {
                "addon": spec.name,
                "installed": False,
                "already_present": True,
                "method": spec.install_method,
                "package": spec.install_package or spec.executable,
                "executable": executable,
            }

        method_used = spec.install_method
        if spec.install_method == "npm":
            npm = self._resolve_executable_in_service_env("npm")
            pnpm = self._resolve_executable_in_service_env("pnpm")
            if npm:
                cmd = [npm, "install", "-g", spec.install_package or spec.executable]
            elif pnpm:
                cmd = [pnpm, "add", "-g", spec.install_package or spec.executable]
                method_used = "pnpm"
            else:
                raise SetupError("npm or pnpm is required to install addons but neither was found in PATH.")
        elif spec.install_method == "pnpm":
            pnpm = self._resolve_executable_in_service_env("pnpm")
            if not pnpm:
                raise SetupError("pnpm is required to install addons but was not found in PATH.")
            cmd = [pnpm, "add", "-g", spec.install_package or spec.executable]
        else:
            raise SetupError(f"addon '{spec.name}' does not define an install method")

        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=self._service_env(""))
        output = "\n".join(part for part in [result.stdout, result.stderr] if str(part).strip()).strip()
        if result.returncode != 0:
            raise SetupError(
                f"failed to install addon {spec.name} via {spec.install_method}: {output or f'exit {result.returncode}'}"
            )

        executable = self._resolve_executable_in_service_env(spec.executable)
        if not executable:
            raise SetupError(f"addon install finished but '{spec.executable}' is still not in PATH")

        state = self.store.read_state()
        self._event(
            state,
            "addons.installed",
            f"Installed addon {spec.name}",
            {
                "addon": spec.name,
                "method": method_used,
                "package": spec.install_package or spec.executable,
                "executable": executable,
            },
        )
        self.store.write_state(state)
        return {
            "addon": spec.name,
            "installed": True,
            "already_present": False,
            "method": method_used,
            "package": spec.install_package or spec.executable,
            "executable": executable,
            "output": output,
        }

    def ensure_support_tool_installed(self, tool: str) -> dict[str, Any]:
        token = str(tool or "").strip().lower()
        if token != "gcloud":
            raise ValueError("support tool must be one of: gcloud")
        executable = self._resolve_executable_in_service_env("gcloud")
        if executable:
            return {
                "tool": token,
                "installed": False,
                "already_present": True,
                "method": "archive",
                "scope": self._shared_toolchain_scope(),
                "executable": executable,
            }
        return self.install_support_tool(token)

    def install_support_tool(self, tool: str) -> dict[str, Any]:
        token = str(tool or "").strip().lower()
        if token != "gcloud":
            raise ValueError("support tool must be one of: gcloud")
        executable = self._resolve_executable_in_service_env("gcloud")
        if executable:
            return {
                "tool": token,
                "installed": False,
                "already_present": True,
                "method": "archive",
                "scope": self._shared_toolchain_scope(),
                "executable": executable,
            }

        root = self._ensure_shared_toolchain_root()
        archive_name = self._gcloud_archive_name()
        url = f"https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/{archive_name}"
        install_dir = root / "google-cloud-sdk"
        if install_dir.exists() or install_dir.is_symlink():
            if install_dir.is_symlink() or install_dir.is_file():
                install_dir.unlink(missing_ok=True)
            else:
                shutil.rmtree(install_dir)

        archive_path = Path(
            tempfile.mkstemp(prefix="clawie-gcloud-", suffix=".tar.gz", dir=str(root))[1]
        )
        try:
            with urllib.request.urlopen(url, timeout=120) as response, archive_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            self._extract_tarball_safe(archive_path, root)
        except Exception as exc:  # noqa: BLE001
            archive_path.unlink(missing_ok=True)
            raise SetupError(f"failed to install gcloud from {url}: {exc}") from exc
        archive_path.unlink(missing_ok=True)
        executable = str(root / "google-cloud-sdk" / "bin" / "gcloud")
        if not Path(executable).exists():
            raise SetupError(f"gcloud install finished but executable was not found at {executable}")
        verify = subprocess.run(
            [executable, "version"],
            capture_output=True,
            text=True,
            check=False,
            env=self._service_env(""),
        )
        if verify.returncode != 0:
            output = "\n".join(
                part for part in [verify.stdout, verify.stderr] if str(part).strip()
            ).strip()
            raise SetupError(f"installed gcloud but version check failed: {output or f'exit {verify.returncode}'}")

        self._relax_path_tree_permissions(root)
        state = self.store.read_state()
        self._event(
            state,
            "toolchain.installed",
            "Installed support tool gcloud",
            {
                "tool": token,
                "method": "archive",
                "scope": self._shared_toolchain_scope(),
                "url": url,
                "executable": executable,
            },
        )
        self.store.write_state(state)
        return {
            "tool": token,
            "installed": True,
            "already_present": False,
            "method": "archive",
            "scope": self._shared_toolchain_scope(),
            "url": url,
            "executable": executable,
        }

    def ensure_addon_installed(self, addon: str) -> dict[str, Any]:
        try:
            spec = get_addon(addon)
        except ValueError:
            raise
        if isinstance(spec, ServiceAddonSpec):
            if check_display_installed(spec.check_executables):
                return {
                    "addon": spec.name,
                    "installed": False,
                    "already_present": True,
                    "method": spec.install_method,
                    "package": ", ".join(spec.apt_packages[:3]) + "...",
                    "executable": spec.check_executables[0] if spec.check_executables else "",
                }
            return self.install_addon(spec.name)
        executable = self._resolve_executable_in_service_env(spec.executable)
        if executable:
            return {
                "addon": spec.name,
                "installed": False,
                "already_present": True,
                "method": spec.install_method,
                "package": spec.install_package or spec.executable,
                "executable": executable,
            }
        return self.install_addon(spec.name)

    def _install_service_addon(self, spec: ServiceAddonSpec) -> dict[str, Any]:
        """Install a service addon (apt packages)."""
        if check_display_installed(spec.check_executables):
            return {
                "addon": spec.name,
                "installed": False,
                "already_present": True,
                "method": spec.install_method,
                "packages": list(spec.apt_packages),
            }
        result = install_display_packages(spec.apt_packages)
        state = self.store.read_state()
        self._event(
            state,
            "addons.installed",
            f"Installed service addon {spec.name}",
            {
                "addon": spec.name,
                "method": spec.install_method,
                "packages": list(spec.apt_packages),
            },
        )
        self.store.write_state(state)
        return {
            "addon": spec.name,
            "installed": True,
            "already_present": False,
            "method": spec.install_method,
            "packages": list(spec.apt_packages),
            "output": result.get("output", ""),
        }

    # ── Display addon methods ───────────────────────────────────────

    def _collect_used_display_numbers(self) -> list[int]:
        """Gather display numbers already allocated to agents."""
        state = self.store.read_state()
        agents = state.get("agents", state.get("users", {}))
        used: list[int] = []
        for agent in agents.values():
            addons = agent.get("addons", {})
            display_data = addons.get("display", {})
            if isinstance(display_data, dict) and display_data.get("display_number"):
                used.append(int(display_data["display_number"]))
        return used

    def enable_agent_display(
        self,
        agent_id: str,
        *,
        resolution: str | None = None,
    ) -> dict[str, Any]:
        """Allocate a display, write systemd units, start services for an agent."""
        self._require_setup()
        token = str(agent_id).strip()
        if not token:
            raise ValueError("agent_id is required")
        spec = get_addon("display")
        if not isinstance(spec, ServiceAddonSpec):
            raise SetupError("display addon spec is misconfigured")
        self.ensure_addon_installed("display")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            raise SetupError(f"agent '{token}' has no linux_user assigned")

        addons = agent.setdefault("addons", {})
        existing_display = addons.get("display", {})
        if isinstance(existing_display, dict) and existing_display.get("enabled") and existing_display.get("display_number"):
            return {
                "addon": "display",
                "agent_id": token,
                "already_enabled": True,
                "display_number": existing_display["display_number"],
                "resolution": existing_display.get("resolution", spec.default_resolution),
                "vnc_port": existing_display.get("vnc_port"),
                "novnc_port": existing_display.get("novnc_port"),
            }

        res = resolution or spec.default_resolution
        used = self._collect_used_display_numbers()
        display_num = allocate_display_number(used, offset=spec.default_display_offset)
        vnc_port = vnc_port_for_display(display_num, spec.default_vnc_port_offset)
        novnc_port = novnc_port_for_display(display_num, spec.default_novnc_port_offset)

        unit_paths = write_systemd_units(
            display_num=display_num,
            linux_user=linux_user,
            resolution=res,
            vnc_port=vnc_port,
            novnc_port=novnc_port,
        )
        services = start_display_services(display_num)

        # ── Inject display awareness into agent's TOOLS.md and shell profile ──
        home = self._agent_linux_home(agent)
        provider = str(info.get("provider", "")).strip().lower()
        if home and provider:
            self._apply_addon_agent_integration(
                "display",
                provider=provider,
                home=home,
                linux_user=linux_user,
                context={
                    "display_number": str(display_num),
                    "resolution": res,
                    "vnc_port": str(vnc_port),
                    "novnc_port": str(novnc_port),
                },
            )

        addon_state: dict[str, Any] = {
            "enabled": True,
            "display_number": display_num,
            "resolution": res,
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "services": services,
            "credential_mode": "none",
            "last_applied_at": now_iso(),
            "last_applied_paths": unit_paths,
            "last_revoked_at": "",
            "last_source": "",
        }
        addons["display"] = addon_state
        info["last_sync"] = now_iso()
        self._event(
            state,
            "addons.display.enabled",
            f"Enabled display :{display_num} for {token}",
            {
                "agent_id": token,
                "display_number": display_num,
                "vnc_port": vnc_port,
                "novnc_port": novnc_port,
                "resolution": res,
                "services": services,
            },
        )
        self.store.write_state(state)
        return {
            "addon": "display",
            "agent_id": token,
            "already_enabled": False,
            "display_number": display_num,
            "resolution": res,
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "services": services,
            "unit_paths": unit_paths,
        }

    def disable_agent_display(self, agent_id: str) -> dict[str, Any]:
        """Stop display services and remove systemd units for an agent."""
        self._require_setup()
        token = str(agent_id).strip()
        if not token:
            raise ValueError("agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        addons = agent.setdefault("addons", {})
        display_data = addons.get("display", {})
        if not isinstance(display_data, dict) or not display_data.get("display_number"):
            raise SetupError(f"agent '{token}' does not have a display enabled")

        display_num = int(display_data["display_number"])
        stopped = stop_display_services(display_num)
        removed = remove_systemd_units(display_num)

        # ── Remove display awareness from agent's TOOLS.md and shell profile ──
        home = self._agent_linux_home(agent)
        provider = str(info.get("provider", "")).strip().lower()
        linux_user = str(info.get("linux_user", "")).strip()
        if home and provider:
            self._remove_addon_agent_integration("display", provider=provider, home=home, linux_user=linux_user)

        display_data["enabled"] = False
        display_data["last_revoked_at"] = now_iso()
        display_data["services"] = []
        display_data["last_applied_paths"] = []
        addons["display"] = display_data
        info["last_sync"] = now_iso()
        self._event(
            state,
            "addons.display.disabled",
            f"Disabled display :{display_num} for {token}",
            {
                "agent_id": token,
                "display_number": display_num,
                "stopped": stopped,
                "removed": removed,
            },
        )
        self.store.write_state(state)
        return {
            "addon": "display",
            "agent_id": token,
            "display_number": display_num,
            "stopped": stopped,
            "removed_units": removed,
        }

    # ── Generic addon tools / env injection helpers ─────────────────

    def _apply_addon_agent_integration(
        self,
        addon_name: str,
        provider: str,
        home: Path,
        linux_user: str,
        context: dict[str, str],
    ) -> None:
        """Inject the addon's TOOLS.md snippet and env exports into the agent's home."""
        spec = get_addon(addon_name)
        # ── TOOLS.md snippet ──
        if spec.tools_snippet:
            rendered_snippet = spec.tools_snippet.format_map(context)
            tools_path = self._core_prompt_path(provider, home, "TOOLS.md")
            current = ""
            if tools_path.exists():
                current = tools_path.read_text(encoding="utf-8")
            updated = inject_addon_tools_snippet(current, addon_name, rendered_snippet)
            self._write_core_prompt_file(provider, home, "TOOLS.md", updated)
            if linux_user and os.geteuid() == 0:
                subprocess.run(["chown", f"{linux_user}:{linux_user}", str(tools_path)], check=False)
        # ── Shell env exports ──
        if spec.env_exports:
            exports = {var: val.format_map(context) for var, val in spec.env_exports}
            block = render_addon_env_block(addon_name, exports)
            for rel in (".bashrc", ".profile"):
                path = home / rel
                current = ""
                if path.exists():
                    current = path.read_text(encoding="utf-8")
                updated = inject_addon_env_block(current, addon_name, block)
                path.write_text(updated, encoding="utf-8")
                if linux_user and os.geteuid() == 0:
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(path)], check=False)

    def _remove_addon_agent_integration(
        self,
        addon_name: str,
        provider: str,
        home: Path,
        linux_user: str,
    ) -> None:
        """Remove the addon's TOOLS.md snippet and env exports from the agent's home."""
        spec = get_addon(addon_name)
        # ── TOOLS.md snippet ──
        if spec.tools_snippet:
            tools_path = self._core_prompt_path(provider, home, "TOOLS.md")
            if tools_path.exists():
                current = tools_path.read_text(encoding="utf-8")
                updated = remove_addon_tools_snippet(current, addon_name)
                self._write_core_prompt_file(provider, home, "TOOLS.md", updated)
                if linux_user and os.geteuid() == 0:
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(tools_path)], check=False)
        # ── Shell env exports ──
        if spec.env_exports:
            for rel in (".bashrc", ".profile"):
                path = home / rel
                if not path.exists():
                    continue
                current = path.read_text(encoding="utf-8")
                updated = remove_addon_env_block(current, addon_name)
                path.write_text(updated, encoding="utf-8")
                if linux_user and os.geteuid() == 0:
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(path)], check=False)

    def agent_display_status(self, agent_id: str) -> dict[str, Any]:
        """Return display status for an agent."""
        token = str(agent_id).strip()
        if not token:
            raise ValueError("agent_id is required")
        state = self.store.read_state()
        agents = state.get("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        addons = agent.get("addons", {})
        display_data = addons.get("display", {})
        if not isinstance(display_data, dict) or not display_data.get("display_number"):
            return {
                "addon": "display",
                "agent_id": token,
                "enabled": False,
            }
        display_num = int(display_data["display_number"])
        vnc_port = int(display_data.get("vnc_port", vnc_port_for_display(display_num)))
        novnc_port = int(display_data.get("novnc_port", novnc_port_for_display(display_num)))
        stack_status = _display_stack_status(display_num, vnc_port, novnc_port)
        return {
            "addon": "display",
            "agent_id": token,
            "enabled": bool(display_data.get("enabled", False)),
            "display_number": display_num,
            "resolution": str(display_data.get("resolution", "")),
            "vnc_port": vnc_port,
            "novnc_port": novnc_port,
            "status": stack_status.get("status", "unknown"),
            "services": stack_status.get("services", {}),
        }

    def create_agent(
        self,
        agent_id: str,
        display_name: str | None,
        template: str,
        clone_from: str | None,
        channel_strategy: str,
        channels: list[dict[str, str]] | None,
        agent_version: str,
        provider: str | None = None,
        core_prompts: dict[str, str] | None = None,
        plugin_overrides: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        self._require_setup()

        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id is required")

        if channel_strategy not in {"new", "migrate"}:
            raise ValueError("channel_strategy must be one of: new, migrate")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        state["users"] = agents
        if agent_id in agents:
            raise AgentExistsError(f"agent already exists: {agent_id}")

        base_channels: list[dict[str, str]] = []
        source_template = template
        source_agent_defaults: dict[str, Any] = {}
        source_addons: dict[str, Any] = {}

        if clone_from:
            source = agents.get(clone_from)
            if not source:
                raise AgentNotFoundError(f"clone source agent not found: {clone_from}")
            base_channels = copy.deepcopy(source.get("channels", []))
            source_template = source.get("source_template") or template
            source_agent_defaults = copy.deepcopy(source.get("agent", {}))
            source_addons = copy.deepcopy(source.get("addons", {}))
        else:
            template_data = state["templates"].get(template)
            if not template_data:
                raise ValueError(f"template not found: {template}")
            base_channels = copy.deepcopy(template_data.get("channels", []))
            source_agent_defaults = copy.deepcopy(template_data.get("agent_defaults", {}))

        if channels:
            base_channels = copy.deepcopy(channels)

        if channel_strategy == "new":
            final_channels = self._mint_channels(agent_id, base_channels)
        else:
            if not clone_from and not channels:
                raise ValueError(
                    "channel strategy 'migrate' requires --clone-from or explicit channels"
                )
            final_channels = copy.deepcopy(base_channels)
            for channel in final_channels:
                channel["migrated_from"] = clone_from or "local-source"
        for channel in final_channels:
            channel["enabled"] = bool(channel.get("enabled", True))
        transfer_from_clone = bool(clone_from and channel_strategy == "migrate")
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=agent_id,
            channels=final_channels,
            allow_owners={str(clone_from)} if transfer_from_clone else set(),
        )

        config = self.store.read_config()
        default_provider = str(config.get("provider", "openclaw")).strip().lower() or "openclaw"
        if provider:
            provider_spec = get_provider(provider)
        elif clone_from:
            source = agents.get(clone_from, {})
            source_provider = str(source.get("agent", {}).get("provider", "")).strip().lower()
            provider_spec = get_provider(source_provider or default_provider)
        else:
            provider_spec = get_provider(default_provider)

        provider_auth = self._preferred_agent_provider_auth(
            provider_spec.name,
            agent=None,
            current_auth_mode="",
            allow_defaults=True,
        )

        raw_plugins = source_agent_defaults.get("plugins", self._default_plugins_for_provider(provider_spec.name))
        if not isinstance(raw_plugins, dict):
            raw_plugins = self._default_plugins_for_provider(provider_spec.name)
        plugins = self._normalize_plugins(raw_plugins)
        if plugin_overrides:
            for key, value in plugin_overrides.items():
                plugins[str(key).strip().lower()] = bool(value)
        runtime = provider_spec.runtime
        if clone_from:
            runtime = str(source_agent_defaults.get("runtime", provider_spec.runtime)).strip() or provider_spec.runtime

        display = display_name.strip() if display_name else agent_id
        agent = {
            "status": "ready",
            "version": agent_version,
            "last_sync": now_iso(),
            "runtime": runtime,
            "provider": provider_spec.name,
            "auth_mode": provider_auth.get("auth_mode", provider_spec.default_auth_mode),
            "autostart": bool(source_agent_defaults.get("autostart", True)),
            "heartbeat_seconds": int(source_agent_defaults.get("heartbeat_seconds", 30)),
            "pid": int(source_agent_defaults.get("pid", 0)),
            "plugins": plugins,
            "model_tier": "balanced",
        }
        if clone_from and not core_prompts:
            core_prompts = copy.deepcopy(agents.get(clone_from, {}).get("core_prompts", {}))
        normalized_prompts = self._normalize_core_prompts(provider_spec.name, core_prompts or {})
        self._seed_core_prompt_defaults(
            provider_spec.name,
            normalized_prompts,
            agent_id=agent_id,
            display_name=display,
        )
        self._seed_delegation_skill(normalized_prompts, plugins)

        agent_state = {
            "agent_id": agent_id,
            "display_name": display,
            "created_at": now_iso(),
            "source_template": source_template,
            "clone_from": clone_from,
            "channel_strategy": channel_strategy,
            "channels": final_channels,
            "core_prompts": normalized_prompts,
            "credential_sync": self._normalize_credential_sync_state({}, default_when_missing=True),
            "addons": self._normalize_agent_addons(source_addons),
            "agent": agent,
        }
        agents[agent_id] = agent_state
        moved_from_clone = 0
        if transfer_from_clone and clone_from:
            source = agents.get(clone_from)
            if source:
                moved_from_clone = self._remove_channel_keys_from_agent(
                    source=source,
                    keys=self._channel_keys(final_channels),
                )
                if moved_from_clone:
                    source.setdefault("agent", {})["last_sync"] = now_iso()

        self._event(
            state,
            "agents.created",
            f"Provisioned agent {agent_id}",
            {
                "agent_id": agent_id,
                "channel_strategy": channel_strategy,
                "channel_count": len(final_channels),
                "clone_from": clone_from or "",
                "provider": provider_spec.name,
                "moved_from_clone": moved_from_clone,
            },
        )
        self.store.write_state(state)
        return agent_state

    def list_agents(self) -> list[dict[str, Any]]:
        self._refresh_managed_agent_provider_alignments()
        state = self.store.read_state()
        agents = list(state.setdefault("agents", state.get("users", {})).values())
        for agent in agents:
            self._hydrate_agent_controls(agent)
        return sorted(
            agents,
            key=lambda row: (row.get("created_at", ""), row.get("agent_id", row.get("user_id", ""))),
        )

    def configured_provider_names(self) -> list[str]:
        config = self.store.read_config()
        ordered: list[str] = []
        seen: set[str] = set()
        for item in [config.get("provider", "")] + list(self._normalized_provider_credentials(config).keys()):
            token = str(item or "").strip().lower()
            if not token or token in seen:
                continue
            try:
                get_provider(token)
            except ValueError:
                continue
            seen.add(token)
            ordered.append(token)
        return ordered

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        return agent

    @staticmethod
    def _current_linux_user() -> str:
        try:
            return str(pwd.getpwuid(os.geteuid()).pw_name)
        except KeyError:
            return ""

    def _can_manage_linux_user(self, linux_user: str) -> bool:
        token = str(linux_user).strip()
        if not token:
            return True
        return os.geteuid() == 0 or token == self._current_linux_user()

    def _require_linux_user_access(self, linux_user: str, purpose: str) -> None:
        if self._can_manage_linux_user(linux_user):
            return
        raise SetupError(
            f"{purpose} requires root when agent linux_user differs from current user. Re-run with sudo/root."
        )

    def set_agent_provider(self, agent_id: str, provider: str) -> dict[str, Any]:
        return self.switch_agent_provider(agent_id, provider)["agent"]

    def switch_agent_provider(self, agent_id: str, provider: str) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token:
            raise ValueError("agent_id is required")
        if token.startswith("@local:"):
            raise ValueError("provider switching is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(token)

        target_provider = str(provider).strip().lower()
        if not target_provider:
            raise ValueError("provider is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)

        info = agent.setdefault("agent", {})
        current_provider = str(info.get("provider", "")).strip().lower()
        target_spec = get_provider(target_provider)
        changed = current_provider != target_spec.name
        linux_user = str(info.get("linux_user", "")).strip()
        stop_result: dict[str, Any] = {}
        stopped_results: list[dict[str, Any]] = []
        start_result: dict[str, Any] = {}
        reconnected_channels: list[dict[str, str]] = []
        old_running = False
        target_running_before = False
        old_service_state = {
            "service_status": str(info.get("service_status", "unknown")),
            "service_mode": str(info.get("service_mode", "unknown")),
            "fallback_pid": int(info.get("fallback_pid", 0) or 0),
        }
        new_service_state = {"service_status": "unknown", "service_mode": "unknown", "fallback_pid": 0}

        try:
            if linux_user:
                # Permission is the most fundamental precondition: check it before
                # auth/runtime preparation so callers get an actionable error
                # instead of a misleading auth or install failure.
                self._require_linux_user_access(linux_user, "provider switching")
            auth_prepare = self._prepare_linked_auth_for_provider_switch(provider=target_spec.name, agent=agent)
            provider_auth = self._preferred_agent_provider_auth(
                target_spec.name,
                agent=agent,
                # The stored auth mode belongs to the current provider; it only
                # carries over when reconciling the same provider. A switch to a
                # different provider must derive its own auth mode.
                current_auth_mode=str(info.get("auth_mode", "")) if not changed else "",
                allow_defaults=True,
            )
            home = self._agent_linux_home(agent)
            prompts = self._normalize_core_prompts(target_spec.name, agent.get("core_prompts", {}))
            effective_channels = self._effective_agent_channels(agent) if linux_user else []
            live_channel_payloads = self._discover_live_channel_payloads(agent) if linux_user else {}

            if not changed and not linux_user:
                auth = self.agent_auth_status(token)
                return {
                    "agent": agent,
                    "changed": False,
                    "from_provider": current_provider,
                    "to_provider": target_spec.name,
                    "service": {},
                    "stopped_service": {},
                    "stopped_services": [],
                    "reconnected_channels": [],
                    "auth": auth,
                    "auth_prepare": auth_prepare,
                }

            if linux_user:
                self.ensure_provider_runtime(target_spec.name)
                target_running_before = self._managed_provider_is_running(
                    provider=target_spec.name,
                    linux_user=linux_user,
                    agent_info=new_service_state,
                )
                if changed and current_provider:
                    self._resolve_provider_executable(current_provider)
                if home:
                    self._write_prompt_files_for_home(target_spec.name, home, prompts, linux_user)
            if linux_user and changed and current_provider:
                old_running = self._managed_provider_is_running(
                    provider=current_provider,
                    linux_user=linux_user,
                    agent_info=old_service_state,
                )
                if old_running or int(old_service_state.get("fallback_pid", 0) or 0) > 0:
                    stop_result = self._run_managed_provider_service_action(
                        provider=current_provider,
                        action="stop",
                        linux_user=linux_user,
                        agent_info=old_service_state,
                    )

            if linux_user:
                self._prepare_agent_provider_home(
                    provider=target_spec.name,
                    agent=agent,
                    linux_user=linux_user,
                    home=home,
                    channels=effective_channels,
                    live_payloads=live_channel_payloads,
                )
                start_result = self._run_managed_provider_service_action(
                    provider=target_spec.name,
                    action="restart" if target_running_before else "start",
                    linux_user=linux_user,
                    agent_info=new_service_state,
                )
                reconnected_channels = self._reconnect_agent_channels(
                    provider=target_spec.name,
                    linux_user=linux_user,
                    channels=effective_channels,
                )
                for other_provider in provider_names():
                    if other_provider == target_spec.name:
                        continue
                    try:
                        self._resolve_provider_executable(other_provider)
                    except SetupError:
                        continue
                    other_state = {"service_status": "unknown", "service_mode": "unknown", "fallback_pid": 0}
                    if self._managed_provider_is_running(
                        provider=other_provider,
                        linux_user=linux_user,
                        agent_info=other_state,
                    ):
                        stopped = self._run_managed_provider_service_action(
                            provider=other_provider,
                            action="stop",
                            linux_user=linux_user,
                            agent_info=other_state,
                        )
                        stopped_results.append(stopped)
                live_after_switch = self._live_provider_names_for_user(linux_user)
                if target_spec.name not in live_after_switch:
                    raise SetupError(
                        f"provider switch to {target_spec.name} did not produce a live {target_spec.name} runtime"
                    )
                other_live = [item for item in live_after_switch if item != target_spec.name]
                if other_live:
                    raise SetupError(
                        f"provider switch to {target_spec.name} left other runtimes active: {', '.join(other_live)}"
                    )
                self._assert_provider_postflight_ready(
                    provider=target_spec.name,
                    linux_user=linux_user,
                    home=home,
                    auth_mode=str(provider_auth.get("auth_mode", target_spec.default_auth_mode)),
                )
        except Exception as exc:
            self._set_agent_provider_issue(
                agent,
                status="error",
                kind="switch_failed",
                issue=f"provider switch to {target_spec.name} failed: {exc}",
                remediation=self._provider_switch_remediation(
                    agent_id=token,
                    target_provider=target_spec.name,
                    linux_user=linux_user,
                    error=str(exc),
                ),
                requested_provider=target_spec.name,
            )
            self._event(
                state,
                "agents.provider_switch_failed",
                f"Provider switch failed for {token}",
                {
                    "agent_id": token,
                    "from_provider": current_provider,
                    "to_provider": target_spec.name,
                    "linux_user": linux_user,
                    "error": str(exc),
                },
            )
            self.store.write_state(state)
            if changed and linux_user and str(start_result.get("service_status", "")) == "running":
                try:
                    self._run_managed_provider_service_action(
                        provider=target_spec.name,
                        action="stop",
                        linux_user=linux_user,
                        agent_info=new_service_state,
                    )
                except Exception:
                    pass
            if changed and linux_user and current_provider and old_running:
                try:
                    self._run_managed_provider_service_action(
                        provider=current_provider,
                        action="start",
                        linux_user=linux_user,
                        agent_info=old_service_state,
                    )
                except Exception:
                    pass
            raise

        info["provider"] = target_spec.name
        info["runtime"] = target_spec.runtime
        info["auth_mode"] = str(provider_auth.get("auth_mode", target_spec.default_auth_mode))
        info["service_status"] = str(start_result.get("service_status", "unknown")) if linux_user else "unknown"
        info["service_mode"] = str(start_result.get("service_mode", "unknown")) if linux_user else "unknown"
        info["pid"] = 0
        if "fallback_pid" in info or int(start_result.get("fallback_pid", 0) or 0) > 0:
            info["fallback_pid"] = int(start_result.get("fallback_pid", 0) or 0)
        info["last_sync"] = now_iso()
        if effective_channels:
            self._persist_effective_agent_channels(agent, effective_channels)
        agent["core_prompts"] = prompts
        self._clear_agent_provider_issue(agent)
        if not stop_result and stopped_results:
            stop_result = stopped_results[-1]
        self._event(
            state,
            "agents.provider_changed" if changed else "agents.provider_reconciled",
            f"Changed provider for {token}" if changed else f"Reconciled provider runtime for {token}",
            {
                "agent_id": token,
                "from_provider": current_provider,
                "to_provider": target_spec.name,
                "linux_user": linux_user,
                "service_status": str(start_result.get("service_status", "unknown")),
                "service_mode": str(start_result.get("service_mode", "unknown")),
                "reconnected_channels": len(reconnected_channels),
                "stopped_provider_count": len(stopped_results or ([stop_result] if stop_result else [])),
            },
        )
        self.store.write_state(state)
        auth = self.agent_auth_status(token)
        return {
            "agent": agent,
            "changed": changed,
            "from_provider": current_provider,
            "to_provider": target_spec.name,
            "service": start_result,
            "stopped_service": stop_result,
            "stopped_services": stopped_results,
            "reconnected_channels": reconnected_channels,
            "auth": auth,
            "auth_prepare": auth_prepare,
        }

    def get_agent_credential_sync(self, agent_id: str) -> dict[str, Any]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        sync = self._normalize_credential_sync_state(payload.get("credential_sync"), default_when_missing=True)
        selected = set(sync.get("bundles", []))
        bundles: list[dict[str, Any]] = []
        for option in self.credential_bundle_options():
            bid = str(option.get("id", ""))
            bundles.append(
                {
                    "id": bid,
                    "label": str(option.get("label", "")),
                    "default": bool(option.get("default", False)),
                    "selected": bid in selected,
                }
            )
        return {
            "agent_id": str(payload.get("agent_id", payload.get("user_id", ""))),
            "linux_user": str(info.get("linux_user", "")),
            "local_user": bool(info.get("local_user", False)),
            "selected_bundles": list(sync.get("bundles", [])),
            "shared_provider_auth": bool(sync.get("shared_provider_auth", False)),
            "last_synced_at": str(sync.get("last_synced_at", "")),
            "last_source_home": str(sync.get("last_source_home", "")),
            "last_synced_paths": list(sync.get("last_synced_paths", [])),
            "last_revoked_at": str(sync.get("last_revoked_at", "")),
            "last_revoked_paths": list(sync.get("last_revoked_paths", [])),
            "bundles": bundles,
        }

    def set_agent_credential_bundles(
        self,
        agent_id: str,
        bundles: list[str],
        *,
        include_defaults: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            raise ValueError("credential bundle policy is only supported for managed agents")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        selected = self._ordered_credential_bundles(
            self._normalize_credential_bundles(bundles, include_defaults=include_defaults)
        )
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        sync["bundles"] = selected
        sync["last_synced_paths"] = []
        sync["last_revoked_paths"] = []
        agent["credential_sync"] = sync
        agent.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "agents.credentials_policy_updated",
            f"Updated credential policy for {token}",
            {"agent_id": token, "bundles": selected},
        )
        self.store.write_state(state)
        return agent

    def toggle_agent_credential_bundle(self, agent_id: str, bundle: str) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            raise ValueError("credential bundle policy is only supported for managed agents")
        selected_bundle = self._normalize_credential_bundles([bundle], include_defaults=False)
        if not selected_bundle:
            raise ValueError("bundle is required")
        bundle_id = selected_bundle[0]
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        current = list(sync.get("bundles", []))
        if bundle_id in current:
            current = [item for item in current if item != bundle_id]
        else:
            current.append(bundle_id)
        sync["bundles"] = self._ordered_credential_bundles(current)
        sync["last_synced_paths"] = []
        sync["last_revoked_paths"] = []
        agent["credential_sync"] = sync
        agent.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "agents.credentials_policy_toggled",
            f"Toggled credential bundle {bundle_id} for {token}",
            {
                "agent_id": token,
                "bundle": bundle_id,
                "enabled": bundle_id in set(sync.get("bundles", [])),
                "bundles": list(sync.get("bundles", [])),
            },
        )
        self.store.write_state(state)
        return agent

    def sync_agent_credentials(
        self,
        agent_id: str,
        *,
        source_home: str | Path | None = None,
        bundles: list[str] | None = None,
        include_defaults: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            raise ValueError("credential sync is only supported for managed agents")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        self._assert_linux_user_manageable(linux_user, "credential sync")
        target_home = self._agent_linux_home(agent)
        if not target_home:
            raise SetupError(f"agent '{token}' has no linux_user home to sync credentials to")
        if not target_home.exists():
            raise SetupError(f"agent '{token}' home does not exist: {target_home}")
        if source_home:
            src_home = Path(source_home).expanduser()
        else:
            src_home = self._default_source_home()
        if not src_home.exists():
            raise FileNotFoundError(f"source home not found: {src_home}")

        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        if bundles is None:
            selected = self._ordered_credential_bundles(list(sync.get("bundles", [])))
        else:
            selected = self._ordered_credential_bundles(
                self._normalize_credential_bundles(bundles, include_defaults=include_defaults)
            )
        copied = self._sync_selected_credential_bundles(
            source_home=src_home,
            target_home=target_home,
            username=linux_user,
            requested_provider=str(info.get("provider", "")),
            bundles=selected,
        )
        sync["bundles"] = selected
        sync["last_synced_at"] = now_iso()
        sync["last_source_home"] = str(src_home)
        sync["last_synced_paths"] = copied
        sync["last_revoked_paths"] = []
        sync["shared_provider_auth"] = "provider-auth" in set(selected)
        agent["credential_sync"] = sync
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.credentials_synced",
            f"Synced credentials for {token}",
            {
                "agent_id": token,
                "linux_user": linux_user,
                "source_home": str(src_home),
                "bundles": selected,
                "copied_paths": copied,
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": token,
            "linux_user": linux_user,
            "source_home": str(src_home),
            "bundles": selected,
            "copied_paths": copied,
        }

    def revoke_agent_credentials(
        self,
        agent_id: str,
        *,
        bundles: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            raise ValueError("credential revoke is only supported for managed agents")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        self._assert_linux_user_manageable(linux_user, "credential revoke")
        target_home = self._agent_linux_home(agent)
        if not target_home:
            raise SetupError(f"agent '{token}' has no linux_user home to revoke credentials from")
        if not target_home.exists():
            raise SetupError(f"agent '{token}' home does not exist: {target_home}")
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        selected = self._ordered_credential_bundles(list(sync.get("bundles", [])))
        if bundles is None:
            revoked_bundles = selected
        else:
            revoked_bundles = self._ordered_credential_bundles(
                self._normalize_credential_bundles(bundles, include_defaults=False)
            )
        removed = self._revoke_selected_credential_bundles(target_home=target_home, bundles=revoked_bundles)
        remaining = [item for item in selected if item not in set(revoked_bundles)]
        sync["bundles"] = self._ordered_credential_bundles(remaining)
        sync["last_revoked_at"] = now_iso()
        sync["last_revoked_paths"] = removed
        sync["last_synced_paths"] = []
        if "provider-auth" in set(revoked_bundles):
            sync["shared_provider_auth"] = False
        agent["credential_sync"] = sync
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.credentials_revoked",
            f"Revoked credentials for {token}",
            {
                "agent_id": token,
                "linux_user": linux_user,
                "bundles": revoked_bundles,
                "removed_paths": removed,
                "remaining_bundles": list(sync.get("bundles", [])),
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": token,
            "linux_user": linux_user,
            "bundles": revoked_bundles,
            "remaining_bundles": list(sync.get("bundles", [])),
            "removed_paths": removed,
        }

    def toggle_agent_channel(self, agent_id: str, channel_index: int) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        channels = agent.get("channels", [])
        if not isinstance(channels, list) or channel_index < 0 or channel_index >= len(channels):
            raise ValueError("invalid channel selection")

        selected = channels[channel_index]
        selected["enabled"] = not bool(selected.get("enabled", True))
        agent_info = agent.setdefault("agent", {})
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.channel_toggled",
            f"Toggled channel {selected.get('name', '')} for {agent_id}",
            {
                "agent_id": agent_id,
                "channel_name": str(selected.get("name", "")),
                "enabled": bool(selected.get("enabled", True)),
            },
        )
        self.store.write_state(state)
        return agent

    def toggle_agent_plugin(self, agent_id: str, plugin: str) -> dict[str, Any]:
        self._require_setup()
        plugin_name = str(plugin).strip().lower()
        if not plugin_name:
            raise ValueError("plugin is required")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        plugins = agent_info.setdefault("plugins", {})
        current = bool(plugins.get(plugin_name, True))
        plugins[plugin_name] = not current
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.plugin_toggled",
            f"Toggled plugin {plugin_name} for {agent_id}",
            {
                "agent_id": agent_id,
                "plugin": plugin_name,
                "enabled": bool(plugins.get(plugin_name, False)),
            },
        )
        self.store.write_state(state)
        return agent

    def toggle_agent_autostart(self, agent_id: str) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        agent_info["autostart"] = not bool(agent_info.get("autostart", True))
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.autostart_toggled",
            f"Toggled autostart for {agent_id}",
            {
                "agent_id": agent_id,
                "autostart": bool(agent_info.get("autostart", True)),
            },
        )
        self.store.write_state(state)
        return agent

    def _run_managed_provider_service_action(
        self,
        *,
        provider: str,
        action: str,
        linux_user: str,
        agent_info: dict[str, Any],
    ) -> dict[str, Any]:
        command = str(action).strip().lower()
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")

        if self._provider_uses_generated_user_unit(provider):
            generated = self._run_generated_user_service_action(
                provider=provider,
                action=command,
                linux_user=linux_user,
                agent_info=agent_info,
            )
            if generated is not None:
                if command == "status":
                    return generated
                desired_running = command in {"start", "restart"}
                observed = self._wait_for_managed_provider_state(
                    provider=provider,
                    linux_user=linux_user,
                    agent_info=agent_info,
                    should_be_running=desired_running,
                    timeout_seconds=5.0,
                )
                if not desired_running and observed == "running":
                    self._force_stop_provider_processes(provider, linux_user)
                    observed = self._wait_for_managed_provider_state(
                        provider=provider,
                        linux_user=linux_user,
                        agent_info=agent_info,
                        should_be_running=False,
                        timeout_seconds=5.0,
                    )
                if desired_running and observed != "running":
                    message = (
                        f"{provider} service {command} reported success but no live {provider} runtime was detected"
                        + (f" for {linux_user}" if linux_user else "")
                    )
                    detail = self._provider_start_failure_detail(provider, linux_user)
                    if detail:
                        message = f"{message}\n{detail}"
                    raise SetupError(message)
                if not desired_running and observed == "running":
                    raise SetupError(
                        f"{provider} service stop reported success but {provider} is still running"
                        + (f" for {linux_user}" if linux_user else "")
                    )
                return {
                    **generated,
                    "service_status": "running" if desired_running else "stopped",
                }

        cmd = self._service_command(provider, command, linux_user)
        env = self._service_env(linux_user)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        output = (result.stdout or result.stderr or "").strip()

        if (
            result.returncode != 0
            and "failed to connect to bus" in output.lower()
            and linux_user
            and os.geteuid() == 0
        ):
            self._bootstrap_user_bus(linux_user)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()

        if result.returncode != 0 and "failed to connect to bus" in output.lower():
            fallback = self._fallback_service_action(
                provider=provider,
                action=command,
                linux_user=linux_user,
                executable=self._command_executable(cmd),
                agent_info=agent_info,
            )
            return {
                "provider": provider,
                "linux_user": linux_user,
                "action": command,
                "service_status": str(fallback.get("service_status", "unknown")),
                "service_mode": "fallback",
                "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
                "output": str(fallback.get("output", "")),
                "command": cmd,
            }

        if result.returncode != 0:
            raise SetupError(
                f"{provider} service {command} failed"
                + (f" for {linux_user}" if linux_user else "")
                + ": "
                + (output or f"exit {result.returncode}")
            )

        if command == "status":
            service_status = self._infer_service_status(output)
        else:
            desired_running = command in {"start", "restart"}
            observed = self._wait_for_managed_provider_state(
                provider=provider,
                linux_user=linux_user,
                agent_info=agent_info,
                should_be_running=desired_running,
            )
            if not desired_running and observed == "running":
                self._force_stop_provider_processes(provider, linux_user)
                observed = self._wait_for_managed_provider_state(
                    provider=provider,
                    linux_user=linux_user,
                    agent_info=agent_info,
                    should_be_running=False,
                )
            if desired_running and observed != "running":
                message = (
                    f"{provider} service {command} reported success but no live {provider} runtime was detected"
                    + (f" for {linux_user}" if linux_user else "")
                )
                detail = self._provider_start_failure_detail(provider, linux_user)
                if detail:
                    message = f"{message}\n{detail}"
                raise SetupError(message)
            if not desired_running and observed == "running":
                raise SetupError(
                    f"{provider} service stop reported success but {provider} is still running"
                    + (f" for {linux_user}" if linux_user else "")
                )
            service_status = "running" if desired_running else "stopped"

        return {
            "provider": provider,
            "linux_user": linux_user,
            "action": command,
            "service_status": service_status,
            "service_mode": "systemd",
            "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
            "output": output,
            "command": cmd,
        }

    def _run_generated_user_service_action(
        self,
        *,
        provider: str,
        action: str,
        linux_user: str,
        agent_info: dict[str, Any],
    ) -> dict[str, Any] | None:
        token = str(linux_user).strip()
        if not token:
            return None
        try:
            self._ensure_generated_user_service_unit(provider, token)
        except Exception:
            return None
        if os.geteuid() == 0:
            self._bootstrap_user_bus(token)
        reloaded = self._run_systemd_user_command(token, ["daemon-reload"])
        if not reloaded.get("ok", False):
            return None

        if action == "status":
            service_status = self._systemd_user_service_status(provider, token)
            if service_status == "unknown":
                return None
            return {
                "provider": provider,
                "linux_user": token,
                "action": action,
                "service_status": service_status,
                "service_mode": "systemd",
                "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
                "output": service_status,
                "command": ["systemctl", "--user", "is-active", f"{provider}.service"],
            }

        if action in {"start", "restart"}:
            self._run_systemd_user_command(token, ["reset-failed", f"{provider}.service"])
            enabled = self._run_systemd_user_command(token, ["enable", f"{provider}.service"])
            if not enabled.get("ok", False):
                return None

        managed = self._systemd_user_service_manage(provider, action, token)
        if not managed.get("ok", False):
            return None
        return {
            "provider": provider,
            "linux_user": token,
            "action": action,
            "service_status": "unknown",
            "service_mode": "systemd",
            "fallback_pid": int(agent_info.get("fallback_pid", 0) or 0),
            "output": str(managed.get("output", "")),
            "command": managed.get("command", []),
        }

    def _wait_for_managed_provider_state(
        self,
        *,
        provider: str,
        linux_user: str,
        agent_info: dict[str, Any],
        should_be_running: bool,
        timeout_seconds: float = 2.0,
        poll_seconds: float = 0.2,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self._run_managed_provider_service_action(
                provider=provider,
                action="status",
                linux_user=linux_user,
                agent_info=agent_info,
            )
            live = self._provider_process_live(provider, linux_user)
            service_running = str(status.get("service_status", "")).strip().lower() == "running"
            if should_be_running and (live or service_running):
                return "running"
            if not should_be_running and not live and not service_running:
                return "stopped"
            if time.monotonic() >= deadline:
                return "running" if (live or service_running) else "stopped"
            time.sleep(poll_seconds)

    def _managed_provider_is_running(
        self,
        *,
        provider: str,
        linux_user: str,
        agent_info: dict[str, Any],
    ) -> bool:
        status = self._run_managed_provider_service_action(
            provider=provider,
            action="status",
            linux_user=linux_user,
            agent_info=agent_info,
        )
        return (
            str(status.get("service_status", "unknown")) == "running"
            or self._provider_process_live_ps_only(provider, linux_user)
            or int(agent_info.get("fallback_pid", 0) or 0) > 0
        )

    def _provider_process_live_ps_only(self, provider: str, linux_user: str) -> bool:
        token = str(linux_user).strip()
        if not token:
            return False
        daemon_map = self._running_provider_daemons_by_user()
        return any(
            str(entry.get("provider", "")).strip().lower() == str(provider).strip().lower()
            for entry in daemon_map.get(token, [])
        )

    def _provider_process_live(self, provider: str, linux_user: str) -> bool:
        token = str(linux_user).strip()
        if not token:
            return False
        if self._provider_process_live_ps_only(provider, token):
            return True
        return self._provider_reports_running(provider, token)

    def _live_provider_names_for_user(self, linux_user: str) -> list[str]:
        token = str(linux_user).strip()
        if not token:
            return []
        daemon_map = self._running_provider_daemons_by_user()
        seen: set[str] = set()
        ordered: list[str] = []
        for entry in daemon_map.get(token, []):
            provider = str(entry.get("provider", "")).strip().lower()
            if not provider or provider in seen:
                continue
            seen.add(provider)
            ordered.append(provider)
        for provider in provider_names():
            if provider in seen:
                continue
            try:
                self._resolve_provider_executable(provider)
            except SetupError:
                continue
            if not self._provider_reports_running(provider, token):
                continue
            seen.add(provider)
            ordered.append(provider)
        return ordered

    def _provider_reports_running(self, provider: str, linux_user: str) -> bool | None:
        token = str(linux_user).strip()
        if not token:
            return False
        if not self._can_manage_linux_user(token):
            return None
        try:
            status = self._run_managed_provider_service_action(
                provider=provider,
                action="status",
                linux_user=token,
                agent_info={"service_status": "unknown", "service_mode": "unknown", "fallback_pid": 0},
            )
        except Exception:
            return None
        return str(status.get("service_status", "")).strip().lower() == "running"

    def _force_stop_provider_processes(self, provider: str, linux_user: str) -> None:
        token = str(linux_user).strip()
        if not token:
            return
        pattern = self._provider_process_pattern(provider)
        quoted = shlex.quote(pattern)
        script = (
            f'if pgrep -u "$(id -u)" -f {quoted} >/dev/null 2>&1; then '
            f'pkill -u "$(id -u)" -f {quoted} >/dev/null 2>&1 || true; '
            'sleep 1; '
            f'if pgrep -u "$(id -u)" -f {quoted} >/dev/null 2>&1; then '
            f'pkill -9 -u "$(id -u)" -f {quoted} >/dev/null 2>&1 || true; '
            "fi; "
            "fi"
        )
        cmd = self._user_shell_command(token, script)
        subprocess.run(cmd, capture_output=True, text=True, check=False)

    def _provider_start_failure_detail(self, provider: str, linux_user: str, lines: int = 40) -> str:
        sections: list[str] = []
        probe = self._provider_start_probe_output(provider=provider, linux_user=linux_user)
        if probe:
            sections.append(probe)
        excerpt = self._provider_daemon_log_excerpt(provider=provider, linux_user=linux_user, lines=lines)
        if excerpt:
            spec = get_provider(provider)
            label = f"Last lines from ~/{spec.state_dir}/daemon.log:\n{excerpt}"
            if label not in sections:
                sections.append(label)
        return "\n\n".join(section for section in sections if section)

    def _provider_daemon_log_excerpt(self, *, provider: str, linux_user: str, lines: int = 40) -> str:
        spec = get_provider(provider)
        script = (
            f'log="$HOME/{spec.state_dir}/daemon.log"; '
            'if [ -f "$log" ]; then '
            f'tail -n {int(lines)} "$log"; '
            "fi"
        )
        try:
            cmd = self._wrap_user_command(["bash", "-lc", script], linux_user, purpose="service log inspection")
        except SetupError:
            return ""
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=self._service_env(linux_user),
        )
        return "\n".join(part.strip() for part in [result.stdout, result.stderr] if str(part).strip()).strip()

    def _provider_start_probe_output(self, *, provider: str, linux_user: str) -> str:
        spec = get_provider(provider)
        try:
            executable = self._resolve_provider_executable(provider)
            cmd = self._wrap_user_command(
                [executable, *spec.background_command],
                linux_user,
                purpose="service startup probe",
            )
        except SetupError:
            return ""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(linux_user),
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            output = self._join_process_output(exc.stdout, exc.stderr)
            prefix = f"{provider} foreground startup probe stayed alive for 5s; process detection may be wrong"
            return f"{prefix}\n{output}".strip()

        output = self._join_process_output(result.stdout, result.stderr)
        if result.returncode == 0 and not output:
            return ""
        if result.returncode == 0:
            return f"{provider} foreground startup probe output:\n{output}".strip()
        if output:
            return f"{provider} foreground startup probe exited {result.returncode}:\n{output}".strip()
        return f"{provider} foreground startup probe exited {result.returncode}"

    def _assert_provider_postflight_ready(
        self,
        *,
        provider: str,
        linux_user: str,
        home: Path | None,
        auth_mode: str,
    ) -> None:
        spec = get_provider(provider)
        command = tuple(str(part).strip() for part in spec.readiness_command if str(part).strip())
        if command:
            executable = self._resolve_provider_executable(provider)
            cmd = self._wrap_user_command([executable, *command], linux_user, purpose="provider readiness probe")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._service_env(linux_user),
                    timeout=10,
                )
            except subprocess.TimeoutExpired as exc:
                output = self._join_process_output(exc.stdout, exc.stderr)
                raise SetupError(
                    f"{provider} readiness probe timed out after startup"
                    + (f" for {linux_user}" if linux_user else "")
                    + (f": {output}" if output else "")
                ) from exc
            if result.returncode != 0:
                output = self._join_process_output(result.stdout, result.stderr)
                raise SetupError(
                    f"{provider} readiness probe failed after startup"
                    + (f" for {linux_user}" if linux_user else "")
                    + ": "
                    + (output or f"exit {result.returncode}")
                )

        if str(auth_mode).strip().lower() == "linked" and home is not None:
            status = self._inspect_provider_auth_state(
                provider=provider,
                auth_mode="linked",
                linux_user=linux_user,
                home=home,
            )
            if not self._auth_status_ready(status):
                detail = str(status.get("detail", "")).strip()
                suffix = f" ({detail})" if detail else ""
                raise SetupError(
                    f"{provider} linked auth is not ready after startup"
                    + (f" for {linux_user}" if linux_user else "")
                    + f": {status.get('auth_status', 'unknown')}{suffix}"
                )

    @staticmethod
    def _join_process_output(stdout: Any, stderr: Any) -> str:
        parts: list[str] = []
        for item in (stdout, stderr):
            if item is None:
                continue
            if isinstance(item, bytes):
                text = item.decode("utf-8", errors="ignore")
            else:
                text = str(item)
            text = text.strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _reconnect_agent_channels(
        self,
        *,
        provider: str,
        linux_user: str,
        channels: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        reconnectable: list[dict[str, str]] = []
        if str(provider).strip().lower() in {"picoclaw", "openclaw"}:
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                if not bool(channel.get("enabled", True)):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name or kind == "cli" or kind != "telegram":
                    continue
                reconnectable.append({"kind": kind, "name": name})
            return reconnectable

        commands: list[list[str]] = []
        seen_commands: set[tuple[str, ...]] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            if not bool(channel.get("enabled", True)):
                continue
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name or kind == "cli":
                continue
            reconnectable.append({"kind": kind, "name": name})
            for cmd in self._channel_connect_commands(provider, kind, name, linux_user):
                key = tuple(cmd)
                if key in seen_commands:
                    continue
                seen_commands.add(key)
                commands.append(cmd)

        env = self._service_env(linux_user)
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                raise SetupError(
                    f"channel reconnect failed for {provider}: {output or f'exit {result.returncode}'}"
                )
        return reconnectable

    def _effective_agent_channels(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        view = self._attach_agent_channel_view(copy.deepcopy(payload))
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for channel in view.get("channels", []):
            if not isinstance(channel, dict):
                continue
            if not bool(channel.get("enabled", True)):
                continue
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "enabled": True,
                    "external_id": str(channel.get("external_id", "")).strip(),
                    "discovered_provider": str(channel.get("discovered_provider", "")).strip().lower(),
                }
            )
        return rows

    def _persist_effective_agent_channels(
        self,
        payload: dict[str, Any],
        channels: list[dict[str, Any]],
    ) -> None:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        existing_rows = payload.get("channels", [])
        existing_map: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing_rows, list):
            for row in existing_rows:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key[0] and key[1]:
                    existing_map[key] = dict(row)

        persisted: list[dict[str, Any]] = []
        for idx, channel in enumerate(channels, start=1):
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            row = dict(existing_map.get(key, {}))
            row["kind"] = kind
            row["name"] = name
            row["enabled"] = bool(channel.get("enabled", True))
            external_id = str(channel.get("external_id", row.get("external_id", ""))).strip()
            if external_id:
                row["external_id"] = external_id
            elif agent_id:
                row["external_id"] = f"{agent_id}:{kind}:{idx}"
            row.pop("channel_source", None)
            row.pop("discovered_provider", None)
            persisted.append(row)
        payload["channels"] = persisted

    def _provider_channel_payloads_for_home(
        self,
        provider: str,
        root: Path,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        name = str(provider).strip().lower()
        if name == "zeroclaw":
            return self._read_zeroclaw_channel_payloads(root)
        if name == "picoclaw":
            return self._read_picoclaw_channel_payloads(root)
        if name == "openclaw":
            return self._read_openclaw_channel_payloads(root)
        return {}

    def _read_zeroclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "config.toml"
        if tomllib is None or not config_path.exists():
            return {}
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        channels_cfg = payload.get("channels_config", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        if bool(channels_cfg.get("cli")):
            rows[("cli", "local")] = {
                "kind": "cli",
                "name": "local",
                "provider": "zeroclaw",
                "settings": {"enabled": True},
            }
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "cli" or not kind or not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue
            name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "zeroclaw",
                "settings": dict(value),
            }
        return rows

    def _read_picoclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "config.json"
        payload = self._read_json_file(config_path)
        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if not kind:
                continue
            if isinstance(value, dict):
                enabled = bool(value.get("enabled", True))
                name = str(value.get("name", kind)).strip().lower() or kind
                settings = dict(value)
            else:
                enabled = bool(value)
                name = kind
                settings = {"enabled": enabled}
            if not enabled:
                continue
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "picoclaw",
                "settings": settings,
            }
        return rows

    def _read_openclaw_channel_payloads(self, root: Path) -> dict[tuple[str, str], dict[str, Any]]:
        config_path = root / "openclaw.json"
        payload = self._read_json_file(config_path)
        channels_cfg = payload.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return {}

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, value in channels_cfg.items():
            kind = str(key).strip().lower()
            if kind == "defaults":
                continue
            if not kind or not isinstance(value, dict):
                continue
            if not bool(value.get("enabled", True)):
                continue

            settings = dict(value)
            settings.pop("accounts", None)
            name = str(value.get("name", kind)).strip().lower().replace(" ", "-") or kind
            rows[(kind, name)] = {
                "kind": kind,
                "name": name,
                "provider": "openclaw",
                "settings": settings,
            }

            accounts = value.get("accounts", {})
            if not isinstance(accounts, dict):
                continue
            for account_id, account_value in accounts.items():
                if not isinstance(account_value, dict):
                    continue
                if not bool(account_value.get("enabled", value.get("enabled", True))):
                    continue
                account_name = (
                    str(account_value.get("name", account_id)).strip().lower().replace(" ", "-")
                    or str(account_id).strip().lower()
                    or kind
                )
                account_settings = dict(settings)
                account_settings.update(account_value)
                rows[(kind, account_name)] = {
                    "kind": kind,
                    "name": account_name,
                    "provider": "openclaw",
                    "settings": account_settings,
                }
        return rows

    def _can_read_provider_channel_roots(self, home: Path, providers: list[str]) -> bool:
        for item in providers:
            token = str(item).strip().lower()
            if not token:
                continue
            try:
                root = home / get_provider(token).state_dir
            except ValueError:
                continue
            if root.exists() and os.access(root, os.R_OK | os.X_OK):
                return True
        return False

    def _discover_live_channel_payloads(self, payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        info = payload.get("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        is_local = bool(info.get("local_user", False))
        provider = str(info.get("provider", "")).strip().lower()
        home = self._local_agent_home(provider) if is_local else self._agent_linux_home(payload)
        if not home:
            return {}
        if linux_user and not is_local and not self._can_manage_linux_user(linux_user):
            if not self._can_read_provider_channel_roots(home, [provider, *provider_names()]):
                return {}

        ordered: list[str] = []
        seen_providers: set[str] = set()
        for item in [provider] + provider_names():
            token = str(item).strip().lower()
            if not token or token in seen_providers:
                continue
            seen_providers.add(token)
            ordered.append(token)

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for name in ordered:
            root = home / get_provider(name).state_dir
            for key, value in self._provider_channel_payloads_for_home(name, root).items():
                rows.setdefault(key, value)
        return rows

    @staticmethod
    def _coerce_string_list(payload: Any) -> list[str]:
        rows: list[str] = []
        if not isinstance(payload, list):
            return rows
        for item in payload:
            token = str(item).strip()
            if token:
                rows.append(token)
        return rows

    def _login_shell_env(self, linux_user: str) -> dict[str, str]:
        env = self._service_env(linux_user)
        if not linux_user:
            return env
        try:
            cmd = self._user_shell_command(linux_user, "env -0")
        except Exception:
            return env
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=False,
                check=False,
                env=self._service_env(linux_user),
                timeout=5,
            )
        except Exception:
            return env
        if result.returncode != 0 or not result.stdout:
            return env
        try:
            raw = result.stdout.decode("utf-8", errors="ignore")
        except Exception:
            return env
        for item in raw.split("\x00"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = str(key).strip()
            if key:
                env[key] = value
        return env

    @staticmethod
    def _resolve_shell_placeholders(payload: Any, env: dict[str, str]) -> Any:
        if isinstance(payload, dict):
            return {key: ZeroClawService._resolve_shell_placeholders(value, env) for key, value in payload.items()}
        if isinstance(payload, list):
            return [ZeroClawService._resolve_shell_placeholders(item, env) for item in payload]
        if not isinstance(payload, str):
            return payload

        def replace(match: re.Match[str]) -> str:
            token = str(match.group(1) or match.group(2) or "").strip()
            if not token:
                return match.group(0)
            return str(env.get(token, match.group(0)))

        return re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", replace, payload)

    @staticmethod
    def _looks_like_unresolved_secret(value: str) -> bool:
        token = str(value).strip()
        if not token:
            return False
        if re.search(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*", token):
            return True
        lowered = token.lower()
        if lowered.startswith("env:") or lowered.startswith("secret:"):
            return True
        return "{{" in token and "}}" in token

    @staticmethod
    def _looks_like_telegram_bot_token(value: str) -> bool:
        token = str(value).strip()
        return bool(re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token))

    def _prepare_agent_provider_home(
        self,
        *,
        provider: str,
        agent: dict[str, Any],
        linux_user: str,
        home: Path | None,
        channels: list[dict[str, Any]],
        live_payloads: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        if not home:
            return
        name = str(provider).strip().lower()
        if name not in {"picoclaw", "openclaw"}:
            return

        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        use_shared_auth = "provider-auth" in set(sync.get("bundles", []))
        if name == "picoclaw":
            self._ensure_picoclaw_native_auth(home=home, linux_user=linux_user, use_shared_auth=use_shared_auth)
        if use_shared_auth:
            self._ensure_shared_provider_auth_links(target_home=home, username=linux_user)
        if name == "openclaw":
            self._ensure_openclaw_agent_auth_link(home=home, linux_user=linux_user)

        auth = self._preferred_agent_provider_auth(
            name,
            agent=agent,
            current_auth_mode=str(agent.get("agent", {}).get("auth_mode", "")),
            allow_defaults=True,
        )
        auth_mode = str(auth.get("auth_mode", get_provider(name).default_auth_mode)).strip().lower()
        api_key = str(auth.get("api_key", "")).strip()
        if name == "picoclaw":
            self._ensure_picoclaw_home_prepared(
                home=home,
                linux_user=linux_user,
                channels=channels,
                live_payloads=live_payloads,
                auth_mode=auth_mode,
                api_key=api_key,
            )
            return
        self._ensure_openclaw_home_prepared(
            home=home,
            linux_user=linux_user,
            channels=channels,
            live_payloads=live_payloads,
            auth_mode=auth_mode,
            api_key=api_key,
        )

    def _ensure_picoclaw_home_prepared(
        self,
        *,
        home: Path,
        linux_user: str,
        channels: list[dict[str, Any]],
        live_payloads: dict[tuple[str, str], dict[str, Any]],
        auth_mode: str,
        api_key: str,
    ) -> None:
        root = home / ".picoclaw"
        root.mkdir(parents=True, exist_ok=True)
        self._chown_tree(root, linux_user)

        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.json"
        config = self._read_json_file(config_path)

        agents_cfg = config.get("agents", {})
        if not isinstance(agents_cfg, dict):
            agents_cfg = {}
        defaults = agents_cfg.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
        defaults["workspace"] = str(workspace)
        defaults["restrict_to_workspace"] = bool(defaults.get("restrict_to_workspace", True))
        defaults["provider"] = "openai"
        defaults["model_name"] = "gpt-5.2"
        defaults["model"] = "gpt-5.2"
        agents_cfg["defaults"] = defaults
        config["agents"] = agents_cfg

        providers_cfg = config.get("providers", {})
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}
        openai_cfg = providers_cfg.get("openai", {})
        if not isinstance(openai_cfg, dict):
            openai_cfg = {}
        openai_cfg.setdefault("api_base", "https://api.openai.com/v1")
        if auth_mode == "linked":
            openai_cfg["auth_method"] = "oauth"
            openai_cfg.pop("api_key", None)
        elif auth_mode == "api_key":
            if not api_key:
                raise SetupError("picoclaw API-key mode requires an API key before the runtime can start")
            openai_cfg["api_key"] = api_key
            openai_cfg.pop("auth_method", None)
        providers_cfg["openai"] = openai_cfg
        config["providers"] = providers_cfg

        model_list = config.get("model_list", [])
        if not isinstance(model_list, list):
            model_list = []
        model_entry: dict[str, Any] | None = None
        for item in model_list:
            if not isinstance(item, dict):
                continue
            model_name = str(item.get("model_name", "")).strip()
            model_ref = str(item.get("model", "")).strip()
            if model_name == "gpt-5.2" or model_ref == "openai/gpt-5.2":
                model_entry = item
                break
        if model_entry is None:
            for item in model_list:
                if isinstance(item, dict) and str(item.get("model", "")).startswith("openai/"):
                    model_entry = item
                    break
        if model_entry is None:
            model_entry = {}
            model_list.append(model_entry)
        model_entry["model_name"] = "gpt-5.2"
        model_entry["model"] = "openai/gpt-5.2"
        model_entry.setdefault("api_base", "https://api.openai.com/v1")
        if auth_mode == "linked":
            model_entry["auth_method"] = "oauth"
            model_entry.pop("api_key", None)
        elif auth_mode == "api_key":
            model_entry["api_key"] = api_key
            model_entry.pop("auth_method", None)
        config["model_list"] = model_list

        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            channels_cfg = {}
        login_env = self._login_shell_env(linux_user)
        payload_by_kind: dict[str, dict[str, Any]] = {}
        for payload in live_payloads.values():
            kind = str(payload.get("kind", "")).strip().lower()
            if kind and kind not in payload_by_kind:
                payload_by_kind[kind] = payload

        for channel in channels:
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or kind == "cli":
                continue
            payload = live_payloads.get((kind, name)) or payload_by_kind.get(kind, {})
            settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
            if not isinstance(settings, dict):
                settings = {}
            settings = self._resolve_shell_placeholders(settings, login_env)
            if kind != "telegram":
                continue
            telegram_cfg = channels_cfg.get("telegram", {})
            if not isinstance(telegram_cfg, dict):
                telegram_cfg = {}
            token = (
                str(settings.get("token", "")).strip()
                or str(settings.get("bot_token", "")).strip()
                or str(telegram_cfg.get("token", "")).strip()
            )
            if not token:
                raise SetupError(
                    "picoclaw telegram bootstrap could not find a bot token; sync live channels or re-link Telegram first"
                )
            if self._looks_like_unresolved_secret(token):
                raise SetupError(
                    "picoclaw telegram bootstrap found an unresolved token placeholder; "
                    "export the bot token in the target user's login shell or re-link Telegram first"
                )
            if not self._looks_like_telegram_bot_token(token):
                raise SetupError(
                    "picoclaw telegram bootstrap found an invalid Telegram bot token in live channel settings; "
                    "re-link Telegram or update the target user's Telegram token"
                )
            telegram_cfg["enabled"] = True
            telegram_cfg["token"] = token
            if name:
                telegram_cfg["name"] = name
            base_url = str(settings.get("base_url", telegram_cfg.get("base_url", ""))).strip()
            if base_url:
                telegram_cfg["base_url"] = base_url
            proxy = str(settings.get("proxy", telegram_cfg.get("proxy", ""))).strip()
            if proxy:
                telegram_cfg["proxy"] = proxy
            allow_from = self._coerce_string_list(settings.get("allow_from", telegram_cfg.get("allow_from", [])))
            telegram_cfg["allow_from"] = allow_from
            group_trigger = settings.get("group_trigger", telegram_cfg.get("group_trigger", {}))
            if isinstance(group_trigger, dict) and group_trigger:
                normalized_trigger: dict[str, Any] = {}
                if "mention_only" in group_trigger:
                    normalized_trigger["mention_only"] = bool(group_trigger.get("mention_only"))
                prefixes = self._coerce_string_list(group_trigger.get("prefixes", []))
                if prefixes:
                    normalized_trigger["prefixes"] = prefixes
                if normalized_trigger:
                    telegram_cfg["group_trigger"] = normalized_trigger
            channels_cfg["telegram"] = telegram_cfg

        config["channels"] = channels_cfg
        has_enabled_channel = any(
            isinstance(value, dict) and bool(value.get("enabled", False))
            for value in channels_cfg.values()
        )
        if not has_enabled_channel:
            raise SetupError("picoclaw requires at least one enabled provider channel before the gateway can run")

        self._write_json_file(config_path, config)
        self._chown_tree(root, linux_user)

    def _ensure_openclaw_home_prepared(
        self,
        *,
        home: Path,
        linux_user: str,
        channels: list[dict[str, Any]],
        live_payloads: dict[tuple[str, str], dict[str, Any]],
        auth_mode: str,
        api_key: str,
    ) -> None:
        root = home / ".openclaw"
        root.mkdir(parents=True, exist_ok=True)
        self._chown_tree(root, linux_user)

        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config_path = root / "openclaw.json"
        config = self._read_json_file(config_path)

        gateway_cfg = config.get("gateway", {})
        if not isinstance(gateway_cfg, dict):
            gateway_cfg = {}
        gateway_cfg["mode"] = "local"
        config["gateway"] = gateway_cfg

        agents_cfg = config.get("agents", {})
        if not isinstance(agents_cfg, dict):
            agents_cfg = {}
        defaults = agents_cfg.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
        defaults["workspace"] = str(workspace)
        heartbeat = defaults.get("heartbeat", {})
        if not isinstance(heartbeat, dict):
            heartbeat = {}
        heartbeat.setdefault("every", "0m")
        heartbeat.setdefault("directPolicy", "block")
        heartbeat.setdefault("lightContext", True)
        heartbeat.setdefault("ackMaxChars", 300)
        defaults["heartbeat"] = heartbeat

        desired_model = ""
        if auth_mode == "linked":
            desired_model = "openai-codex/gpt-5.4"
        elif auth_mode == "api_key":
            if not api_key:
                raise SetupError("openclaw API-key mode requires an API key before the runtime can start")
            desired_model = "openai/gpt-5.2"

        current_model = defaults.get("model")
        if desired_model:
            if isinstance(current_model, dict):
                current_model["primary"] = desired_model
                defaults["model"] = current_model
            else:
                defaults["model"] = desired_model
        agents_cfg["defaults"] = defaults
        config["agents"] = agents_cfg

        if auth_mode == "linked":
            auth_cfg = config.get("auth", {})
            if not isinstance(auth_cfg, dict):
                auth_cfg = {}
            profiles = auth_cfg.get("profiles", {})
            if not isinstance(profiles, dict):
                profiles = {}
            profiles.setdefault(
                "openai-codex:default",
                {
                    "provider": "openai-codex",
                    "mode": "oauth",
                },
            )
            auth_cfg["profiles"] = profiles
            order = auth_cfg.get("order", {})
            if not isinstance(order, dict):
                order = {}
            existing_order = order.get("openai-codex", [])
            order["openai-codex"] = [
                "openai-codex:default",
                *[
                    str(item).strip()
                    for item in existing_order
                    if str(item).strip() and str(item).strip() != "openai-codex:default"
                ],
            ]
            auth_cfg["order"] = order
            config["auth"] = auth_cfg
        elif auth_mode == "api_key":
            models_cfg = config.get("models", {})
            if not isinstance(models_cfg, dict):
                models_cfg = {}
            providers_cfg = models_cfg.get("providers", {})
            if not isinstance(providers_cfg, dict):
                providers_cfg = {}
            openai_cfg = providers_cfg.get("openai", {})
            if not isinstance(openai_cfg, dict):
                openai_cfg = {}
            openai_cfg["apiKey"] = api_key
            providers_cfg["openai"] = openai_cfg
            models_cfg["providers"] = providers_cfg
            config["models"] = models_cfg

        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            channels_cfg = {}
        channel_defaults = channels_cfg.get("defaults", {})
        if not isinstance(channel_defaults, dict):
            channel_defaults = {}
        heartbeat_visibility = channel_defaults.get("heartbeat", {})
        if not isinstance(heartbeat_visibility, dict):
            heartbeat_visibility = {}
        heartbeat_visibility.setdefault("showOk", False)
        heartbeat_visibility.setdefault("showAlerts", False)
        heartbeat_visibility.setdefault("useIndicator", False)
        channel_defaults["heartbeat"] = heartbeat_visibility
        channels_cfg["defaults"] = channel_defaults

        existing_telegram_cfg = channels_cfg.get("telegram", {})
        if isinstance(existing_telegram_cfg, dict):
            existing_telegram_cfg["streaming"] = "off"
            channels_cfg["telegram"] = existing_telegram_cfg
        login_env = self._login_shell_env(linux_user)
        payload_by_kind: dict[str, dict[str, Any]] = {}
        for payload in live_payloads.values():
            kind = str(payload.get("kind", "")).strip().lower()
            if kind and kind not in payload_by_kind:
                payload_by_kind[kind] = payload

        for channel in channels:
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or kind == "cli":
                continue
            payload = live_payloads.get((kind, name)) or payload_by_kind.get(kind, {})
            settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
            if not isinstance(settings, dict):
                settings = {}
            settings = self._resolve_shell_placeholders(settings, login_env)
            if kind != "telegram":
                continue

            telegram_cfg = channels_cfg.get("telegram", {})
            if not isinstance(telegram_cfg, dict):
                telegram_cfg = {}
            telegram_cfg["streaming"] = "off"
            token = (
                str(settings.get("botToken", "")).strip()
                or str(settings.get("bot_token", "")).strip()
                or str(settings.get("token", "")).strip()
                or str(telegram_cfg.get("botToken", "")).strip()
            )
            if not token:
                raise SetupError(
                    "openclaw telegram bootstrap could not find a bot token; sync live channels or re-link Telegram first"
                )
            if self._looks_like_unresolved_secret(token):
                raise SetupError(
                    "openclaw telegram bootstrap found an unresolved token placeholder; "
                    "export the bot token in the target user's login shell or re-link Telegram first"
                )
            if not self._looks_like_telegram_bot_token(token):
                raise SetupError(
                    "openclaw telegram bootstrap found an invalid Telegram bot token in live channel settings; "
                    "re-link Telegram or update the target user's Telegram token"
                )
            telegram_cfg["enabled"] = True
            telegram_cfg["botToken"] = token
            allow_from = self._coerce_string_list(
                settings.get("allowFrom", settings.get("allow_from", telegram_cfg.get("allowFrom", [])))
            )
            existing_allow_from = self._coerce_string_list(telegram_cfg.get("allowFrom", []))
            effective_allow_from = allow_from or existing_allow_from
            explicit_dm_policy = str(
                settings.get("dmPolicy", settings.get("dm_policy", telegram_cfg.get("dmPolicy", "")))
            ).strip().lower()
            if effective_allow_from:
                telegram_cfg["allowFrom"] = effective_allow_from
                telegram_cfg["dmPolicy"] = "open" if "*" in set(effective_allow_from) else "allowlist"
            elif explicit_dm_policy in {"open", "allowlist", "disabled"}:
                telegram_cfg["dmPolicy"] = explicit_dm_policy
            else:
                # Managed Telegram cutovers should stay reachable by default.
                # Heal older Clawie-generated openclaw configs that inherited the runtime default
                # pairing mode without an explicit allowlist.
                telegram_cfg["allowFrom"] = ["*"]
                telegram_cfg["dmPolicy"] = "open"
            proxy = str(settings.get("proxy", telegram_cfg.get("proxy", ""))).strip()
            if proxy:
                telegram_cfg["proxy"] = proxy
            webhook_url = str(
                settings.get("webhookUrl", settings.get("webhook_url", telegram_cfg.get("webhookUrl", "")))
            ).strip()
            if webhook_url:
                telegram_cfg["webhookUrl"] = webhook_url
            webhook_secret = str(
                settings.get("webhookSecret", settings.get("webhook_secret", telegram_cfg.get("webhookSecret", "")))
            ).strip()
            if webhook_secret:
                telegram_cfg["webhookSecret"] = webhook_secret
            group_trigger = settings.get("group_trigger", settings.get("groupTrigger", {}))
            if isinstance(group_trigger, dict) and "mention_only" in group_trigger:
                groups = telegram_cfg.get("groups", {})
                if not isinstance(groups, dict):
                    groups = {}
                default_group = groups.get("*", {})
                if not isinstance(default_group, dict):
                    default_group = {}
                default_group["requireMention"] = bool(group_trigger.get("mention_only"))
                groups["*"] = default_group
                telegram_cfg["groups"] = groups
            channels_cfg["telegram"] = telegram_cfg

        config["channels"] = channels_cfg
        self._write_json_file(config_path, config)
        if auth_mode == "linked":
            self._ensure_openclaw_agent_auth_link(home=home, linux_user=linux_user)
        self._chown_tree(root, linux_user)

    def _remove_picoclaw_channel_from_home(
        self,
        *,
        home: Path,
        linux_user: str,
        kind: str,
    ) -> None:
        config_path = home / ".picoclaw" / "config.json"
        config = self._read_json_file(config_path)
        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return
        token = str(kind).strip().lower()
        if token in channels_cfg:
            channels_cfg.pop(token, None)
            config["channels"] = channels_cfg
            self._write_json_file(config_path, config)
            self._chown_tree(home / ".picoclaw", linux_user)

    def _remove_openclaw_channel_from_home(
        self,
        *,
        home: Path,
        linux_user: str,
        kind: str,
    ) -> None:
        config_path = home / ".openclaw" / "openclaw.json"
        config = self._read_json_file(config_path)
        channels_cfg = config.get("channels", {})
        if not isinstance(channels_cfg, dict):
            return
        token = str(kind).strip().lower()
        if token in channels_cfg:
            channels_cfg.pop(token, None)
            config["channels"] = channels_cfg
            self._write_json_file(config_path, config)
            self._chown_tree(home / ".openclaw", linux_user)

    def agent_service_action(self, agent_id: str, action: str) -> dict[str, Any]:
        self._require_setup()
        command = str(action).strip().lower()
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")
        self._refresh_managed_agent_provider_alignment(agent_id)

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)

        agent_info = agent.setdefault("agent", {})
        provider = str(agent_info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{agent_id}' has no provider configured")
        linux_user = str(agent_info.get("linux_user", "")).strip()
        # Permission is the most fundamental precondition: check it before
        # resolving or installing the provider runtime so callers get an
        # actionable "requires root" error instead of an install hint.
        self._require_linux_user_access(linux_user, "service control")
        home = self._agent_linux_home(agent)
        if command in {"start", "restart"}:
            self.ensure_provider_runtime(provider)
            if home and linux_user:
                self._apply_staged_prompts_if_possible(provider, home, linux_user)
            if home:
                self._write_prompt_files_for_home(
                    provider, home, agent.get("core_prompts", {}), linux_user,
                )
            self._prepare_agent_provider_home(
                provider=provider,
                agent=agent,
                linux_user=linux_user,
                home=home,
                channels=self._effective_agent_channels(agent),
                live_payloads=self._discover_live_channel_payloads(agent),
            )

        result = self._run_managed_provider_service_action(
            provider=provider,
            action=command,
            linux_user=linux_user,
            agent_info=agent_info,
        )
        agent_info["service_status"] = str(result.get("service_status", "unknown"))
        agent_info["service_mode"] = str(result.get("service_mode", "unknown"))
        if "fallback_pid" in agent_info or int(result.get("fallback_pid", 0) or 0) > 0:
            agent_info["fallback_pid"] = int(result.get("fallback_pid", 0) or 0)
        agent_info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.service_action",
            f"Service {command} for {agent_id}",
            {
                "agent_id": agent_id,
                "provider": provider,
                "linux_user": linux_user,
                "action": command,
                "service_status": str(result.get("service_status", "unknown")),
                "mode": str(result.get("service_mode", "unknown")),
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": agent_id,
            "provider": provider,
            "linux_user": linux_user,
            "action": command,
            "service_status": str(result.get("service_status", "unknown")),
            "service_mode": str(result.get("service_mode", "unknown")),
            "output": str(result.get("output", "")),
        }

    def apply_staged_prompts(self, agent_id: str) -> dict[str, Any]:
        """Apply staged prompt files to an agent workspace.

        Runs the pickup shell snippet as the agent's linux_user so it has
        write access to the workspace.  Requires root when linux_user differs
        from the current user.
        """
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        provider = str(agent_info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{agent_id}' has no provider configured")
        linux_user = str(agent_info.get("linux_user", "")).strip()
        spec = get_provider(provider)
        pickup = self._staged_prompt_pickup_shell(provider, spec.state_dir, spec.workspace_dir)
        cmd = self._user_shell_command(linux_user, pickup)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            raise SetupError(f"apply staged prompts failed: {output or f'exit {result.returncode}'}")
        # Check what was applied by looking at what's no longer in staging
        stage_dir = self._prompt_stage_dir(linux_user) if linux_user else None
        remaining: list[str] = []
        if stage_dir and stage_dir.is_dir():
            remaining = [f.name for f in stage_dir.iterdir() if f.name.startswith(f"{provider}--")]
        # Also write from DB to disk (may succeed now that workspace exists)
        home = self._agent_linux_home(agent)
        if home:
            self._write_prompt_files_for_home(provider, home, agent.get("core_prompts", {}), linux_user)
        applied = [
            name.split("--", 1)[1] for name in
            [f.name for f in (stage_dir.iterdir() if stage_dir and stage_dir.is_dir() else [])]
            if name.startswith(f"{provider}--")
        ]
        # If staging dir is now empty or doesn't exist, all were applied
        prompts = agent.get("core_prompts", {})
        prompt_names = [k for k in prompts if prompts[k]]
        if not remaining:
            applied = prompt_names
        return {"agent_id": agent_id, "applied": applied, "remaining": remaining}

    def ensure_agent_permissions(self, agent_id: str, manager_user: str = "") -> dict[str, Any]:
        """Set up group-based access so the manager user can manage the agent without sudo.

        Requires root.  Adds *manager_user* (default: current user) to the
        agent's Linux group and sets the provider state/workspace directories
        to setgid-group-writable (2775).  The agent user's permissions are
        unaffected—only group bits are widened.

        After running this once (with sudo), the manager can write prompt
        files, update configs, and apply staged prompts without root.
        """
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("ensure_agent_permissions requires root. Re-run with sudo.")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        agent_info = agent.setdefault("agent", {})
        provider = str(agent_info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{agent_id}' has no provider configured")
        linux_user = str(agent_info.get("linux_user", "")).strip()
        if not linux_user:
            raise SetupError(f"agent '{agent_id}' has no linux_user")
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"cannot resolve home directory for {linux_user}")

        manager = (
            str(manager_user).strip()
            or str(agent_info.get("manager_user", "")).strip()
            or os.environ.get("SUDO_USER", "").strip()
            or self._current_linux_user()
        )
        changes: list[str] = []

        spec = get_provider(provider)
        state_dir = home / spec.state_dir
        ws = state_dir / spec.workspace_dir

        # Home dir: ensure group r-x for traversal.
        if home.is_dir():
            subprocess.run(["chmod", "g+rx", str(home)], check=False)
            changes.append(f"{home}: g+rx")

        # State dir: setgid + group-writable.
        if state_dir.is_dir():
            subprocess.run(["chmod", "2775", str(state_dir)], check=False)
            changes.append(f"{state_dir}: 2775")
            for child in state_dir.iterdir():
                if child.is_file():
                    subprocess.run(["chmod", "g+rw", str(child)], check=False)

        # Workspace: setgid + group-writable.
        if ws.is_dir():
            subprocess.run(["chmod", "2775", str(ws)], check=False)
            changes.append(f"{ws}: 2775")
            for child in ws.iterdir():
                if child.is_file():
                    subprocess.run(["chmod", "g+rw", str(child)], check=False)
                    changes.append(f"{child.name}: g+rw")

        # Add manager to agent group.
        if manager and manager != linux_user:
            result = subprocess.run(
                ["usermod", "-a", "-G", linux_user, manager],
                check=False, capture_output=True, text=True,
            )
            if result.returncode == 0:
                changes.append(f"added {manager} to group {linux_user}")
            else:
                changes.append(f"usermod failed: {(result.stderr or '').strip()}")

        return {
            "agent_id": agent_id,
            "linux_user": linux_user,
            "manager": manager,
            "changes": changes,
        }

    def delete_agent(self, agent_id: str) -> None:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        if agent_id not in agents:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        del agents[agent_id]
        self._event(
            state,
            "agents.deleted",
            f"Deleted agent {agent_id}",
            {"agent_id": agent_id},
        )
        self.store.write_state(state)

    def purge_agent(self, agent_id: str) -> dict[str, Any]:
        self._require_setup()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
        user_removed = False
        home_removed = False
        if linux_user:
            if os.geteuid() != 0:
                raise SetupError(
                    "purge requires root privileges for spawned Linux users. Re-run with sudo/root."
                )
            home_path = Path("/home") / linux_user
            if self._linux_user_exists(linux_user):
                subprocess.run(["userdel", "-r", linux_user], check=True)
                user_removed = True
                home_removed = True
            elif home_path.exists():
                shutil.rmtree(home_path)
                home_removed = True

        del agents[agent_id]
        self._event(
            state,
            "agents.purged",
            f"Purged agent {agent_id}",
            {
                "agent_id": agent_id,
                "linux_user": linux_user,
                "linux_user_removed": user_removed,
                "home_removed": home_removed,
            },
        )
        self.store.write_state(state)
        return {
            "agent_id": agent_id,
            "linux_user": linux_user,
            "linux_user_removed": user_removed,
            "home_removed": home_removed,
        }

    def migrate_channels(
        self,
        from_agent: str,
        to_agent: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        if from_agent == to_agent:
            raise ValueError("from_agent and to_agent must differ")
        state = self.store.read_state()

        agents = state.setdefault("agents", state.get("users", {}))
        source = agents.get(from_agent)
        target = agents.get(to_agent)
        if not source:
            raise AgentNotFoundError(f"source agent not found: {from_agent}")
        if not target:
            raise AgentNotFoundError(f"target agent not found: {to_agent}")

        source_channels = copy.deepcopy(source.get("channels", []))
        for channel in source_channels:
            channel["migrated_from"] = from_agent
        source_keys = self._channel_keys(source_channels)
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=to_agent,
            channels=source_channels,
            allow_owners={from_agent, to_agent},
        )

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
        moved_from_source = self._remove_channel_keys_from_agent(source=source, keys=source_keys)
        if moved_from_source:
            source.setdefault("agent", {})["last_sync"] = now_iso()
        target["channel_strategy"] = "migrate"
        target["agent"]["status"] = "syncing"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.migrated",
            f"Migrated channels from {from_agent} to {to_agent}",
            {
                "from_agent": from_agent,
                "to_agent": to_agent,
                "replace": replace,
                "channel_count": len(target_channels),
                "moved_from_source": moved_from_source,
            },
        )
        self.store.write_state(state)
        return target

    def bootstrap_channels(
        self,
        agent_id: str,
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
        agents = state.setdefault("agents", state.get("users", {}))
        target = agents.get(agent_id)
        if not target:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        generated = self._mint_channels(agent_id, presets[preset])
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
        self._assert_channels_unclaimed(
            agents=agents,
            owner_agent_id=agent_id,
            channels=target_channels,
        )

        target["channels"] = target_channels
        target["agent"]["status"] = "ready"
        target["agent"]["last_sync"] = now_iso()

        self._event(
            state,
            "channels.bootstrapped",
            f"Applied {preset} channel preset for {agent_id}",
            {
                "agent_id": agent_id,
                "preset": preset,
                "replace": replace,
                "channel_count": len(target_channels),
            },
        )
        self.store.write_state(state)
        return target

    def channel_inventory(self) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        rows: list[dict[str, Any]] = []
        assigned_keys: set[tuple[str, str]] = set()
        for aid, payload in sorted(agents.items()):
            view = self._attach_agent_channel_view(copy.deepcopy(payload))
            provider = str(view.get("agent", {}).get("provider", "")).strip().lower()
            for channel in view.get("channels", []):
                if not isinstance(channel, dict):
                    continue
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                assigned_keys.add((kind, name))
                rows.append(
                    {
                        "source": str(channel.get("channel_source", "agent")) or "agent",
                        "owner_agent_id": str(aid),
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                        "discovered_provider": str(channel.get("discovered_provider", "")),
                    }
                )

        for channel in self._read_channel_pool():
            kind = str(channel.get("kind", "")).strip().lower()
            name = str(channel.get("name", "")).strip()
            if not kind or not name or (kind, name) in assigned_keys:
                continue
            rows.append(
                {
                    "source": "pool",
                    "owner_agent_id": "@pool",
                    "provider": str(channel.get("provider", "")).strip().lower(),
                    "kind": kind,
                    "name": name,
                    "enabled": False,
                }
            )

        for item in self._local_channel_inventory():
            key = (str(item.get("kind", "")).strip().lower(), str(item.get("name", "")).strip())
            if key in assigned_keys:
                continue
            rows.append(item)

        kinds = {str(row.get("kind", "")) for row in rows if str(row.get("kind", "")).strip()}
        return {
            "generated_at": now_iso(),
            "rows": rows,
            "totals": {
                "channels": len(rows),
                "kinds": len(kinds),
                "assigned": sum(
                    1 for row in rows if str(row.get("owner_agent_id", "")).strip() not in {"", "@pool"}
                ),
                "local": sum(1 for row in rows if str(row.get("source", "")) == "local"),
                "pool": sum(1 for row in rows if str(row.get("source", "")) == "pool"),
            },
        }

    def assign_channel_to_agent(
        self,
        source_agent_id: str,
        kind: str,
        name: str,
        target_agent_id: str,
    ) -> dict[str, Any]:
        self._require_setup()
        src = str(source_agent_id).strip()
        dst = str(target_agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")
        if not dst:
            raise ValueError("target_agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        target = agents.get(dst)
        if not target:
            raise AgentNotFoundError(f"target agent not found: {dst}")
        self._hydrate_agent_controls(target)
        target_channels = target.setdefault("channels", [])
        if not isinstance(target_channels, list):
            target_channels = []
            target["channels"] = target_channels

        moved_from_agents = self._remove_channel_from_other_agents(
            agents=agents,
            kind=channel_kind,
            name=channel_name,
            keep_agent_id=dst,
        )
        self._remove_pool_channel(channel_kind, channel_name)
        if self._find_channel(target_channels, channel_kind, channel_name) is None:
            target_channels.append(
                {
                    "kind": channel_kind,
                    "name": channel_name,
                    "enabled": True,
                    "external_id": f"{dst}:{channel_kind}:{len(target_channels) + 1}",
                }
            )

        moved = bool(moved_from_agents)

        target.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "channels.assigned",
            f"Assigned channel {channel_kind}:{channel_name} to {dst}",
            {
                "source_agent_id": src,
                "target_agent_id": dst,
                "kind": channel_kind,
                "name": channel_name,
                "moved": moved,
                "moved_from_agent_ids": moved_from_agents,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "target_agent_id": dst,
            "kind": channel_kind,
            "name": channel_name,
            "moved": moved,
            "moved_from_agent_ids": moved_from_agents,
        }

    def unassign_channel_from_agent(
        self,
        agent_id: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        self._require_setup()
        src = str(agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not src:
            raise ValueError("agent_id is required")
        if src.startswith("@local:"):
            raise ValueError("cannot unassign local-user channel")
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        source = agents.get(src)
        if not source:
            raise AgentNotFoundError(f"agent not found: {src}")
        self._hydrate_agent_controls(source)
        channels = source.setdefault("channels", [])
        if not isinstance(channels, list):
            channels = []
            source["channels"] = channels

        found_idx = self._find_channel(channels, channel_kind, channel_name)
        if found_idx is None:
            raise ValueError(f"channel not found on {src}: {channel_kind}:{channel_name}")
        removed = channels.pop(found_idx)
        source.setdefault("agent", {})["last_sync"] = now_iso()

        pool = self._read_channel_pool()
        if self._find_channel(pool, channel_kind, channel_name) is None:
            pool.append(
                {
                    "kind": channel_kind,
                    "name": channel_name,
                    "provider": str(source.get("agent", {}).get("provider", "")).strip().lower(),
                    "external_id": str(removed.get("external_id", "")),
                }
            )
            self._write_channel_pool(pool)

        provider = str(source.get("agent", {}).get("provider", "")).strip().lower()
        linux_user = str(source.get("agent", {}).get("linux_user", "")).strip()
        home = self._agent_linux_home(source)
        if provider in {"picoclaw", "openclaw"} and linux_user and home:
            self._prepare_agent_provider_home(
                provider=provider,
                agent=source,
                linux_user=linux_user,
                home=home,
                channels=self._effective_agent_channels(source),
                live_payloads=self._discover_live_channel_payloads(source),
            )
            if provider == "picoclaw":
                self._remove_picoclaw_channel_from_home(home=home, linux_user=linux_user, kind=channel_kind)
            else:
                self._remove_openclaw_channel_from_home(home=home, linux_user=linux_user, kind=channel_kind)
            if self._provider_process_live(provider, linux_user):
                result = self._run_managed_provider_service_action(
                    provider=provider,
                    action="restart",
                    linux_user=linux_user,
                    agent_info=source.setdefault("agent", {}),
                )
                source["agent"]["service_status"] = str(result.get("service_status", "unknown"))
                source["agent"]["service_mode"] = str(result.get("service_mode", "unknown"))

        self._event(
            state,
            "channels.unassigned",
            f"Unassigned channel {channel_kind}:{channel_name} from {src}",
            {
                "source_agent_id": src,
                "kind": channel_kind,
                "name": channel_name,
            },
        )
        self.store.write_state(state)
        return {
            "source_agent_id": src,
            "kind": channel_kind,
            "name": channel_name,
            "status": "unassigned",
        }

    def connect_agent_channel(
        self,
        agent_id: str,
        kind: str,
        name: str,
    ) -> dict[str, Any]:
        self._require_setup()
        target = str(agent_id).strip()
        channel_kind = str(kind).strip().lower()
        channel_name = str(name).strip()
        if not target:
            raise ValueError("agent_id is required")
        if not channel_kind or not channel_name:
            raise ValueError("kind and name are required")
        if target.startswith("@local:"):
            raise ValueError("connect is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(target)

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(target)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {target}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{target}' has no provider configured")
        linux_user = str(info.get("linux_user", "")).strip()
        existing_channels = agent.get("channels", [])
        already_assigned = (
            isinstance(existing_channels, list)
            and self._find_channel(existing_channels, channel_kind, channel_name) is not None
        )
        if not already_assigned:
            self.assign_channel_to_agent("", channel_kind, channel_name, target)
            state = self.store.read_state()
            agents = state.setdefault("agents", state.get("users", {}))
            agent = agents.get(target)
            if not agent:
                raise AgentNotFoundError(f"agent not found: {target}")
            self._hydrate_agent_controls(agent)
            info = agent.setdefault("agent", {})
            provider = str(info.get("provider", "")).strip().lower()
            linux_user = str(info.get("linux_user", "")).strip()

        if provider == "picoclaw":
            home = self._agent_linux_home(agent)
            effective_channels = self._effective_agent_channels(agent)
            live_payloads = self._discover_live_channel_payloads(agent)
            if home:
                self._write_prompt_files_for_home(
                    provider, home, agent.get("core_prompts", {}), linux_user,
                )
            self._prepare_agent_provider_home(
                provider=provider,
                agent=agent,
                linux_user=linux_user,
                home=home,
                channels=effective_channels,
                live_payloads=live_payloads,
            )
            if self._provider_process_live(provider, linux_user):
                result = self._run_managed_provider_service_action(
                    provider=provider,
                    action="restart",
                    linux_user=linux_user,
                    agent_info=info,
                )
                info["service_status"] = str(result.get("service_status", "unknown"))
                info["service_mode"] = str(result.get("service_mode", "unknown"))
            info["last_sync"] = now_iso()
            self._event(
                state,
                "channels.connected",
                f"Connected channel {channel_kind}:{channel_name} for {target}",
                {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": "config-write",
                },
            )
            self.store.write_state(state)
            return {
                "agent_id": target,
                "provider": provider,
                "kind": channel_kind,
                "name": channel_name,
                "command": [],
                "output": "configured provider channel",
                "status": "connected",
            }
        if provider == "openclaw":
            home = self._agent_linux_home(agent)
            effective_channels = self._effective_agent_channels(agent)
            live_payloads = self._discover_live_channel_payloads(agent)
            if channel_kind == "telegram" and (
                live_payloads.get((channel_kind, channel_name))
                or any(str(key[0]).strip().lower() == channel_kind for key in live_payloads)
            ):
                if home:
                    self._write_prompt_files_for_home(
                        provider, home, agent.get("core_prompts", {}), linux_user,
                    )
                self._prepare_agent_provider_home(
                    provider=provider,
                    agent=agent,
                    linux_user=linux_user,
                    home=home,
                    channels=effective_channels,
                    live_payloads=live_payloads,
                )
                if self._provider_process_live(provider, linux_user):
                    result = self._run_managed_provider_service_action(
                        provider=provider,
                        action="restart",
                        linux_user=linux_user,
                        agent_info=info,
                    )
                    info["service_status"] = str(result.get("service_status", "unknown"))
                    info["service_mode"] = str(result.get("service_mode", "unknown"))
                info["last_sync"] = now_iso()
                self._event(
                    state,
                    "channels.connected",
                    f"Connected channel {channel_kind}:{channel_name} for {target}",
                    {
                        "agent_id": target,
                        "provider": provider,
                        "kind": channel_kind,
                        "name": channel_name,
                        "command": "config-write",
                    },
                )
                self.store.write_state(state)
                return {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": [],
                    "output": "configured provider channel",
                    "status": "connected",
                }

        commands = self._channel_connect_commands(provider, channel_kind, channel_name, linux_user)
        last_error = ""
        env = self._service_env(linux_user)
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                state = self.store.read_state()
                agents = state.setdefault("agents", state.get("users", {}))
                refreshed = agents.get(target, {})
                refreshed_info = refreshed.setdefault("agent", {})
                refreshed_info["last_sync"] = now_iso()
                self._event(
                    state,
                    "channels.connected",
                    f"Connected channel {channel_kind}:{channel_name} for {target}",
                    {
                        "agent_id": target,
                        "provider": provider,
                        "kind": channel_kind,
                        "name": channel_name,
                        "command": " ".join(cmd),
                    },
                )
                self.store.write_state(state)
                return {
                    "agent_id": target,
                    "provider": provider,
                    "kind": channel_kind,
                    "name": channel_name,
                    "command": cmd,
                    "output": output,
                    "status": "connected",
                }
            last_error = output or f"exit {result.returncode}"

        if not already_assigned:
            try:
                self.unassign_channel_from_agent(target, channel_kind, channel_name)
            except Exception:
                pass

        raise SetupError(
            f"channel connect failed for {target} ({provider}): {last_error}. "
            + ("attempted: " + " || ".join(" ".join(cmd) for cmd in commands) if commands else "")
        )

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        config = self.store.read_config()
        state = self.store.read_state()

        provider = str(config.get("provider", "openclaw"))
        provider_auth = self._provider_auth(provider)
        mode = provider_auth.get("auth_mode", get_provider(provider).default_auth_mode)
        if self._is_provider_configured(provider, provider_auth):
            checks.append(
                {
                    "status": "pass",
                    "message": f"Provider auth configured ({provider}/{mode})",
                }
            )
        else:
            checks.append(
                {
                    "status": "fail",
                    "message": "Provider credentials are missing. Run setup.",
                }
            )

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
        checks.append(
            {
                "status": "pass",
                "message": f"Local database: {self.store.db_path}",
            }
        )

        agents = state.setdefault("agents", state.get("users", {}))
        if agents:
            checks.append({"status": "pass", "message": f"{len(agents)} agent(s) provisioned"})
        else:
            checks.append({"status": "warn", "message": "No agents provisioned yet"})

        no_channels = [aid for aid, row in agents.items() if not row.get("channels")]
        if no_channels:
            checks.append({
                "status": "warn",
                "message": "Agents without channels: " + ", ".join(no_channels),
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

    def list_installed_claws(self, source_home: str | Path | None = None) -> list[dict[str, Any]]:
        if source_home:
            root = Path(source_home).expanduser()
        else:
            sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
            if os.geteuid() == 0 and sudo_user and sudo_user != "root":
                try:
                    root = Path(pwd.getpwnam(sudo_user).pw_dir)
                except KeyError:
                    root = Path.home()
            elif os.geteuid() == 0:
                discovered: list[dict[str, Any]] = []
                home_root = Path("/home")
                if home_root.exists():
                    for entry in sorted(home_root.iterdir()):
                        if not entry.is_dir():
                            continue
                        discovered.extend(detect_installed_providers(str(entry)))
                if discovered:
                    return discovered
                root = Path.home()
            else:
                root = Path.home()
        return detect_installed_providers(str(root))

    def get_dashboard_agent(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            payload = self._local_agent_view(provider)
        else:
            self._refresh_managed_agent_provider_alignment(token)
            payload = copy.deepcopy(self.get_agent(token))
        payload = self._attach_agent_runtime_status(payload)
        info = payload.setdefault("agent", {})
        info["status"] = self._dashboard_status(str(info.get("status", "")), info)
        payload = self._attach_agent_auth_status(payload)
        payload = self._attach_agent_addon_status(payload)
        return self._attach_agent_channel_view(payload)

    def sync_agent_channels_from_provider(self, agent_id: str, *, replace: bool = True) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            raise ValueError("channel sync is only supported for managed agents")
        self._refresh_managed_agent_provider_alignment(token)
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        discovery = self._discover_agent_channels(agent)
        if str(discovery.get("source", "")) == "permission":
            raise SetupError(str(discovery.get("detail", "live channel discovery requires root")))
        discovered = discovery.get("channels", [])
        if not isinstance(discovered, list) or not discovered:
            raise SetupError(str(discovery.get("detail", "no live channels discovered")))

        existing = agent.get("channels", [])
        existing_map: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(existing, list):
            for row in existing:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key[0] and key[1]:
                    existing_map[key] = dict(row)

        synced: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for channel in discovered:
            key = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if key in seen or not key[0] or not key[1]:
                continue
            seen.add(key)
            row = dict(existing_map.get(key, {}))
            row["kind"] = key[0]
            row["name"] = key[1]
            row["enabled"] = bool(channel.get("enabled", row.get("enabled", True)))
            row["external_id"] = str(row.get("external_id", f"{token}:{key[0]}:{len(synced) + 1}"))
            synced.append(row)

        if not replace and isinstance(existing, list):
            for row in existing:
                if not isinstance(row, dict):
                    continue
                key = self._channel_key(row.get("kind", ""), row.get("name", ""))
                if key in seen or not key[0] or not key[1]:
                    continue
                synced.append(dict(row))

        agent["channels"] = synced
        agent.setdefault("agent", {})["last_sync"] = now_iso()
        self._event(
            state,
            "channels.synced_from_provider",
            f"Synced live channels for {token}",
            {
                "agent_id": token,
                "replace": bool(replace),
                "channel_count": len(synced),
                "discovered_provider": list(discovery.get("providers", [])),
            },
        )
        self.store.write_state(state)
        return self.get_dashboard_agent(token)

    def list_agent_core_prompts(self, agent_id: str) -> list[dict[str, Any]]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        prompts = self._normalize_core_prompts(provider, payload.get("core_prompts", {}))
        rows: list[dict[str, Any]] = []
        for name in self._provider_core_prompt_names(provider):
            content = str(prompts.get(name, ""))
            rows.append(
                {
                    "name": name,
                    "chars": len(content),
                    "configured": bool(content.strip()),
                }
            )
        return rows

    def get_agent_core_prompt(self, agent_id: str, prompt_name: str) -> dict[str, str]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        name = self._canonical_core_prompt_name(provider, prompt_name)
        prompts = self._normalize_core_prompts(provider, payload.get("core_prompts", {}))
        return {"name": name, "content": str(prompts.get(name, ""))}

    def set_agent_core_prompt(
        self,
        agent_id: str,
        prompt_name: str,
        content: str,
        sync_to_disk: bool = True,
    ) -> dict[str, Any]:
        token = str(agent_id).strip()
        body = str(content)
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            name = self._canonical_core_prompt_name(provider, prompt_name)
            home = self._local_agent_home(provider)
            if not home:
                raise SetupError(f"could not resolve local home for provider '{provider}'")
            self._write_core_prompt_file(provider, home, name, body)
            return self._local_agent_view(provider)

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        name = self._canonical_core_prompt_name(provider, prompt_name)
        prompts = self._normalize_core_prompts(provider, agent.get("core_prompts", {}))
        prompts[name] = body
        agent["core_prompts"] = prompts
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.prompt_updated",
            f"Updated {name} for {token}",
            {"agent_id": token, "prompt": name, "chars": len(body)},
        )
        self.store.write_state(state)
        if sync_to_disk:
            self.write_agent_core_prompts_to_disk(token)
        return agent

    def clone_agent_prompts(
        self,
        from_agent: str,
        to_agent: str,
        apply_to_disk: bool = True,
    ) -> dict[str, Any]:
        source = self.get_dashboard_agent(from_agent)
        target = self.get_dashboard_agent(to_agent)
        source_info = source.get("agent", {})
        target_info = target.get("agent", {})
        source_provider = str(source_info.get("provider", "")).strip().lower()
        target_provider = str(target_info.get("provider", "")).strip().lower()
        if source_provider != target_provider:
            raise ValueError("source and target providers must match to clone core prompts")
        prompt_payload = self._normalize_core_prompts(source_provider, source.get("core_prompts", {}))
        for name, content in prompt_payload.items():
            self.set_agent_core_prompt(to_agent, name, content, sync_to_disk=False)
        if apply_to_disk:
            self.write_agent_core_prompts_to_disk(to_agent)
        return self.get_dashboard_agent(to_agent)

    def sync_agent_core_prompts_from_disk(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            provider = token.split(":", 1)[1]
            return self._local_agent_view(provider)
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"agent '{token}' has no linux_user home to read prompts from")
        disk_prompts = self._read_core_prompts_from_home(provider, home)
        agent["core_prompts"] = self._normalize_core_prompts(provider, disk_prompts)
        self._seed_core_prompt_defaults(
            provider,
            agent["core_prompts"],
            agent_id=token,
            display_name=str(agent.get("display_name", "")),
        )
        self._seed_delegation_skill(agent["core_prompts"], self._normalize_plugins(info.get("plugins", {})))
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.prompt_synced",
            f"Synced core prompts from disk for {token}",
            {"agent_id": token, "source": str(home)},
        )
        self.store.write_state(state)
        return agent

    def write_agent_core_prompts_to_disk(self, agent_id: str) -> dict[str, Any]:
        payload = self.get_dashboard_agent(agent_id)
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        prompts = self._normalize_core_prompts(provider, payload.get("core_prompts", {}))
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            home = self._local_agent_home(provider)
            if not home:
                raise SetupError(f"could not resolve local home for provider '{provider}'")
            self._write_prompt_files_for_home(provider, home, prompts)
            return self._local_agent_view(provider)

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        home = self._agent_linux_home(agent)
        if not home:
            raise SetupError(f"agent '{token}' has no linux_user home to write prompts to")
        linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
        self._write_prompt_files_for_home(provider, home, prompts, linux_user)
        return agent

    def shared_auth_status(self, provider: str) -> dict[str, Any]:
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        shared_home = self._ensure_shared_provider_auth_root()
        auth_mode = str(self._preferred_shared_provider_auth(spec.name, allow_defaults=True).get("auth_mode", spec.default_auth_mode))
        payload = self._inspect_provider_auth_state(
            provider=spec.name,
            auth_mode=auth_mode,
            linux_user="",
            home=shared_home,
        )
        payload.update(
            {
                "provider": spec.name,
                "linux_user": "",
                "home": str(shared_home),
                "shared_scope": self._shared_provider_auth_scope(),
                "shared_agents": self._shared_provider_auth_agent_ids(spec.name),
            }
        )
        return payload

    def list_shared_auth_statuses(self) -> list[dict[str, Any]]:
        providers = self.configured_provider_names()
        if not providers:
            config = self.store.read_config()
            providers = [str(config.get("provider", "openclaw")).strip().lower() or "openclaw"]
        return [self.shared_auth_status(provider) for provider in providers]

    def shared_auth_login(self, provider: str) -> dict[str, Any]:
        self._require_setup()
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        shared_home = self._ensure_shared_provider_auth_root()
        auth_mode = str(self._preferred_shared_provider_auth(spec.name, allow_defaults=True).get("auth_mode", spec.default_auth_mode))
        payload = self._refresh_or_login_linked_auth(
            provider=spec.name,
            auth_mode=auth_mode,
            linux_user="",
            home=shared_home,
        )
        self._relax_shared_provider_auth_permissions()
        applied = self.apply_shared_auth_links()
        payload.update(
            {
                "provider": spec.name,
                "linux_user": "",
                "home": str(shared_home),
                "shared_scope": self._shared_provider_auth_scope(),
                "shared_agents": list(applied.get("updated_agents", [])),
                "restart_required_agents": self._shared_provider_auth_agent_ids_for_providers(
                    [spec.name],
                    include_eligible=True,
                )
                if str(payload.get("action_performed", "")).strip().lower() != "status"
                else [],
            }
        )
        return payload

    def import_shared_auth(
        self,
        provider: str,
        *,
        source: str,
        source_home: str | Path | None = None,
    ) -> dict[str, Any]:
        self._require_setup()
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        shared_home = self._ensure_shared_provider_auth_root()
        src_home = Path(source_home).expanduser() if source_home else self._default_source_home()
        if not src_home.exists():
            raise FileNotFoundError(f"source home not found: {src_home}")

        mode = str(source).strip().lower()
        updated: list[str] = []
        if mode == "provider":
            updated.extend(
                self._seed_shared_provider_auth_from_home(
                    source_home=src_home,
                    requested_provider=name,
                )
            )
        elif mode == "codex":
            imported = load_codex_auth(src_home)
            updated.extend(self._write_provider_auth_profiles([name], imported))
            target = shared_home / ".codex" / "auth.json"
            if self._copy_if_present(src_home / ".codex" / "auth.json", target):
                updated.append(str(target))
        elif mode == "claude":
            imported = load_claude_auth(src_home)
            updated.extend(self._write_provider_auth_profiles([name], imported))
        else:
            raise ValueError("source must be one of: provider, codex, claude")

        self._relax_shared_provider_auth_permissions()
        applied = self.apply_shared_auth_links()
        auth = self.shared_auth_status(name)
        restart_required_agents = (
            self._shared_provider_auth_agent_ids_for_providers([name], include_eligible=True)
            if updated
            else []
        )
        return {
            "provider": name,
            "source": mode,
            "source_home": str(src_home),
            "home": str(shared_home),
            "updated_paths": self._dedupe_paths(updated),
            "updated_agents": list(applied.get("updated_agents", [])),
            "skipped_agents": list(applied.get("skipped_agents", [])),
            "restart_required_agents": restart_required_agents,
            "auth": auth,
        }

    def apply_shared_auth_links(self, agent_id: str | None = None) -> dict[str, Any]:
        self._require_setup()
        shared_home = self._ensure_shared_provider_auth_root()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        updated_agents: list[str] = []
        skipped_agents: list[str] = []
        linked_paths: list[str] = []
        changed = False
        for aid, agent in sorted(agents.items()):
            token = str(aid).strip()
            if agent_id and token != str(agent_id).strip():
                continue
            self._hydrate_agent_controls(agent)
            sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
            if "provider-auth" not in set(sync.get("bundles", [])):
                continue
            info = agent.setdefault("agent", {})
            linux_user = str(info.get("linux_user", "")).strip()
            home = self._agent_linux_home(agent)
            if not linux_user or home is None or not home.exists():
                skipped_agents.append(token)
                continue
            if not self._can_manage_linux_user(linux_user):
                skipped_agents.append(token)
                continue
            linked = self._ensure_shared_provider_auth_links(target_home=home, username=linux_user)
            linked_paths.extend(linked)
            sync["shared_provider_auth"] = True
            sync["last_synced_at"] = now_iso()
            sync["last_source_home"] = str(shared_home)
            sync["last_synced_paths"] = self._dedupe_paths(list(sync.get("last_synced_paths", [])) + linked)
            sync["last_revoked_paths"] = []
            agent["credential_sync"] = sync
            info["last_sync"] = now_iso()
            updated_agents.append(token)
            changed = True
        if changed:
            self._event(
                state,
                "agents.shared_auth_applied",
                "Applied shared provider auth links",
                {
                    "agent_id": str(agent_id or ""),
                    "shared_home": str(shared_home),
                    "updated_agents": updated_agents,
                    "linked_paths": self._dedupe_paths(linked_paths),
                },
            )
            self.store.write_state(state)
        return {
            "home": str(shared_home),
            "updated_agents": updated_agents,
            "skipped_agents": skipped_agents,
            "restart_required_agents": updated_agents,
            "linked_paths": self._dedupe_paths(linked_paths),
        }

    def _shared_provider_auth_agent_ids(self, provider: str) -> list[str]:
        return self._shared_provider_auth_agent_ids_for_providers([provider])

    def _shared_provider_auth_agent_ids_for_providers(
        self,
        providers: list[str],
        *,
        include_eligible: bool = False,
    ) -> list[str]:
        names = {str(item).strip().lower() for item in providers if str(item).strip()}
        if not names:
            return []
        rows: list[str] = []
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        for aid, agent in sorted(agents.items()):
            self._hydrate_agent_controls(agent)
            info = agent.get("agent", {})
            if str(info.get("provider", "")).strip().lower() not in names:
                continue
            sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
            bundles = {str(item).strip() for item in sync.get("bundles", []) if str(item).strip()}
            if bool(sync.get("shared_provider_auth", False)) or (include_eligible and "provider-auth" in bundles):
                rows.append(str(aid))
        return rows

    def list_addons(self) -> list[dict[str, Any]]:
        return [self.get_addon_status(addon) for addon in addon_names()]

    def get_addon_status(self, addon: str) -> dict[str, Any]:
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)
        if isinstance(spec, ServiceAddonSpec):
            return self._get_service_addon_status(spec)
        auth = self.shared_addon_auth_status(spec.name)
        executable = self._resolve_executable_in_service_env(spec.executable)
        return {
            "addon": spec.name,
            "label": spec.label,
            "description": spec.description,
            "installed": bool(executable),
            "executable": executable,
            "install_method": spec.install_method,
            "install_package": spec.install_package or spec.executable,
            "auth_status": str(auth.get("auth_status", "unknown")),
            "auth_detail": str(auth.get("detail", "")),
            "config_dir": str(auth.get("config_dir", "")),
            "shared_scope": str(auth.get("shared_scope", "")),
            "linked_agents": list(auth.get("linked_agents", [])),
        }

    def _get_service_addon_status(self, spec: ServiceAddonSpec) -> dict[str, Any]:
        """Status for a service-type addon (e.g. display)."""
        installed = check_display_installed(spec.check_executables)
        linked_agents = self._shared_addon_agent_ids(spec.name)
        active_displays: list[dict[str, Any]] = []
        state = self.store.read_state()
        agents = state.get("agents", state.get("users", {}))
        for aid, agent_data in sorted(agents.items()):
            addons = agent_data.get("addons", {})
            display_data = addons.get("display", {})
            if isinstance(display_data, dict) and display_data.get("enabled") and display_data.get("display_number"):
                active_displays.append({
                    "agent_id": aid,
                    "display_number": display_data["display_number"],
                    "vnc_port": display_data.get("vnc_port"),
                    "novnc_port": display_data.get("novnc_port"),
                    "resolution": display_data.get("resolution", spec.default_resolution),
                })
        return {
            "addon": spec.name,
            "label": spec.label,
            "description": spec.description,
            "installed": installed,
            "executable": spec.check_executables[0] if spec.check_executables else "",
            "install_method": spec.install_method,
            "install_package": ", ".join(spec.apt_packages[:4]) + "...",
            "auth_status": "n/a",
            "auth_detail": "service addon (no auth required)",
            "config_dir": "",
            "shared_scope": "system",
            "linked_agents": linked_agents,
            "active_displays": active_displays,
        }

    def shared_addon_auth_status(self, addon: str) -> dict[str, Any]:
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)
        if isinstance(spec, (ServiceAddonSpec, ToolAddonSpec)):
            return {
                "addon": spec.name,
                "label": spec.label,
                "auth_status": "n/a",
                "detail": "tool addon (no auth required)" if isinstance(spec, ToolAddonSpec) else "service addon (no auth required)",
                "login_required": False,
                "config_dir": "",
                "shared_scope": "system",
                "linked_agents": self._shared_addon_agent_ids(spec.name),
            }
        config_dir = self._ensure_shared_addon_config_dir(spec.name)
        payload: dict[str, Any] = {}
        executable = self._resolve_executable_in_service_env(spec.executable)
        if executable and spec.auth_status_command:
            try:
                result = subprocess.run(
                    self._addon_shell_command(spec.name, spec.auth_status_command, linux_user="", config_dir=config_dir),
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._service_env(""),
                )
            except Exception:
                result = None
            raw = ""
            if result is not None:
                raw = str(result.stdout or "").strip() or str(result.stderr or "").strip()
            if raw:
                try:
                    if spec.name == "gws":
                        payload = parse_gws_status_output(raw, config_dir=config_dir)
                except ValueError:
                    payload = {}
        if not payload:
            payload = inspect_addon_auth(spec.name, config_dir)
        payload.update(
            {
                "addon": spec.name,
                "label": spec.label,
                "description": spec.description,
                "config_dir": str(config_dir),
                "shared_scope": self._shared_addon_auth_scope(),
                "linked_agents": self._shared_addon_agent_ids(spec.name),
            }
        )
        return payload

    def shared_addon_auth_login(self, addon: str) -> dict[str, Any]:
        self._require_setup()
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)
        if isinstance(spec, ServiceAddonSpec):
            return {
                "addon": spec.name,
                "auth_status": "n/a",
                "detail": "service addon (no auth required)",
                "action_performed": "status",
                "login_required": False,
                "linked_agents": self._shared_addon_agent_ids(spec.name),
            }
        self.ensure_addon_installed(spec.name)
        status = self.shared_addon_auth_status(spec.name)
        if str(status.get("auth_status", "")).strip().lower() == "ready":
            status["action_performed"] = "status"
            return status

        config_dir = self._ensure_shared_addon_config_dir(spec.name)
        command = spec.auth_login_command
        client_error = str(status.get("client_config_error", "")).strip()
        if spec.name == "gws" and (not bool(status.get("client_secret_present", False)) or client_error):
            try:
                self.ensure_support_tool_installed("gcloud")
            except SetupError as exc:
                raise SetupError(
                    "gws auth setup requires gcloud, and Clawie could not provision it automatically: "
                    f"{exc}"
                ) from exc
            command = spec.auth_setup_command or spec.auth_login_command
        result = subprocess.run(
            self._addon_shell_command(spec.name, command, linux_user="", config_dir=config_dir),
            check=False,
            env=self._service_env(""),
        )
        if result.returncode != 0:
            action = " ".join(command)
            raise SetupError(f"{spec.name} {action} failed with exit code {result.returncode}")

        updated = self._materialize_shared_addon_credentials(spec.name, source_config_dir=config_dir, linux_user="")
        self._relax_shared_addon_permissions(spec.name)
        applied = self.apply_shared_addon_links(spec.name)
        payload = self.shared_addon_auth_status(spec.name)
        payload["action_performed"] = "login"
        payload["updated_paths"] = updated
        payload["linked_agents"] = list(applied.get("updated_agents", []))
        return payload

    def import_shared_addon_auth(
        self,
        addon: str,
        *,
        source_home: str | Path | None = None,
        source_agent: str | None = None,
    ) -> dict[str, Any]:
        self._require_setup()
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)
        if isinstance(spec, ServiceAddonSpec):
            raise ValueError(f"addon '{spec.name}' is a service addon and does not use credential import")
        self.ensure_addon_installed(spec.name)
        src_home, src_linux_user, source_label = self._resolve_addon_source(source_home=source_home, source_agent=source_agent)
        src_config = src_home / spec.target_config_rel
        if not src_config.exists():
            raise FileNotFoundError(f"{spec.label} config directory not found: {src_config}")
        shared_dir = self._ensure_shared_addon_config_dir(spec.name)
        updated = self._replace_tree(src_config, shared_dir)
        updated.extend(
            self._materialize_shared_addon_credentials(
                spec.name,
                source_config_dir=src_config,
                linux_user=src_linux_user,
            )
        )
        status = self.shared_addon_auth_status(spec.name)
        if str(status.get("auth_status", "")).strip().lower() != "ready":
            raise SetupError(
                f"no portable {spec.name} credentials were found in {src_config}. "
                f"Log in with 'clawie addon auth login {spec.name}' first."
            )
        self._relax_shared_addon_permissions(spec.name)
        applied = self.apply_shared_addon_links(spec.name)
        auth = self.shared_addon_auth_status(spec.name)
        return {
            "addon": spec.name,
            "source_home": str(src_home),
            "source_agent": str(source_agent or ""),
            "source_label": source_label,
            "config_dir": str(shared_dir),
            "updated_paths": self._dedupe_paths(updated),
            "updated_agents": list(applied.get("updated_agents", [])),
            "skipped_agents": list(applied.get("skipped_agents", [])),
            "auth": auth,
        }

    def apply_shared_addon_links(self, addon: str, agent_id: str | None = None) -> dict[str, Any]:
        self._require_setup()
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        if is_service_addon(name):
            return {
                "addon": name,
                "home": "",
                "updated_agents": [],
                "skipped_agents": [],
                "linked_paths": [],
            }
        status = self.shared_addon_auth_status(name)
        if str(status.get("auth_status", "")).strip().lower() != "ready":
            raise SetupError(
                f"shared {name} credentials are missing. Run 'clawie addon auth login {name}' or import them first."
            )
        shared_dir = self._shared_addon_config_dir(name)
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        updated_agents: list[str] = []
        skipped_agents: list[str] = []
        linked_paths: list[str] = []
        changed = False
        for aid, agent in sorted(agents.items()):
            token = str(aid).strip()
            if agent_id and token != str(agent_id).strip():
                continue
            self._hydrate_agent_controls(agent)
            addons = self._normalize_agent_addons(agent.get("addons"))
            addon_state = dict(addons.get(name, {}))
            if not bool(addon_state.get("enabled", False)):
                continue
            info = agent.setdefault("agent", {})
            linux_user = str(info.get("linux_user", "")).strip()
            home = self._agent_linux_home(agent)
            if not linux_user or home is None or not home.exists():
                skipped_agents.append(token)
                continue
            if not self._can_manage_linux_user(linux_user):
                skipped_agents.append(token)
                continue
            linked = self._ensure_shared_addon_links(name, target_home=home, username=linux_user)
            linked_paths.extend(linked)
            addon_state["enabled"] = True
            addon_state["credential_mode"] = "shared"
            addon_state["last_applied_at"] = now_iso()
            addon_state["last_applied_paths"] = self._dedupe_paths(
                list(addon_state.get("last_applied_paths", [])) + linked
            )
            addon_state["last_source"] = str(shared_dir)
            addons[name] = addon_state
            agent["addons"] = addons
            info["last_sync"] = now_iso()
            updated_agents.append(token)
            changed = True
        if changed:
            self._event(
                state,
                "addons.applied",
                f"Applied shared addon {name}",
                {
                    "addon": name,
                    "agent_id": str(agent_id or ""),
                    "config_dir": str(shared_dir),
                    "updated_agents": updated_agents,
                    "linked_paths": self._dedupe_paths(linked_paths),
                },
            )
            self.store.write_state(state)
        return {
            "addon": name,
            "config_dir": str(shared_dir),
            "updated_agents": updated_agents,
            "skipped_agents": skipped_agents,
            "linked_paths": self._dedupe_paths(linked_paths),
        }

    def get_agent_addons(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            raise ValueError("addon management is only supported for managed agents")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        home = self._agent_linux_home(agent)
        rows: list[dict[str, Any]] = []
        addons = self._normalize_agent_addons(agent.get("addons"))
        for addon_name in addon_names():
            spec = get_addon(addon_name)
            addon_state = dict(addons.get(spec.name, {}))

            if isinstance(spec, ServiceAddonSpec):
                installed = check_display_installed(spec.check_executables)
                display_status = self.agent_display_status(token) if spec.name == "display" else {}
                row: dict[str, Any] = {
                    "addon": spec.name,
                    "label": spec.label,
                    "description": spec.description,
                    "enabled": bool(addon_state.get("enabled", False)),
                    "installed": installed,
                    "auth_status": "n/a",
                    "auth_detail": "service addon",
                    "applied": bool(addon_state.get("enabled", False)) and bool(addon_state.get("display_number")),
                    "access_status": "ok" if bool(addon_state.get("enabled", False)) else "",
                    "access_detail": "",
                    "target_path": "",
                    "last_applied_at": str(addon_state.get("last_applied_at", "")),
                    "last_revoked_at": str(addon_state.get("last_revoked_at", "")),
                }
                if display_status.get("enabled"):
                    row["display_number"] = display_status.get("display_number")
                    row["vnc_port"] = display_status.get("vnc_port")
                    row["novnc_port"] = display_status.get("novnc_port")
                    row["resolution"] = display_status.get("resolution")
                    row["display_status"] = display_status.get("status")
                rows.append(row)
                continue

            if isinstance(spec, ToolAddonSpec):
                tool_installed = all(shutil.which(exe) for exe in spec.check_executables) if spec.check_executables else True
                rows.append({
                    "addon": spec.name,
                    "label": spec.label,
                    "description": spec.description,
                    "enabled": bool(addon_state.get("enabled", False)),
                    "installed": tool_installed,
                    "auth_status": "n/a",
                    "auth_detail": "tool addon (no auth required)",
                    "applied": bool(addon_state.get("enabled", False)),
                    "access_status": "ok",
                    "access_detail": "",
                    "target_path": "",
                    "last_applied_at": str(addon_state.get("last_applied_at", "")),
                    "last_revoked_at": str(addon_state.get("last_revoked_at", "")),
                })
                continue

            shared_auth = self.shared_addon_auth_status(spec.name)
            link = self._agent_addon_link_status(spec.name, home, linux_user=info.get("linux_user", ""))
            rows.append(
                {
                    "addon": spec.name,
                    "label": spec.label,
                    "description": spec.description,
                    "enabled": bool(addon_state.get("enabled", False)),
                    "installed": bool(self._resolve_executable_in_service_env(spec.executable)),
                    "auth_status": str(shared_auth.get("auth_status", "unknown")),
                    "auth_detail": str(shared_auth.get("detail", "")),
                    "applied": bool(link.get("applied", False)),
                    "access_status": str(link.get("access_status", "unknown")),
                    "access_detail": str(link.get("access_detail", "")),
                    "target_path": str(link.get("target_path", "")),
                    "last_applied_at": str(addon_state.get("last_applied_at", "")),
                    "last_revoked_at": str(addon_state.get("last_revoked_at", "")),
                }
            )
        return {
            "agent_id": token,
            "linux_user": str(info.get("linux_user", "")),
            "home": str(home or ""),
            "addons": rows,
        }

    def enable_agent_addon(
        self,
        agent_id: str,
        addon: str,
        *,
        source_home: str | Path | None = None,
        source_agent: str | None = None,
        login_if_missing: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            raise ValueError("addon management is only supported for managed agents")
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)
        if isinstance(spec, ServiceAddonSpec):
            result = self.enable_agent_display(token)
            result["agent"] = self.get_dashboard_agent(token)
            return result
        if isinstance(spec, ToolAddonSpec):
            return self._enable_tool_addon(token, spec)
        self.ensure_addon_installed(spec.name)
        if source_home or source_agent:
            self.import_shared_addon_auth(spec.name, source_home=source_home, source_agent=source_agent)
        status = self.shared_addon_auth_status(spec.name)
        if str(status.get("auth_status", "")).strip().lower() != "ready":
            if login_if_missing:
                status = self.shared_addon_auth_login(spec.name)
            if str(status.get("auth_status", "")).strip().lower() != "ready":
                raise SetupError(
                    f"shared {spec.name} credentials are missing. Run 'clawie addon auth login {spec.name}' first."
                )

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(agent)
        addons = self._normalize_agent_addons(agent.get("addons"))
        addon_state = dict(addons.get(spec.name, {}))
        addon_state["enabled"] = True
        addon_state["credential_mode"] = "shared"
        linked: list[str] = []
        pending = True
        if linux_user and home is not None and home.exists():
            if not self._can_manage_linux_user(linux_user):
                raise SetupError(
                    "addon activation requires root when agent linux_user differs from current user. Re-run with sudo/root."
                )
            linked = self._ensure_shared_addon_links(spec.name, target_home=home, username=linux_user)
            addon_state["last_applied_at"] = now_iso()
            addon_state["last_applied_paths"] = self._dedupe_paths(
                list(addon_state.get("last_applied_paths", [])) + linked
            )
            addon_state["last_source"] = str(self._shared_addon_config_dir(spec.name))
            pending = False
            # ── Inject addon tools/env into agent home ──
            provider = str(info.get("provider", "")).strip().lower()
            if provider and (spec.tools_snippet or spec.env_exports):
                context = {"config_dir": str(home / spec.target_config_rel)}
                self._apply_addon_agent_integration(
                    spec.name,
                    provider=provider,
                    home=home,
                    linux_user=linux_user,
                    context=context,
                )
        addons[spec.name] = addon_state
        agent["addons"] = addons
        info["last_sync"] = now_iso()
        self._event(
            state,
            "addons.enabled",
            f"Enabled addon {spec.name} for {token}",
            {
                "agent_id": token,
                "addon": spec.name,
                "linked_paths": linked,
                "pending": pending,
            },
        )
        self.store.write_state(state)
        return {
            "agent": agent,
            "addon": spec.name,
            "linked_paths": linked,
            "pending": pending,
            "shared_auth": status,
        }

    def _enable_tool_addon(self, agent_id: str, spec: ToolAddonSpec) -> dict[str, Any]:
        """Enable a lightweight tool addon (no auth, no service — just TOOLS.md injection)."""
        for exe in spec.check_executables:
            if not shutil.which(exe):
                raise SetupError(f"required executable '{exe}' not found in PATH")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(agent)
        provider = str(info.get("provider", "")).strip().lower()
        addons = self._normalize_agent_addons(agent.get("addons"))
        addon_state = dict(addons.get(spec.name, {}))
        addon_state["enabled"] = True
        pending = True
        if provider and home is not None and spec.tools_snippet:
            context: dict[str, str] = {}
            try:
                self._apply_addon_agent_integration(
                    spec.name,
                    provider=provider,
                    home=home,
                    linux_user=linux_user,
                    context=context,
                )
                addon_state["last_applied_at"] = now_iso()
                pending = False
            except PermissionError:
                pass
        # Also inject into DB core_prompts so staging/future writes include it
        prompts = agent.setdefault("core_prompts", {})
        tools_md = str(prompts.get("TOOLS.md", ""))
        if spec.tools_snippet and spec.name not in tools_md:
            from clawie.addon_integration import inject_addon_tools_snippet
            prompts["TOOLS.md"] = inject_addon_tools_snippet(
                tools_md, spec.name, spec.tools_snippet,
            )
        addons[spec.name] = addon_state
        agent["addons"] = addons
        info["last_sync"] = now_iso()
        self._event(
            state,
            "addons.enabled",
            f"Enabled addon {spec.name} for {agent_id}",
            {"agent_id": agent_id, "addon": spec.name, "pending": pending},
        )
        self.store.write_state(state)
        return {"agent": agent, "addon": spec.name, "linked_paths": [], "pending": pending}

    def disable_agent_addon(self, agent_id: str, addon: str) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            raise ValueError("addon management is only supported for managed agents")
        name = self._canonical_addon(addon)
        if not name:
            raise ValueError("addon is required")
        spec = get_addon(name)
        if isinstance(spec, ServiceAddonSpec):
            result = self.disable_agent_display(token)
            result["agent"] = self.get_dashboard_agent(token)
            return result
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(agent)
        addons = self._normalize_agent_addons(agent.get("addons"))
        addon_state = dict(addons.get(spec.name, {}))
        removed: list[str] = []
        if linux_user and home is not None and home.exists() and self._can_manage_linux_user(linux_user):
            removed = self._revoke_addon_from_home(spec.name, home)
            # ── Remove addon tools/env from agent home ──
            provider = str(info.get("provider", "")).strip().lower()
            if provider and (spec.tools_snippet or spec.env_exports):
                self._remove_addon_agent_integration(
                    spec.name, provider=provider, home=home, linux_user=linux_user
                )
        addon_state["enabled"] = False
        addon_state["last_revoked_at"] = now_iso()
        addon_state["last_applied_paths"] = []
        addons[spec.name] = addon_state
        agent["addons"] = addons
        info["last_sync"] = now_iso()
        self._event(
            state,
            "addons.disabled",
            f"Disabled addon {spec.name} for {token}",
            {
                "agent_id": token,
                "addon": spec.name,
                "removed_paths": removed,
            },
        )
        self.store.write_state(state)
        return {
            "agent": agent,
            "addon": spec.name,
            "removed_paths": removed,
        }

    def apply_agent_addons(self, agent_id: str, addons: list[str] | None = None) -> dict[str, Any]:
        self._require_setup()
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            raise ValueError("addon management is only supported for managed agents")
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(agent)
        if not linux_user or home is None or not home.exists():
            raise SetupError(f"agent '{token}' has no linux_user home to apply addons to")
        self._assert_linux_user_manageable(linux_user, "addon apply")
        selected = [self._canonical_addon(item) for item in (addons or self._enabled_agent_addons(agent))]
        selected = [item for item in selected if item]
        if not selected:
            raise ValueError("no enabled addons selected")
        updated: list[str] = []
        addon_state_map = self._normalize_agent_addons(agent.get("addons"))
        for name in selected:
            if is_service_addon(name):
                continue
            if str(self.shared_addon_auth_status(name).get("auth_status", "")).strip().lower() != "ready":
                raise SetupError(
                    f"shared {name} credentials are missing. Run 'clawie addon auth login {name}' first."
                )
            linked = self._ensure_shared_addon_links(name, target_home=home, username=linux_user)
            updated.extend(linked)
            addon_state = dict(addon_state_map.get(name, {}))
            addon_state["enabled"] = True
            addon_state["credential_mode"] = "shared"
            addon_state["last_applied_at"] = now_iso()
            addon_state["last_applied_paths"] = self._dedupe_paths(
                list(addon_state.get("last_applied_paths", [])) + linked
            )
            addon_state["last_source"] = str(self._shared_addon_config_dir(name))
            addon_state_map[name] = addon_state
        agent["addons"] = addon_state_map
        info["last_sync"] = now_iso()
        self._event(
            state,
            "addons.reapplied",
            f"Applied addons for {token}",
            {
                "agent_id": token,
                "addons": selected,
                "linked_paths": self._dedupe_paths(updated),
            },
        )
        self.store.write_state(state)
        return {
            "agent": agent,
            "addons": selected,
            "linked_paths": self._dedupe_paths(updated),
        }

    def _shared_addon_agent_ids(self, addon: str) -> list[str]:
        name = self._canonical_addon(addon)
        rows: list[str] = []
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        for aid, agent in sorted(agents.items()):
            self._hydrate_agent_controls(agent)
            addons = self._normalize_agent_addons(agent.get("addons"))
            if bool(addons.get(name, {}).get("enabled", False)):
                rows.append(str(aid))
        return rows

    def _resolve_addon_source(
        self,
        *,
        source_home: str | Path | None,
        source_agent: str | None,
    ) -> tuple[Path, str, str]:
        if source_home and source_agent:
            raise ValueError("use either source_home or source_agent, not both")
        if source_agent:
            token = str(source_agent).strip()
            if token.startswith("@local:"):
                raise ValueError("source_agent must be a managed agent")
            state = self.store.read_state()
            agents = state.setdefault("agents", state.get("users", {}))
            agent = agents.get(token)
            if not agent:
                raise AgentNotFoundError(f"agent not found: {token}")
            self._hydrate_agent_controls(agent)
            linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
            if linux_user:
                self._require_linux_user_access(linux_user, "addon credential import")
            home = self._agent_linux_home(agent)
            if home is None or not home.exists():
                raise SetupError(f"agent '{token}' has no linux_user home to import addon credentials from")
            return (home, linux_user, token)
        src_home = Path(source_home).expanduser() if source_home else self._default_source_home()
        if not src_home.exists():
            raise FileNotFoundError(f"source home not found: {src_home}")
        return (src_home, "", str(src_home))

    def _addon_shell_command(
        self,
        addon: str,
        command: tuple[str, ...],
        *,
        linux_user: str,
        config_dir: Path,
    ) -> list[str]:
        spec = get_addon(addon)
        env_bits: list[str] = []
        if spec.config_dir_env:
            env_bits.append(f'export {spec.config_dir_env}={shlex.quote(str(config_dir))}')
        env_bits.append(
            "exec " + " ".join([shlex.quote(spec.executable), *[shlex.quote(part) for part in command]])
        )
        script = "; ".join(env_bits)
        return self._user_shell_command(linux_user, script)

    def _materialize_shared_addon_credentials(
        self,
        addon: str,
        *,
        source_config_dir: Path,
        linux_user: str,
    ) -> list[str]:
        spec = get_addon(addon)
        if spec.name != "gws":
            return []
        target_dir = self._ensure_shared_addon_config_dir(spec.name)
        target = target_dir / "credentials.json"
        try:
            result = subprocess.run(
                self._addon_shell_command(
                    spec.name,
                    spec.auth_export_command,
                    linux_user=linux_user,
                    config_dir=source_config_dir,
                ),
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(linux_user),
            )
        except Exception:
            result = None
        if result is None or result.returncode != 0 or not str(result.stdout).strip():
            copied = self._copy_if_present(source_config_dir / "credentials.json", target)
            return [str(target)] if copied else []
        payload = parse_gws_exported_credentials(result.stdout)
        self._write_json_file(target, payload)
        self._relax_shared_addon_permissions(spec.name)
        return [str(target)]

    def _replace_tree(self, src: Path, dst: Path) -> list[str]:
        if dst.exists() or dst.is_symlink():
            if dst.is_symlink() or dst.is_file():
                dst.unlink(missing_ok=True)
            else:
                shutil.rmtree(dst)
        shutil.copytree(src, dst)
        self._relax_path_tree_permissions(dst)
        return [str(dst)]

    def _ensure_shared_addon_links(self, addon: str, *, target_home: Path, username: str) -> list[str]:
        spec = get_addon(addon)
        src = self._shared_addon_config_dir(spec.name)
        if not src.exists():
            return []
        target = target_home / spec.target_config_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        self._chown_tree(target.parent, username)
        if target.is_symlink():
            try:
                if target.resolve() == src.resolve():
                    return [str(target)]
            except OSError:
                pass
            target.unlink(missing_ok=True)
        elif target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.symlink_to(src, target_is_directory=True)
        self._chown_path(target, username)
        return [str(target)]

    def _revoke_addon_from_home(self, addon: str, target_home: Path) -> list[str]:
        spec = get_addon(addon)
        target = target_home / spec.target_config_rel
        if not target.exists() and not target.is_symlink():
            return []
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        else:
            shutil.rmtree(target)
        return [str(target)]

    def _agent_addon_link_status(self, addon: str, home: Path | None, *, linux_user: str = "") -> dict[str, Any]:
        spec = get_addon(addon)
        if isinstance(spec, (ServiceAddonSpec, ToolAddonSpec)):
            return {
                "applied": True,
                "access_status": "ok",
                "access_detail": "",
                "target_path": "",
            }
        target = (home / spec.target_config_rel) if home else Path(spec.target_config_rel)
        if home is None:
            return {
                "applied": False,
                "access_status": "missing",
                "access_detail": "agent has no Linux home",
                "target_path": "",
            }
        if linux_user and not self._can_manage_linux_user(str(linux_user)):
            return {
                "applied": False,
                "access_status": "permission",
                "access_detail": "inspecting addon links for managed agents owned by another Linux user requires root",
                "target_path": str(target),
            }
        applied = False
        access_status = "ok"
        access_detail = ""
        try:
            present = target.exists() or target.is_symlink()
        except PermissionError:
            return {
                "applied": False,
                "access_status": "permission",
                "access_detail": "inspecting addon link paths requires root for this agent home",
                "target_path": str(target),
            }
        except OSError as exc:
            return {
                "applied": False,
                "access_status": "error",
                "access_detail": str(exc),
                "target_path": str(target),
            }
        if present:
            if target.is_symlink():
                try:
                    applied = target.resolve() == self._shared_addon_config_dir(spec.name).resolve()
                except PermissionError:
                    access_status = "permission"
                    access_detail = "resolving addon link target requires root for this agent home"
                    applied = False
                except OSError as exc:
                    access_status = "error"
                    access_detail = str(exc)
                    applied = False
            else:
                applied = True
        return {
            "applied": applied,
            "access_status": access_status,
            "access_detail": access_detail,
            "target_path": str(target),
        }

    def local_claw_service_action(self, provider: str, action: str) -> dict[str, Any]:
        self._require_setup()
        name = str(provider).strip().lower()
        command = str(action).strip().lower()
        if not name:
            raise ValueError("provider is required")
        if command not in {"start", "stop", "restart", "status"}:
            raise ValueError("action must be one of: start, stop, restart, status")
        if command in {"start", "restart"}:
            self.ensure_provider_runtime(name)

        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        local_info = local_state.setdefault(name, {})
        linux_user = self._local_linux_user_hint(name, self._local_target_user())
        if command == "status":
            unit_status = self._systemd_user_service_status(name, linux_user)
            if unit_status != "unknown":
                local_info["service_status"] = unit_status
                local_info["service_mode"] = "systemd"
                config["local_service_state"] = local_state
                self.store.write_config(config)
                return {
                    "provider": name,
                    "action": command,
                    "service_status": unit_status,
                    "service_mode": "systemd",
                    "output": f"systemctl user unit state: {unit_status}",
                }
        else:
            managed = self._systemd_user_service_manage(name, command, linux_user)
            if managed.get("ok", False):
                status = "running" if command in {"start", "restart"} else "stopped"
                local_info["service_status"] = status
                local_info["service_mode"] = "systemd"
                config["local_service_state"] = local_state
                self.store.write_config(config)
                return {
                    "provider": name,
                    "action": command,
                    "service_status": status,
                    "service_mode": "systemd",
                    "output": str(managed.get("output", "")),
                }
        try:
            probe = self._run_local_provider_command(name, command, linux_user)
            cmd = probe["command"]
            result = probe["result"]
            output = probe["output"]
        except Exception as exc:
            if command != "status":
                raise
            status = self._best_effort_local_status(local_info, linux_user)
            local_info["service_status"] = status
            local_info["service_mode"] = "fallback"
            config["local_service_state"] = local_state
            self.store.write_config(config)
            return {
                "provider": name,
                "action": command,
                "service_status": status,
                "service_mode": "fallback",
                "output": str(exc),
            }

        if result.returncode != 0 and "failed to connect to bus" in output.lower():
            fallback = self._fallback_service_action(
                provider=name,
                action=command,
                linux_user=linux_user,
                executable=cmd[0],
                agent_info=local_info,
            )
            local_info["service_status"] = str(fallback.get("service_status", "unknown"))
            local_info["service_mode"] = "fallback"
            config["local_service_state"] = local_state
            self.store.write_config(config)
            return {
                "provider": name,
                "action": command,
                "service_status": local_info["service_status"],
                "service_mode": "fallback",
                "output": str(fallback.get("output", "")),
            }

        if result.returncode != 0 and command == "status":
            status = self._best_effort_local_status(local_info, linux_user)
            local_info["service_status"] = status
            local_info["service_mode"] = "fallback"
            config["local_service_state"] = local_state
            self.store.write_config(config)
            return {
                "provider": name,
                "action": command,
                "service_status": status,
                "service_mode": "fallback",
                "output": output,
            }

        if result.returncode != 0:
            raise SetupError(
                f"{name} service {command} failed: " + (output or f"exit {result.returncode}")
            )

        if command == "start":
            status = "running"
            mode = "systemd"
        elif command == "stop":
            status = "stopped"
            mode = "systemd"
        elif command == "restart":
            status = "running"
            mode = "systemd"
        else:
            inferred = self._infer_service_status(output)
            if inferred == "unknown":
                status = self._best_effort_local_status(local_info, linux_user)
                mode = "fallback"
            else:
                status = inferred
                mode = "systemd"
        local_info["service_status"] = status
        local_info["service_mode"] = mode
        config["local_service_state"] = local_state
        self.store.write_config(config)
        return {
            "provider": name,
            "action": command,
            "service_status": status,
            "service_mode": local_info["service_mode"],
            "output": output,
        }

    def list_local_runtime_statuses(self, refresh: bool = True) -> list[dict[str, Any]]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        installed = self.list_installed_claws()
        user_hints: dict[str, str] = {}
        providers: list[str] = []
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            providers.append(provider)
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                user_hints[provider] = hint
        if refresh and providers:
            local_state = self._refresh_local_service_statuses(providers, local_state, user_hints=user_hints)
            config = self.store.read_config()
            local_state = self._normalized_local_service_state(config)

        rows: list[dict[str, Any]] = []
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            info = dict(local_state.get(provider, {}))
            auth = self.local_claw_auth_status(provider)
            rows.append(
                {
                    "provider": provider,
                    "linux_user": str(info.get("linux_user", auth.get("linux_user", ""))),
                    "service_status": str(info.get("service_status", "unknown")),
                    "service_mode": str(info.get("service_mode", "unknown")),
                    "root": str(claw.get("root", "")),
                    "markers": list(claw.get("markers", [])),
                    "auth_mode": str(auth.get("auth_mode", "")),
                    "auth_status": str(auth.get("auth_status", "unknown")),
                    "auth_profile": str(auth.get("auth_profile", "")),
                    "account": str(auth.get("account", "")),
                    "expires_at": str(auth.get("expires_at", "")),
                    "login_required": bool(auth.get("login_required", False)),
                    "source": str(auth.get("source", "")),
                    "detail": str(auth.get("detail", "")),
                }
            )
        return sorted(rows, key=lambda row: str(row.get("provider", "")))

    def local_claw_auth_status(self, provider: str) -> dict[str, Any]:
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        target = self._resolve_local_runtime_target(name)
        auth_mode = str(self._provider_auth(spec.name).get("auth_mode", spec.default_auth_mode))
        payload = self._inspect_provider_auth_state(
            provider=spec.name,
            auth_mode=auth_mode,
            linux_user=str(target.get("linux_user", "")),
            home=self._path_or_none(target.get("home")),
        )
        payload.update(
            {
                "provider": spec.name,
                "linux_user": str(target.get("linux_user", "")),
                "home": str(target.get("home", "")),
                "root": str(target.get("root", "")),
                "local_user": True,
            }
        )
        return payload

    def local_claw_auth_login(self, provider: str) -> dict[str, Any]:
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        target = self._resolve_local_runtime_target(name)
        auth_mode = str(self._provider_auth(spec.name).get("auth_mode", spec.default_auth_mode))
        payload = self._refresh_or_login_linked_auth(
            provider=spec.name,
            auth_mode=auth_mode,
            linux_user=str(target.get("linux_user", "")),
            home=self._path_or_none(target.get("home")),
        )
        payload.update(
            {
                "provider": spec.name,
                "linux_user": str(target.get("linux_user", "")),
                "home": str(target.get("home", "")),
                "root": str(target.get("root", "")),
                "local_user": True,
            }
        )
        return payload

    def agent_auth_status(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            payload = self.local_claw_auth_status(token.split(":", 1)[1])
            payload["agent_id"] = token
            return payload
        self._refresh_managed_agent_provider_alignment(token)

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{token}' has no provider configured")
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(agent)
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        shared_provider_auth = bool(sync.get("shared_provider_auth", False))
        auth = self._preferred_agent_provider_auth(
            provider,
            agent=agent,
            current_auth_mode=str(info.get("auth_mode", "")),
            allow_defaults=True,
        )
        auth_mode = str(auth.get("auth_mode", get_provider(provider).default_auth_mode))
        inspect_linux_user = "" if shared_provider_auth else linux_user
        inspect_home = self._shared_provider_auth_home() if shared_provider_auth else home
        payload = self._inspect_provider_auth_state(
            provider=provider,
            auth_mode=auth_mode,
            linux_user=inspect_linux_user,
            home=inspect_home,
        )
        payload.update(
            {
                "agent_id": token,
                "linux_user": linux_user,
                "home": str(inspect_home or ""),
                "shared_provider_auth": shared_provider_auth,
                "local_user": False,
            }
        )
        return payload

    def agent_auth_login(self, agent_id: str) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            payload = self.local_claw_auth_login(token.split(":", 1)[1])
            payload["agent_id"] = token
            return payload
        self._refresh_managed_agent_provider_alignment(token)

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        agent = agents.get(token)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {token}")
        self._hydrate_agent_controls(agent)
        info = agent.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        if not provider:
            raise SetupError(f"agent '{token}' has no provider configured")
        linux_user = str(info.get("linux_user", "")).strip()
        home = self._agent_linux_home(agent)
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        shared_provider_auth = bool(sync.get("shared_provider_auth", False))
        if shared_provider_auth:
            payload = self.shared_auth_login(provider)
        else:
            payload = self._refresh_or_login_linked_auth(
                provider=provider,
                auth_mode=str(info.get("auth_mode", get_provider(provider).default_auth_mode)),
                linux_user=linux_user,
                home=home,
            )
        payload.update(
            {
                "agent_id": token,
                "linux_user": linux_user,
                "home": str(self._shared_provider_auth_home() if shared_provider_auth else (home or "")),
                "shared_provider_auth": shared_provider_auth,
                "local_user": False,
            }
        )
        return payload

    def dashboard_snapshot(self, agent_id: str | None = None) -> dict[str, Any]:
        return self.performance_snapshot(agent_id=agent_id, refresh=True)

    def performance_snapshot(
        self,
        agent_id: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if refresh:
            self.collect_metrics(agent_id=agent_id)
        daemon_map = self._running_provider_daemons_by_user()
        self._refresh_managed_agent_provider_alignments(agent_id=agent_id, daemon_map=daemon_map)
        state = self.store.read_state()
        agents = list(state.setdefault("agents", state.get("users", {})).values())
        if agent_id:
            agents = [
                row
                for row in agents
                if row.get("agent_id", row.get("user_id", "")) == agent_id
            ]
        latest_metrics = self.store.latest_metrics(limit_per_user=1)

        rows: list[dict[str, Any]] = []
        channel_total = 0
        migrated_total = 0
        cpu_total = 0.0
        mem_total = 0.0
        for agent_state in sorted(
            agents,
            key=lambda row: row.get("agent_id", row.get("user_id", "")),
        ):
            self._hydrate_agent_controls(agent_state)
            channel_view = self._attach_agent_channel_view(copy.deepcopy(agent_state))
            channel_view = self._attach_agent_runtime_status(channel_view, daemon_map=daemon_map)
            agent_info = channel_view.get("agent", {})
            channels = channel_view.get("channels", [])
            active_channels = sum(1 for channel in channels if bool(channel.get("enabled", True)))
            migrated_count = sum(1 for row in channels if row.get("migrated_from"))
            channel_total += len(channels)
            migrated_total += migrated_count
            current_id = str(agent_state.get("agent_id", agent_state.get("user_id", "")))
            metric = (latest_metrics.get(current_id, [{}]) or [{}])[0]
            cpu = float(metric.get("cpu_percent", 0.0))
            mem = float(metric.get("mem_percent", 0.0))
            rss = int(metric.get("rss_kb", 0))
            metric_status = str(metric.get("status", "")).strip()
            live_pid = int(agent_info.get("live_pid", 0) or 0)
            if live_pid > 0 and (metric_status in {"", "offline", "stopped", "unknown"} or rss <= 0):
                probe = self._probe_process(live_pid)
                if probe is not None:
                    cpu = float(probe["cpu_percent"])
                    mem = float(probe["mem_percent"])
                    rss = int(probe["rss_kb"])
                    metric_status = "running"
            sampled_status = self._dashboard_status(metric_status, agent_info)
            cpu_total += cpu
            mem_total += mem
            rows.append(
                {
                    "agent_id": current_id,
                    "display_name": agent_state.get("display_name", ""),
                    "status": sampled_status,
                    "version": agent_info.get("version", ""),
                    "provider": agent_info.get("provider", ""),
                    "provider_status": agent_info.get("provider_status", "ok"),
                    "provider_issue": agent_info.get("provider_issue", ""),
                    "provider_remediation": agent_info.get("provider_remediation", ""),
                    "strategy": agent_state.get("channel_strategy", ""),
                    "channels": active_channels,
                    "channels_total": len(channels),
                    "migrated": migrated_count,
                    "last_sync": agent_info.get("last_sync", ""),
                    "pid": live_pid or int(agent_info.get("pid") or 0),
                    "cpu_percent": cpu,
                    "mem_percent": mem,
                    "rss_kb": rss,
                }
            )

        for local_agent in self._local_dashboard_rows(refresh=refresh):
            if agent_id and local_agent["agent_id"] != agent_id:
                continue
            rows.append(local_agent)

        config = self.store.read_config()
        return {
            "generated_at": now_iso(),
            "workspace": config.get("workspace", ""),
            "provider": config.get("provider", "openclaw"),
            "totals": {
                "agents": len(rows),
                "channels": channel_total,
                "migrated_channels": migrated_total,
                "cpu_percent": round(cpu_total, 2),
                "mem_percent": round(mem_total, 2),
            },
            "rows": rows,
            "events": self.list_events(limit=8),
        }

    def collect_metrics(self, agent_id: str | None = None) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        sampled = 0
        for aid, agent_state in agents.items():
            if agent_id and aid != agent_id:
                continue
            agent = agent_state.setdefault("agent", {})
            pid = int(agent.get("pid") or 0)
            status = "offline"
            cpu_percent = 0.0
            mem_percent = 0.0
            rss_kb = 0
            if pid > 0:
                probe = self._probe_process(pid)
                if probe is not None:
                    cpu_percent = float(probe["cpu_percent"])
                    mem_percent = float(probe["mem_percent"])
                    rss_kb = int(probe["rss_kb"])
                    status = "running"
                else:
                    agent["pid"] = 0

            agent["status"] = status
            agent["last_sync"] = now_iso()
            self.store.write_metric(
                timestamp=now_iso(),
                user_id=aid,
                cpu_percent=cpu_percent,
                mem_percent=mem_percent,
                rss_kb=rss_kb,
                status=status,
            )
            sampled += 1
        self.store.write_state(state)
        return {"sampled": sampled}

    def spawn_linux_user(
        self,
        agent_id: str,
        linux_user: str | None = None,
        copy_configs: bool = True,
        source_home: str | Path | None = None,
        template: str = "baseline",
        agent_version: str = "1.0.0",
        provider: str | None = None,
        password: str | None = None,
        password_hash: str | None = None,
        use_global_password: bool = True,
        clone_from_agent: str | None = None,
        credential_bundles: list[str] | None = None,
        include_default_credentials: bool = True,
        plugin_overrides: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        self._require_setup()
        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        if agent_id in agents:
            raise AgentExistsError(f"agent already exists: {agent_id}")
        if template not in state.get("templates", {}):
            raise ValueError(f"template not found: {template}")

        target_user = (linux_user or agent_id).strip()
        self._validate_linux_username(target_user)
        if os.geteuid() != 0:
            raise SetupError(
                "spawn requires root privileges. Re-run with sudo/root to create Linux users."
            )

        config = self.store.read_config()
        resolved_provider = str(provider or config.get("provider", "openclaw")).strip().lower() or "openclaw"
        self.ensure_provider_runtime(resolved_provider)

        target_home = self._linux_home_for_user(target_user) or (Path("/home") / target_user)
        if self._linux_user_exists(target_user):
            raise AgentExistsError(f"linux user already exists: {target_user}")

        spawn_shell = self._spawn_user_shell()
        subprocess.run(["useradd", "-m", "-s", spawn_shell, target_user], check=True)
        password_source, password_value = self._apply_spawn_password(
            username=target_user,
            password=password,
            password_hash=password_hash,
            use_global_password=use_global_password,
        )
        ssh_login_disabled = self._disable_ssh_login_for_user(target_user)

        if source_home:
            src_home = Path(source_home).expanduser()
        else:
            src_home = self._default_source_home()
        system_prepared = self._ensure_system_shared_runtime(src_home)
        selected_credential_bundles = self._ordered_credential_bundles(
            self._normalize_credential_bundles(
                credential_bundles,
                include_defaults=include_default_credentials,
            )
        )
        clone_token = str(clone_from_agent or "").strip()
        cloned_prompts: dict[str, str] = {}
        cloned_channels: list[dict[str, str]] = []
        clone_source_agent: str | None = None
        if clone_token:
            source_payload = self.get_dashboard_agent(clone_token)
            source_provider = str(source_payload.get("agent", {}).get("provider", "")).strip().lower()
            if source_provider != resolved_provider:
                raise ValueError("spawn clone source provider must match target provider")
            clone_source_agent = clone_token if not clone_token.startswith("@local:") else None
            cloned_channels = copy.deepcopy(source_payload.get("channels", []))
            cloned_prompts = self._normalize_core_prompts(
                resolved_provider,
                source_payload.get("core_prompts", {}),
            )
        else:
            cloned_channels = self._discover_channels_from_source_home(src_home, resolved_provider)
            cloned_prompts = self._normalize_core_prompts(
                resolved_provider,
                self._read_core_prompts_from_home(resolved_provider, src_home),
            )

        copied = self._copy_user_configs(src_home, target_home, target_user, enabled=copy_configs)
        if copy_configs:
            copied += self._sync_selected_credential_bundles(
                source_home=src_home,
                target_home=target_home,
                username=target_user,
                requested_provider=resolved_provider,
                bundles=selected_credential_bundles,
            )
        for path in self._ensure_shared_toolchain_shell_init(target_home, target_user):
            if path not in copied:
                copied.append(path)
        for path in self._ensure_shared_claude_links(target_home, target_user):
            if path not in copied:
                copied.append(path)
        for path in system_prepared:
            if path not in copied:
                copied.append(path)
        agent_state = self.create_agent(
            agent_id=agent_id,
            display_name=agent_id,
            template=template,
            clone_from=clone_source_agent,
            channel_strategy="migrate" if cloned_channels else "new",
            channels=cloned_channels or None,
            agent_version=agent_version,
            provider=resolved_provider,
            core_prompts=cloned_prompts,
            plugin_overrides=plugin_overrides,
        )
        agent_state["agent"]["linux_user"] = target_user
        agent_state["agent"]["manager_user"] = self._current_linux_user()
        agent_state["agent"]["ssh_login_disabled"] = bool(ssh_login_disabled)
        agent_state["agent"]["login_shell"] = spawn_shell
        credential_sync = self._normalize_credential_sync_state({}, default_when_missing=False)
        credential_sync["bundles"] = selected_credential_bundles
        credential_sync["last_source_home"] = str(src_home)
        credential_sync["shared_provider_auth"] = copy_configs and "provider-auth" in set(selected_credential_bundles)
        if copy_configs:
            credential_sync["last_synced_at"] = now_iso()
            credential_sync["last_synced_paths"] = list(copied)
        agent_state["credential_sync"] = credential_sync
        if target_home.exists():
            try:
                self._write_prompt_files_for_home(
                    resolved_provider,
                    target_home,
                    agent_state.get("core_prompts", {}),
                    target_user,
                )
            except PermissionError:
                pass
        self._ensure_workspace_accessible(resolved_provider, target_home, target_user)
        state = self.store.read_state()
        self._event(
            state,
            "agents.spawned",
            f"Spawned linux user {target_user} for {agent_id}",
            {
                "agent_id": agent_id,
                "linux_user": target_user,
                "copied": copied,
                "detected_providers": [
                    row["provider"] for row in self.list_installed_claws(source_home=src_home)
                ],
                "imported_channels": len(cloned_channels),
                "clone_from_agent": clone_from_agent or "",
                "credential_bundles": selected_credential_bundles,
                "password_source": password_source,
                "ssh_login_disabled": bool(ssh_login_disabled),
            },
        )
        agents = state.setdefault("agents", state.get("users", {}))
        agents[agent_id] = agent_state
        self.store.write_state(state)
        return {
            "agent": agent_state,
            "linux_user": target_user,
            "copied_paths": copied,
            "password_source": password_source,
            "password_value": password_value,
            "ssh_login_disabled": bool(ssh_login_disabled),
            "credential_bundles": selected_credential_bundles,
        }

    def batch_create_agents(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        results = {"created": [], "errors": []}
        for entry in entries:
            agent_id = str(entry.get("agent_id", entry.get("user_id", ""))).strip()
            if not agent_id:
                results["errors"].append({
                    "agent_id": "",
                    "error": "entry missing agent_id",
                })
                continue
            try:
                agent_state = self.create_agent(
                    agent_id=agent_id,
                    display_name=entry.get("display_name"),
                    template=str(entry.get("template", "baseline")),
                    clone_from=entry.get("clone_from"),
                    channel_strategy=str(entry.get("channel_strategy", "new")),
                    channels=entry.get("channels"),
                    agent_version=str(entry.get("agent_version", "1.0.0")),
                    provider=entry.get("provider"),
                    core_prompts=entry.get("core_prompts"),
                )
                results["created"].append(agent_state["agent_id"])
            except Exception as exc:  # noqa: BLE001
                results["errors"].append({"agent_id": agent_id, "error": str(exc)})
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
            merged_state.setdefault("agents", merged_state.get("users", {}))
            merged_state.setdefault("events", [])
            merged_state["templates"].update(state.get("templates", {}))
            merged_state["agents"].update(state.get("agents", state.get("users", {})))
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
        provider = str(config.get("provider", "openclaw")).strip().lower() or "openclaw"
        credentials = self._provider_auth(provider)
        if not self._is_provider_configured(provider, credentials):
            raise SetupError("setup is incomplete. Run 'clawie setup'.")

    def _resolve_auth_mode(self, provider: str, api_key: str, auth_mode: str | None) -> str:
        spec = get_provider(provider)
        if auth_mode:
            mode = auth_mode.strip().lower()
            if not spec.supports_auth_mode(mode):
                allowed = ", ".join(spec.auth_modes)
                raise ValueError(f"auth mode for {provider} must be one of: {allowed}")
        elif api_key:
            mode = "api_key"
        else:
            mode = spec.default_auth_mode

        if mode == "api_key" and not api_key:
            raise ValueError("API key is required when --auth-mode api_key is selected")
        return mode

    def _normalized_provider_credentials(self, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        payload = config.get("provider_credentials", {})
        if not isinstance(payload, dict):
            payload = {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            normalized[str(key).strip().lower()] = dict(value)
        return normalized

    def _provider_auth(self, provider: str) -> dict[str, Any]:
        spec = get_provider(provider)
        config = self.store.read_config()
        credentials = self._normalized_provider_credentials(config)
        provider_auth = dict(credentials.get(spec.name, {}))
        if not provider_auth:
            if str(config.get("provider", "")).strip().lower() == spec.name:
                provider_auth["auth_mode"] = str(config.get("auth_mode") or spec.default_auth_mode)
                api_key = str(config.get("api_key", "")).strip()
                if api_key:
                    provider_auth["api_key"] = api_key
        return provider_auth

    def _effective_provider_auth(self, provider: str, *, allow_defaults: bool) -> dict[str, Any]:
        spec = get_provider(provider)
        auth = self._provider_auth(spec.name)
        if allow_defaults and not str(auth.get("auth_mode", "")).strip():
            auth["auth_mode"] = spec.default_auth_mode
        return auth

    def _agent_prefers_shared_provider_auth(self, agent: dict[str, Any]) -> bool:
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        bundles = {str(item).strip() for item in sync.get("bundles", [])}
        return bool(sync.get("shared_provider_auth", False) or "provider-auth" in bundles)

    def _shared_linked_auth_status(self, provider: str) -> dict[str, Any]:
        return self._inspect_provider_auth_state(
            provider=provider,
            auth_mode="linked",
            linux_user="",
            home=self._ensure_shared_provider_auth_root(),
        )

    def _shared_linked_auth_available(self, provider: str) -> bool:
        try:
            payload = self._shared_linked_auth_status(provider)
        except Exception:
            return False
        status = str(payload.get("auth_status", "")).strip().lower()
        return status in {"ready", "expired"}

    def _shared_linked_auth_ready(self, provider: str) -> bool:
        try:
            payload = self._shared_linked_auth_status(provider)
        except Exception:
            return False
        return str(payload.get("auth_status", "")).strip().lower() == "ready"

    @staticmethod
    def _auth_status_ready(status: dict[str, Any]) -> bool:
        return str(status.get("auth_status", "")).strip().lower() == "ready"

    @staticmethod
    def _auth_status_usable(status: dict[str, Any]) -> bool:
        return str(status.get("auth_status", "")).strip().lower() in {"ready", "expired"}

    def _source_home_has_codex_auth(self, source_home: Path) -> bool:
        try:
            load_codex_auth(source_home)
        except Exception:
            return False
        return True

    def _source_home_has_provider_auth(self, provider: str, source_home: Path) -> bool:
        spec = get_provider(provider)
        for rel in spec.shared_auth_paths:
            if self._path_exists(source_home / rel):
                return True
        return False

    def _prepare_linked_auth_for_provider_switch(
        self,
        *,
        provider: str,
        agent: dict[str, Any],
    ) -> dict[str, Any]:
        spec = get_provider(provider)
        result = {
            "provider": spec.name,
            "required": False,
            "prepared": False,
            "action": "",
            "source": "",
            "source_home": "",
            "auth": {},
        }
        sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
        if (
            # Fail-fast auth preparation only applies to agents that consume the
            # shared provider-auth store (shared_provider_auth flag). Agents that
            # merely have the default provider-auth bundle selected keep their
            # own auth and must not be blocked on shared linked auth.
            not bool(sync.get("shared_provider_auth", False))
            or not spec.supports_auth_mode("linked")
        ):
            return result

        result["required"] = True
        status = self.shared_auth_status(spec.name)
        result["auth"] = status
        if self._auth_status_ready(status):
            return result
        if self._shared_linked_auth_available(spec.name):
            # Shared linked auth material exists on disk (possibly expired):
            # nothing to import. Cutover repair/refresh handles staleness.
            return result

        source_home = self._default_source_home()
        result["source_home"] = str(source_home)
        last_error = ""

        if self._source_home_has_codex_auth(source_home):
            try:
                imported = self.import_shared_auth(spec.name, source="codex", source_home=source_home)
                status = dict(imported.get("auth", {}))
                result.update(
                    {
                        "prepared": True,
                        "action": "import",
                        "source": "codex",
                        "auth": status,
                    }
                )
                if self._auth_status_ready(status):
                    return result
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        if not self._auth_status_ready(status) and self._source_home_has_provider_auth(spec.name, source_home):
            try:
                imported = self.import_shared_auth(spec.name, source="provider", source_home=source_home)
                status = dict(imported.get("auth", {}))
                result.update(
                    {
                        "prepared": True,
                        "action": "import",
                        "source": "provider",
                        "auth": status,
                    }
                )
                if self._auth_status_ready(status):
                    return result
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        if spec.name != "openclaw" and not self._auth_status_ready(status):
            try:
                logged_in = self.shared_auth_login(spec.name)
                status = dict(logged_in)
                result.update(
                    {
                        "prepared": True,
                        "action": "login",
                        "source": "shared",
                        "auth": status,
                    }
                )
                if self._auth_status_ready(status):
                    return result
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        result["auth"] = status
        auth_status = str(status.get("auth_status", "")).strip().lower() or "missing"
        if self._source_home_has_codex_auth(source_home):
            raise SetupError(
                f"linked auth for {spec.name} is {auth_status} after importing Codex auth from {source_home}. "
                "Refresh the Codex session first, then retry the provider switch."
            )
        if self._source_home_has_provider_auth(spec.name, source_home):
            raise SetupError(
                f"linked auth for {spec.name} is {auth_status} after importing provider auth from {source_home}. "
                f"Refresh that source session first, then retry the provider switch."
            )
        if last_error:
            raise SetupError(
                f"linked auth for {spec.name} is unavailable and automatic login/import failed: {last_error}"
            )
        raise SetupError(
            f"linked auth for {spec.name} is missing. "
            f"Sign in to Codex first or run 'clawie auth login {spec.name}', then retry the provider switch."
        )

    def _preferred_shared_provider_auth(
        self,
        provider: str,
        *,
        allow_defaults: bool,
    ) -> dict[str, Any]:
        spec = get_provider(provider)
        auth = self._provider_auth(spec.name)
        explicit_mode = str(auth.get("auth_mode", "")).strip().lower()
        if explicit_mode:
            if explicit_mode == "none" and spec.supports_auth_mode("linked") and self._shared_linked_auth_available(spec.name):
                auth["auth_mode"] = "linked"
            return auth

        if spec.supports_auth_mode("linked") and self._shared_linked_auth_available(spec.name):
            auth["auth_mode"] = "linked"
            return auth

        if allow_defaults:
            auth["auth_mode"] = spec.default_auth_mode
        return auth

    def _preferred_agent_provider_auth(
        self,
        provider: str,
        *,
        agent: dict[str, Any] | None = None,
        current_auth_mode: str = "",
        allow_defaults: bool,
    ) -> dict[str, Any]:
        spec = get_provider(provider)
        auth = self._provider_auth(spec.name)
        current_mode = str(current_auth_mode).strip().lower()
        explicit_mode = str(auth.get("auth_mode", "")).strip().lower()
        if explicit_mode:
            if (
                explicit_mode == "none"
                and agent is not None
                and spec.supports_auth_mode("linked")
                and (
                    # An agent whose own record says "linked" keeps linked auth
                    # (it may hold private linked auth in its home), even when
                    # the shared store has nothing for this provider.
                    current_mode == "linked"
                    or (
                        self._agent_prefers_shared_provider_auth(agent)
                        and self._shared_linked_auth_available(spec.name)
                    )
                )
            ):
                auth["auth_mode"] = "linked"
            return auth

        if agent is not None and spec.supports_auth_mode("linked"):
            if current_mode == "linked":
                auth["auth_mode"] = "linked"
                return auth
            if self._agent_prefers_shared_provider_auth(agent) and self._shared_linked_auth_available(spec.name):
                auth["auth_mode"] = "linked"
                return auth

        if allow_defaults:
            auth["auth_mode"] = spec.default_auth_mode
        return auth

    def _is_provider_configured(self, provider: str, auth: dict[str, Any]) -> bool:
        spec = get_provider(provider)
        mode = str(auth.get("auth_mode", "")).strip().lower()
        if not mode:
            return False
        if not spec.supports_auth_mode(mode):
            return False
        if mode == "api_key":
            return bool(str(auth.get("api_key", "")).strip())
        if mode in {"linked", "none"}:
            return True
        return False

    def _mint_channels(
        self,
        agent_id: str,
        base_channels: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        items = list(base_channels or [])
        minted: list[dict[str, str]] = []
        for idx, channel in enumerate(items, start=1):
            kind = str(channel.get("kind", "chat"))
            raw_name = str(channel.get("name", f"channel-{idx}"))
            if raw_name.startswith(f"{agent_id}-"):
                full_name = raw_name
            else:
                full_name = f"{agent_id}-{raw_name}"
            minted.append(
                {
                    "kind": kind,
                    "name": full_name,
                    "external_id": f"{agent_id}:{kind}:{idx}",
                }
            )
        return minted

    # Backward-compatible aliases.
    def create_user(self, **kwargs: Any) -> dict[str, Any]:
        return self.create_agent(
            agent_id=str(kwargs.get("user_id", kwargs.get("agent_id", ""))),
            display_name=kwargs.get("display_name"),
            template=str(kwargs.get("template", "baseline")),
            clone_from=kwargs.get("clone_from"),
            channel_strategy=str(kwargs.get("channel_strategy", "new")),
            channels=kwargs.get("channels"),
            agent_version=str(kwargs.get("agent_version", "1.0.0")),
            provider=kwargs.get("provider"),
            core_prompts=kwargs.get("core_prompts"),
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self.list_agents()

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self.get_agent(user_id)

    def delete_user(self, user_id: str) -> None:
        self.delete_agent(user_id)

    def batch_create_users(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        return self.batch_create_agents(entries)

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

    def _probe_process(self, pid: int) -> dict[str, Any] | None:
        if pid <= 0:
            return None
        cmd = ["ps", "-p", str(pid), "-o", "%cpu=,%mem=,rss="]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        parts = result.stdout.strip().split()
        if len(parts) < 3:
            return None
        try:
            return {
                "cpu_percent": float(parts[0]),
                "mem_percent": float(parts[1]),
                "rss_kb": int(parts[2]),
            }
        except ValueError:
            return None

    @staticmethod
    def _validate_linux_username(username: str) -> None:
        if not username:
            raise ValueError("linux username is required")
        if len(username) > 32:
            raise ValueError("linux username must be <= 32 chars")
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if username[0] == "-" or any(ch not in allowed for ch in username):
            raise ValueError("linux username can only contain a-z, 0-9, _ and -")

    @staticmethod
    def _linux_user_exists(username: str) -> bool:
        result = subprocess.run(
            ["id", "-u", username],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _copy_user_configs(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        enabled: bool,
    ) -> list[str]:
        candidates = [
            ".bashrc",
            ".profile",
            ".config/clawie",
            ".clawie",
        ]
        return self._copy_selected_paths(
            source_home=source_home,
            target_home=target_home,
            username=username,
            relative_paths=candidates,
            enabled=enabled,
        )

    def _ensure_shared_toolchain_shell_init(self, target_home: Path, username: str) -> list[str]:
        if not target_home.exists():
            return []
        if not os.access(target_home, os.W_OK | os.X_OK):
            return []
        updated: list[str] = []
        for rel in [".profile", ".bashrc"]:
            path = target_home / rel
            current = ""
            if path.exists():
                current = path.read_text(encoding="utf-8")
            if self.SHARED_TOOLCHAIN_BEGIN in current and self.SHARED_TOOLCHAIN_END in current:
                continue
            rendered = current
            if rendered and not rendered.endswith("\n"):
                rendered += "\n"
            rendered += self.SHARED_TOOLCHAIN_BLOCK
            path.write_text(rendered, encoding="utf-8")
            subprocess.run(["chown", f"{username}:{username}", str(path)], check=True)
            updated.append(str(path))
        return updated

    def _ensure_shared_claude_links(self, target_home: Path, username: str) -> list[str]:
        if not target_home.exists():
            return []
        if not os.access(target_home, os.W_OK | os.X_OK):
            return []
        shared_dir = self.SHARED_CLAUDE_DIR
        targets = [
            (target_home / ".claude", shared_dir),
            (target_home / ".claude.json", shared_dir / ".claude.json"),
        ]
        updated: list[str] = []
        for dst, src in targets:
            if dst.is_symlink():
                try:
                    if dst.resolve() == src.resolve():
                        continue
                except OSError:
                    pass
                dst.unlink(missing_ok=True)
            elif dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.symlink_to(src, target_is_directory=src.is_dir())
            subprocess.run(["chown", "-h", f"{username}:{username}", str(dst)], check=False)
            updated.append(str(dst))
        return updated

    @staticmethod
    def _write_managed_text(path: Path, content: str, mode: int) -> bool:
        changed = not path.exists()
        if path.exists():
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                current = ""
            if current != content:
                changed = True
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        current_mode = int(path.stat().st_mode) & 0o777
        if current_mode != mode:
            os.chmod(path, mode)
            changed = True
        return changed

    def _patch_claude_cli_for_shared_permissions(self) -> str:
        global_root = self.HOMEBREW_PREFIX / "bin" / "global"
        if not global_root.exists():
            return ""
        candidates: list[Path] = []
        for row in global_root.rglob("cli.js"):
            token = str(row)
            if "@anthropic-ai+claude-code@" not in token:
                continue
            if not token.endswith("/node_modules/@anthropic-ai/claude-code/cli.js"):
                continue
            candidates.append(row)
        if not candidates:
            return ""
        target = sorted(candidates)[-1]
        text = target.read_text(encoding="utf-8")
        rendered = text.replace("mode:384", "mode:438").replace("bt9(K,384)", "bt9(K,438)")
        changed = rendered != text
        if changed:
            target.write_text(rendered, encoding="utf-8")
        current_mode = int(target.stat().st_mode) & 0o777
        if current_mode != 0o644:
            os.chmod(target, 0o644)
            changed = True
        return str(target) if changed else ""

    def _seed_shared_claude_state(self, source_home: Path) -> list[str]:
        shared = self.SHARED_CLAUDE_DIR
        updated: list[str] = []
        mapping = [
            (source_home / ".claude" / ".credentials.json", shared / ".credentials.json"),
            (source_home / ".claude.json", shared / ".claude.json"),
        ]
        for src, dst in mapping:
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                updated.append(str(dst))
            if dst.exists():
                current_mode = int(dst.stat().st_mode) & 0o777
                if current_mode != 0o666:
                    os.chmod(dst, 0o666)
                    if str(dst) not in updated:
                        updated.append(str(dst))
        return updated

    def _ensure_system_shared_runtime(self, source_home: Path) -> list[str]:
        if os.geteuid() != 0:
            return []
        profile_dir = self.GLOBAL_PROFILE_DIR
        shared_parent = self.SHARED_CLAUDE_DIR.parent
        if not profile_dir.exists() or not os.access(profile_dir, os.W_OK | os.X_OK):
            return []
        if not shared_parent.exists() or not os.access(shared_parent, os.W_OK | os.X_OK):
            return []

        updated: list[str] = []
        managed_profiles = [
            (
                self.GLOBAL_HOMEBREW_PROFILE_FILE,
                self.GLOBAL_HOMEBREW_PROFILE_CONTENT,
            ),
            (
                self.GLOBAL_FNM_PROFILE_FILE,
                self.GLOBAL_FNM_PROFILE_CONTENT,
            ),
            (
                self.GLOBAL_CLAUDE_PROFILE_FILE,
                self.GLOBAL_CLAUDE_PROFILE_CONTENT,
            ),
        ]
        for path, content in managed_profiles:
            if self._write_managed_text(path, content, 0o644):
                updated.append(str(path))

        # Directories that benefit from the sticky bit (any user can create
        # their own entries but cannot delete other users' entries).
        sticky_dirs = {"session-env", "projects", "tasks", "plans", "file-history", "todos"}

        shared_paths = [self.SHARED_CLAUDE_DIR]
        for token in self.SHARED_CLAUDE_SUBDIRS:
            shared_paths.append(self.SHARED_CLAUDE_DIR / token)
        for path in shared_paths:
            existed = path.exists()
            path.mkdir(parents=True, exist_ok=True)
            target_mode = 0o1777 if path.name in sticky_dirs else 0o777
            current_mode = int(path.stat().st_mode) & 0o1777
            if (not existed) or current_mode != target_mode:
                os.chmod(path, target_mode)
                updated.append(str(path))

        # Sweep all existing children to fix dirs created by Claude Code at runtime.
        if self.SHARED_CLAUDE_DIR.is_dir():
            for child in self.SHARED_CLAUDE_DIR.iterdir():
                if child.is_dir():
                    try:
                        target_mode = 0o1777 if child.name in sticky_dirs else 0o777
                        current_mode = int(child.stat().st_mode) & 0o1777
                        if current_mode != target_mode:
                            os.chmod(child, target_mode)
                            if str(child) not in updated:
                                updated.append(str(child))
                    except OSError:
                        pass
            for child in self.SHARED_CLAUDE_DIR.iterdir():
                if child.is_file() and not child.name.startswith("."):
                    try:
                        current_mode = int(child.stat().st_mode) & 0o777
                        if current_mode != 0o666:
                            os.chmod(child, 0o666)
                            if str(child) not in updated:
                                updated.append(str(child))
                    except OSError:
                        pass

        for path in self._seed_shared_claude_state(source_home):
            if path not in updated:
                updated.append(path)

        patched = self._patch_claude_cli_for_shared_permissions()
        if patched and patched not in updated:
            updated.append(patched)
        return updated

    def _credential_bundle_paths(self, bundle_id: str) -> list[str]:
        token = self._canonical_credential_bundle(bundle_id)
        spec = self._credential_bundle_spec_map().get(token, {})
        kind = str(spec.get("kind", "")).strip().lower()
        if kind == "paths":
            raw = spec.get("paths", ())
            if isinstance(raw, tuple):
                return [str(item) for item in raw if str(item).strip()]
            if isinstance(raw, list):
                return [str(item) for item in raw if str(item).strip()]
        if token == "provider-auth":
            return shared_auth_paths_for_providers(provider_names())
        return []

    def _sync_selected_credential_bundles(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        requested_provider: str | None,
        bundles: list[str],
    ) -> list[str]:
        copied: list[str] = []
        for bundle in self._ordered_credential_bundles(bundles):
            if bundle == "provider-auth":
                copied.extend(
                    self._sync_shared_provider_auth(
                        source_home=source_home,
                        target_home=target_home,
                        username=username,
                        requested_provider=requested_provider,
                    )
                )
                continue
            paths = self._credential_bundle_paths(bundle)
            copied.extend(
                self._copy_selected_paths(
                    source_home=source_home,
                    target_home=target_home,
                    username=username,
                    relative_paths=paths,
                    enabled=True,
                )
            )
        return self._dedupe_paths(copied)

    def _revoke_selected_credential_bundles(self, target_home: Path, bundles: list[str]) -> list[str]:
        removed: list[str] = []
        seen_rel: set[str] = set()
        for bundle in self._ordered_credential_bundles(bundles):
            for rel in self._credential_bundle_paths(bundle):
                token = str(rel).strip()
                if not token or token in seen_rel:
                    continue
                seen_rel.add(token)
                dst = target_home / token
                if not dst.exists() and not dst.is_symlink():
                    continue
                if dst.is_symlink() or dst.is_file():
                    dst.unlink(missing_ok=True)
                elif dst.is_dir():
                    shutil.rmtree(dst)
                removed.append(str(dst))
        return self._dedupe_paths(removed)

    def _sync_shared_provider_auth(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        requested_provider: str | None,
    ) -> list[str]:
        updated = self._seed_shared_provider_auth_from_home(
            source_home=source_home,
            requested_provider=requested_provider,
        )
        updated.extend(self._ensure_shared_provider_auth_links(target_home=target_home, username=username))
        self._relax_shared_provider_auth_permissions()
        return self._dedupe_paths(updated)

    def _copy_selected_paths(
        self,
        source_home: Path,
        target_home: Path,
        username: str,
        relative_paths: list[str],
        enabled: bool,
    ) -> list[str]:
        if not enabled:
            return []
        copied: list[str] = []
        seen: set[str] = set()
        for rel in relative_paths:
            token = str(rel).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            src = source_home / token
            dst = target_home / token
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            subprocess.run(["chown", "-R", f"{username}:{username}", str(dst)], check=True)
            copied.append(str(dst))
        return copied

    @staticmethod
    def _find_channel(channels: list[dict[str, Any]], kind: str, name: str) -> int | None:
        for idx, channel in enumerate(channels):
            if not isinstance(channel, dict):
                continue
            row_kind = str(channel.get("kind", "")).strip().lower()
            row_name = str(channel.get("name", "")).strip()
            if row_kind == kind and row_name == name:
                return idx
        return None

    @staticmethod
    def _channel_key(kind: str, name: str) -> tuple[str, str]:
        return (str(kind).strip().lower(), str(name).strip())

    def _channel_keys(self, channels: list[dict[str, Any]]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            kind, name = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if not kind or not name:
                continue
            keys.add((kind, name))
        return keys

    def _assert_channels_unclaimed(
        self,
        agents: dict[str, Any],
        owner_agent_id: str,
        channels: list[dict[str, Any]],
        allow_owners: set[str] | None = None,
    ) -> None:
        keys = self._channel_keys(channels)
        if not keys:
            return
        allowed = {str(owner_agent_id).strip()}
        if allow_owners:
            for item in allow_owners:
                token = str(item).strip()
                if token:
                    allowed.add(token)
        conflicts: list[str] = []
        for aid, payload in sorted(agents.items()):
            token = str(aid).strip()
            if token in allowed:
                continue
            rows = payload.get("channels", [])
            if not isinstance(rows, list):
                continue
            claimed = [
                f"{kind}:{name}" for (kind, name) in keys if self._find_channel(rows, kind, name) is not None
            ]
            if claimed:
                conflicts.append(f"{token} owns {', '.join(claimed)}")
        if conflicts:
            raise ValueError("channel already assigned to another agent: " + "; ".join(conflicts))

    def _remove_channel_keys_from_agent(
        self,
        source: dict[str, Any],
        keys: set[tuple[str, str]],
    ) -> int:
        if not keys:
            return 0
        channels = source.setdefault("channels", [])
        if not isinstance(channels, list):
            source["channels"] = []
            return 0
        kept: list[Any] = []
        removed = 0
        for channel in channels:
            if not isinstance(channel, dict):
                kept.append(channel)
                continue
            kind, name = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
            if (kind, name) in keys:
                removed += 1
                continue
            kept.append(channel)
        source["channels"] = kept
        return removed

    def _remove_channel_from_other_agents(
        self,
        agents: dict[str, Any],
        kind: str,
        name: str,
        keep_agent_id: str,
    ) -> list[str]:
        keep = str(keep_agent_id).strip()
        moved_from: list[str] = []
        for aid, payload in agents.items():
            token = str(aid).strip()
            if token == keep:
                continue
            rows = payload.setdefault("channels", [])
            if not isinstance(rows, list):
                continue
            removed_any = False
            while True:
                found_idx = self._find_channel(rows, kind, name)
                if found_idx is None:
                    break
                rows.pop(found_idx)
                removed_any = True
            if removed_any:
                moved_from.append(token)
                payload.setdefault("agent", {})["last_sync"] = now_iso()
        return moved_from

    def _apply_spawn_password(
        self,
        username: str,
        password: str | None,
        password_hash: str | None,
        use_global_password: bool,
    ) -> tuple[str, str]:
        raw_password = str(password or "").strip()
        raw_hash = str(password_hash or "").strip()
        if raw_password and raw_hash:
            raise ValueError("use either password or password_hash, not both")

        if raw_password:
            self._set_password_plaintext(username, raw_password)
            return ("spawn-password", raw_password)
        if raw_hash:
            self._set_password_hash(username, raw_hash)
            return ("spawn-password-hash", "")

        if use_global_password:
            config = self.store.read_config()
            global_hash = str(config.get("spawn_password_hash", "")).strip()
            if global_hash:
                self._set_password_hash(username, global_hash)
                return ("global-password-hash", "")

        # No password was provided or configured: generate a strong one-off
        # password instead of falling back to a fixed, well-known default.
        # It is returned (and printed once by the CLI) so the operator can
        # record it; SSH login is separately disabled for spawned users.
        generated = secrets.token_urlsafe(12)
        self._set_password_plaintext(username, generated)
        return ("generated-password", generated)

    @staticmethod
    def _spawn_user_shell() -> str:
        return "/bin/bash"

    def _disable_ssh_login_for_user(self, username: str) -> bool:
        user = str(username).strip()
        if not user:
            raise ValueError("username is required")
        path = self.SSHD_DENY_USERS_FILE
        users: set[str] = set()
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    token = line.strip()
                    if not token or token.startswith("#"):
                        continue
                    if token.lower().startswith("denyusers"):
                        users.update(part.strip() for part in token.split()[1:] if part.strip())
            except OSError as exc:
                raise SetupError(f"failed reading ssh deny-users config: {exc}") from exc
        users.add(user)
        rendered = "# Managed by clawie. Spawned users are denied SSH login.\n"
        rendered += f"DenyUsers {' '.join(sorted(users))}\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            raise SetupError(f"failed writing ssh deny-users config: {exc}") from exc

        attempts = [
            ["systemctl", "reload", "ssh"],
            ["systemctl", "reload", "sshd"],
            ["service", "ssh", "reload"],
            ["service", "sshd", "reload"],
        ]
        last_error = ""
        for cmd in attempts:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                return True
            last_error = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise SetupError(f"updated ssh deny-users config but failed to reload ssh daemon: {last_error}")

    @staticmethod
    def _set_password_plaintext(username: str, password: str) -> None:
        if not password:
            raise ValueError("password cannot be empty")
        subprocess.run(
            ["chpasswd"],
            input=f"{username}:{password}\n",
            text=True,
            check=True,
        )

    @staticmethod
    def _set_password_hash(username: str, password_hash: str) -> None:
        if not password_hash:
            raise ValueError("password_hash cannot be empty")
        subprocess.run(["usermod", "-p", password_hash, username], check=True)

    @staticmethod
    def _hash_password(password: str) -> str:
        if not password:
            raise ValueError("spawn password cannot be empty")
        if crypt is not None:
            return str(crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512)))
        # Python 3.13+ removed the stdlib crypt module (PEP 594). Fall back to
        # openssl, which emits the same SHA512-crypt ($6$...) format chpasswd
        # and usermod -p expect.
        openssl = shutil.which("openssl")
        if openssl:
            result = subprocess.run(
                [openssl, "passwd", "-6", "-stdin"],
                input=f"{password}\n",
                capture_output=True,
                text=True,
                check=False,
            )
            hashed = (result.stdout or "").strip()
            if result.returncode == 0 and hashed.startswith("$6$"):
                return hashed
        raise SetupError(
            "hashing spawn passwords requires the stdlib 'crypt' module (Python <= 3.12) "
            "or the 'openssl' executable; neither is available."
        )

    def _service_command(self, provider: str, action: str, linux_user: str) -> list[str]:
        # Check permission before resolving the executable: a missing-root error
        # is more fundamental (and more actionable) than a missing-binary error.
        self._require_linux_user_access(linux_user, "service control")
        spec = get_provider(provider)
        executable = self._resolve_provider_executable(spec.name)
        if spec.service_group:
            base = [executable, spec.service_group, action]
        else:
            base = ["bash", "-lc", self._process_service_shell(spec.name, executable, action)]
        return self._wrap_user_command(base, linux_user, purpose="service control")

    def _channel_connect_commands(
        self,
        provider: str,
        kind: str,
        name: str,
        linux_user: str,
    ) -> list[list[str]]:
        executable = self._resolve_provider_executable(provider)
        adapter = get_channel_adapter(provider)
        commands = adapter.connect_commands(executable=executable, kind=kind, name=name)

        wrapped: list[list[str]] = []
        for raw in commands:
            wrapped.append(self._wrap_user_command(raw, linux_user, purpose="channel connect"))
        return wrapped

    def _wrap_user_command(self, base: list[str], linux_user: str, *, purpose: str) -> list[str]:
        if not linux_user or linux_user == self._current_linux_user():
            return base
        if os.geteuid() != 0:
            raise SetupError(
                f"{purpose} requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )
        return ["sudo", "-u", linux_user, "-H", "--", *base]

    @staticmethod
    def _command_executable(cmd: list[str]) -> str:
        for marker in ("service", "daemon", "auth", "gateway"):
            if marker not in cmd:
                continue
            idx = cmd.index(marker)
            if idx > 0:
                return str(cmd[idx - 1])
        return str(cmd[0])

    def _resolve_executable_in_service_env(self, executable: str, *, linux_user: str = "") -> str:
        token = str(executable).strip()
        if not token:
            return ""
        env = self._service_env(linux_user)
        env_path = env.get("PATH", "")
        try:
            resolved = shutil.which(token, path=env_path)
        except TypeError:
            resolved = shutil.which(token)
        if resolved:
            return resolved
        if "/" in token:
            candidate = Path(token)
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        for segment in env_path.split(":"):
            piece = segment.strip()
            if not piece:
                continue
            candidate = Path(piece) / token
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        fallback = f"/home/linuxbrew/.linuxbrew/bin/{token}"
        if Path(fallback).exists():
            return fallback
        return ""

    def _resolve_provider_executable(self, provider: str) -> str:
        resolved = self._resolve_executable_in_service_env(provider)
        if resolved:
            return resolved
        raise SetupError(
            f"provider executable '{provider}' was not found in PATH. Run 'clawie runtime install {provider}' first."
        )

    def _resolve_addon_executable(self, addon: str) -> str:
        spec = get_addon(addon)
        resolved = self._resolve_executable_in_service_env(spec.executable)
        if resolved:
            return resolved
        raise SetupError(
            f"addon executable '{spec.executable}' was not found in PATH. Run 'clawie addon install {spec.name}' first."
        )

    def _service_env(self, linux_user: str) -> dict[str, str]:
        env = dict(os.environ)
        current_path = env.get("PATH", "")
        required_paths = [
            *self._shared_toolchain_path_entries(),
            "/home/linuxbrew/.linuxbrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        merged: list[str] = []
        for segment in [*required_paths, *current_path.split(":")]:
            piece = segment.strip()
            if not piece or piece in merged:
                continue
            merged.append(piece)
        env["PATH"] = ":".join(merged)

        if linux_user:
            home = self._linux_home_for_user(linux_user)
            if home is not None:
                env["HOME"] = str(home)
            env["USER"] = linux_user
            env["LOGNAME"] = linux_user
            try:
                record = pwd.getpwnam(linux_user)
            except KeyError:
                record = None
            if record is not None:
                uid = int(record.pw_uid)
                env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
        return env

    @staticmethod
    def _extract_tarball_safe(archive_path: Path, target_dir: Path) -> None:
        root = target_dir.resolve()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                candidate = (target_dir / member.name).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError as exc:
                    raise SetupError(f"archive contained an unsafe path: {member.name}") from exc
            archive.extractall(target_dir)

    @staticmethod
    def _gcloud_archive_name() -> str:
        machine = platform.machine().strip().lower()
        mapping = {
            "x86_64": "google-cloud-cli-linux-x86_64.tar.gz",
            "amd64": "google-cloud-cli-linux-x86_64.tar.gz",
            "aarch64": "google-cloud-cli-linux-arm.tar.gz",
            "arm64": "google-cloud-cli-linux-arm.tar.gz",
        }
        archive = mapping.get(machine)
        if archive:
            return archive
        raise SetupError(f"automatic gcloud install is not supported on architecture '{machine}'")

    def _linux_home_for_user(self, linux_user: str) -> Path | None:
        token = str(linux_user).strip()
        if not token:
            return None
        try:
            return Path(pwd.getpwnam(token).pw_dir)
        except KeyError:
            if token == self._current_linux_user():
                return Path.home()
            return Path("/home") / token

    @staticmethod
    def _provider_uses_generated_user_unit(provider: str) -> bool:
        spec = get_provider(provider)
        if spec.name == "openclaw":
            return True
        return not bool(spec.service_group) and bool(spec.background_command)

    def _generated_user_service_unit_path(self, provider: str, linux_user: str) -> Path | None:
        home = self._linux_home_for_user(linux_user)
        if home is None:
            return None
        return home / ".config" / "systemd" / "user" / f"{provider}.service"

    @staticmethod
    def _staged_prompt_pickup_shell(provider: str, state_dir: str, workspace_dir: str) -> str:
        """Shell snippet that copies staged prompt files into the workspace.

        Runs as the target user so it has write access to $HOME.
        Staged files live at /tmp/clawie-prompt-stage/$USER/<provider>--<name>.
        """
        return (
            f'STAGE_DIR="/tmp/clawie-prompt-stage/$USER"; '
            f'WS="$HOME/{state_dir}/{workspace_dir}"; '
            f'if [ -d "$STAGE_DIR" ]; then '
            f'  mkdir -p "$WS"; '
            f'  for f in "$STAGE_DIR"/{provider}--*; do '
            f'    [ -f "$f" ] || continue; '
            f'    name="${{f##*--}}"; '
            f'    cp "$f" "$WS/$name" && rm -f "$f" 2>/dev/null; '
            f'  done; '
            f'fi'
        )

    def _generated_user_service_unit_contents(self, provider: str, executable: str) -> str:
        spec = get_provider(provider)
        command = " ".join([shlex.quote(executable), *[shlex.quote(part) for part in spec.background_command]])
        state_dir = spec.state_dir
        workspace_dir = spec.workspace_dir
        path_entries = self._service_env("").get("PATH", "")
        pickup = self._staged_prompt_pickup_shell(provider, state_dir, workspace_dir)
        shell = (
            f'mkdir -p "$HOME/{state_dir}" "$HOME/{state_dir}/{workspace_dir}"; '
            f'cd "$HOME/{state_dir}/{workspace_dir}"; '
            f'exec {command} >>"$HOME/{state_dir}/daemon.log" 2>&1'
        )
        lines = [
            "[Unit]",
            f"Description=Clawie managed {provider} runtime",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "Environment=HOME=%h",
            "Environment=USER=%u",
            "Environment=LOGNAME=%u",
            f"Environment=PATH={path_entries}",
            f"ExecStartPre=/bin/bash -c {shlex.quote(pickup)}",
            f"ExecStart=/bin/bash -lc {shlex.quote(shell)}",
            "Restart=always",
            "RestartSec=2",
            "KillMode=control-group",
            "TimeoutStopSec=20",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        return "\n".join(lines)

    def _ensure_generated_user_service_unit(self, provider: str, linux_user: str) -> bool:
        unit_path = self._generated_user_service_unit_path(provider, linux_user)
        if unit_path is None:
            return False
        executable = self._resolve_provider_executable(provider)
        unit_text = self._generated_user_service_unit_contents(provider, executable)
        unit_dir = unit_path.parent
        unit_dir.mkdir(parents=True, exist_ok=True)
        current = ""
        try:
            current = unit_path.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != unit_text:
            unit_path.write_text(unit_text, encoding="utf-8")
        self._chown_tree(unit_dir, linux_user)
        return True

    def _run_systemd_user_command(self, linux_user: str, args: list[str]) -> dict[str, Any]:
        candidates = self._systemd_user_candidates(linux_user)
        last_output = ""
        for candidate in candidates:
            if candidate == "root":
                continue
            cmd = ["systemctl", "--machine", f"{candidate}@", "--user", *args]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._systemctl_env(),
                )
            except Exception as exc:
                last_output = str(exc)
                continue
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": cmd}
            last_output = output or f"exit {result.returncode}"

        fallback_user = candidates[0] if candidates else str(linux_user).strip()
        cmd = ["systemctl", "--user", *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(fallback_user),
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": cmd}
            last_output = output or f"exit {result.returncode}"
        except Exception as exc:
            last_output = str(exc)
        return {"ok": False, "output": last_output, "command": cmd}

    def _auth_env(self, linux_user: str, home: Path | None) -> dict[str, str]:
        env = self._service_env(linux_user)
        if home:
            env["HOME"] = str(home)
        return env

    def _provider_auth_command(self, provider: str, action: str, linux_user: str) -> list[str]:
        spec = get_provider(provider)
        executable = self._resolve_provider_executable(spec.name)
        if action == "login":
            base = [executable, *spec.auth_login_command]
        elif action == "refresh":
            base = [executable, *spec.auth_refresh_command]
        elif action == "status":
            base = [executable, *spec.auth_status_command]
        else:
            base = [executable, "auth", action]
        return self._wrap_user_command(base, linux_user, purpose="auth control")

    def _process_service_shell(self, provider: str, executable: str, action: str) -> str:
        spec = get_provider(provider)
        state_dir = spec.state_dir
        pattern = self._provider_process_pattern(spec.name)
        quoted_executable = shlex.quote(executable)
        start_cmd = " ".join([quoted_executable, *[shlex.quote(part) for part in spec.background_command]])
        lines = [f'pattern={shlex.quote(pattern)}']
        lines.append('existing="$(pgrep -u "$(id -u)" -f "$pattern" | tr \'\\n\' \' \' | sed \'s/[[:space:]]*$//\')"')
        if action == "status":
            lines.append('if [ -n "$existing" ]; then echo "active (running)"; else echo "inactive"; fi')
        elif action == "stop":
            lines.append('if [ -n "$existing" ]; then pkill -u "$(id -u)" -f "$pattern" || true; echo "stopped"; else echo "inactive"; fi')
        elif action == "restart":
            lines.append('if [ -n "$existing" ]; then pkill -u "$(id -u)" -f "$pattern" || true; fi')
            lines.append(f'mkdir -p "$HOME/{state_dir}"')
            lines.append(f'setsid {start_cmd} < /dev/null >>"$HOME/{state_dir}/daemon.log" 2>&1 & echo "started pid=$!"')
        else:
            lines.append('if [ -n "$existing" ]; then echo "already running"; exit 0; fi')
            lines.append(f'mkdir -p "$HOME/{state_dir}"')
            lines.append(f'setsid {start_cmd} < /dev/null >>"$HOME/{state_dir}/daemon.log" 2>&1 & echo "started pid=$!"')
        return "; ".join(lines)

    @staticmethod
    def _provider_process_pattern(provider: str) -> str:
        spec = get_provider(provider)
        return " ".join([spec.name, *spec.background_command]).strip()

    @staticmethod
    def _path_or_none(value: Any) -> Path | None:
        token = str(value or "").strip()
        if not token:
            return None
        return Path(token)

    def _resolve_local_runtime_target(self, provider: str) -> dict[str, str]:
        name = str(provider).strip().lower()
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        cached = dict(local_state.get(name, {}))
        root_path: Path | None = None
        hint_user = ""
        for claw in self.list_installed_claws():
            if str(claw.get("provider", "")).strip().lower() != name:
                continue
            root_path = Path(str(claw.get("root", "")).strip())
            hint_user = self._linux_user_from_provider_root(root_path)
            break

        linux_user = self._preferred_local_linux_user(
            default_user=self._local_target_user(),
            hint_user=hint_user,
            cached_user=str(cached.get("linux_user", "")),
        )
        if root_path:
            home = root_path.parent
        elif linux_user and linux_user != "root":
            home = Path("/home") / linux_user
        else:
            home = Path.home()
        return {
            "linux_user": linux_user,
            "home": str(home),
            "root": str(root_path or ""),
        }

    def _inspect_provider_auth_state(
        self,
        *,
        provider: str,
        auth_mode: str,
        linux_user: str,
        home: Path | None,
    ) -> dict[str, Any]:
        spec = get_provider(provider)
        mode = str(auth_mode or spec.default_auth_mode).strip().lower() or spec.default_auth_mode
        configured = self._is_provider_configured(spec.name, {"auth_mode": mode, **self._provider_auth(spec.name)})
        payload = empty_auth_payload(spec.name, mode)

        if mode == "none":
            payload.update(
                {
                    "auth_status": "not_required",
                    "can_login": False,
                    "detail": "login not required",
                }
            )
            return payload

        if mode == "api_key":
            payload.update(
                {
                    "auth_status": "ready" if configured else "missing",
                    "login_required": not configured,
                    "can_login": False,
                    "detail": "API key configured" if configured else "API key missing",
                }
            )
            return payload

        if linux_user and not self._can_manage_linux_user(linux_user):
            payload.update(
                {
                    "auth_status": "unknown",
                    "can_login": False,
                    "detail": "auth inspection requires root for managed agents owned by another Linux user",
                    "source": "permission",
                }
            )
            return payload

        cli_status = self._run_provider_auth_status(provider=spec.name, linux_user=linux_user, home=home)
        if cli_status:
            payload.update(cli_status)
            payload["source"] = str(cli_status.get("source", "cli"))
            payload["login_required"] = login_required(str(payload.get("auth_status", "")))
            return payload

        file_status = inspect_auth_files(provider=spec.name, home=home)
        if file_status:
            payload.update(file_status)
            payload["source"] = str(file_status.get("source", "files"))
            payload["login_required"] = login_required(str(payload.get("auth_status", "")))
            return payload

        payload.update(
            {
                "auth_status": "missing",
                "login_required": True,
                "detail": "no linked auth session found",
                "source": "none",
            }
        )
        return payload

    def _refresh_or_login_linked_auth(
        self,
        *,
        provider: str,
        auth_mode: str,
        linux_user: str,
        home: Path | None,
    ) -> dict[str, Any]:
        mode = str(auth_mode).strip().lower()
        if mode != "linked":
            raise ValueError(f"{provider} uses '{mode}' auth; linked login is not applicable")
        if linux_user:
            self._require_linux_user_access(linux_user, "auth control")

        initial = self._inspect_provider_auth_state(
            provider=provider,
            auth_mode=mode,
            linux_user=linux_user,
            home=home,
        )
        if str(initial.get("auth_status", "")).strip().lower() == "ready":
            initial["action_performed"] = "status"
            return initial

        env = self._auth_env(linux_user, home)
        refresh_cmd = self._provider_auth_command(provider, "refresh", linux_user)
        refresh = subprocess.run(refresh_cmd, capture_output=True, text=True, check=False, env=env)
        refreshed = self._inspect_provider_auth_state(
            provider=provider,
            auth_mode=mode,
            linux_user=linux_user,
            home=home,
        )
        refreshed["refresh_output"] = (refresh.stdout or refresh.stderr or "").strip()
        if str(refreshed.get("auth_status", "")).strip().lower() == "ready":
            refreshed["action_performed"] = "refresh"
            return refreshed

        login_cmd = self._provider_auth_command(provider, "login", linux_user)
        login = subprocess.run(login_cmd, check=False, env=env)
        if login.returncode != 0:
            raise SetupError(f"{provider} auth login failed with exit code {login.returncode}")
        logged_in = self._inspect_provider_auth_state(
            provider=provider,
            auth_mode=mode,
            linux_user=linux_user,
            home=home,
        )
        logged_in["action_performed"] = "login"
        if str(logged_in.get("auth_status", "")).strip().lower() != "ready":
            raise SetupError(
                f"{provider} auth login completed but session is still {logged_in.get('auth_status', 'unknown')}"
            )
        return logged_in

    def _run_provider_auth_status(
        self,
        *,
        provider: str,
        linux_user: str,
        home: Path | None,
    ) -> dict[str, Any] | None:
        try:
            cmd = self._provider_auth_command(provider, "status", linux_user)
        except Exception:
            return None

        env = self._auth_env(linux_user, home)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        except Exception:
            return None

        output = "\n".join(part for part in [result.stdout, result.stderr] if str(part).strip()).strip()
        if not output and result.returncode != 0:
            return None
        parsed = parse_provider_auth_status_output(output)
        if not parsed:
            return None
        parsed["source"] = "cli"
        return parsed

    def _attach_agent_auth_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        info = payload.setdefault("agent", {})
        try:
            auth = self.agent_auth_status(agent_id)
        except Exception as exc:
            info["auth_status"] = "unknown"
            info["auth_profile"] = ""
            info["auth_account"] = ""
            info["auth_expires_at"] = ""
            info["auth_last_refresh"] = ""
            info["auth_source"] = "error"
            info["auth_detail"] = str(exc)
            info["login_required"] = False
            info["can_login"] = False
            return payload

        info["auth_mode"] = str(auth.get("auth_mode", info.get("auth_mode", "")))
        info["auth_status"] = str(auth.get("auth_status", "unknown"))
        info["auth_profile"] = str(auth.get("auth_profile", ""))
        info["auth_account"] = str(auth.get("account", ""))
        info["auth_expires_at"] = str(auth.get("expires_at", ""))
        info["auth_last_refresh"] = str(auth.get("last_refresh", ""))
        info["auth_source"] = str(auth.get("source", ""))
        info["auth_detail"] = str(auth.get("detail", ""))
        info["login_required"] = bool(auth.get("login_required", False))
        info["can_login"] = bool(auth.get("can_login", False))
        return payload

    def _attach_agent_addon_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", payload.get("user_id", ""))).strip()
        if not agent_id or agent_id.startswith("@local:"):
            payload["addon_access"] = {"agent_id": agent_id, "addons": []}
            return payload
        try:
            payload["addon_access"] = self.get_agent_addons(agent_id)
        except Exception:
            payload["addon_access"] = {"agent_id": agent_id, "addons": []}
        return payload

    def _attach_agent_runtime_status(
        self,
        payload: dict[str, Any],
        *,
        daemon_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        info = payload.setdefault("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        info["provider_status"] = str(info.get("provider_status", "ok") or "ok")
        if bool(info.get("local_user", False)):
            info["live_provider"] = provider
            info["live_providers"] = [provider] if provider else []
            info["live_pid"] = int(info.get("fallback_pid", 0) or 0)
            return payload

        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            info["live_provider"] = ""
            info["live_providers"] = []
            info["live_pid"] = 0
            return payload

        if daemon_map is None:
            daemon_map = self._running_provider_daemons_by_user()
        live_entries = list(daemon_map.get(linux_user, []))
        live_providers: list[str] = []
        chosen_entry: dict[str, Any] | None = None
        reported_running = self._provider_reports_running(provider, linux_user) if provider else False
        for entry in live_entries:
            entry_provider = str(entry.get("provider", "")).strip().lower()
            if not entry_provider:
                continue
            if entry_provider not in live_providers:
                live_providers.append(entry_provider)
            if chosen_entry is None and (
                not str(info.get("provider", "")).strip().lower()
                or entry_provider == str(info.get("provider", "")).strip().lower()
            ):
                chosen_entry = entry
        if chosen_entry is None and live_entries:
            chosen_entry = live_entries[0]

        info["live_provider"] = str((chosen_entry or {}).get("provider", "")).strip().lower()
        info["live_providers"] = live_providers
        info["live_pid"] = int((chosen_entry or {}).get("pid", 0) or 0)
        info["live_command"] = str((chosen_entry or {}).get("args", ""))
        if live_entries:
            info["service_status"] = "running"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "process"
        elif reported_running is True:
            if provider:
                info["live_provider"] = provider
                info["live_providers"] = [provider]
            info["service_status"] = "running"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "systemd"
        elif reported_running is None:
            info["service_status"] = "unknown"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "systemd"
        else:
            info["service_status"] = "stopped"
            if not str(info.get("service_mode", "")).strip() or str(info.get("service_mode", "")).strip() == "unknown":
                info["service_mode"] = "process"
        return payload

    def _refresh_managed_agent_provider_alignment(
        self,
        agent_id: str,
        *,
        daemon_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        token = str(agent_id).strip()
        if not token or token.startswith("@local:"):
            return
        self._refresh_managed_agent_provider_alignments(agent_id=token, daemon_map=daemon_map)

    def _refresh_managed_agent_provider_alignments(
        self,
        *,
        agent_id: str | None = None,
        daemon_map: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        if daemon_map is None:
            daemon_map = self._running_provider_daemons_by_user()
        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        dirty = False
        for token, agent in agents.items():
            if agent_id and token != agent_id:
                continue
            self._hydrate_agent_controls(agent)
            if self._apply_live_provider_alignment(
                state=state,
                agent_id=token,
                agent=agent,
                daemon_map=daemon_map,
            ):
                dirty = True
        if dirty:
            self.store.write_state(state)

    def _apply_live_provider_alignment(
        self,
        *,
        state: dict[str, Any],
        agent_id: str,
        agent: dict[str, Any],
        daemon_map: dict[str, list[dict[str, Any]]],
    ) -> bool:
        info = agent.setdefault("agent", {})
        if bool(info.get("local_user", False)):
            return False
        linux_user = str(info.get("linux_user", "")).strip()
        if not linux_user:
            return self._clear_runtime_provider_issue(agent)

        current_provider = str(info.get("provider", "")).strip().lower()
        live_entries = list(daemon_map.get(linux_user, []))
        live_providers: list[str] = []
        for entry in live_entries:
            provider = str(entry.get("provider", "")).strip().lower()
            if provider and provider not in live_providers:
                live_providers.append(provider)

        if not live_providers:
            return self._clear_runtime_provider_issue(agent)

        effective_provider = current_provider if current_provider in live_providers else live_providers[0]
        changed = False
        if effective_provider and effective_provider != current_provider:
            info["provider"] = effective_provider
            info["runtime"] = get_provider(effective_provider).runtime
            auth_mode = str(info.get("auth_mode", "")).strip().lower()
            spec = get_provider(effective_provider)
            if not spec.supports_auth_mode(auth_mode):
                info["auth_mode"] = spec.default_auth_mode
            changed = True

        if len(live_providers) > 1:
            changed = self._set_agent_provider_issue(
                agent,
                status="warning",
                kind="runtime_conflict",
                issue=f"multiple provider daemons detected: {', '.join(live_providers)}; using {effective_provider}",
                remediation=(
                    f"Run 'sudo clawie agent provider set {agent_id} {effective_provider}' to stop the extra runtimes."
                ),
                requested_provider="",
            ) or changed
            if changed:
                self._event(
                    state,
                    "agents.provider_runtime_conflict",
                    f"Detected multiple runtimes for {agent_id}",
                    {
                        "agent_id": agent_id,
                        "linux_user": linux_user,
                        "live_providers": list(live_providers),
                        "effective_provider": effective_provider,
                    },
                )
            return changed

        live_provider = live_providers[0]
        if live_provider != current_provider:
            changed = self._set_agent_provider_issue(
                agent,
                status="warning",
                kind="runtime_drift",
                issue=(
                    f"live runtime was {live_provider}; Clawie aligned state away from {current_provider or 'unknown'}"
                ),
                remediation=(
                    f"Run 'sudo clawie agent provider set {agent_id} {current_provider}' if you still want to switch."
                    if current_provider
                    else ""
                ),
                requested_provider=current_provider,
            ) or changed
            self._event(
                state,
                "agents.provider_aligned_to_runtime",
                f"Aligned {agent_id} to live runtime {live_provider}",
                {
                    "agent_id": agent_id,
                    "linux_user": linux_user,
                    "previous_provider": current_provider,
                    "live_provider": live_provider,
                },
            )
            return True

        return self._clear_runtime_provider_issue(agent) or changed

    @staticmethod
    def _clear_agent_provider_issue(agent: dict[str, Any]) -> None:
        info = agent.setdefault("agent", {})
        info["provider_status"] = "ok"
        for key in ("provider_issue_kind", "provider_issue", "provider_remediation", "provider_requested"):
            info.pop(key, None)

    def _clear_runtime_provider_issue(self, agent: dict[str, Any]) -> bool:
        info = agent.setdefault("agent", {})
        if str(info.get("provider_issue_kind", "")) != "runtime_conflict":
            return False
        self._clear_agent_provider_issue(agent)
        return True

    @staticmethod
    def _set_agent_provider_issue(
        agent: dict[str, Any],
        *,
        status: str,
        kind: str,
        issue: str,
        remediation: str,
        requested_provider: str,
    ) -> bool:
        info = agent.setdefault("agent", {})
        next_values = {
            "provider_status": str(status or "warning"),
            "provider_issue_kind": str(kind or "").strip(),
            "provider_issue": str(issue or "").strip(),
            "provider_remediation": str(remediation or "").strip(),
            "provider_requested": str(requested_provider or "").strip().lower(),
        }
        current_values = {
            key: str(info.get(key, "") if key != "provider_status" else info.get(key, "ok"))
            for key in next_values
        }
        if current_values == next_values:
            return False
        info.update(next_values)
        return True

    def _provider_switch_remediation(
        self,
        *,
        agent_id: str,
        target_provider: str,
        linux_user: str,
        error: str,
    ) -> str:
        message = str(error).strip().lower()
        if "requires root" in message or "sudo" in message:
            return f"Re-run 'sudo clawie agent provider set {agent_id} {target_provider}'."
        if "executable" in message or "not found" in message or "install" in message:
            return f"Install or link '{target_provider}', then run 'sudo clawie agent provider set {agent_id} {target_provider}'."
        if linux_user:
            return (
                f"Check the {target_provider} service for {linux_user}, then run "
                f"'sudo clawie agent provider set {agent_id} {target_provider}' again."
            )
        return f"Retry 'clawie agent provider set {agent_id} {target_provider}' after fixing the provider runtime."

    def _running_provider_daemons_by_user(self) -> dict[str, list[dict[str, Any]]]:
        result = subprocess.run(["ps", "-eo", "user=,pid=,args="], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return {}

        rows: dict[str, list[dict[str, Any]]] = {}
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            linux_user, pid_text, args = parts
            provider = self._provider_from_process_args(args)
            if not provider:
                continue
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            rows.setdefault(linux_user, []).append(
                {
                    "provider": provider,
                    "pid": pid,
                    "args": args,
                }
            )
        return rows

    @staticmethod
    def _provider_from_process_args(args: str) -> str:
        raw = str(args).strip()
        if not raw:
            return ""
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if len(tokens) < 2:
            return ""
        known = set(provider_names())
        for idx, token in enumerate(tokens):
            name = Path(token).name.strip().lower()
            candidate = name
            if candidate not in known and "." in candidate:
                stem = Path(token).stem.strip().lower()
                if stem in known:
                    candidate = stem
            if candidate not in known:
                continue
            expected = [str(item).strip().lower() for item in get_provider(candidate).background_command]
            if not expected:
                continue
            tail = [str(item).strip().lower() for item in tokens[idx + 1 : idx + 1 + len(expected)]]
            if tail == expected:
                return candidate
        return ""

    def _bootstrap_user_bus(self, linux_user: str) -> None:
        if not linux_user or os.geteuid() != 0:
            return
        try:
            uid = int(pwd.getpwnam(linux_user).pw_uid)
        except KeyError:
            return

        subprocess.run(["loginctl", "enable-linger", linux_user], check=False)
        subprocess.run(["systemctl", "start", f"user@{uid}.service"], check=False)

    def _fallback_service_action(
        self,
        provider: str,
        action: str,
        linux_user: str,
        executable: str,
        agent_info: dict[str, Any],
    ) -> dict[str, str]:
        _ = provider
        pid = int(agent_info.get("fallback_pid", 0) or 0)

        if action == "status":
            running = self._is_pid_running(pid, linux_user) or self._provider_process_live_ps_only(provider, linux_user)
            return {
                "service_status": "running" if running else "stopped",
                "output": "fallback daemon " + ("running" if running else "stopped"),
            }

        if action == "stop":
            if pid and self._is_pid_running(pid, linux_user):
                self._kill_pid(pid, linux_user)
            self._force_stop_provider_processes(provider, linux_user)
            agent_info["fallback_pid"] = 0
            running = self._provider_process_live_ps_only(provider, linux_user)
            return {
                "service_status": "running" if running else "stopped",
                "output": "fallback daemon " + ("running" if running else "stopped"),
            }

        if action == "restart":
            if pid and self._is_pid_running(pid, linux_user):
                self._kill_pid(pid, linux_user)
            new_pid = self._start_fallback_daemon(provider, executable, linux_user)
            agent_info["fallback_pid"] = new_pid
            return {"service_status": "running", "output": f"fallback daemon restarted pid={new_pid}"}

        # start
        if pid and self._is_pid_running(pid, linux_user):
            return {"service_status": "running", "output": f"fallback daemon already running pid={pid}"}
        new_pid = self._start_fallback_daemon(provider, executable, linux_user)
        agent_info["fallback_pid"] = new_pid
        return {"service_status": "running", "output": f"fallback daemon started pid={new_pid}"}

    def _start_fallback_daemon(self, provider: str, executable: str, linux_user: str) -> int:
        spec = get_provider(provider)
        state_dir = spec.state_dir
        workspace_dir = spec.workspace_dir
        pickup = self._staged_prompt_pickup_shell(provider, state_dir, workspace_dir)
        background = " ".join([f'"{executable}"', *[shlex.quote(part) for part in spec.background_command]])
        script = (
            f'{pickup}; '
            f'mkdir -p "$HOME/{state_dir}"; '
            f'setsid {background} < /dev/null >>"$HOME/{state_dir}/daemon.log" 2>&1 & echo $!'
        )
        cmd = self._user_shell_command(linux_user, script)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise SetupError("fallback daemon start failed: " + (message or f"exit {result.returncode}"))
        token = (result.stdout or "").strip().splitlines()
        if not token:
            raise SetupError("fallback daemon start failed: pid not reported")
        try:
            return int(token[-1].strip())
        except ValueError as exc:
            raise SetupError("fallback daemon start failed: invalid pid output") from exc

    def _kill_pid(self, pid: int, linux_user: str) -> None:
        if pid <= 0:
            return
        script = f"kill {pid}"
        cmd = self._user_shell_command(linux_user, script)
        subprocess.run(cmd, capture_output=True, text=True, check=False)

    def _is_pid_running(self, pid: int, linux_user: str) -> bool:
        if pid <= 0:
            return False
        script = f"kill -0 {pid}"
        cmd = self._user_shell_command(linux_user, script)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return result.returncode == 0

    @staticmethod
    def _normalized_local_service_state(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        payload = config.get("local_service_state", {})
        if not isinstance(payload, dict):
            payload = {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            normalized[str(key).strip().lower()] = dict(value)
        return normalized

    @staticmethod
    def _normalized_channel_pool(config: dict[str, Any]) -> list[dict[str, str]]:
        raw = config.get("channel_pool", [])
        if not isinstance(raw, list):
            return []
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if not kind or not name:
                continue
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "provider": str(item.get("provider", "")).strip().lower(),
                    "external_id": str(item.get("external_id", "")).strip(),
                }
            )
        return rows

    def _read_channel_pool(self) -> list[dict[str, str]]:
        config = self.store.read_config()
        return self._normalized_channel_pool(config)

    def _write_channel_pool(self, channels: list[dict[str, str]]) -> None:
        config = self.store.read_config()
        config["channel_pool"] = self._normalized_channel_pool({"channel_pool": channels})
        config["updated_at"] = now_iso()
        self.store.write_config(config)

    def _remove_pool_channel(self, kind: str, name: str) -> None:
        current = self._read_channel_pool()
        remaining = [
            row
            for row in current
            if not (
                str(row.get("kind", "")).strip().lower() == kind
                and str(row.get("name", "")).strip() == name
            )
        ]
        if len(remaining) != len(current):
            self._write_channel_pool(remaining)

    def _local_dashboard_rows(self, refresh: bool = False) -> list[dict[str, Any]]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        installed = self.list_installed_claws()
        user_hints: dict[str, str] = {}
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                user_hints[provider] = hint
        providers = [
            str(row.get("provider", "")).strip().lower()
            for row in installed
            if str(row.get("provider", "")).strip()
        ]
        if refresh and providers:
            local_state = self._refresh_local_service_statuses(providers, local_state, user_hints=user_hints)
            config = self.store.read_config()
        rows: list[dict[str, Any]] = []
        # Use the same home-resolution logic as list_installed_claws() so
        # `sudo clawie dashboard` still inspects the invoking user's claws.
        for claw in installed:
            provider = str(claw.get("provider", "")).strip().lower()
            if not provider:
                continue
            local_info = local_state.get(provider, {})
            rows.append(
                {
                    "agent_id": f"@local:{provider}",
                    "display_name": "local-user",
                    "status": str(local_info.get("service_status", "unknown")),
                    "version": "local",
                    "provider": provider,
                    "strategy": "local-user",
                    "channels": 0,
                    "channels_total": 0,
                    "migrated": 0,
                    "last_sync": str(config.get("updated_at", "")),
                    "pid": int(local_info.get("fallback_pid", 0) or 0),
                    "cpu_percent": 0.0,
                    "mem_percent": 0.0,
                    "rss_kb": 0,
                    "local_user": True,
                }
            )
        return rows

    def _local_channel_inventory(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for claw in self.list_installed_claws():
            provider = str(claw.get("provider", "")).strip().lower()
            root = Path(str(claw.get("root", "")).strip())
            if not provider or not root:
                continue
            discovered = self._discover_channels_for_provider_root(provider, root)
            for channel in discovered:
                kind = str(channel.get("kind", "")).strip().lower()
                name = str(channel.get("name", "")).strip()
                if not kind or not name:
                    continue
                rows.append(
                    {
                        "source": "local",
                        "owner_agent_id": f"@local:{provider}",
                        "provider": provider,
                        "kind": kind,
                        "name": name,
                        "enabled": bool(channel.get("enabled", True)),
                    }
                )
        return rows

    def _discover_channels_for_provider_root(self, provider: str, root: Path) -> list[dict[str, str]]:
        adapter = get_channel_adapter(provider)
        return adapter.discover_channels(root)

    def _discover_agent_channels(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("agent", {})
        provider = str(info.get("provider", "")).strip().lower()
        linux_user = str(info.get("linux_user", "")).strip()
        is_local = bool(info.get("local_user", False))
        if is_local:
            home = self._local_agent_home(provider)
        else:
            home = self._agent_linux_home(payload)
        if not home:
            return {"source": "none", "detail": "agent home is not available", "channels": [], "providers": []}
        if linux_user and not is_local and not self._can_manage_linux_user(linux_user):
            if not self._can_read_provider_channel_roots(home, [provider, *provider_names()]):
                return {
                    "source": "permission",
                    "detail": "live channel discovery requires root for managed agents owned by another Linux user",
                    "channels": [],
                    "providers": [],
                }

        ordered: list[str] = []
        seen_providers: set[str] = set()
        candidate_providers: list[str] = []
        if linux_user and not is_local:
            candidate_providers.extend(self._live_provider_names_for_user(linux_user))
        if not candidate_providers:
            candidate_providers = [provider, *provider_names()]
        for item in candidate_providers:
            token = str(item or "").strip().lower()
            if not token or token in seen_providers:
                continue
            seen_providers.add(token)
            ordered.append(token)

        discovered: list[dict[str, Any]] = []
        found_providers: list[str] = []
        seen_channels: set[tuple[str, str]] = set()
        for name in ordered:
            root = home / get_provider(name).state_dir
            channels = self._discover_channels_for_provider_root(name, root)
            provider_had_rows = False
            for channel in channels:
                key = self._channel_key(channel.get("kind", ""), channel.get("name", ""))
                if key in seen_channels or not key[0] or not key[1]:
                    continue
                seen_channels.add(key)
                provider_had_rows = True
                discovered.append(
                    {
                        "kind": key[0],
                        "name": key[1],
                        "enabled": bool(channel.get("enabled", True)),
                        "discovered_provider": name,
                    }
                )
            if provider_had_rows:
                found_providers.append(name)

        if discovered:
            return {
                "source": "provider",
                "detail": "live channels discovered",
                "channels": discovered,
                "providers": found_providers,
            }
        return {
            "source": "none",
            "detail": "no live channels discovered",
            "channels": [],
            "providers": [],
        }

    def _attach_agent_channel_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.setdefault("agent", {})
        stored = payload.get("channels", [])
        stored_rows = [dict(row) for row in stored if isinstance(row, dict)] if isinstance(stored, list) else []
        discovery = self._discover_agent_channels(payload)
        live_rows = discovery.get("channels", [])
        live_map = {
            self._channel_key(row.get("kind", ""), row.get("name", "")): dict(row)
            for row in live_rows
            if isinstance(row, dict)
        }

        merged: list[dict[str, Any]] = []
        appended: set[tuple[str, str]] = set()
        for row in stored_rows:
            key = self._channel_key(row.get("kind", ""), row.get("name", ""))
            if not key[0] or not key[1]:
                continue
            live = live_map.get(key)
            if live:
                row["channel_source"] = "live"
                row["discovered_provider"] = str(live.get("discovered_provider", ""))
            elif str(discovery.get("source", "")) == "provider":
                row["channel_source"] = "stale"
            else:
                row["channel_source"] = "state"
            merged.append(row)
            appended.add(key)

        for row in live_rows:
            if not isinstance(row, dict):
                continue
            key = self._channel_key(row.get("kind", ""), row.get("name", ""))
            if key in appended or not key[0] or not key[1]:
                continue
            merged.append(
                {
                    "kind": key[0],
                    "name": key[1],
                    "enabled": bool(row.get("enabled", True)),
                    "external_id": "",
                    "channel_source": "discovered",
                    "discovered_provider": str(row.get("discovered_provider", "")),
                }
            )
            appended.add(key)

        def _sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
            source = str(row.get("channel_source", "state"))
            order = {"live": 0, "discovered": 1, "state": 2, "stale": 3}
            return (order.get(source, 9), str(row.get("kind", "")), str(row.get("name", "")))

        payload["channels"] = sorted(merged, key=_sort_key)
        info["channel_status_source"] = str(discovery.get("source", "state"))
        info["channel_status_detail"] = str(discovery.get("detail", ""))
        info["live_channel_count"] = sum(
            1 for row in payload["channels"] if str(row.get("channel_source", "")) in {"live", "discovered"}
        )
        info["stale_channel_count"] = sum(
            1 for row in payload["channels"] if str(row.get("channel_source", "")) == "stale"
        )
        return payload

    @staticmethod
    def _provider_core_prompt_names(provider: str) -> tuple[str, ...]:
        try:
            names = get_provider(provider).core_prompt_files
        except ValueError:
            names = ()
        if names:
            return names
        return (
            "SOUL.md",
            "IDENTITY.md",
            "AGENTS.md",
            "TOOLS.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "BOOTSTRAP.md",
            "USER.md",
        )

    def _canonical_core_prompt_name(self, provider: str, prompt_name: str) -> str:
        token = str(prompt_name).strip().upper()
        if token and not token.endswith(".MD"):
            token = f"{token}.MD"
        for item in self._provider_core_prompt_names(provider):
            if item.upper() == token:
                return item
        raise ValueError(
            f"unknown core prompt '{prompt_name}'. supported: {', '.join(self._provider_core_prompt_names(provider))}"
        )

    def _normalize_core_prompts(self, provider: str, payload: dict[str, Any]) -> dict[str, str]:
        rows: dict[str, str] = {}
        data = payload if isinstance(payload, dict) else {}
        for name in self._provider_core_prompt_names(provider):
            value = data.get(name, "")
            rows[name] = str(value) if value is not None else ""
        return rows

    def _seed_core_prompt_defaults(
        self,
        provider: str,
        core_prompts: dict[str, str],
        agent_id: str = "",
        display_name: str = "",
    ) -> None:
        for name in self._provider_core_prompt_names(provider):
            existing = str(core_prompts.get(name, "") or "")
            if existing and _is_legacy_core_prompt_default(name, existing):
                content = _default_core_prompt_content(name, agent_id=agent_id, display_name=display_name)
                if content:
                    core_prompts[name] = content
                continue
            if existing:
                continue
            content = _default_core_prompt_content(name, agent_id=agent_id, display_name=display_name)
            if content:
                core_prompts[name] = content

    def _core_prompt_path(self, provider: str, home: Path, prompt_name: str) -> Path:
        spec = get_provider(provider)
        name = self._canonical_core_prompt_name(provider, prompt_name)
        return home / spec.state_dir / spec.workspace_dir / name

    def _read_core_prompts_from_home(self, provider: str, home: Path) -> dict[str, str]:
        rows: dict[str, str] = {}
        for name in self._provider_core_prompt_names(provider):
            path = self._core_prompt_path(provider, home, name)
            if path.exists():
                rows[name] = path.read_text(encoding="utf-8")
            else:
                rows[name] = ""
        return rows

    # ── prompt staging ──────────────────────────────────────────────
    _PROMPT_STAGE_ROOT = Path("/tmp/clawie-prompt-stage")

    @classmethod
    def _prompt_stage_dir(cls, linux_user: str) -> Path:
        return cls._PROMPT_STAGE_ROOT / linux_user

    def _write_core_prompt_file(self, provider: str, home: Path, prompt_name: str, content: str) -> Path:
        path = self._core_prompt_path(provider, home, prompt_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        try:
            os.chmod(str(path), 0o664)
        except OSError:
            pass
        return path

    def _write_prompt_files_for_home(
        self,
        provider: str,
        home: Path,
        prompts: dict[str, str],
        linux_user: str = "",
    ) -> list[str]:
        written: list[str] = []
        staged: list[str] = []
        for name, content in self._normalize_core_prompts(provider, prompts).items():
            try:
                target = self._write_core_prompt_file(provider, home, name, content)
                if linux_user and os.geteuid() == 0:
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(target)], check=False)
                written.append(str(target))
            except PermissionError:
                if linux_user:
                    self._stage_prompt_file(linux_user, provider, name, content)
                    staged.append(name)
                else:
                    raise
        if staged:
            self._apply_staged_prompts_if_possible(provider, home, linux_user)
        return written

    def _stage_prompt_file(
        self, linux_user: str, provider: str, prompt_name: str, content: str,
    ) -> Path:
        root = self._PROMPT_STAGE_ROOT
        root.mkdir(parents=True, exist_ok=True, mode=0o1777)
        try:
            os.chmod(str(root), 0o1777)
        except OSError:
            pass
        stage = self._prompt_stage_dir(linux_user)
        # Per-user dir is 777 (no sticky) so the target user can delete files
        # written by the manager user after copying them to the workspace.
        stage.mkdir(parents=True, exist_ok=True, mode=0o777)
        try:
            os.chmod(str(stage), 0o777)
        except OSError:
            pass
        target = stage / f"{provider}--{prompt_name}"
        target.write_text(str(content), encoding="utf-8")
        os.chmod(str(target), 0o666)
        return target

    def _apply_staged_prompts_if_possible(
        self, provider: str, home: Path, linux_user: str,
    ) -> list[str]:
        stage = self._prompt_stage_dir(linux_user)
        if not stage.is_dir():
            return []
        applied: list[str] = []
        prefix = f"{provider}--"
        for entry in sorted(stage.iterdir()):
            if not entry.name.startswith(prefix):
                continue
            prompt_name = entry.name[len(prefix):]
            content = entry.read_text(encoding="utf-8")
            try:
                target = self._write_core_prompt_file(provider, home, prompt_name, content)
                if linux_user and os.geteuid() == 0:
                    subprocess.run(
                        ["chown", f"{linux_user}:{linux_user}", str(target)],
                        check=False,
                    )
                applied.append(prompt_name)
                entry.unlink(missing_ok=True)
            except PermissionError:
                continue
        if applied and not any(stage.iterdir()):
            stage.rmdir()
        return applied

    def _ensure_workspace_accessible(
        self, provider: str, home: Path, linux_user: str,
    ) -> None:
        """Make agent home group-accessible so the manager user can operate without sudo.

        Sets the provider state dir and workspace to setgid-group-writable (2775)
        so that files created there inherit the agent's group. Adds the current
        (manager) user to the agent's group for traversal and write access.
        The agent user's own access is unaffected (owner bits stay rwx).
        """
        if os.geteuid() != 0:
            return
        try:
            spec = get_provider(provider)
            state = home / spec.state_dir
            ws = state / spec.workspace_dir

            # Home dir: ensure group can at least traverse (g+rx).
            # 750 (rwxr-x---) is the minimum; don't weaken if already tighter
            # for "other", but ensure group bits.
            if home.is_dir():
                subprocess.run(["chmod", "g+rx", str(home)], check=False)

            # State dir (.openclaw/): setgid + group-writable so manager
            # can write config and new files inherit the agent group.
            if state.is_dir():
                subprocess.run(["chmod", "2775", str(state)], check=False)

            # Workspace dir: same treatment.
            if ws.is_dir():
                subprocess.run(["chmod", "2775", str(ws)], check=False)

            # Make existing files in workspace group-writable.
            if ws.is_dir():
                for child in ws.iterdir():
                    if child.is_file():
                        subprocess.run(["chmod", "g+rw", str(child)], check=False)

            # Make existing files in state dir group-writable (config, logs).
            if state.is_dir():
                for child in state.iterdir():
                    if child.is_file():
                        subprocess.run(["chmod", "g+rw", str(child)], check=False)

            # Add the manager (spawner) user to the agent's group so it can
            # traverse home and write to state/workspace via group bits.
            # Prefer SUDO_USER over current user (root) when running under sudo.
            spawner = (
                os.environ.get("SUDO_USER", "").strip()
                or self._current_linux_user()
            )
            if spawner and spawner != linux_user:
                subprocess.run(
                    ["usermod", "-a", "-G", linux_user, spawner],
                    check=False,
                )
        except OSError:
            pass

    def _agent_linux_home(self, agent: dict[str, Any]) -> Path | None:
        linux_user = str(agent.get("agent", {}).get("linux_user", "")).strip()
        if not linux_user:
            return None
        return self._linux_home_for_user(linux_user)

    def _local_agent_home(self, provider: str) -> Path | None:
        for claw in self.list_installed_claws():
            if str(claw.get("provider", "")).strip().lower() != provider:
                continue
            root = Path(str(claw.get("root", "")).strip())
            hint = self._linux_user_from_provider_root(root)
            if hint and hint != "root":
                return self._linux_home_for_user(hint)
        target = self._local_target_user()
        if target and target != "root":
            return self._linux_home_for_user(target)
        return None

    def _refresh_local_service_statuses(
        self,
        providers: list[str],
        local_state: dict[str, dict[str, Any]],
        user_hints: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        config = self.store.read_config()
        default_user = self._local_target_user()
        dirty = False
        for provider in providers:
            info = local_state.setdefault(provider, {})
            hint_user = str((user_hints or {}).get(provider, "")).strip()
            cached_user = str(info.get("linux_user", "")).strip()
            linux_user = self._preferred_local_linux_user(
                default_user=default_user,
                hint_user=hint_user,
                cached_user=cached_user,
            )
            if linux_user and linux_user != str(info.get("linux_user", "")).strip():
                info["linux_user"] = linux_user
                dirty = True
            unit_status = self._systemd_user_service_status(provider, linux_user)
            if unit_status != "unknown":
                status = unit_status
                mode = "systemd"
                if status != str(info.get("service_status", "unknown")):
                    info["service_status"] = status
                    dirty = True
                if mode != str(info.get("service_mode", "unknown")):
                    info["service_mode"] = mode
                    dirty = True
                continue
            try:
                probe = self._run_local_provider_command(provider, "status", linux_user)
                cmd = probe["command"]
                result = probe["result"]
                output = probe["output"]
                lowered = output.lower()
                if result.returncode != 0 and "failed to connect to bus" in lowered:
                    fallback = self._fallback_service_action(
                        provider=provider,
                        action="status",
                        linux_user=linux_user,
                        executable=cmd[0],
                        agent_info=info,
                    )
                    status = str(fallback.get("service_status", "unknown"))
                    mode = "fallback"
                else:
                    inferred = self._infer_service_status(output)
                    if inferred == "unknown":
                        status = self._best_effort_local_status(info, linux_user)
                        mode = "fallback"
                    elif result.returncode != 0:
                        status = self._best_effort_local_status(info, linux_user)
                        mode = "fallback"
                    else:
                        status = inferred
                        mode = "systemd"
            except Exception:
                status = self._best_effort_local_status(info, linux_user)
                mode = "fallback"
            if status != str(info.get("service_status", "unknown")):
                info["service_status"] = status
                dirty = True
            if mode != str(info.get("service_mode", "unknown")):
                info["service_mode"] = mode
                dirty = True

        if dirty:
            config["local_service_state"] = local_state
            self.store.write_config(config)
        return local_state

    def _systemd_user_candidates(self, linux_user: str, provider: str = "") -> list[str]:
        candidates: list[str] = []
        hint = str(self._local_linux_user_hint(provider, "")).strip() if provider else ""
        for token in (
            str(linux_user).strip(),
            str(self._local_target_user()).strip(),
            hint,
        ):
            if token and token not in candidates:
                candidates.append(token)
        home_root = Path("/home")
        if home_root.exists():
            for entry in sorted(home_root.iterdir(), key=lambda row: str(getattr(row, "name", ""))):
                if not entry.is_dir():
                    continue
                token = entry.name.strip()
                if token and token not in candidates:
                    candidates.append(token)
        return candidates

    def _systemd_user_service_status(self, provider: str, linux_user: str) -> str:
        service = f"{provider}.service"
        candidates = self._systemd_user_candidates(linux_user, provider)

        saw_stopped = False
        for candidate in candidates:
            if candidate == "root":
                continue
            cmd = ["systemctl", "--machine", f"{candidate}@", "--user", "is-active", service]
            env = self._systemctl_env()
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            except Exception:
                continue
            parsed = self._parse_systemctl_status(result.stdout, result.stderr)
            if parsed == "running":
                return "running"
            if parsed == "stopped":
                saw_stopped = True

        fallback_env_user = candidates[0] if candidates else str(linux_user).strip()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(fallback_env_user),
            )
        except Exception:
            return "stopped" if saw_stopped else "unknown"
        parsed = self._parse_systemctl_status(result.stdout, result.stderr)
        if parsed == "running":
            return parsed
        if parsed == "stopped":
            return "stopped"
        return "stopped" if saw_stopped else "unknown"

    def _systemd_user_service_manage(self, provider: str, action: str, linux_user: str) -> dict[str, Any]:
        service = f"{provider}.service"
        candidates = self._systemd_user_candidates(linux_user, provider)

        last_output = ""
        for candidate in candidates:
            if candidate == "root":
                continue
            cmd = ["systemctl", "--machine", f"{candidate}@", "--user", action, service]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=self._systemctl_env(),
                )
            except Exception as exc:
                last_output = str(exc)
                continue
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": cmd}
            last_output = output or f"exit {result.returncode}"

        fallback_user = candidates[0] if candidates else str(linux_user).strip()
        try:
            result = subprocess.run(
                ["systemctl", "--user", action, service],
                capture_output=True,
                text=True,
                check=False,
                env=self._service_env(fallback_user),
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                return {"ok": True, "output": output, "command": ["systemctl", "--user", action, service]}
            last_output = output or f"exit {result.returncode}"
        except Exception as exc:
            last_output = str(exc)
        return {"ok": False, "output": last_output}

    @staticmethod
    def _systemctl_env() -> dict[str, str]:
        env = dict(os.environ)
        current_path = env.get("PATH", "")
        required_paths = ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        merged = [segment for segment in current_path.split(":") if segment]
        for segment in required_paths:
            if segment not in merged:
                merged.append(segment)
        env["PATH"] = ":".join(merged)
        return env

    @staticmethod
    def _parse_systemctl_status(stdout: str, stderr: str) -> str:
        text = (stdout or "").strip().lower()
        err = (stderr or "").strip().lower()
        token = text or err
        if not token:
            return "unknown"
        if "connect to bus" in token:
            return "unknown"
        if token in {"active", "activating", "reloading"} or token.startswith("active"):
            return "running"
        if token in {"inactive", "failed", "deactivating", "dead"} or token.startswith("inactive"):
            return "stopped"
        return "unknown"

    def _local_linux_user_hint(self, provider: str, fallback: str) -> str:
        fallback_user = str(fallback).strip()
        if fallback_user and fallback_user != "root":
            return fallback_user
        name = str(provider).strip().lower()
        for claw in self.list_installed_claws():
            if str(claw.get("provider", "")).strip().lower() != name:
                continue
            hint = self._linux_user_from_provider_root(Path(str(claw.get("root", "")).strip()))
            if hint:
                return hint
        return fallback_user

    @staticmethod
    def _preferred_local_linux_user(
        default_user: str,
        hint_user: str,
        cached_user: str,
    ) -> str:
        default_token = str(default_user).strip()
        hint_token = str(hint_user).strip()
        cached_token = str(cached_user).strip()
        # Always prefer the current invoking user (e.g. SUDO_USER) over stale cache.
        for candidate in (default_token, hint_token, cached_token):
            if candidate and candidate != "root":
                return candidate
        return default_token or hint_token or cached_token

    @staticmethod
    def _linux_user_from_provider_root(root: Path) -> str:
        parts = root.parts
        if len(parts) >= 3 and parts[1] == "home":
            return str(parts[2]).strip()
        if len(parts) >= 2 and parts[1] == "root":
            return "root"
        return ""

    def _run_local_provider_command(
        self,
        provider: str,
        action: str,
        linux_user: str,
    ) -> dict[str, Any]:
        attempts: list[tuple[list[str], dict[str, str]]] = []
        if os.geteuid() == 0 and linux_user and linux_user != "root":
            attempts.append(
                (self._service_command(provider, action, linux_user=linux_user), self._service_env(linux_user))
            )
        attempts.append((self._service_command(provider, action, linux_user=""), self._service_env(linux_user)))

        last: dict[str, Any] | None = None
        for idx, (cmd, env) in enumerate(attempts):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
            except Exception:
                if idx + 1 < len(attempts):
                    continue
                raise

            output = (result.stdout or result.stderr or "").strip()
            last = {"command": cmd, "result": result, "output": output}

            retry_allowed = idx + 1 < len(attempts)
            unparseable_status = action == "status" and self._infer_service_status(output) == "unknown"
            failed = result.returncode != 0
            if retry_allowed and (failed or unparseable_status):
                continue
            return last

        if last is not None:
            return last
        raise SetupError(f"{provider} service {action} failed before process launch")

    def _best_effort_local_status(self, info: dict[str, Any], linux_user: str) -> str:
        status = self._normalize_status_text(str(info.get("service_status", "unknown")))
        if status != "unknown":
            return status
        pid = int(info.get("fallback_pid", 0) or 0)
        if pid > 0:
            return "running" if self._is_pid_running(pid, linux_user) else "stopped"
        return "stopped"

    @staticmethod
    def _local_target_user() -> str:
        if os.geteuid() == 0:
            sudo_user = str(os.environ.get("SUDO_USER", "")).strip()
            if sudo_user and sudo_user != "root":
                return sudo_user
        try:
            return str(pwd.getpwuid(os.geteuid()).pw_name)
        except KeyError:
            return ""

    def _local_agent_view(self, provider: str) -> dict[str, Any]:
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        local_state = self._refresh_local_service_statuses([provider], local_state)
        config = self.store.read_config()
        local_state = self._normalized_local_service_state(config)
        info = dict(local_state.get(provider, {}))
        home = self._local_agent_home(provider)
        prompts = self._normalize_core_prompts(provider, {})
        if home:
            prompts = self._normalize_core_prompts(provider, self._read_core_prompts_from_home(provider, home))
        self._seed_core_prompt_defaults(provider, prompts, agent_id=f"@local:{provider}", display_name="local-user")
        self._seed_delegation_skill(prompts, self._default_plugins_for_provider(provider))
        return {
            "agent_id": f"@local:{provider}",
            "display_name": "local-user",
            "source_template": "local-user",
            "clone_from": "",
            "channel_strategy": "local-user",
            "channels": [],
            "core_prompts": prompts,
            "credential_sync": self._normalize_credential_sync_state({}, default_when_missing=False),
            "agent": {
                "provider": provider,
                "auth_mode": str(self._provider_auth(provider).get("auth_mode", "")),
                "autostart": False,
                "heartbeat_seconds": 0,
                "status": str(info.get("service_status", "unknown")),
                "service_status": str(info.get("service_status", "unknown")),
                "service_mode": str(info.get("service_mode", "unknown")),
                "fallback_pid": int(info.get("fallback_pid", 0) or 0),
                "version": "local",
                "plugins": self._default_plugins_for_provider(provider),
                "local_user": True,
            },
        }

    def _user_shell_command(self, linux_user: str, script: str) -> list[str]:
        if not linux_user:
            return ["bash", "-lc", script]

        current_user = ""
        try:
            current_user = pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            current_user = ""
        if linux_user == current_user:
            return ["bash", "-lc", script]
        if os.geteuid() != 0:
            raise SetupError(
                "service control requires root when agent linux_user differs from current user. Re-run with sudo/root."
            )
        return ["sudo", "-u", linux_user, "-H", "--", "bash", "-lc", script]

    @staticmethod
    def _infer_service_status(output: str) -> str:
        text = str(output).strip().lower()
        if "inactive" in text or "stopped" in text or "dead" in text:
            return "stopped"
        if "running" in text or "active" in text or "started" in text:
            return "running"
        return "unknown"

    @staticmethod
    def _normalize_status_text(value: str) -> str:
        token = str(value).strip().lower()
        if token in {"running", "active", "started"}:
            return "running"
        if token in {"stopped", "inactive", "dead", "offline"}:
            return "stopped"
        if token in {"ready", "syncing"}:
            return token
        return "unknown"

    def _dashboard_status(self, metric_status: str, agent_info: dict[str, Any]) -> str:
        service_status = self._normalize_status_text(str(agent_info.get("service_status", "")))
        if service_status != "unknown":
            return service_status
        measured = self._normalize_status_text(metric_status)
        if measured != "unknown":
            return measured
        return self._normalize_status_text(str(agent_info.get("status", "unknown")))

    def _default_plugins_for_provider(self, provider: str) -> dict[str, bool]:
        _ = provider
        return copy.deepcopy(self.DEFAULT_AGENT_PLUGINS)

    def _normalize_plugins(self, plugins: dict[str, Any]) -> dict[str, bool]:
        merged = self._default_plugins_for_provider("")
        for key, value in plugins.items():
            token = str(key).strip().lower()
            if not token:
                continue
            merged[token] = bool(value)
        return merged

    def _hydrate_agent_controls(self, agent_state: dict[str, Any]) -> None:
        channels = agent_state.get("channels", [])
        if isinstance(channels, list):
            for channel in channels:
                if isinstance(channel, dict):
                    channel["enabled"] = bool(channel.get("enabled", True))
        agent = agent_state.setdefault("agent", {})
        provider = str(agent.get("provider", "")).strip().lower()
        raw_plugins = agent.get("plugins", self._default_plugins_for_provider(str(agent.get("provider", ""))))
        if not isinstance(raw_plugins, dict):
            raw_plugins = self._default_plugins_for_provider(str(agent.get("provider", "")))
        agent["plugins"] = self._normalize_plugins(raw_plugins)
        if "model_tier" not in agent:
            agent["model_tier"] = "balanced"
        agent_state["core_prompts"] = self._normalize_core_prompts(provider, agent_state.get("core_prompts", {}))
        self._seed_core_prompt_defaults(
            provider,
            agent_state["core_prompts"],
            agent_id=str(agent_state.get("agent_id", "")),
            display_name=str(agent_state.get("display_name", "")),
        )
        agent_state["credential_sync"] = self._normalize_credential_sync_state(
            agent_state.get("credential_sync"),
            default_when_missing=True,
        )
        agent_state["addons"] = self._normalize_agent_addons(agent_state.get("addons"))
        self._seed_delegation_skill(agent_state["core_prompts"], agent["plugins"])

    _DELEGATION_AGENTS_MARKER = "<!-- clawie-delegation-boot-begin -->"
    _DELEGATION_AGENTS_MARKER_END = "<!-- clawie-delegation-boot-end -->"
    _DELEGATION_AGENTS_SNIPPET = (
        "<!-- clawie-delegation-boot-begin -->\n"
        "5. Read `DELEGATION.md` if it exists — you have a recursive task "
        "delegation system managed by **Clawie** (the control plane that "
        "manages your runtime, channels, credentials, and plugins)\n"
        "<!-- clawie-delegation-boot-end -->"
    )

    @classmethod
    def _seed_delegation_skill(
        cls,
        core_prompts: dict[str, str],
        plugins: dict[str, bool],
    ) -> None:
        if not plugins.get("delegation", False):
            core_prompts.pop("DELEGATION.md", None)
            return
        if not core_prompts.get("DELEGATION.md"):
            try:
                from clawie.delegation import DELEGATION_SKILL_CONTENT

                core_prompts["DELEGATION.md"] = DELEGATION_SKILL_CONTENT
            except ImportError:
                pass
        # Ensure AGENTS.md tells the bot to read DELEGATION.md on startup.
        agents_md = core_prompts.get("AGENTS.md", "")
        if agents_md and cls._DELEGATION_AGENTS_MARKER not in agents_md:
            # Insert after the "4. **If in MAIN SESSION**" line or after
            # the last numbered step in the "Every Session" section.
            insertion_point = agents_md.find("\n\nDon't ask permission")
            if insertion_point == -1:
                insertion_point = agents_md.find("\n\n## Memory")
            if insertion_point != -1:
                core_prompts["AGENTS.md"] = (
                    agents_md[:insertion_point]
                    + "\n" + cls._DELEGATION_AGENTS_SNIPPET + "\n"
                    + agents_md[insertion_point:]
                )
            else:
                # Fallback: append to end
                core_prompts["AGENTS.md"] = agents_md + "\n\n" + cls._DELEGATION_AGENTS_SNIPPET + "\n"

    def _discover_channels_from_source_home(
        self,
        source_home: Path,
        requested_provider: str | None,
    ) -> list[dict[str, str]]:
        providers: list[str] = []
        if requested_provider:
            providers.append(str(requested_provider).strip().lower())
        config = self.store.read_config()
        providers.append(str(config.get("provider", "openclaw")).strip().lower())

        channels: list[dict[str, str]] = []
        for provider in providers:
            try:
                state_dir = get_provider(provider).state_dir
            except ValueError:
                continue
            adapter = get_channel_adapter(provider)
            channels.extend(adapter.discover_channels(source_home / state_dir))
        return dedupe_channels(channels)

    def set_agent_model_tier(self, agent_id: str, tier: str = "") -> str:
        """Set or cycle the model tier for *agent_id*. Returns the new tier."""
        from clawie.delegation import VALID_TIER_NAMES

        state = self.store.read_state()
        agents = state.setdefault("agents", state.get("users", {}))
        state["users"] = agents
        agent_state = agents.get(agent_id)
        if not agent_state:
            raise AgentNotFoundError(f"agent not found: {agent_id}")

        agent = agent_state.setdefault("agent", {})
        current = str(agent.get("model_tier", "balanced"))

        if tier:
            if tier not in VALID_TIER_NAMES:
                raise ValueError(
                    f"unknown tier {tier!r}; valid: {', '.join(VALID_TIER_NAMES)}"
                )
            new_tier = tier
        else:
            # Cycle: fast -> balanced -> power -> fast
            idx = list(VALID_TIER_NAMES).index(current) if current in VALID_TIER_NAMES else 0
            new_tier = VALID_TIER_NAMES[(idx + 1) % len(VALID_TIER_NAMES)]

        agent["model_tier"] = new_tier
        self._event(
            state,
            "agent.model_tier.changed",
            f"Agent {agent_id} model tier changed to {new_tier}",
            {"agent_id": agent_id, "old_tier": current, "new_tier": new_tier},
        )
        self.store.write_state(state)
        return new_tier

    # ── Delegation methods ────────────────────────────────────────────────

    def delegate_task(
        self,
        parent_id: str,
        child_id: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 300.0,
        model_tier: str = "",
    ) -> dict[str, Any]:
        from clawie.delegation import DelegationCoordinator, DelegationBus, DelegationTree, DEFAULT_TIER

        tier = model_tier or DEFAULT_TIER
        task_id = str(__import__("uuid").uuid4().hex)
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload or {},
            depth=0,
            timeout_seconds=timeout,
            model_tier=tier,
        )
        bus = DelegationBus(parent_id)
        tree = DelegationTree()
        coordinator = DelegationCoordinator(parent_id, bus, tree, model_tier=tier)
        try:
            result = coordinator.delegate(child_id, payload or {}, timeout=timeout)
        except Exception as exc:
            self.store.write_delegation_task(
                task_id=task_id,
                parent_agent_id=parent_id,
                child_agent_id=child_id,
                payload=payload or {},
                depth=0,
                timeout_seconds=timeout,
                status="failed",
                error=str(exc),
                model_tier=tier,
            )
            state = self.store.read_state()
            self._event(
                state,
                "delegation.failed",
                f"Delegation {parent_id}->{child_id} failed: {exc}",
                {"task_id": task_id, "parent": parent_id, "child": child_id},
            )
            self.store.write_state(state)
            return {"task_id": task_id, "status": "failed", "error": str(exc)}
        self.store.write_delegation_task(
            task_id=task_id,
            parent_agent_id=parent_id,
            child_agent_id=child_id,
            payload=payload or {},
            depth=0,
            timeout_seconds=timeout,
            status="completed",
            result=result,
            model_tier=tier,
        )
        tree_data = tree.to_dict()
        self.store.write_delegation_tree(parent_id, tree_data)
        state = self.store.read_state()
        self._event(
            state,
            "delegation.completed",
            f"Delegation {parent_id}->{child_id} completed",
            {"task_id": task_id, "parent": parent_id, "child": child_id},
        )
        self.store.write_state(state)
        return {"task_id": task_id, "status": "completed", "result": result}

    def start_agent_repl(self, agent_id: str, handler: Any = None, model_tier: str = "") -> None:
        from clawie.delegation import AgentREPL, DEFAULT_TIER

        tier = model_tier or DEFAULT_TIER

        def _default_handler(msg: Any, repl: Any) -> dict[str, Any]:
            return {"echo": msg.payload, "agent": agent_id}

        repl = AgentREPL(agent_id, handler=handler or _default_handler, model_tier=tier)
        import signal

        def _shutdown(*_a: Any) -> None:
            repl.stop()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
        repl.start()

    def delegation_tree(self, root_agent_id: str) -> dict[str, Any]:
        return self.store.read_delegation_tree(root_agent_id) or {}

    def delegation_tree_lines(self, root_agent_id: str) -> list[str]:
        from clawie.delegation import render_tree_ascii

        tree_data = self.store.read_delegation_tree(root_agent_id) or {}
        if not tree_data:
            return []
        return render_tree_ascii(tree_data, root_agent_id)

    def delegation_tasks(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.store.read_delegation_tasks(
            parent_agent_id=agent_id,
            status=status,
            limit=limit,
        )

    def cleanup_delegation(self) -> dict[str, Any]:
        from clawie.delegation import cleanup_stale_sockets, list_active_agents

        removed = cleanup_stale_sockets()
        active = list_active_agents()
        return {"removed_sockets": removed, "active_agents": active}

    # ── Maintenance cron ──────────────────────────────────────────────────

    def maintenance_enable(self, *, interval_hours: int = 4) -> dict[str, Any]:
        """Install a system cron job that periodically syncs agent credentials."""
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("maintenance enable requires root. Re-run with sudo.")
        clawie_bin = shutil.which("clawie") or "/usr/local/bin/clawie"
        hour_spec = f"*/{interval_hours}" if interval_hours < 24 else "0"
        cron_content = (
            "# Managed by clawie -- do not edit manually\n"
            "SHELL=/bin/bash\n"
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ":/home/linuxbrew/.linuxbrew/bin\n"
            f"0 {hour_spec} * * * root {clawie_bin} maintenance run"
            f" >> {self.MAINTENANCE_LOG_FILE} 2>&1\n"
        )
        self.MAINTENANCE_CRON_FILE.write_text(cron_content, encoding="utf-8")
        os.chmod(str(self.MAINTENANCE_CRON_FILE), 0o644)
        config = self.store.read_config()
        config["maintenance_cron_enabled"] = True
        config["maintenance_cron_interval_hours"] = interval_hours
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(state, "maintenance.enabled", f"Maintenance cron enabled (every {interval_hours}h)", {
            "interval_hours": interval_hours, "cron_file": str(self.MAINTENANCE_CRON_FILE),
        })
        self.store.write_state(state)
        return {"enabled": True, "cron_file": str(self.MAINTENANCE_CRON_FILE),
                "interval_hours": interval_hours, "clawie_binary": clawie_bin}

    def maintenance_disable(self) -> dict[str, Any]:
        """Remove the maintenance cron job."""
        self._require_setup()
        if os.geteuid() != 0:
            raise SetupError("maintenance disable requires root. Re-run with sudo.")
        removed = self.MAINTENANCE_CRON_FILE.exists()
        self.MAINTENANCE_CRON_FILE.unlink(missing_ok=True)
        config = self.store.read_config()
        config["maintenance_cron_enabled"] = False
        self.store.write_config(config)
        state = self.store.read_state()
        self._event(state, "maintenance.disabled", "Maintenance cron disabled", {})
        self.store.write_state(state)
        return {"enabled": False, "removed": removed}

    def maintenance_status(self) -> dict[str, Any]:
        """Check whether the maintenance cron job is installed."""
        self._require_setup()
        config = self.store.read_config()
        cron_exists = self.MAINTENANCE_CRON_FILE.exists()
        cron_content = ""
        if cron_exists:
            try:
                cron_content = self.MAINTENANCE_CRON_FILE.read_text(encoding="utf-8")
            except OSError:
                cron_content = "<unreadable>"
        return {
            "enabled": bool(config.get("maintenance_cron_enabled", False)),
            "cron_file_exists": cron_exists,
            "interval_hours": int(config.get("maintenance_cron_interval_hours", 4)),
            "cron_file": str(self.MAINTENANCE_CRON_FILE),
            "cron_content": cron_content,
        }

    def maintenance_run(self) -> dict[str, Any]:
        """Run maintenance tasks: sync credentials and apply staged prompts for all managed agents."""
        self._require_setup()

        # First, refresh the shared auth store from the freshest source (codex).
        # This converts codex OAuth tokens into openclaw/picoclaw auth-profiles
        # so agents get a live token instead of a stale copy.
        src_home = self._default_source_home()
        auth_refresh = "skipped"
        for source_type in ("codex", "claude"):
            try:
                self.import_shared_auth("openclaw", source=source_type, source_home=str(src_home))
                auth_refresh = f"ok ({source_type} from {src_home})"
                break
            except Exception:
                continue

        state = self.store.read_state()
        agents = state.get("agents", state.get("users", {}))
        results: dict[str, dict[str, str]] = {}
        errors = 0
        skipped = 0

        for agent_id, agent in agents.items():
            if not isinstance(agent, dict):
                continue
            info = agent.get("agent", {})
            linux_user = str(info.get("linux_user", "")).strip()
            if not linux_user:
                skipped += 1
                continue
            sync_cfg = agent.get("credential_sync", {})
            bundles = sync_cfg.get("bundles", []) if isinstance(sync_cfg, dict) else []

            entry: dict[str, str] = {}

            # Credential sync
            if bundles:
                try:
                    self.sync_agent_credentials(agent_id)
                    entry["credentials"] = "ok"
                except Exception as exc:
                    entry["credentials"] = f"error: {exc}"
                    errors += 1
            else:
                entry["credentials"] = "skipped (no bundles)"

            # Apply staged prompts
            try:
                applied = self.apply_staged_prompts(agent_id)
                count = len(applied.get("applied", []))
                entry["prompts"] = f"ok ({count} applied)" if count else "ok (none staged)"
            except Exception as exc:
                entry["prompts"] = f"error: {exc}"
                errors += 1

            results[agent_id] = entry

        self._event(state, "maintenance.run", f"Maintenance run: {len(results)} agents, {errors} errors", {
            "agents_processed": len(results), "skipped": skipped, "errors": errors,
        })
        self.store.write_state(state)
        return {
            "auth_refresh": auth_refresh,
            "agents_processed": len(results),
            "agents_skipped": skipped,
            "errors": errors,
            "results": results,
        }

    # ── Session agent methods ─────────────────────────────────────────────

    _session_managers: dict[str, Any] = {}

    def _get_session_manager(self, parent_id: str) -> Any:
        if parent_id not in self._session_managers:
            from clawie.delegation import SessionAgentManager

            self._session_managers[parent_id] = SessionAgentManager(parent_id)
        return self._session_managers[parent_id]

    def spawn_session_agent(
        self,
        parent_id: str,
        child_id: str,
        timeout: float = 300.0,
        model_tier: str = "",
    ) -> dict[str, Any]:
        from clawie.delegation import DEFAULT_TIER

        tier = model_tier or DEFAULT_TIER
        mgr = self._get_session_manager(parent_id)
        info = mgr.spawn(child_id, timeout=timeout, model_tier=tier)
        state = self.store.read_state()
        self._event(
            state,
            "session.agent.spawned",
            f"Session agent {child_id} spawned under {parent_id}",
            {"parent": parent_id, "child": child_id, "model_tier": tier},
        )
        self.store.write_state(state)
        return dict(info)

    def stop_session_agent(self, parent_id: str, child_id: str) -> None:
        mgr = self._get_session_manager(parent_id)
        mgr.stop_agent(child_id)
        state = self.store.read_state()
        self._event(
            state,
            "session.agent.stopped",
            f"Session agent {child_id} stopped under {parent_id}",
            {"parent": parent_id, "child": child_id},
        )
        self.store.write_state(state)

    def stop_all_session_agents(self, parent_id: str) -> None:
        if parent_id in self._session_managers:
            self._session_managers[parent_id].stop_all()
            del self._session_managers[parent_id]

    def list_session_agents(self, parent_id: str) -> list[dict[str, Any]]:
        if parent_id not in self._session_managers:
            return []
        return self._session_managers[parent_id].list_agents()

    def session_tree_lines(self, parent_id: str) -> list[str]:
        if parent_id not in self._session_managers:
            return []
        return self._session_managers[parent_id].tree_lines()
