"""Linux user provisioning for managed agents (ClawieService mixin)."""
from __future__ import annotations

import copy
import math
import os
import pwd
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Any
try:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="'crypt' is deprecated.*",
            category=DeprecationWarning,
        )
        import crypt  # removed from the stdlib in Python 3.13 (PEP 594)
except ModuleNotFoundError:  # pragma: no cover
    crypt = None  # type: ignore[assignment]
from clawie.providers import (
    get_provider,
)
from clawie.service_common import SetupError, AgentExistsError, now_iso
from clawie.safe_fs import UnsafePathError, ensure_directory_under, read_text_under, write_text_under

_MANAGED_USER_MARKER = ".clawie-managed-user.json"


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
        saga: dict[str, Any] = {"operation_id": secrets.token_hex(16)}
        target_user = str(linux_user or agent_id).strip()
        try:
            return self._spawn_linux_user_impl(
                agent_id=agent_id,
                linux_user=linux_user,
                copy_configs=copy_configs,
                source_home=source_home,
                template=template,
                agent_version=agent_version,
                provider=provider,
                password=password,
                password_hash=password_hash,
                use_global_password=use_global_password,
                clone_from_agent=clone_from_agent,
                credential_bundles=credential_bundles,
                include_default_credentials=include_default_credentials,
                plugin_overrides=plugin_overrides,
                _saga=saga,
            )
        except Exception as exc:
            cleanup_errors = self._rollback_spawn_linux_user(
                agent_id=str(agent_id).strip(),
                linux_user=target_user,
                saga=saga,
            )
            if cleanup_errors and hasattr(exc, "add_note"):
                exc.add_note("spawn rollback warnings: " + "; ".join(cleanup_errors))
            raise

    def _spawn_linux_user_impl(
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
        _saga: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        saga = _saga if _saga is not None else {}
        self._require_setup()
        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id is required")

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
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

        if self._linux_user_exists(target_user):
            raise AgentExistsError(f"linux user already exists: {target_user}")

        spawn_shell = self._spawn_user_shell()
        subprocess.run(["useradd", "-m", "-s", spawn_shell, target_user], check=True)
        saga["user_created"] = True
        # useradd can exit 0 yet skip the home (full disk, CREATE_HOME=no). Once
        # the user is registered in the password database, confirm its home
        # really exists so later credential/config copies fail with a clear
        # cause instead of a cryptic downstream error.
        try:
            created_account = pwd.getpwnam(target_user)
            created_home = Path(created_account.pw_dir)
        except KeyError as exc:
            raise SetupError(
                f"useradd succeeded but {target_user} has no passwd database entry"
            ) from exc
        if not created_home.is_dir():
            raise SetupError(
                f"useradd succeeded but home directory {created_home} was not created "
                "(check disk space and CREATE_HOME in /etc/login.defs)."
            )
        created_uid = int(created_account.pw_uid)
        if created_uid <= 0:
            raise SetupError(
                f"refusing managed user {target_user} with unsafe uid {created_uid}"
            )
        target_home = created_home
        saga["home"] = str(created_home)
        marker = {
            "format_version": 1,
            "agent_id": agent_id,
            "linux_user": target_user,
            "linux_uid": created_uid,
            "operation_id": str(saga.get("operation_id", "")),
            "state_root": str(self.store.root.resolve()),
        }
        self._write_agent_json_file(
            target_home,
            _MANAGED_USER_MARKER,
            marker,
            target_user,
        )
        saga["marker_written"] = True
        password_source, password_value = self._apply_spawn_password(
            username=target_user,
            password=password,
            password_hash=password_hash,
            use_global_password=use_global_password,
        )
        ssh_login_disabled = self._disable_ssh_login_for_user(target_user)
        saga["ssh_login_disabled"] = bool(ssh_login_disabled)

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
        saga["agent_created"] = True
        saga["agent_created_at"] = str(agent_state.get("created_at", ""))
        agent_state["agent"]["linux_user"] = target_user
        agent_state["agent"]["linux_uid"] = created_uid
        agent_state["agent"]["linux_home"] = str(target_home)
        agent_state["agent"]["linux_user_managed"] = True
        agent_state["agent"]["managed_user_operation_id"] = str(saga.get("operation_id", ""))
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
        permission_warnings = self._ensure_workspace_accessible(
            resolved_provider, target_home, target_user
        )
        if permission_warnings:
            raise SetupError(
                "failed to enforce private agent-home permissions: "
                + "; ".join(permission_warnings)
            )
        state = self.store.read_state()
        self._event(
            state,
            "agents.spawned",
            f"Spawned linux user {target_user} for {agent_id}",
            {
                "agent_id": agent_id,
                "linux_user": target_user,
                "permission_warnings": permission_warnings,
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
        agents = state.setdefault("agents", {})
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

    def _rollback_spawn_linux_user(
        self,
        *,
        agent_id: str,
        linux_user: str,
        saga: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if saga.get("agent_created"):
            try:
                state = self.store.read_state()
                current = state.get("agents", {}).get(agent_id)
                if isinstance(current, dict) and str(current.get("created_at", "")) == str(
                    saga.get("agent_created_at", "")
                ):
                    del state["agents"][agent_id]
                    self.store.write_state(state)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve original spawn error.
                errors.append(f"agent record cleanup failed: {cleanup_exc}")
        if saga.get("ssh_login_disabled"):
            try:
                self._remove_ssh_login_denial(linux_user)
            except Exception as cleanup_exc:  # noqa: BLE001
                errors.append(f"SSH deny cleanup failed: {cleanup_exc}")
        if saga.get("user_created") and os.geteuid() == 0:
            try:
                outcome = subprocess.run(
                    ["userdel", "-r", linux_user],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if outcome.returncode != 0:
                    detail = (outcome.stderr or outcome.stdout or "").strip()
                    errors.append(f"user rollback failed: {detail or f'exit {outcome.returncode}'}")
            except Exception as cleanup_exc:  # noqa: BLE001
                errors.append(f"user rollback failed: {cleanup_exc}")
        return errors

    def _remove_ssh_login_denial(self, username: str) -> bool:
        path = self.SSHD_DENY_USERS_FILE
        if not path.exists():
            return False
        users: set[str] = set()
        for line in read_text_under(path.parent, path.name, max_bytes=1024 * 1024).splitlines():
            token = line.strip()
            if token.lower().startswith("denyusers"):
                users.update(part for part in token.split()[1:] if part)
        if username not in users:
            return False
        users.remove(username)
        rendered = "# Managed by clawie. Spawned users are denied SSH login.\n"
        if users:
            rendered += f"DenyUsers {' '.join(sorted(users))}\n"
        write_text_under(path.parent, path.name, rendered, mode=0o644)
        for service_name in ("ssh", "sshd"):
            result = subprocess.run(
                ["systemctl", "reload", service_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return True
        raise SetupError("updated ssh deny-users config but failed to reload ssh daemon")

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
            try:
                current = self._read_agent_text_file(target_home, rel)
            except FileNotFoundError:
                current = ""
            if self.SHARED_TOOLCHAIN_BEGIN in current and self.SHARED_TOOLCHAIN_END in current:
                continue
            rendered = current
            if rendered and not rendered.endswith("\n"):
                rendered += "\n"
            rendered += self.SHARED_TOOLCHAIN_BLOCK
            self._write_agent_text_file(target_home, rel, rendered, username, mode=0o600)
            updated.append(str(path))
        return updated

    def _ensure_shared_claude_links(self, target_home: Path, username: str) -> list[str]:
        """Copy seeded Claude config into an agent home as private files."""
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
            try:
                source_st = src.lstat()
            except FileNotFoundError:
                continue
            relative = dst.relative_to(target_home)
            if stat.S_ISDIR(source_st.st_mode):
                self._copy_tree_to_agent(src.parent, src.name, target_home, relative, username)
            elif stat.S_ISREG(source_st.st_mode):
                self._copy_file_to_agent(src.parent, src.name, target_home, relative, username)
            else:
                raise SetupError(f"refusing to copy symlink or special shared config: {src}")
            updated.append(str(dst))
        return updated

    @staticmethod
    def _write_managed_text(path: Path, content: str, mode: int) -> bool:
        changed = not path.exists()
        if path.exists():
            try:
                current = read_text_under(path.parent, path.name, max_bytes=4 * 1024 * 1024)
            except OSError:
                current = ""
            if current != content:
                changed = True
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_under(path.parent, path.name, content, mode=mode)
        current_mode = int(path.stat().st_mode) & 0o777
        if current_mode != mode:
            os.chmod(path, mode)
            changed = True
        return changed

    def _seed_shared_claude_state(self, source_home: Path) -> list[str]:
        shared = self.SHARED_CLAUDE_DIR
        updated: list[str] = []
        mapping = [
            (source_home / ".claude" / ".credentials.json", shared / ".credentials.json"),
            (source_home / ".claude.json", shared / ".claude.json"),
        ]
        for src, dst in mapping:
            if src.exists() and not dst.exists() and self._copy_if_present(src, dst):
                updated.append(str(dst))
            if dst.exists():
                current_mode = int(dst.stat().st_mode) & 0o777
                if current_mode != 0o600:
                    os.chmod(dst, 0o600)
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

        shared_paths = [self.SHARED_CLAUDE_DIR]
        for token in self.SHARED_CLAUDE_SUBDIRS:
            shared_paths.append(self.SHARED_CLAUDE_DIR / token)
        for path in shared_paths:
            existed = path.exists()
            path.mkdir(parents=True, exist_ok=True)
            target_mode = 0o700
            current_mode = int(path.stat().st_mode) & 0o777
            if (not existed) or current_mode != target_mode:
                os.chmod(path, target_mode)
                updated.append(str(path))

        # Sweep all existing children to fix dirs created by Claude Code at runtime.
        if self.SHARED_CLAUDE_DIR.is_dir():
            for child in self.SHARED_CLAUDE_DIR.iterdir():
                if child.is_dir():
                    try:
                        target_mode = 0o700
                        current_mode = int(child.stat().st_mode) & 0o777
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
                        if current_mode != 0o600:
                            os.chmod(child, 0o600)
                            if str(child) not in updated:
                                updated.append(str(child))
                    except OSError:
                        pass

        for path in self._seed_shared_claude_state(source_home):
            if path not in updated:
                updated.append(path)

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
            write_text_under(path.parent, path.name, rendered, mode=0o644)
        except OSError as exc:
            raise SetupError(f"failed writing ssh deny-users config: {exc}") from exc

        # Validate the config before asking the daemon to reload it, so a bad
        # drop-in can't lock everyone out of SSH. Skip if sshd isn't on PATH.
        sshd_bin = shutil.which("sshd")
        if not sshd_bin and Path("/usr/sbin/sshd").exists():
            sshd_bin = "/usr/sbin/sshd"
        if sshd_bin:
            check = subprocess.run(
                [sshd_bin, "-t"], capture_output=True, text=True, check=False
            )
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or "").strip() or f"exit {check.returncode}"
                raise SetupError(
                    f"refusing to reload ssh: sshd config validation failed: {detail}"
                )

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
        result = subprocess.run(
            ["chpasswd"],
            input=f"{username}:{password}\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
            raise SetupError(f"failed to set password for {username}: {detail}")

    @staticmethod
    def _set_password_hash(username: str, password_hash: str) -> None:
        if not password_hash:
            raise ValueError("password_hash cannot be empty")
        result = subprocess.run(
            ["usermod", "-p", password_hash, username],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
            raise SetupError(f"failed to set password hash for {username}: {detail}")

    @staticmethod
    def _hash_password(password: str) -> str:
        if not password:
            raise ValueError("spawn password cannot be empty")
        # Prefer the stdlib crypt module, but only trust it if it actually
        # produced a SHA512-crypt ($6$...) hash. Some platforms' crypt(3)
        # (notably macOS) silently downgrade to a weaker/non-standard scheme
        # that chpasswd and `usermod -p` won't accept, so fall through to
        # openssl in that case.
        if crypt is not None:
            try:
                hashed = str(crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512)))
            except (OSError, ValueError):
                hashed = ""
            if hashed.startswith("$6$"):
                return hashed
        # Python 3.13+ removed the stdlib crypt module (PEP 594), and some
        # platforms can't emit SHA512 via crypt. openssl produces the same
        # SHA512-crypt ($6$...) format chpasswd and `usermod -p` expect.
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
            "hashing spawn passwords requires a crypt(3) that supports SHA512 "
            "or the 'openssl' executable; neither produced a usable $6$ hash."
        )

    def _ensure_workspace_accessible(
        self, provider: str, home: Path, linux_user: str,
    ) -> list[str]:
        """Enforce the private-home boundary required by host validation.

        Cross-user management is deliberately handled by root or ``clawied``;
        group traversal/write access would defeat the isolation contract.  The
        historical method name is retained for compatibility with callers.
        """
        warnings: list[str] = []
        if os.geteuid() != 0:
            return warnings

        try:
            account = pwd.getpwnam(linux_user)
            owner = (int(account.pw_uid), int(account.pw_gid))
        except KeyError:
            return [f"linux user does not exist: {linux_user}"]

        def _harden(path: Path, mode: int) -> None:
            try:
                current = path.lstat()
            except OSError as exc:
                warnings.append(f"inspect {path}: {exc}")
                return
            if stat.S_ISLNK(current.st_mode):
                warnings.append(f"refusing symlink in private agent state: {path}")
                return
            if not (stat.S_ISDIR(current.st_mode) or stat.S_ISREG(current.st_mode)):
                warnings.append(f"refusing special file in private agent state: {path}")
                return
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if stat.S_ISDIR(current.st_mode):
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                warnings.append(f"harden {path}: {exc}")
                return
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    warnings.append(f"path changed while hardening private agent state: {path}")
                    return
                os.fchown(fd, owner[0], owner[1])
                os.fchmod(fd, mode)
                verified = os.fstat(fd)
            except OSError as exc:
                warnings.append(f"harden {path}: {exc}")
                return
            finally:
                os.close(fd)
            if int(verified.st_uid) != owner[0] or int(verified.st_gid) != owner[1]:
                warnings.append(f"unexpected owner after hardening {path}")
            if stat.S_IMODE(verified.st_mode) != mode:
                warnings.append(
                    f"unexpected mode after hardening {path}: {stat.S_IMODE(verified.st_mode):o}"
                )

        try:
            spec = get_provider(provider)
            state = home / spec.state_dir
            try:
                home_st = home.lstat()
            except OSError as exc:
                return [f"inspect managed home {home}: {exc}"]
            if stat.S_ISLNK(home_st.st_mode) or not stat.S_ISDIR(home_st.st_mode):
                return [f"managed home is not a real directory: {home}"]
            _harden(home, 0o700)
            try:
                state_st = state.lstat()
            except FileNotFoundError:
                state_st = None
            if state_st is not None:
                if stat.S_ISLNK(state_st.st_mode) or not stat.S_ISDIR(state_st.st_mode):
                    warnings.append(f"provider state is not a real directory: {state}")
                    return warnings
                def walk_error(exc: OSError) -> None:
                    warnings.append(f"traverse private provider state: {exc}")

                for root, directory_names, file_names in os.walk(
                    state,
                    onerror=walk_error,
                    followlinks=False,
                ):
                    root_path = Path(root)
                    _harden(root_path, 0o700)
                    for name in directory_names:
                        _harden(root_path / name, 0o700)
                    for name in file_names:
                        _harden(root_path / name, 0o600)
        except OSError as exc:
            warnings.append(f"private workspace hardening: {exc}")
        return warnings

    def spawn_session_agent(
        self,
        parent_id: str,
        child_id: str,
        timeout: float = 300.0,
        model_tier: str = "",
        detached: bool = False,
    ) -> dict[str, Any]:
        from clawie.delegation import DEFAULT_TIER, get_model_tier

        parent_id = self._validate_agent_id(parent_id)
        child_id = self._validate_agent_id(child_id)
        if parent_id == child_id:
            raise ValueError("parent and child agent ids must differ")
        self.get_agent(parent_id)
        effective_timeout = float(timeout)
        if not math.isfinite(effective_timeout) or effective_timeout <= 0:
            raise ValueError("session timeout must be a positive finite number")
        depth_limit = self._delegation_depth_limit(parent_id)
        if 1 >= depth_limit:
            raise ValueError(
                f"max recursion depth ({depth_limit}) exceeded at depth=1"
            )
        tier = model_tier or DEFAULT_TIER
        get_model_tier(tier)
        if detached:
            existing = self.store.read_session_agent(parent_id, child_id)
            if existing:
                status = self._session_record_with_liveness(existing)
                if status.get("running"):
                    raise ValueError(f"session agent already exists: {child_id}")
                self.store.delete_session_agent(parent_id, child_id)

            session_dir = ensure_directory_under(self.store.root, "session-agents", mode=0o700)
            log_path = session_dir / f"{parent_id}-{child_id}.log"
            socket_path = self._delegation_socket_path(child_id)
            if self._socket_alive(socket_path):
                raise ValueError(f"delegation socket already active for: {child_id}")

            cmd = [
                sys.executable,
                "-m",
                "clawie",
                "--config-dir",
                str(self.store.root),
                "delegation",
                "repl",
                "--agent-id",
                child_id,
                "--executor-agent",
                parent_id,
                "--tier",
                tier,
            ]
            log_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            log_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            log_fd: int | None = None
            null_fd: int | None = None
            try:
                log_fd = os.open(log_path, log_flags, 0o600)
                log_stat = os.fstat(log_fd)
                if not stat.S_ISREG(log_stat.st_mode):
                    raise UnsafePathError(f"refusing non-regular session log: {log_path}")
                os.fchmod(log_fd, 0o600)
                null_fd = os.open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                pid = os.posix_spawn(
                    sys.executable,
                    cmd,
                    os.environ.copy(),
                    file_actions=[
                        (os.POSIX_SPAWN_DUP2, null_fd, 0),
                        (os.POSIX_SPAWN_DUP2, log_fd, 1),
                        (os.POSIX_SPAWN_DUP2, log_fd, 2),
                    ],
                    setsid=True,
                )
            except (OSError, NotImplementedError) as exc:
                raise SetupError(f"could not start detached session agent: {exc}") from exc
            finally:
                if null_fd is not None:
                    os.close(null_fd)
                if log_fd is not None:
                    os.close(log_fd)
            ready_timeout = min(max(effective_timeout, 0.5), 5.0)
            if not self._wait_for_session_socket(
                child_id,
                timeout=ready_timeout,
                pid=int(pid),
            ):
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        try:
                            waited, _status = os.waitpid(pid, os.WNOHANG)
                        except (ChildProcessError, OSError):
                            waited = pid
                        if waited == pid or not self._pid_alive(pid):
                            break
                        time.sleep(0.05)
                    if waited == pid or not self._pid_alive(pid):
                        break
                raise RuntimeError(
                    f"session agent failed to start: {child_id}; see {log_path}"
                )

            created_at = now_iso()
            self.store.write_session_agent(
                parent_agent_id=parent_id,
                child_agent_id=child_id,
                pid=int(pid),
                depth=1,
                status="running",
                model_tier=tier,
                socket_path=str(socket_path),
                log_path=str(log_path),
                created_at=created_at,
                updated_at=created_at,
            )
            self._persist_session_tree(parent_id)
            state = self.store.read_state()
            self._event(
                state,
                "session.agent.spawned",
                f"Session agent {child_id} spawned under {parent_id}",
                {
                    "parent": parent_id,
                    "child": child_id,
                    "model_tier": tier,
                    "pid": int(pid),
                },
            )
            self.store.write_state(state)
            return {
                "agent_id": child_id,
                "parent_id": parent_id,
                "depth": 1,
                "status": "running",
                "session": True,
                "model_tier": tier,
                "pid": int(pid),
                "socket": str(socket_path),
                "log": str(log_path),
            }

        mgr = self._get_session_manager(parent_id)
        info = mgr.spawn(
            child_id,
            handler=self._gateway_task_handler(parent_id),
            timeout=effective_timeout,
            model_tier=tier,
        )
        state = self.store.read_state()
        self._event(
            state,
            "session.agent.spawned",
            f"Session agent {child_id} spawned under {parent_id}",
            {"parent": parent_id, "child": child_id, "model_tier": tier},
        )
        self.store.write_state(state)
        return dict(info)
