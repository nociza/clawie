"""Shared addon install, auth, and agent attachment (ZeroClawService mixin)."""
from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from clawie.addon_auth import inspect_addon_auth, parse_gws_exported_credentials, parse_gws_status_output
from clawie.addon_integration import (
    inject_addon_env_block,
    inject_addon_tools_snippet,
    remove_addon_env_block,
    remove_addon_tools_snippet,
    render_addon_env_block,
)
from clawie.addons import ServiceAddonSpec, ToolAddonSpec, addon_names, get_addon, is_service_addon
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
from clawie.service_common import SetupError, AgentNotFoundError, now_iso


class AddonOpsMixin:

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
                subprocess.run(["chown", f"{linux_user}:{linux_user}", str(tools_path)], check=False, capture_output=True)
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
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(path)], check=False, capture_output=True)

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
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(tools_path)], check=False, capture_output=True)
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
                    subprocess.run(["chown", f"{linux_user}:{linux_user}", str(path)], check=False, capture_output=True)

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

    def _resolve_addon_executable(self, addon: str) -> str:
        spec = get_addon(addon)
        resolved = self._resolve_executable_in_service_env(spec.executable)
        if resolved:
            return resolved
        raise SetupError(
            f"addon executable '{spec.executable}' was not found in PATH. Run 'clawie addon install {spec.name}' first."
        )

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
