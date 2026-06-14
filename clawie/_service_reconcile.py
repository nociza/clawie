"""Manifest reconciliation operations for ClawieService."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from clawie.manifest import AgentManifest, ReconcileAction, reconcile_plan
from clawie.service_common import AgentNotFoundError, now_iso


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
        desired.write(target)
        return target

    def list_agent_manifests(self) -> list[Path]:
        root = self.agent_manifest_dir()
        if not root.exists():
            return []
        return sorted(path for path in root.glob("*.json") if path.is_file())

    def observed_agent_manifest_state(self, agent_id: str) -> dict[str, Any] | None:
        token = self._validate_agent_id(agent_id)
        try:
            payload = self.get_agent(token)
        except AgentNotFoundError:
            return None

        info = payload.setdefault("agent", {})
        channels: list[dict[str, str]] = []
        for row in payload.get("channels", []):
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind", "")).strip().lower()
            name = str(row.get("name", "")).strip()
            if kind and name:
                channels.append({"kind": kind, "name": name})

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
            "addons": {
                str(name): bool(data.get("enabled", False))
                for name, data in addons.items()
            },
            "display_name": str(payload.get("display_name", "")),
            "role": str(info.get("role", "worker")).strip().lower() or "worker",
        }

    def reconcile_agent_manifest(
        self,
        manifest: AgentManifest | dict[str, Any] | str | Path,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._require_setup()
        desired = self._coerce_agent_manifest(manifest)
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

        if not errors:
            identity_result = self._sync_manifest_identity(desired)
            if identity_result.get("changed"):
                applied.append(
                    {
                        "kind": "sync_identity",
                        "detail": {
                            "display_name": desired.display_name,
                            "role": desired.role,
                            "prompts_dir": desired.prompts_dir,
                        },
                        "result": identity_result,
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
            return {"kind": result.get("kind", ""), "name": result.get("name", "")}
        if kind == "remove_channel":
            result = self.unassign_channel_from_agent(
                desired.id,
                str(action.detail.get("kind", "")),
                str(action.detail.get("name", "")),
            )
            return {"kind": result.get("kind", ""), "name": result.get("name", "")}
        if kind == "set_credentials":
            agent = self.set_agent_credential_bundles(
                desired.id,
                [str(item) for item in action.detail.get("to", [])],
                include_defaults=False,
            )
            sync = self._normalize_credential_sync_state(
                agent.get("credential_sync"),
                default_when_missing=True,
            )
            return {"credential_bundles": list(sync.get("bundles", []))}
        if kind == "set_addon":
            addon = str(action.detail.get("addon", "")).strip()
            enabled = bool(action.detail.get("enabled", False))
            if enabled:
                result = self.enable_agent_addon(desired.id, addon)
            else:
                result = self.disable_agent_addon(desired.id, addon)
            return {"addon": result.get("addon", addon), "enabled": enabled}
        raise ValueError(f"unsupported reconcile action: {kind}")

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
