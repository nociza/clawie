"""Central service layer for clawie (composed from concern-based mixins)."""
from __future__ import annotations

import copy
import json
import os
import pwd
import subprocess

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
from clawie._service_reconcile import ReconcileOpsMixin
from clawie._service_escalation import ControlEscalationMixin
from clawie._service_watchdog import ControlWatchdogMixin

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
    ReconcileOpsMixin,
    ControlEscalationMixin,
    ControlWatchdogMixin,
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
    CONTROL_WATCHDOG_UNIT_FILE = Path("/etc/systemd/system/clawie-control-watchdog.service")
    CONTROL_WATCHDOG_ALERT_UNIT_FILE = Path("/etc/systemd/system/clawie-control-alert.service")
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
            "label": "provider auth sessions (.codex/auth.json + provider auth stores)",
            "default": False,
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
    DEFAULT_CREDENTIAL_BUNDLES: tuple[str, ...] = ()
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
        self._session_managers: dict[str, Any] = {}

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

        agents = state.setdefault("agents", {})
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
        checks.extend(self._host_isolation_checks(agents))

        overall = "healthy"
        if any(check["status"] == "fail" for check in checks):
            overall = "unhealthy"
        elif any(check["status"] == "warn" for check in checks):
            overall = "degraded"

        return {"status": overall, "checks": checks}

    def _host_isolation_checks(self, agents: dict[str, Any]) -> list[dict[str, str]]:
        checks: list[dict[str, str]] = []
        shared_home = self._shared_provider_auth_home()
        if shared_home.exists():
            try:
                mode = int(shared_home.stat().st_mode) & 0o777
            except OSError as exc:
                checks.append({"status": "warn", "message": f"Cannot inspect shared provider auth store: {exc}"})
            else:
                if mode & 0o077:
                    checks.append(
                        {
                            "status": "fail",
                            "message": f"Shared provider auth store is not private: {shared_home} mode {mode:o}",
                        }
                    )
                else:
                    checks.append({"status": "pass", "message": "Shared provider auth store is private"})

        consumers: list[str] = []
        verified: list[str] = []
        for aid, agent in sorted(agents.items()):
            self._hydrate_agent_controls(agent)
            sync = self._normalize_credential_sync_state(agent.get("credential_sync"), default_when_missing=True)
            if not bool(sync.get("shared_provider_auth", False)):
                continue
            consumers.append(str(aid))
            home = self._agent_linux_home(agent)
            if home is None or not home.exists():
                checks.append({"status": "warn", "message": f"Cannot inspect provider auth for {aid}: home missing"})
                continue
            copied = 0
            unsafe = False
            for rel in self._credential_bundle_paths("provider-auth"):
                path = home / rel
                if path.is_symlink():
                    unsafe = True
                    checks.append({"status": "fail", "message": f"Provider auth path is a symlink: {path}"})
                    continue
                if not path.exists() or not path.is_file():
                    continue
                copied += 1
                try:
                    mode = int(path.stat().st_mode) & 0o777
                except OSError as exc:
                    unsafe = True
                    checks.append({"status": "warn", "message": f"Cannot inspect provider auth path {path}: {exc}"})
                    continue
                if mode & 0o077:
                    unsafe = True
                    checks.append({"status": "fail", "message": f"Provider auth file is not private: {path} mode {mode:o}"})
            if copied == 0:
                checks.append({"status": "warn", "message": f"Agent {aid} uses shared provider auth but has no copied provider auth files"})
            elif not unsafe:
                verified.append(str(aid))

        if consumers and verified:
            checks.append({"status": "pass", "message": "Private provider auth copies verified for: " + ", ".join(verified)})
        elif not consumers:
            checks.append({"status": "pass", "message": "No agents consume shared provider auth"})
        return checks

    def host_validation_report(self) -> dict[str, Any]:
        """Run Linux/root-only host isolation validation against provisioned agents.

        This is intentionally separate from ``doctor``: normal health checks are
        read-only and portable, while this proof requires root so it can attempt
        cross-user access checks with the real OS users.
        """
        checks: list[dict[str, str]] = []
        if not self._linux_proc_available():
            return {
                "status": "skipped",
                "checks": [
                    {
                        "status": "skip",
                        "message": "Host validation requires Linux with /proc available",
                    }
                ],
            }
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return {
                "status": "skipped",
                "checks": [
                    {
                        "status": "skip",
                        "message": "Host validation requires root so cross-user read checks can run",
                    }
                ],
            }

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        managed = self._host_validation_managed_agents(agents)
        distinct_users = {row["linux_user"] for row in managed}
        if len(distinct_users) < 2:
            checks.append(
                {
                    "status": "fail",
                    "message": "Host validation requires at least two managed Linux-user agents",
                }
            )
        else:
            checks.append(
                {
                    "status": "pass",
                    "message": f"Found {len(managed)} managed agents across {len(distinct_users)} Linux users",
                }
            )

        for row in managed:
            checks.extend(self._host_validation_agent_checks(row))

        cross_user_checks = 0
        for reader in managed:
            for target in managed:
                if reader["linux_user"] == target["linux_user"]:
                    continue
                for path in self._host_validation_sensitive_paths(target):
                    cross_user_checks += 1
                    ok, detail = self._path_unreadable_as_user(path, reader["linux_user"])
                    if ok:
                        checks.append(
                            {
                                "status": "pass",
                                "message": f"{reader['linux_user']} cannot read {path}",
                            }
                        )
                    else:
                        message = f"{reader['linux_user']} can read or probe failed for {path}"
                        if detail:
                            message = f"{message}: {detail}"
                        checks.append({"status": "fail", "message": message})
        if len(distinct_users) >= 2 and cross_user_checks == 0:
            checks.append(
                {
                    "status": "fail",
                    "message": "No sensitive host paths were available for cross-user read validation",
                }
            )

        status = "passed"
        if any(row["status"] == "fail" for row in checks):
            status = "failed"
        elif any(row["status"] == "warn" for row in checks):
            status = "degraded"
        elif any(row["status"] == "skip" for row in checks):
            status = "skipped"
        return {"status": status, "checks": checks}

    def production_readiness_report(
        self,
        *,
        exercise_watchdog_restart: bool = False,
        watchdog_timeout_seconds: int = 30,
        all_provider_contracts: bool = False,
    ) -> dict[str, Any]:
        """Aggregate the target-host proof gates required for production.

        This report is deliberately stricter than ``doctor``. Normal health can
        be useful on a developer machine, but production readiness requires
        target-host isolation proof, watchdog restart proof, and no registered
        production provider lacking a source-pinned delivery adapter contract.
        """
        checks: list[dict[str, Any]] = []

        def add(name: str, status: str, message: str, evidence: dict[str, Any] | None = None) -> None:
            row: dict[str, Any] = {"name": name, "status": status, "message": message}
            if evidence is not None:
                row["evidence"] = evidence
            checks.append(row)

        try:
            doctor = self.doctor()
        except Exception as exc:  # noqa: BLE001 - readiness reports all failed gates.
            add("doctor", "fail", "standard health checks could not run", {"error": str(exc)})
        else:
            doctor_status = str(doctor.get("status", "unknown"))
            if doctor_status == "healthy":
                add("doctor", "pass", "standard health checks are healthy", doctor)
            elif doctor_status == "degraded":
                add("doctor", "warn", "standard health checks are degraded", doctor)
            else:
                add("doctor", "fail", "standard health checks are not healthy", doctor)

        try:
            host = self.host_validation_report()
        except Exception as exc:  # noqa: BLE001 - readiness reports all failed gates.
            add("host_validation", "fail", "Linux/root host isolation proof could not run", {"error": str(exc)})
        else:
            host_status = str(host.get("status", "unknown"))
            if host_status == "passed":
                add("host_validation", "pass", "Linux/root host isolation proof passed", host)
            else:
                add(
                    "host_validation",
                    "fail",
                    "Linux/root host isolation proof did not pass",
                    host,
                )

        try:
            watchdog = self.control_watchdog_verify(
                exercise_restart=exercise_watchdog_restart,
                timeout_seconds=watchdog_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - readiness reports all failed gates.
            add("watchdog", "fail", "systemd watchdog proof could not run", {"error": str(exc)})
        else:
            watchdog_status = str(watchdog.get("status", "unknown"))
            if watchdog_status == "passed":
                add("watchdog", "pass", "systemd watchdog proof passed", watchdog)
                if bool(watchdog.get("restart_exercised", False)):
                    add(
                        "watchdog_restart_exercise",
                        "pass",
                        "systemd watchdog restart was exercised",
                        watchdog,
                    )
                else:
                    add(
                        "watchdog_restart_exercise",
                        "fail",
                        "production readiness requires --exercise-watchdog-restart",
                        watchdog,
                    )
            else:
                add("watchdog", "fail", "systemd watchdog proof did not pass", watchdog)

        try:
            checks.extend(
                self._production_runtime_adapter_contract_checks(
                    all_provider_contracts=all_provider_contracts,
                )
            )
        except Exception as exc:  # noqa: BLE001 - readiness reports all failed gates.
            add(
                "runtime_adapters",
                "fail",
                "runtime adapter contract checks could not run",
                {"error": str(exc)},
            )

        status = "passed"
        if any(row["status"] == "fail" for row in checks):
            status = "failed"
        elif any(row["status"] == "warn" for row in checks):
            status = "degraded"
        return {
            "status": status,
            "generated_at": now_iso(),
            "checks": checks,
            "exercise_watchdog_restart": bool(exercise_watchdog_restart),
            "all_provider_contracts": bool(all_provider_contracts),
        }

    def _production_runtime_adapter_contract_checks(
        self,
        *,
        all_provider_contracts: bool = False,
    ) -> list[dict[str, Any]]:
        from clawie.adapters import AdapterError, get_adapter
        from clawie.providers import verified_delivery_provider_names

        providers: set[str] = set()
        if all_provider_contracts:
            providers.update(verified_delivery_provider_names())
        config = self.store.read_config()
        provider = str(config.get("provider", "") or "").strip().lower()
        if provider:
            providers.add(provider)
        state = self.store.read_state()
        agents = state.get("agents", {})
        if isinstance(agents, dict):
            for payload in agents.values():
                if not isinstance(payload, dict):
                    continue
                info = payload.get("agent", {})
                if not isinstance(info, dict):
                    continue
                provider = str(info.get("provider", "") or "").strip().lower()
                if provider:
                    providers.add(provider)
        if not providers:
            providers.add("openclaw")

        checks: list[dict[str, Any]] = []
        for provider in sorted(providers):
            checks.append(self._production_runtime_adapter_contract_check(provider, get_adapter))
        return checks

    @staticmethod
    def _production_runtime_adapter_contract_check(provider: str, get_adapter: Any) -> dict[str, Any]:
        from clawie.adapters import AdapterError
        from clawie.providers import get_provider

        name = f"runtime_adapter_{provider}"
        try:
            spec = get_provider(provider)
        except ValueError:
            spec = None
        if spec is not None and not bool(spec.verified_delivery):
            return {
                "name": name,
                "status": "fail",
                "message": f"Provider {provider} does not have a verified delegated-task delivery adapter",
                "evidence": {
                    "provider": provider,
                    "runtime": spec.runtime,
                    "delivery_note": spec.delivery_note,
                },
            }
        try:
            adapter = get_adapter(provider)
        except AdapterError as exc:
            return {
                "name": name,
                "status": "fail",
                "message": f"No verified delivery adapter is registered for provider {provider}",
                "evidence": {"provider": provider, "error": str(exc)},
            }
        models = {
            "fast": str(adapter.TIER_MODELS.get("fast", "") or "").strip(),
            "balanced": str(adapter.TIER_MODELS.get("balanced", "") or "").strip(),
            "power": str(adapter.TIER_MODELS.get("power", "") or "").strip(),
        }
        evidence: dict[str, Any] = {
            "provider": provider,
            "adapter": str(getattr(adapter, "name", "") or "").strip(),
            "contract_verified": bool(getattr(adapter, "CONTRACT_VERIFIED", False)),
            "models": models,
            "default_model": str(getattr(adapter, "DEFAULT_MODEL", "") or "").strip(),
        }
        if not evidence["contract_verified"]:
            return {
                "name": name,
                "status": "fail",
                "message": f"Provider {provider} adapter contract is not source-pinned",
                "evidence": evidence,
            }
        if not all(models.values()) or not evidence["default_model"]:
            return {
                "name": name,
                "status": "fail",
                "message": f"Provider {provider} adapter model mapping is incomplete",
                "evidence": evidence,
            }
        try:
            adapter.tier_to_model("balanced")
        except AdapterError as exc:
            return {
                "name": name,
                "status": "fail",
                "message": f"Provider {provider} adapter refuses model selection: {exc}",
                "evidence": evidence,
            }
        return {
            "name": name,
            "status": "pass",
            "message": f"Provider {provider} adapter contract is verified",
            "evidence": evidence,
        }

    @staticmethod
    def _linux_proc_available() -> bool:
        return os.name == "posix" and Path("/proc").exists()

    def _host_validation_managed_agents(self, agents: dict[str, Any]) -> list[dict[str, Any]]:
        managed: list[dict[str, Any]] = []
        for aid, payload in sorted(agents.items()):
            if not isinstance(payload, dict):
                continue
            self._hydrate_agent_controls(payload)
            info = payload.setdefault("agent", {})
            if bool(info.get("local_user", False)):
                continue
            linux_user = str(info.get("linux_user", "")).strip()
            if not linux_user:
                continue
            managed.append(
                {
                    "agent_id": str(aid),
                    "linux_user": linux_user,
                    "home": self._agent_linux_home(payload),
                    "payload": payload,
                }
            )
        return managed

    def _host_validation_agent_checks(self, row: dict[str, Any]) -> list[dict[str, str]]:
        checks: list[dict[str, str]] = []
        agent_id = str(row.get("agent_id", ""))
        linux_user = str(row.get("linux_user", ""))
        home = row.get("home")
        try:
            pwd.getpwnam(linux_user)
        except KeyError:
            checks.append({"status": "fail", "message": f"Linux user does not exist for {agent_id}: {linux_user}"})
        else:
            checks.append({"status": "pass", "message": f"Linux user exists for {agent_id}: {linux_user}"})

        if not isinstance(home, Path) or not home.exists():
            checks.append({"status": "fail", "message": f"Home directory is missing for {agent_id}: {home}"})
            return checks
        if home.is_symlink():
            checks.append({"status": "fail", "message": f"Home directory is a symlink for {agent_id}: {home}"})
        try:
            mode = int(home.stat().st_mode) & 0o777
        except OSError as exc:
            checks.append({"status": "fail", "message": f"Cannot inspect home mode for {agent_id}: {exc}"})
        else:
            if mode & 0o077:
                checks.append({"status": "fail", "message": f"Home directory is not private for {agent_id}: {home} mode {mode:o}"})
            else:
                checks.append({"status": "pass", "message": f"Home directory is private for {agent_id}: {home}"})

        for path in self._host_validation_credential_paths(row):
            if path.is_symlink():
                checks.append({"status": "fail", "message": f"Credential path is a symlink for {agent_id}: {path}"})
                continue
            try:
                mode = int(path.stat().st_mode) & 0o777
            except OSError as exc:
                checks.append({"status": "fail", "message": f"Cannot inspect credential path for {agent_id}: {path}: {exc}"})
                continue
            if mode & 0o077:
                checks.append({"status": "fail", "message": f"Credential file is not private for {agent_id}: {path} mode {mode:o}"})
            else:
                checks.append({"status": "pass", "message": f"Credential file is private for {agent_id}: {path}"})
        return checks

    def _host_validation_sensitive_paths(self, row: dict[str, Any]) -> list[Path]:
        home = row.get("home")
        if not isinstance(home, Path) or not home.exists():
            return []
        paths = [home]
        paths.extend(self._host_validation_credential_paths(row))
        return paths

    def _host_validation_credential_paths(self, row: dict[str, Any]) -> list[Path]:
        home = row.get("home")
        if not isinstance(home, Path):
            return []
        paths: list[Path] = []
        for rel in self._credential_bundle_paths("provider-auth"):
            path = home / rel
            if path.exists() or path.is_symlink():
                paths.append(path)
        return paths

    def _path_unreadable_as_user(self, path: Path, linux_user: str) -> tuple[bool, str]:
        try:
            cmd = self._wrap_user_command(
                ["test", "!", "-r", str(path)],
                linux_user,
                purpose="host validation",
            )
        except SetupError as exc:
            return False, str(exc)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if str(part).strip()).strip()
        return result.returncode == 0, output

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
            merged_state.setdefault("agents", {})
            merged_state.setdefault("events", [])
            merged_state["templates"].update(state.get("templates", {}))
            merged_state["agents"].update(state.get("agents", {}))
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
