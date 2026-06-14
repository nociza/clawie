"""Central service layer for clawie (composed from concern-based mixins)."""
from __future__ import annotations

import copy
import json
import os
import pwd

# Kept importable as ``clawie.service.shutil`` so tests can monkeypatch
# ``clawie.service.shutil.which``; the shutil module object is shared, so this
# also patches the executable lookups performed in the runtime mixins.
import shutil  # noqa: F401
from pathlib import Path
from typing import Any
from clawie.providers import (
    detect_installed_providers,
    get_provider,
)
from clawie.store import StateStore

# Re-exported for backwards compatibility: callers (clawie.cli, tests) import
# these names from clawie.service.
from clawie.service_common import SetupError, AgentExistsError, AgentNotFoundError, now_iso, redact, _LEGACY_HEARTBEAT_PROMPT, _default_core_prompt_content, _is_legacy_core_prompt_default  # noqa: F401
from clawie._service_shared import SharedInfraMixin
from clawie._service_auth import ProviderAuthMixin
from clawie._service_addons import AddonOpsMixin
from clawie._service_backup import BackupOpsMixin
from clawie._service_channels import ChannelOpsMixin
from clawie._service_runtime import RuntimeOpsMixin
from clawie._service_spawn import SpawnOpsMixin
from clawie._service_credentials import CredentialOpsMixin
from clawie._service_prompts import PromptOpsMixin
from clawie._service_agents import AgentOpsMixin
from clawie._service_telemetry import TelemetryOpsMixin
from clawie._service_delegation import DelegationOpsMixin

# Sections aggregated by ``status_snapshot`` / ``clawie status``, in display order.
STATUS_SECTIONS: tuple[str, ...] = (
    "setup",
    "health",
    "agents",
    "runtimes",
    "auth",
    "delegation",
    "maintenance",
    "backup",
    "events",
)


class ClawieService(
    SharedInfraMixin,
    ProviderAuthMixin,
    AddonOpsMixin,
    BackupOpsMixin,
    ChannelOpsMixin,
    RuntimeOpsMixin,
    SpawnOpsMixin,
    CredentialOpsMixin,
    PromptOpsMixin,
    AgentOpsMixin,
    TelemetryOpsMixin,
    DelegationOpsMixin,
):
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
            "# Managed by clawie: use each user's private Claude Code config directory.",
            "# Older clawie releases exported CLAUDE_CONFIG_DIR=/var/lib/clawie/claude-shared.",
            'if [ "${CLAUDE_CONFIG_DIR:-}" = "/var/lib/clawie/claude-shared" ]; then',
            "  unset CLAUDE_CONFIG_DIR",
            "fi",
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
                    "message": "Provider credentials are missing. Run 'clawie config set'.",
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

    def status_snapshot(
        self,
        agent_id: str | None = None,
        sections: list[str] | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Aggregate every clawie status surface into one read-only snapshot.

        Each section is collected independently: a failure in one (e.g. an
        unconfigured maintenance cron, or an unreadable runtime) is captured as
        an ``{"error": ...}`` entry rather than aborting the whole report. That
        makes ``clawie status`` safe to run on a half-broken system, which is
        exactly when it is most useful.

        With ``refresh=False`` (the default) no metrics are sampled and nothing
        is written, so this is a pure read. ``refresh=True`` samples live
        CPU/memory for the agents section.
        """
        agent = (agent_id or "").strip() or None
        wanted = self._resolve_status_sections(sections)
        collectors: dict[str, Any] = {
            "setup": self.setup_status,
            "health": self.doctor,
            "agents": lambda: self.performance_snapshot(agent_id=agent, refresh=refresh),
            "runtimes": lambda: self.list_local_runtime_statuses(refresh=refresh),
            "auth": self.list_shared_auth_statuses,
            "delegation": lambda: {
                "tasks": self.delegation_tasks(agent_id=agent, limit=10),
                "active_agents": self.active_delegation_agents(),
            },
            "maintenance": self.maintenance_status,
            "backup": self.backup_status,
            "events": lambda: self.list_events(limit=10),
        }

        result: dict[str, Any] = {"generated_at": now_iso()}
        if agent:
            result["agent_id"] = agent
        for name in wanted:
            collect = collectors.get(name)
            if collect is None:
                continue
            try:
                result[name] = collect()
            except Exception as exc:  # noqa: BLE001 - status must survive partial failures
                result[name] = {"error": str(exc)}
        return result

    def _resolve_status_sections(self, sections: list[str] | None) -> list[str]:
        if not sections:
            return list(STATUS_SECTIONS)
        resolved: list[str] = []
        for raw in sections:
            name = str(raw).strip().lower()
            if name not in STATUS_SECTIONS:
                raise ValueError(
                    f"unknown status section '{raw}'. choose from: "
                    + ", ".join(STATUS_SECTIONS)
                )
            if name not in resolved:
                resolved.append(name)
        return resolved

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
        # The snapshot carries unredacted credentials (api keys, password
        # hashes); keep it private to the exporting user.
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
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
            raise SetupError("setup is incomplete. Run 'clawie config set'.")

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

    _DELEGATION_AGENTS_MARKER = "<!-- clawie-delegation-boot-begin -->"
    _DELEGATION_AGENTS_MARKER_END = "<!-- clawie-delegation-boot-end -->"
    _DELEGATION_AGENTS_SNIPPET = (
        "<!-- clawie-delegation-boot-begin -->\n"
        "5. Read `DELEGATION.md` if it exists — you have a recursive task "
        "delegation system managed by **Clawie** (the control plane that "
        "manages your runtime, channels, credentials, and plugins)\n"
        "<!-- clawie-delegation-boot-end -->"
    )

    # ── Session agent methods ─────────────────────────────────────────────

    _session_managers: dict[str, Any] = {}
