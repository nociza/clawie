"""Linux user provisioning for managed agents (ZeroClawService mixin)."""
from __future__ import annotations

import copy
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any
try:
    import crypt  # removed from the stdlib in Python 3.13 (PEP 594)
except ModuleNotFoundError:  # pragma: no cover
    crypt = None  # type: ignore[assignment]
from clawie.providers import (
    get_provider,
)
from clawie.service_common import SetupError, AgentExistsError, now_iso


class SpawnOpsMixin:

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
