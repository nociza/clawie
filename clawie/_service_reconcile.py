"""Manifest reconciliation operations for ClawieService."""
from __future__ import annotations

import copy
import stat
from pathlib import Path
from typing import Any

from clawie.manifest import AgentManifest, ReconcileAction, reconcile_plan
from clawie.service_common import AgentNotFoundError, now_iso
from clawie.safe_fs import ensure_directory_under, read_text_under, write_text_under

_MAX_MANIFEST_PROMPT_BYTES = 1024 * 1024


class ReconcileOpsMixin:
    """Apply declarative agent manifests through existing service operations."""

    def agent_manifest_dir(self) -> Path:
        return self.store.root / "manifests"

    def agent_manifest_path(self, agent_id: str) -> Path:
        token = self._validate_agent_id(agent_id)
        return self.agent_manifest_dir() / f"{token}.json"

    def write_agent_manifest(self, manifest: AgentManifest | dict[str, Any]) -> Path:
        desired = self._coerce_agent_manifest(manifest)
        target = self.agent_manifest_path(desired.id)
        ensure_directory_under(self.store.root, "manifests", mode=0o700)
        write_text_under(self.store.root, Path("manifests") / target.name, desired.to_json(), mode=0o600)
        return target

    def list_agent_manifests(self) -> list[Path]:
        root = self.agent_manifest_dir()
        if not root.exists():
            return []
        paths: list[Path] = []
        for path in root.glob("*.json"):
            path_st = path.lstat()
            if stat.S_ISREG(path_st.st_mode) and not stat.S_ISLNK(path_st.st_mode):
                paths.append(path)
        return sorted(paths)

    def observed_agent_manifest_state(self, agent_id: str) -> dict[str, Any] | None:
        token = self._validate_agent_id(agent_id)
        try:
            payload = self.get_agent(token)
        except AgentNotFoundError:
            return None

        info = payload.setdefault("agent", {})
        channels: list[dict[str, Any]] = []
        for row in payload.get("channels", []):
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind", "")).strip().lower()
            name = str(row.get("name", "")).strip()
            if kind and name:
                channels.append(
                    {
                        "kind": kind,
                        "name": name,
                        "allow_from": [
                            str(item).strip()
                            for item in row.get("allow_from", [])
                            if str(item).strip()
                        ],
                    }
                )

        addons = self._normalize_agent_addons(payload.get("addons"))
        sync = self._normalize_credential_sync_state(
            payload.get("credential_sync"),
            default_when_missing=True,
        )
        return {
            "provider": str(info.get("provider", "")).strip().lower(),
            "model_tier": str(info.get("model_tier", "")).strip().lower(),
            "channels": channels,
            "credential_bundles": list(sync.get("bundles", [])),
            "credential_refs": [
                {
                    "name": bundle,
                    "scope": str(sync.get("credential_scopes", {}).get(bundle, "agent")),
                }
                for bundle in sync.get("bundles", [])
            ],
            "addons": {
                str(name): bool(data.get("enabled", False))
                for name, data in addons.items()
            },
            "display_name": str(payload.get("display_name", "")),
            "role": str(info.get("role", "worker")).strip().lower() or "worker",
            "prompts_dir": str(payload.get("manifest_prompts_dir", "prompts") or "prompts"),
            "limits": dict(info.get("limits", {})) if isinstance(info.get("limits"), dict) else {},
        }

    def reconcile_agent_manifest(
        self,
        manifest: AgentManifest | dict[str, Any] | str | Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        desired = self._coerce_agent_manifest(manifest)
        manifest_prompts = self._load_manifest_prompts(manifest, desired)
        observed = self.observed_agent_manifest_state(desired.id)
        actions = reconcile_plan(desired, observed)
        action_rows = [self._reconcile_action_payload(action) for action in actions]
        if dry_run:
            return {
                "agent_id": desired.id,
                "dry_run": True,
                "converged": not actions,
                "actions": action_rows,
                "applied": [],
                "remaining": action_rows,
                "errors": [],
            }

        applied: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for action in actions:
            try:
                result = self._apply_reconcile_action(desired, action)
            except Exception as exc:  # noqa: BLE001 - surface the action that failed.
                errors.append(
                    {
                        **self._reconcile_action_payload(action),
                        "error": str(exc),
                    }
                )
                break
            applied.append(
                {
                    **self._reconcile_action_payload(action),
                    "result": result,
                }
            )

        if not errors and manifest_prompts is not None:
            prompt_result = self._sync_manifest_prompts(desired, manifest_prompts)
            if prompt_result.get("changed"):
                applied.append(
                    {
                        "kind": "sync_prompts",
                        "detail": {"prompts_dir": desired.prompts_dir},
                        "result": prompt_result,
                    }
                )

        final_observed = self.observed_agent_manifest_state(desired.id)
        remaining = reconcile_plan(desired, final_observed)
        return {
            "agent_id": desired.id,
            "dry_run": False,
            "converged": not errors and not remaining,
            "actions": action_rows,
            "applied": applied,
            "remaining": [self._reconcile_action_payload(action) for action in remaining],
            "errors": errors,
        }

    def reconcile_all_manifests(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        return [
            self.reconcile_agent_manifest(path, dry_run=dry_run)
            for path in self.list_agent_manifests()
        ]

    @staticmethod
    def _coerce_agent_manifest(manifest: AgentManifest | dict[str, Any] | str | Path) -> AgentManifest:
        if isinstance(manifest, AgentManifest):
            return manifest
        if isinstance(manifest, dict):
            return AgentManifest.from_dict(manifest)
        return AgentManifest.read(Path(manifest))

    @staticmethod
    def _reconcile_action_payload(action: ReconcileAction) -> dict[str, Any]:
        return {"kind": action.kind, "detail": dict(action.detail)}

    def _apply_reconcile_action(self, desired: AgentManifest, action: ReconcileAction) -> dict[str, Any]:
        kind = action.kind
        if kind == "ensure_agent":
            try:
                self.get_agent(desired.id)
            except AgentNotFoundError:
                agent = self.create_agent(
                    agent_id=desired.id,
                    display_name=desired.display_name,
                    template="baseline",
                    clone_from=None,
                    channel_strategy="new",
                    channels=[],
                    agent_version="1.0.0",
                    provider=desired.provider,
                )
                return {"created": True, "provider": agent.get("agent", {}).get("provider", "")}
            return {"created": False}
        if kind == "set_provider":
            agent = self.set_agent_provider(desired.id, str(action.detail.get("to", desired.provider)))
            return {"provider": agent.get("agent", {}).get("provider", "")}
        if kind == "set_model_tier":
            return {"model_tier": self.set_agent_model_tier(desired.id, desired.model_tier)}
        if kind == "ensure_channel":
            result = self.assign_channel_to_agent(
                "@manifest",
                str(action.detail.get("kind", "")),
                str(action.detail.get("name", "")),
                desired.id,
            )
            allow_from = [str(item) for item in action.detail.get("allow_from", [])]
            if allow_from:
                self._set_manifest_channel_allow_from(
                    desired.id,
                    str(action.detail.get("kind", "")),
                    str(action.detail.get("name", "")),
                    allow_from,
                )
            return {
                "kind": result.get("kind", ""),
                "name": result.get("name", ""),
                "allow_from": allow_from,
            }
        if kind == "set_channel_allow_from":
            allow_from = [str(item) for item in action.detail.get("to", [])]
            self._set_manifest_channel_allow_from(
                desired.id,
                str(action.detail.get("kind", "")),
                str(action.detail.get("name", "")),
                allow_from,
            )
            return {"allow_from": allow_from}
        if kind == "remove_channel":
            result = self.unassign_channel_from_agent(
                desired.id,
                str(action.detail.get("kind", "")),
                str(action.detail.get("name", "")),
            )
            return {"kind": result.get("kind", ""), "name": result.get("name", "")}
        if kind == "set_credentials":
            refs = [item for item in action.detail.get("to", []) if isinstance(item, dict)]
            agent = self.set_agent_credential_bundles(
                desired.id,
                [str(item.get("name", "")) for item in refs],
                include_defaults=False,
            )
            state = self.store.read_state()
            stored = state.setdefault("agents", {}).get(desired.id)
            if not isinstance(stored, dict):
                raise AgentNotFoundError(f"agent not found: {desired.id}")
            sync = self._normalize_credential_sync_state(
                stored.get("credential_sync"), default_when_missing=False
            )
            sync["credential_scopes"] = {
                str(item.get("name", "")): str(item.get("scope", "agent")) for item in refs
            }
            sync["shared_provider_auth"] = (
                sync["credential_scopes"].get("provider-auth") == "shared"
            )
            stored["credential_sync"] = sync
            self.store.write_state(state)
            agent = stored
            sync = self._normalize_credential_sync_state(
                agent.get("credential_sync"),
                default_when_missing=True,
            )
            return {"credential_bundles": list(sync.get("bundles", []))}
        if kind == "sync_identity":
            return self._sync_manifest_identity(desired)
        if kind == "set_limits":
            return self._set_manifest_limits(desired)
        if kind == "set_addon":
            addon = str(action.detail.get("addon", "")).strip()
            enabled = bool(action.detail.get("enabled", False))
            if enabled:
                result = self.enable_agent_addon(desired.id, addon)
            else:
                result = self.disable_agent_addon(desired.id, addon)
            return {"addon": result.get("addon", addon), "enabled": enabled}
        raise ValueError(f"unsupported reconcile action: {kind}")

    def _set_manifest_channel_allow_from(
        self,
        agent_id: str,
        kind: str,
        name: str,
        allow_from: list[str],
    ) -> None:
        state = self.store.read_state()
        agent = state.setdefault("agents", {}).get(agent_id)
        if not isinstance(agent, dict):
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        for row in agent.get("channels", []):
            if not isinstance(row, dict):
                continue
            if str(row.get("kind", "")).strip().lower() == kind and str(
                row.get("name", "")
            ).strip() == name:
                row["allow_from"] = list(dict.fromkeys(allow_from))
                self.store.write_state(state)
                return
        raise ValueError(f"channel not found on {agent_id}: {kind}:{name}")

    def _set_manifest_limits(self, desired: AgentManifest) -> dict[str, Any]:
        state = self.store.read_state()
        agent = state.setdefault("agents", {}).get(desired.id)
        if not isinstance(agent, dict):
            raise AgentNotFoundError(f"agent not found: {desired.id}")
        info = agent.setdefault("agent", {})
        info["limits"] = dict(desired.limits)
        info["last_sync"] = now_iso()
        self.store.write_state(state)
        return {"limits": dict(desired.limits)}

    def _load_manifest_prompts(
        self,
        source: AgentManifest | dict[str, Any] | str | Path,
        desired: AgentManifest,
    ) -> dict[str, str] | None:
        if isinstance(source, (str, Path)):
            manifest_path = Path(source).expanduser()
        else:
            candidate = self.agent_manifest_path(desired.id)
            if not candidate.exists():
                return None
            manifest_path = candidate
        valid_names = set(self._provider_core_prompt_names(desired.provider))
        prompts: dict[str, str] = {}
        for name in sorted(valid_names):
            relative = Path(desired.prompts_dir) / name
            try:
                prompts[name] = read_text_under(
                    manifest_path.parent,
                    relative,
                    max_bytes=_MAX_MANIFEST_PROMPT_BYTES,
                )
            except FileNotFoundError:
                continue
        return prompts

    def _sync_manifest_prompts(
        self,
        desired: AgentManifest,
        prompts: dict[str, str],
    ) -> dict[str, Any]:
        agent = self.get_agent(desired.id)
        current = (
            agent.get("core_prompts", {})
            if isinstance(agent.get("core_prompts"), dict)
            else {}
        )
        target_agent = copy.deepcopy(agent)
        target_prompts = dict(current)
        target_prompts.update(prompts)
        target_agent["core_prompts"] = target_prompts
        self._sync_control_role_workspace(target_agent)
        effective = target_agent.get("core_prompts", {})
        changed_names = [
            name
            for name, content in effective.items()
            if str(current.get(name, "")) != str(content)
        ]
        for name in changed_names:
            self.set_agent_core_prompt(
                desired.id,
                name,
                str(effective[name]),
                sync_to_disk=False,
            )
        return {"changed": bool(changed_names), "prompts": changed_names}

    def _sync_manifest_identity(self, desired: AgentManifest) -> dict[str, Any]:
        state = self.store.read_state()
        agents = state.setdefault("agents", {})
        agent = agents.get(desired.id)
        if not agent:
            return {"changed": False}
        info = agent.setdefault("agent", {})
        changed = False
        if str(agent.get("display_name", "")) != desired.display_name:
            agent["display_name"] = desired.display_name
            changed = True
        if str(info.get("role", "worker")).strip().lower() != desired.role:
            info["role"] = desired.role
            changed = True
        if str(agent.get("manifest_prompts_dir", "")) != desired.prompts_dir:
            agent["manifest_prompts_dir"] = desired.prompts_dir
            changed = True
        if not changed:
            return {"changed": False}
        self._sync_control_role_workspace(agent)
        info["last_sync"] = now_iso()
        self._event(
            state,
            "agents.manifest_identity_synced",
            f"Synced manifest identity for {desired.id}",
            {
                "agent_id": desired.id,
                "display_name": desired.display_name,
                "role": desired.role,
                "prompts_dir": desired.prompts_dir,
            },
        )
        self.store.write_state(state)
        return {
            "changed": True,
            "display_name": desired.display_name,
            "role": desired.role,
            "prompts_dir": desired.prompts_dir,
        }
