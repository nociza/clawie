"""Workspace manifest + reconcile core (Phase 3).

The manifest is the declarative source of truth for an agent — identity,
provider, tier, prompts, channels, credentials *by reference*, addons, limits.
``reconcile_plan`` diffs a desired manifest against observed runtime state and
returns an ordered, idempotent action list. This is what replaces the imperative
provider-switch / spawn sagas with a converging "edit the manifest, reconcile"
loop (Principle 3).

This module is pure: serialization + diffing only. Allocating ports, running
commands, and writing files is the service/daemon's job — keeping the manifest
model decoupled and fully unit-testable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_ROLES = ("worker", "control")
VALID_TIERS = ("fast", "balanced", "power")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid."""


@dataclass(frozen=True)
class ChannelSpec:
    kind: str
    name: str
    allow_from: tuple[str, ...] = ()

    def key(self) -> tuple[str, str]:
        return (self.kind.strip().lower(), self.name.strip())


@dataclass(frozen=True)
class CredentialRef:
    """A credential referenced by name and resolved at apply time — the manifest
    never carries inline secrets (Principle: per-scope, first-class secrets)."""

    name: str
    scope: str = "agent"  # "agent" (default, isolated) | "shared" (opt-in)


@dataclass
class AgentManifest:
    id: str
    provider: str = "openclaw"
    role: str = "worker"
    model_tier: str = "balanced"
    display_name: str = ""
    prompts_dir: str = "prompts"
    channels: list[ChannelSpec] = field(default_factory=list)
    credentials: list[CredentialRef] = field(default_factory=list)
    addons: dict[str, bool] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        if not _ID_RE.fullmatch(self.id) or ".." in self.id:
            raise ManifestError(
                "manifest id must start with a letter/digit and contain only "
                "letters, digits, '.', '_' or '-' (max 64 chars)"
            )
        self.provider = str(self.provider).strip().lower() or "openclaw"
        self.role = str(self.role).strip().lower() or "worker"
        if self.role not in VALID_ROLES:
            raise ManifestError(f"role must be one of {VALID_ROLES}, got {self.role!r}")
        self.model_tier = str(self.model_tier).strip().lower() or "balanced"
        if self.model_tier not in VALID_TIERS:
            raise ManifestError(f"model_tier must be one of {VALID_TIERS}, got {self.model_tier!r}")
        self.display_name = str(self.display_name or self.id)

    # --- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "role": self.role,
            "model_tier": self.model_tier,
            "display_name": self.display_name,
            "prompts_dir": self.prompts_dir,
            "channels": [
                {"kind": c.kind, "name": c.name, "allow_from": list(c.allow_from)}
                for c in self.channels
            ],
            "credentials": [{"name": r.name, "scope": r.scope} for r in self.credentials],
            "addons": dict(self.addons),
            "limits": dict(self.limits),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentManifest":
        if not isinstance(data, dict):
            raise ManifestError("manifest must be a mapping")
        if not str(data.get("id", "")).strip():
            raise ManifestError("manifest requires an 'id'")
        channels = [
            ChannelSpec(
                kind=str(c.get("kind", "")),
                name=str(c.get("name", "")),
                allow_from=tuple(str(x) for x in c.get("allow_from", []) if str(x).strip()),
            )
            for c in data.get("channels", [])
            if isinstance(c, dict)
        ]
        credentials = [
            CredentialRef(name=str(r.get("name", "")), scope=str(r.get("scope", "agent")) or "agent")
            for r in data.get("credentials", [])
            if isinstance(r, dict) and str(r.get("name", "")).strip()
        ]
        addons = {
            str(k): bool(v) for k, v in dict(data.get("addons", {})).items() if str(k).strip()
        }
        return cls(
            id=str(data["id"]),
            provider=str(data.get("provider", "openclaw")),
            role=str(data.get("role", "worker")),
            model_tier=str(data.get("model_tier", "balanced")),
            display_name=str(data.get("display_name", "")),
            prompts_dir=str(data.get("prompts_dir", "prompts")),
            channels=channels,
            credentials=credentials,
            addons=addons,
            limits=dict(data.get("limits", {})),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "AgentManifest":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"invalid manifest JSON: {exc}") from exc
        return cls.from_dict(data)

    def write(self, path: str | Path) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json(), encoding="utf-8")
        return target

    @classmethod
    def read(cls, path: str | Path) -> "AgentManifest":
        return cls.from_json(Path(path).expanduser().read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReconcileAction:
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


def reconcile_plan(desired: AgentManifest, observed: dict[str, Any] | None) -> list[ReconcileAction]:
    """Diff *desired* against *observed* runtime state and return idempotent
    actions to converge. An empty list means already in sync (a no-op reconcile).

    *observed* is a plain dict the service builds from live state::

        {"provider": str, "model_tier": str,
         "channels": [{"kind","name"}], "addons": {name: bool}}
    """
    observed = observed or {}
    actions: list[ReconcileAction] = []

    obs_provider = str(observed.get("provider", "")).strip().lower()
    if obs_provider != desired.provider:
        actions.append(
            ReconcileAction("set_provider", {"from": obs_provider, "to": desired.provider})
        )

    obs_tier = str(observed.get("model_tier", "")).strip().lower()
    if obs_tier != desired.model_tier:
        actions.append(
            ReconcileAction("set_model_tier", {"from": obs_tier, "to": desired.model_tier})
        )

    desired_ch = {c.key(): c for c in desired.channels}
    observed_ch = {
        (str(c.get("kind", "")).strip().lower(), str(c.get("name", "")).strip())
        for c in observed.get("channels", [])
        if isinstance(c, dict)
    }
    for key, channel in sorted(desired_ch.items()):
        if key not in observed_ch:
            actions.append(
                ReconcileAction("ensure_channel", {"kind": channel.kind, "name": channel.name})
            )
    for key in sorted(observed_ch - set(desired_ch)):
        actions.append(ReconcileAction("remove_channel", {"kind": key[0], "name": key[1]}))

    obs_addons = dict(observed.get("addons", {}))
    for name in sorted(desired.addons):
        want = bool(desired.addons[name])
        if bool(obs_addons.get(name, False)) != want:
            actions.append(ReconcileAction("set_addon", {"addon": name, "enabled": want}))

    return actions


def is_converged(desired: AgentManifest, observed: dict[str, Any] | None) -> bool:
    """True when no reconcile actions are needed."""
    return not reconcile_plan(desired, observed)
