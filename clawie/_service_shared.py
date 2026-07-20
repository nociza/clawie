"""Shared filesystem, permissions, and path helpers (ClawieService mixin)."""
from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from pathlib import Path
from typing import Any
from clawie.addons import get_credential_addon
from clawie.providers import (
    provider_names,
    shared_auth_paths_for_providers,
)
from clawie.service_common import SetupError
from clawie.safe_fs import (
    copy_file_under,
    copy_tree_under,
    ensure_directory_under,
    owner_for_username,
    read_text_under,
    remove_under,
    write_text_under,
)


class SharedInfraMixin:

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
        return "system" if self._shared_provider_auth_home() == self.SHARED_PROVIDER_AUTH_DIR else "local"

    def _ensure_shared_provider_auth_root(self) -> Path:
        root = self._shared_provider_auth_home()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            root = self.store.root / "shared-provider-auth"
            root.mkdir(parents=True, exist_ok=True)
        self._harden_private_path_permissions(root)
        for rel in shared_auth_paths_for_providers(provider_names()):
            parent = (root / rel).parent
            parent.mkdir(parents=True, exist_ok=True)
            self._harden_private_path_permissions(parent)
        return root

    def _harden_shared_provider_auth_permissions(self) -> None:
        root = self._shared_provider_auth_home()
        if not root.exists():
            return
        self._harden_private_path_permissions(root)
        for rel in shared_auth_paths_for_providers(provider_names()):
            path = root / rel
            if path.parent.exists():
                self._harden_private_path_permissions(path.parent)
            if path.exists():
                self._harden_private_path_permissions(path)

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
        self._harden_shared_toolchain_permissions(root)
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
        self._harden_private_tree_permissions(root)
        return root

    def _shared_addon_config_dir(self, addon: str) -> Path:
        spec = get_credential_addon(addon)
        return self._ensure_shared_addon_auth_root() / spec.shared_config_dir

    def _ensure_shared_addon_config_dir(self, addon: str) -> Path:
        root = self._shared_addon_config_dir(addon)
        root.mkdir(parents=True, exist_ok=True)
        self._harden_private_tree_permissions(root)
        return root

    def _harden_shared_addon_permissions(self, addon: str | None = None) -> None:
        root = self._shared_addon_auth_home()
        if addon:
            root = self._shared_addon_config_dir(addon)
        if not root.exists():
            return
        self._harden_private_tree_permissions(root)

    @staticmethod
    def _harden_private_path_permissions(path: Path) -> None:
        """Make a credential/config path private to its owner.

        Shared auth stores are a manager-side cache, not a cross-user secret
        transport. Agents receive owned copies, so the cache itself should never
        be world-readable.
        """
        try:
            target_mode = 0o700 if path.is_dir() else 0o600
            current_mode = int(path.stat().st_mode) & 0o777
            if current_mode != target_mode:
                os.chmod(path, target_mode)
        except OSError:
            return

    @classmethod
    def _harden_private_tree_permissions(cls, path: Path) -> None:
        if not path.exists():
            return
        cls._harden_private_path_permissions(path)
        if not path.is_dir():
            return
        for child in path.rglob("*"):
            cls._harden_private_path_permissions(child)

    @staticmethod
    def _harden_shared_toolchain_path_permissions(path: Path) -> None:
        try:
            if path.is_dir():
                target_mode = 0o755
            else:
                current_mode = int(path.stat().st_mode) & 0o777
                target_mode = 0o644 | (current_mode & 0o111)
            current_mode = int(path.stat().st_mode) & 0o777
            if current_mode != target_mode:
                os.chmod(path, target_mode)
        except OSError:
            return

    @classmethod
    def _harden_shared_toolchain_permissions(cls, path: Path) -> None:
        if not path.exists():
            return
        cls._harden_shared_toolchain_path_permissions(path)
        if not path.is_dir():
            return
        for child in path.rglob("*"):
            cls._harden_shared_toolchain_path_permissions(child)

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(read_text_under(path.parent, path.name, max_bytes=16 * 1024 * 1024))
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_under(
            path.parent,
            path.name,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )

    @staticmethod
    def _write_replaceable_json_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        write_text_under(path.parent, path.name, content, mode=0o600)

    @staticmethod
    def _agent_owner(username: str) -> tuple[int, int] | None:
        return owner_for_username(username) if os.geteuid() == 0 else None

    @classmethod
    def _ensure_agent_directory(
        cls,
        home: Path,
        relative: str | Path,
        username: str,
        *,
        mode: int = 0o700,
    ) -> Path:
        return ensure_directory_under(
            home,
            relative,
            mode=mode,
            owner=cls._agent_owner(username),
        )

    @staticmethod
    def _read_agent_text_file(
        home: Path,
        relative: str | Path,
        *,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> str:
        return read_text_under(home, relative, max_bytes=max_bytes)

    @classmethod
    def _write_agent_text_file(
        cls,
        home: Path,
        relative: str | Path,
        content: str,
        username: str,
        *,
        mode: int = 0o600,
    ) -> Path:
        return write_text_under(
            home,
            relative,
            content,
            mode=mode,
            directory_mode=0o700,
            owner=cls._agent_owner(username),
        )

    @classmethod
    def _write_agent_json_file(
        cls,
        home: Path,
        relative: str | Path,
        payload: dict[str, Any],
        username: str,
    ) -> Path:
        return cls._write_agent_text_file(
            home,
            relative,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            username,
            mode=0o600,
        )

    @classmethod
    def _read_agent_json_file(cls, home: Path, relative: str | Path) -> dict[str, Any]:
        try:
            payload = json.loads(cls._read_agent_text_file(home, relative))
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _copy_file_to_agent(
        cls,
        source_root: Path,
        source_relative: str | Path,
        home: Path,
        target_relative: str | Path,
        username: str,
    ) -> Path:
        return copy_file_under(
            source_root,
            source_relative,
            home,
            target_relative,
            mode=0o600,
            directory_mode=0o700,
            owner=cls._agent_owner(username),
        )

    @classmethod
    def _copy_tree_to_agent(
        cls,
        source_root: Path,
        source_relative: str | Path,
        home: Path,
        target_relative: str | Path,
        username: str,
    ) -> Path:
        return copy_tree_under(
            source_root,
            source_relative,
            home,
            target_relative,
            file_mode=0o600,
            directory_mode=0o700,
            owner=cls._agent_owner(username),
        )

    @staticmethod
    def _remove_agent_path(home: Path, relative: str | Path, *, recursive: bool = False) -> None:
        remove_under(home, relative, recursive=recursive)

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
        try:
            source_st = src.lstat()
        except FileNotFoundError:
            return False
        if src.absolute() == dst.absolute():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISDIR(source_st.st_mode):
            copy_tree_under(src.parent, src.name, dst.parent, dst.name)
        elif stat.S_ISREG(source_st.st_mode):
            copy_file_under(src.parent, src.name, dst.parent, dst.name)
        else:
            raise SetupError(f"refusing to copy symlink or special file: {src}")
        self._harden_private_path_permissions(dst.parent)
        self._harden_private_path_permissions(dst)
        return True

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

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

    def _replace_tree(self, src: Path, dst: Path) -> list[str]:
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_tree_under(src.parent, src.name, dst.parent, dst.name)
        self._harden_private_tree_permissions(dst)
        return [str(dst)]

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
    def _path_or_none(value: Any) -> Path | None:
        token = str(value or "").strip()
        if not token:
            return None
        return Path(token)

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
