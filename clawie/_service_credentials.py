"""Credential bundle policy and sync/revoke (ClawieService mixin)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from clawie.providers import (
    provider_names,
    shared_auth_paths_for_providers,
)
from clawie.service_common import SetupError, AgentNotFoundError, now_iso


class CredentialOpsMixin:

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
        self._harden_shared_provider_auth_permissions()
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
