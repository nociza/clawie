"""Provider auth stores, linked-auth inspection, and login flows (ClawieService mixin)."""
from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from clawie.auth_sources import (
    extract_picoclaw_credentials,
    extract_openclaw_sqlite_auth_profiles,
    extract_provider_auth_profiles,
    load_claude_auth,
    load_codex_auth,
    merge_picoclaw_auth_store,
    merge_provider_auth_profile,
)
from clawie.provider_auth import (
    empty_auth_payload,
    inspect_auth_files,
    login_required,
    parse_iso_timestamp,
    parse_openclaw_models_status_output,
    parse_provider_auth_status_output,
)
from clawie.providers import (
    get_provider,
    provider_names,
    shared_auth_paths_for_providers,
)
from clawie.service_common import SetupError, AgentNotFoundError, now_iso


class ProviderAuthMixin:

    def _write_provider_auth_profile(
        self,
        provider: str,
        imported: dict[str, str],
    ) -> list[str]:
        if str(provider).strip().lower() == "openclaw":
            self._verify_detected_runtime_before_write("openclaw")
            return self._write_openclaw_native_auth_profile(imported)
        shared_home = self._ensure_shared_provider_auth_root()
        target = shared_home / get_provider(provider).state_dir / "auth-profiles.json"
        existing = self._read_json_file(target)
        payload = merge_provider_auth_profile(existing, imported)
        self._write_replaceable_json_file(target, payload)
        self._harden_private_path_permissions(target.parent)
        self._harden_private_path_permissions(target)
        return [str(target)]

    def _write_openclaw_native_auth_profile(self, imported: dict[str, str]) -> list[str]:
        shared_home = self._ensure_shared_provider_auth_root()
        agent_dir = self._openclaw_native_auth_agent_dir(shared_home)
        agent_dir.mkdir(parents=True, exist_ok=True)
        db_path = agent_dir / "openclaw-agent.sqlite"
        profile_id, provider, credential = self._openclaw_native_auth_credential(imported)
        store, state = self._read_openclaw_native_auth_store(db_path)
        profiles = store.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            store["profiles"] = profiles
        profiles[profile_id] = credential
        store["version"] = int(store.get("version", 1) or 1)

        order = state.setdefault("order", {})
        if not isinstance(order, dict):
            order = {}
            state["order"] = order
        existing_order = order.get(provider, [])
        if not isinstance(existing_order, list):
            existing_order = []
        order[provider] = [
            profile_id,
            *[
                str(item).strip()
                for item in existing_order
                if str(item).strip() and str(item).strip() != profile_id
            ],
        ]
        state["version"] = int(state.get("version", 1) or 1)

        self._write_openclaw_native_auth_store(db_path, store=store, state=state)
        self._harden_private_path_permissions(agent_dir)
        self._harden_private_path_permissions(db_path)
        return [str(db_path)]

    @staticmethod
    def _openclaw_native_auth_agent_dir(home: Path) -> Path:
        return home / ".openclaw" / "agents" / "main" / "agent"

    def _read_openclaw_native_auth_store(self, db_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        if not db_path.exists():
            return {"version": 1, "profiles": {}}, {"version": 1}
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(db_path)
            store_row = conn.execute(
                "SELECT store_json FROM auth_profile_store WHERE store_key = ?",
                ("primary",),
            ).fetchone()
            state_row = conn.execute(
                "SELECT state_json FROM auth_profile_state WHERE state_key = ?",
                ("primary",),
            ).fetchone()
        except sqlite3.Error:
            return {"version": 1, "profiles": {}}, {"version": 1}
        finally:
            if conn is not None:
                conn.close()
        store = self._decode_json_obj(str(store_row[0])) if store_row and store_row[0] else {}
        state = self._decode_json_obj(str(state_row[0])) if state_row and state_row[0] else {}
        if not isinstance(store, dict):
            store = {}
        if not isinstance(state, dict):
            state = {}
        store.setdefault("version", 1)
        store.setdefault("profiles", {})
        state.setdefault("version", 1)
        return store, state

    @staticmethod
    def _decode_json_obj(value: str) -> dict[str, Any]:
        import json

        try:
            payload = json.loads(value)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_openclaw_native_auth_store(
        self,
        db_path: Path,
        *,
        store: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        import json

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_profile_store (
                    store_key TEXT PRIMARY KEY,
                    store_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_profile_state (
                    state_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            updated_at = int(time.time() * 1000)
            conn.execute(
                """
                INSERT INTO auth_profile_store(store_key, store_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(store_key) DO UPDATE SET
                    store_json = excluded.store_json,
                    updated_at = excluded.updated_at
                """,
                ("primary", json.dumps(store, sort_keys=True), updated_at),
            )
            conn.execute(
                """
                INSERT INTO auth_profile_state(state_key, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                ("primary", json.dumps(state, sort_keys=True), updated_at),
            )
            conn.commit()
        finally:
            conn.close()

    def _openclaw_native_auth_credential(
        self,
        imported: dict[str, str],
    ) -> tuple[str, str, dict[str, Any]]:
        provider = self._openclaw_native_provider(str(imported.get("upstream_provider", "")))
        profile_id = self._openclaw_native_profile_id(str(imported.get("profile_id", "")), provider)
        kind = str(imported.get("kind", "oauth")).strip().lower().replace("-", "_") or "oauth"
        account_id = str(imported.get("account_id", "")).strip()
        expires_at = str(imported.get("expires_at", "")).strip()
        expires_ms = 0
        if expires_at:
            parsed = parse_iso_timestamp(expires_at)
            if parsed is not None:
                expires_ms = int(parsed.timestamp() * 1000)
        if kind == "api_key":
            key = str(imported.get("api_key", imported.get("key", ""))).strip()
            if not key:
                raise ValueError("OpenClaw native API-key auth import requires api_key/key material")
            credential: dict[str, Any] = {"type": "api_key", "provider": provider, "key": key}
        elif kind == "token":
            token = str(imported.get("access_token", imported.get("token", ""))).strip()
            if not token:
                raise ValueError("OpenClaw native token auth import requires token/access_token material")
            credential = {"type": "token", "provider": provider, "token": token}
            if expires_ms > 0:
                credential["expires"] = expires_ms
        elif kind == "oauth":
            access = str(imported.get("access_token", imported.get("access", ""))).strip()
            refresh = str(imported.get("refresh_token", imported.get("refresh", ""))).strip()
            if not access or not refresh:
                raise ValueError("OpenClaw native OAuth auth import requires access and refresh tokens")
            if expires_ms <= 0:
                raise ValueError("OpenClaw native OAuth auth import requires a valid expires_at timestamp")
            credential = {
                "type": "oauth",
                "provider": provider,
                "access": access,
                "refresh": refresh,
                "expires": expires_ms,
            }
            id_token = str(imported.get("id_token", "")).strip()
            if id_token:
                credential["idToken"] = id_token
            if account_id:
                credential["accountId"] = account_id
        else:
            raise ValueError(f"unsupported OpenClaw native auth credential type: {kind}")
        return profile_id, provider, credential

    @staticmethod
    def _openclaw_native_provider(value: str) -> str:
        token = str(value).strip().lower()
        if token in {"openai-codex", "openai-chatgpt"}:
            return "openai"
        if token == "anthropic-claude":
            return "anthropic"
        return token

    @staticmethod
    def _openclaw_native_profile_id(value: str, provider: str) -> str:
        token = str(value).strip()
        if not token:
            return f"{provider}:default"
        prefix, sep, suffix = token.partition(":")
        if not sep:
            return f"{provider}:{token}"
        normalized_prefix = ProviderAuthMixin._openclaw_native_provider(prefix)
        if normalized_prefix != provider:
            return token
        return f"{provider}:{suffix or 'default'}"

    def _write_picoclaw_auth_store(self, imported: dict[str, str]) -> list[str]:
        shared_home = self._ensure_shared_provider_auth_root()
        target = shared_home / ".picoclaw" / "auth.json"
        existing = self._read_json_file(target)
        payload = merge_picoclaw_auth_store(existing, imported)
        self._write_replaceable_json_file(target, payload)
        self._harden_private_path_permissions(target.parent)
        self._harden_private_path_permissions(target)
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
        """Copy shared provider auth into an agent home as private owned files.

        Kept under the historical "links" name because callers and event keys
        use it, but this no longer creates symlinks. The shared store is only a
        manager-side cache; each agent gets its own 0600 copy so agents cannot
        read or mutate one another's auth sessions through a shared inode.
        """
        if not target_home.exists():
            return []
        shared_home = self._ensure_shared_provider_auth_root()
        updated: list[str] = []
        for rel in shared_auth_paths_for_providers(provider_names()):
            src = shared_home / rel
            if not self._path_exists(src):
                continue
            if rel == ".openclaw/auth-profiles.json":
                self._repair_openclaw_auth_store(src)
            dst = self._copy_file_to_agent(
                shared_home,
                rel,
                target_home,
                rel,
                username,
            )
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
            payload = merge_picoclaw_auth_store(
                self._read_agent_json_file(home, ".picoclaw/auth.json"),
                imported,
            )
            self._write_agent_json_file(home, ".picoclaw/auth.json", payload, linux_user)
            return

    def _ensure_openclaw_agent_auth_link(self, *, home: Path, linux_user: str) -> None:
        root = home / ".openclaw"
        source = root / "auth-profiles.json"
        if not self._path_exists(source):
            return
        self._repair_openclaw_auth_store(source)
        self._ensure_agent_directory(home, ".openclaw/agents/main/agent", linux_user)
        self._copy_file_to_agent(
            home,
            ".openclaw/auth-profiles.json",
            home,
            ".openclaw/agents/main/agent/auth-profiles.json",
            linux_user,
        )

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
            self._harden_private_path_permissions(path)
        return changed

    def shared_auth_status(self, provider: str, *, probe_cli: bool = True) -> dict[str, Any]:
        name = str(provider).strip().lower()
        if not name:
            raise ValueError("provider is required")
        spec = get_provider(name)
        shared_home = self._shared_provider_auth_home()
        auth_mode = str(self._preferred_shared_provider_auth(spec.name, allow_defaults=True).get("auth_mode", spec.default_auth_mode))
        payload = self._inspect_provider_auth_state(
            provider=spec.name,
            auth_mode=auth_mode,
            linux_user="",
            home=shared_home,
            probe_cli=probe_cli,
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

    def list_shared_auth_statuses(self, *, probe_cli: bool = True) -> list[dict[str, Any]]:
        providers = self.configured_provider_names()
        if not providers:
            config = self.store.read_config()
            providers = [str(config.get("provider", "openclaw")).strip().lower() or "openclaw"]
        return [self.shared_auth_status(provider, probe_cli=probe_cli) for provider in providers]

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
        self._harden_shared_provider_auth_permissions()
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

        self._harden_shared_provider_auth_permissions()
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

    def port_shared_auth(self, from_provider: str, to_provider: str) -> dict[str, Any]:
        """Port shared provider-auth sessions from one claw provider to another.

        Reads the shared auth store for *from_provider*, normalizes every
        usable profile, and merges them into *to_provider*'s shared auth store
        (including picoclaw's native ``auth.json`` when applicable). Agents
        that consume the shared provider-auth bundle receive fresh private
        copies afterwards.
        """
        self._require_setup()
        src = get_provider(str(from_provider).strip().lower())
        dst = get_provider(str(to_provider).strip().lower())
        if src.name == dst.name:
            raise ValueError("source and target providers must differ")

        shared_home = self._ensure_shared_provider_auth_root()
        profiles = self._collect_shared_auth_profiles(src.name, shared_home)
        if not profiles:
            looked_at = ", ".join(str(shared_home / rel) for rel in src.shared_auth_paths)
            raise SetupError(
                f"no shared {src.name} auth sessions found to port (looked at: {looked_at}). "
                f"Run 'clawie auth login {src.name}' or "
                f"'clawie auth import {src.name} --from codex' first."
            )

        updated: list[str] = []
        for profile in profiles:
            updated.extend(self._write_provider_auth_profiles([dst.name], profile))
        self._harden_shared_provider_auth_permissions()
        applied = self.apply_shared_auth_links()
        auth = self.shared_auth_status(dst.name)

        state = self.store.read_state()
        self._event(
            state,
            "auth.ported",
            f"Ported shared auth {src.name} -> {dst.name}",
            {
                "from_provider": src.name,
                "to_provider": dst.name,
                "profiles": [str(row.get("profile_id", "")) for row in profiles],
                "updated_paths": self._dedupe_paths(updated),
            },
        )
        self.store.write_state(state)
        return {
            "from_provider": src.name,
            "to_provider": dst.name,
            "profiles": [str(row.get("profile_id", "")) for row in profiles],
            "home": str(shared_home),
            "updated_paths": self._dedupe_paths(updated),
            "updated_agents": list(applied.get("updated_agents", [])),
            "skipped_agents": list(applied.get("skipped_agents", [])),
            "restart_required_agents": self._shared_provider_auth_agent_ids_for_providers(
                [dst.name],
                include_eligible=True,
            ),
            "auth": auth,
        }

    def _collect_shared_auth_profiles(self, provider: str, shared_home: Path) -> list[dict[str, str]]:
        """Read every shared auth file for *provider* and normalize the profiles.

        Profiles are deduped by profile id; when the same profile appears in
        several files the later file in ``shared_auth_paths`` wins. Within one
        file, active profiles sort last so replaying the merge keeps them active.
        """
        spec = get_provider(provider)
        merged: dict[str, dict[str, str]] = {}
        order: list[str] = []
        for rel in spec.shared_auth_paths:
            path = shared_home / rel
            if not self._path_exists(path):
                continue
            if path.name == "openclaw-agent.sqlite":
                rows = extract_openclaw_sqlite_auth_profiles(path)
            elif path.name == "auth.json":
                payload = self._read_json_file(path)
                rows = extract_picoclaw_credentials(payload)
            else:
                payload = self._read_json_file(path)
                rows = extract_provider_auth_profiles(payload)
            for row in rows:
                profile_id = str(row.get("profile_id", "")).strip()
                if not profile_id:
                    continue
                if profile_id in merged:
                    order.remove(profile_id)
                merged[profile_id] = row
                order.append(profile_id)
        return [merged[profile_id] for profile_id in order]

    def apply_shared_auth_links(self, agent_id: str | None = None) -> dict[str, Any]:
        self._require_setup()
        shared_home = self._ensure_shared_provider_auth_root()
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
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
        agents = state.setdefault("agents", {})
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

    def local_claw_auth_status(self, provider: str, *, probe_cli: bool = True) -> dict[str, Any]:
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
            probe_cli=probe_cli,
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

    def agent_auth_status(
        self,
        agent_id: str,
        *,
        persist_alignment: bool = True,
        probe_cli: bool = True,
    ) -> dict[str, Any]:
        token = str(agent_id).strip()
        if token.startswith("@local:"):
            payload = self.local_claw_auth_status(
                token.split(":", 1)[1],
                probe_cli=probe_cli,
            )
            payload["agent_id"] = token
            return payload
        if persist_alignment:
            self._refresh_managed_agent_provider_alignment(token)

        state = self.store.read_state()
        agents = state.setdefault("agents", {})
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
        inspect_linux_user = linux_user
        inspect_home = home
        payload = self._inspect_provider_auth_state(
            provider=provider,
            auth_mode=auth_mode,
            linux_user=inspect_linux_user,
            home=inspect_home,
            probe_cli=probe_cli,
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
        agents = state.setdefault("agents", {})
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
                "home": str(home or ""),
                "shared_provider_auth": shared_provider_auth,
                "local_user": False,
            }
        )
        return payload

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
            home=self._shared_provider_auth_home(),
            probe_cli=False,
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
            # shared provider-auth store (shared_provider_auth flag). Agents can
            # select the provider-auth bundle without having it synced yet; those
            # agents keep their own auth until sync/apply explicitly opts them in.
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

    def _auth_env(self, linux_user: str, home: Path | None) -> dict[str, str]:
        env = self._service_env(linux_user)
        if home:
            env["HOME"] = str(home)
        return env

    def _provider_auth_command(self, provider: str, action: str, linux_user: str) -> list[str]:
        spec = get_provider(provider)
        executable = self._resolve_provider_executable(spec.name)
        if spec.name == "openclaw":
            from clawie.adapters import OpenclawAdapter

            adapter = OpenclawAdapter()
            if action == "login":
                base = adapter.auth_login_command(
                    "openai",
                    set_default=True,
                    openclaw_bin=executable,
                )
            elif action in {"refresh", "status"}:
                base = adapter.readiness_command(openclaw_bin=executable)
            else:
                base = [executable, "models", "auth", action]
            return self._wrap_user_command(base, linux_user, purpose="auth control")
        if action == "login":
            base = [executable, *spec.auth_login_command]
        elif action == "refresh":
            base = [executable, *spec.auth_refresh_command]
        elif action == "status":
            base = [executable, *spec.auth_status_command]
        else:
            base = [executable, "auth", action]
        return self._wrap_user_command(base, linux_user, purpose="auth control")

    def _inspect_provider_auth_state(
        self,
        *,
        provider: str,
        auth_mode: str,
        linux_user: str,
        home: Path | None,
        probe_cli: bool = True,
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

        cli_status = (
            self._run_provider_auth_status(provider=spec.name, linux_user=linux_user, home=home)
            if probe_cli
            else {}
        )
        if cli_status:
            payload.update(cli_status)
            payload["source"] = str(cli_status.get("source", "cli"))
            # The runtime CLI is authoritative for whether the session is
            # usable, but some OpenClaw versions omit identity metadata from
            # their JSON status response. Enrich only blank descriptive fields
            # from the same private native store; never replace the CLI status.
            file_status = inspect_auth_files(provider=spec.name, home=home)
            enriched = False
            for key in ("auth_profile", "account", "expires_at", "last_refresh", "detail"):
                if not str(payload.get(key, "")).strip() and str(file_status.get(key, "")).strip():
                    payload[key] = file_status[key]
                    enriched = True
            if enriched:
                payload["metadata_source"] = str(file_status.get("source", "files"))
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
        if provider == "openclaw":
            parsed = parse_openclaw_models_status_output(output)
            if not parsed:
                parsed = parse_provider_auth_status_output(output)
        else:
            parsed = parse_provider_auth_status_output(output)
        if not parsed:
            return None
        parsed["source"] = "cli"
        return parsed
