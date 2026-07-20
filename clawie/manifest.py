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
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LIMIT_RANGES = {
    "delegation_depth": (1, 10),
    "gateway_timeout": (1, 3600),
}


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
        if len(self.display_name) > 200 or "\x00" in self.display_name:
            raise ManifestError("display_name must be at most 200 characters and contain no NUL")
        prompts = Path(str(self.prompts_dir or "prompts"))
        if (
            prompts.is_absolute()
            or not prompts.parts
            or any(part in {"", ".", ".."} for part in prompts.parts)
        ):
            raise ManifestError("prompts_dir must be a safe relative path")
        self.prompts_dir = prompts.as_posix()

        channel_keys: set[tuple[str, str]] = set()
        normalized_channels: list[ChannelSpec] = []
        for channel in self.channels:
            kind = str(channel.kind).strip().lower()
            name = str(channel.name).strip()
            if not kind or not name or "\x00" in kind or "\x00" in name:
                raise ManifestError("channel kind and name are required and may not contain NUL")
            key = (kind, name)
            if key in channel_keys:
                raise ManifestError(f"duplicate channel in manifest: {kind}:{name}")
            channel_keys.add(key)
            allow_from = tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in channel.allow_from
                    if str(value).strip()
                )
            )
            if any(len(value) > 256 or "\x00" in value for value in allow_from):
                raise ManifestError("channel allow_from entries must be <= 256 characters and contain no NUL")
            normalized_channels.append(ChannelSpec(kind=kind, name=name, allow_from=allow_from))
        self.channels = normalized_channels

        credential_names: set[str] = set()
        normalized_credentials: list[CredentialRef] = []
        for credential in self.credentials:
            name = str(credential.name).strip().lower().replace("_", "-")
            scope = str(credential.scope or "agent").strip().lower()
            if not _REFERENCE_RE.fullmatch(name):
                raise ManifestError(f"invalid credential reference name: {credential.name!r}")
            if scope not in {"agent", "shared"}:
                raise ManifestError("credential scope must be 'agent' or 'shared'")
            if name in credential_names:
                raise ManifestError(f"duplicate credential reference: {name}")
            credential_names.add(name)
            normalized_credentials.append(CredentialRef(name=name, scope=scope))
        self.credentials = normalized_credentials

        normalized_addons: dict[str, bool] = {}
        for name, enabled in self.addons.items():
            token = str(name).strip().lower()
            if not _REFERENCE_RE.fullmatch(token):
                raise ManifestError(f"invalid addon name: {name!r}")
            normalized_addons[token] = bool(enabled)
        self.addons = normalized_addons

        if not isinstance(self.limits, dict):
            raise ManifestError("limits must be a mapping")
        unknown_limits = sorted(set(self.limits) - set(_LIMIT_RANGES))
        if unknown_limits:
            raise ManifestError(f"unsupported limit(s): {', '.join(str(key) for key in unknown_limits)}")
        normalized_limits: dict[str, int] = {}
        for limit_name, value in self.limits.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ManifestError(f"limit {limit_name} must be an integer")
            low, high = _LIMIT_RANGES[limit_name]
            if not low <= value <= high:
                raise ManifestError(
                    f"limit {limit_name} must be between {low} and {high}"
                )
            normalized_limits[limit_name] = value
        self.limits = normalized_limits

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
        raw_channels = data.get("channels", [])
        if not isinstance(raw_channels, list):
            raise ManifestError("channels must be a list")
        if any(not isinstance(item, dict) for item in raw_channels):
            raise ManifestError("every channel must be a mapping")
        for channel in raw_channels:
            raw_allow_from = channel.get("allow_from", [])
            if not isinstance(raw_allow_from, list):
                raise ManifestError("channel allow_from must be a list")
        channels = [
            ChannelSpec(
                kind=str(c.get("kind", "")),
                name=str(c.get("name", "")),
                allow_from=tuple(str(x) for x in c.get("allow_from", []) if str(x).strip()),
            )
            for c in raw_channels
        ]
        raw_credentials = data.get("credentials", [])
        if not isinstance(raw_credentials, list):
            raise ManifestError("credentials must be a list")
        if any(not isinstance(item, dict) for item in raw_credentials):
            raise ManifestError("every credential reference must be a mapping")
        if any(not str(item.get("name", "")).strip() for item in raw_credentials):
            raise ManifestError("every credential reference requires a name")
        credentials = [
            CredentialRef(
                name=str(r.get("name", "")),
                scope=str(r.get("scope", "agent")) or "agent",
            )
            for r in raw_credentials
        ]
        raw_addons = data.get("addons", {})
        raw_limits = data.get("limits", {})
        if not isinstance(raw_addons, dict):
            raise ManifestError("addons must be a mapping")
        if any(not isinstance(value, bool) for value in raw_addons.values()):
            raise ManifestError("addon values must be booleans")
        if not isinstance(raw_limits, dict):
            raise ManifestError("limits must be a mapping")
        addons = {str(k): bool(v) for k, v in raw_addons.items() if str(k).strip()}
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
            limits=dict(raw_limits),
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
         "channels": [{"kind","name"}],
         "credential_bundles": [str],
         "addons": {name: bool}}
    """
    missing = not observed
    observed = observed or {}
    actions: list[ReconcileAction] = []

    if missing:
        actions.append(ReconcileAction("ensure_agent", {"agent_id": desired.id}))

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
        (str(c.get("kind", "")).strip().lower(), str(c.get("name", "")).strip()): tuple(
            str(item).strip() for item in c.get("allow_from", []) if str(item).strip()
        )
        for c in observed.get("channels", [])
        if isinstance(c, dict)
    }
    for key, channel in sorted(desired_ch.items()):
        if key not in observed_ch:
            actions.append(
                ReconcileAction(
                    "ensure_channel",
                    {
                        "kind": channel.kind,
                        "name": channel.name,
                        "allow_from": list(channel.allow_from),
                    },
                )
            )
        elif observed_ch[key] != channel.allow_from:
            actions.append(
                ReconcileAction(
                    "set_channel_allow_from",
                    {
                        "kind": channel.kind,
                        "name": channel.name,
                        "from": list(observed_ch[key]),
                        "to": list(channel.allow_from),
                    },
                )
            )
    for key in sorted(set(observed_ch) - set(desired_ch)):
        actions.append(ReconcileAction("remove_channel", {"kind": key[0], "name": key[1]}))

    desired_credentials = sorted(
        [{"name": ref.name, "scope": ref.scope} for ref in desired.credentials],
        key=lambda item: (item["name"], item["scope"]),
    )
    observed_refs = observed.get("credential_refs")
    if isinstance(observed_refs, list):
        observed_credentials = sorted(
            [
                {
                    "name": str(item.get("name", "")).strip().lower().replace("_", "-"),
                    "scope": str(item.get("scope", "agent") or "agent").strip().lower(),
                }
                for item in observed_refs
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ],
            key=lambda item: (item["name"], item["scope"]),
        )
    else:
        observed_credentials = sorted(
            [
                {
                    "name": str(item).strip().lower().replace("_", "-"),
                    "scope": "agent",
                }
                for item in observed.get("credential_bundles", [])
                if str(item).strip()
            ],
            key=lambda item: item["name"],
        )
    if observed_credentials != desired_credentials:
        actions.append(
            ReconcileAction(
                "set_credentials",
                {"from": observed_credentials, "to": desired_credentials},
            )
        )

    desired_identity = {
        "display_name": desired.display_name,
        "role": desired.role,
        "prompts_dir": desired.prompts_dir,
    }
    observed_identity = {
        "display_name": str(observed.get("display_name", desired.display_name)),
        "role": str(observed.get("role", "worker") or "worker").strip().lower(),
        "prompts_dir": str(observed.get("prompts_dir", "prompts") or "prompts"),
    }
    if desired_identity != observed_identity:
        actions.append(ReconcileAction("sync_identity", {"from": observed_identity, "to": desired_identity}))

    observed_limits = observed.get("limits", {}) if isinstance(observed.get("limits"), dict) else {}
    if observed_limits != desired.limits:
        actions.append(ReconcileAction("set_limits", {"from": observed_limits, "to": desired.limits}))

    obs_addons = dict(observed.get("addons", {}))
    for name in sorted(desired.addons):
        want = bool(desired.addons[name])
        if bool(obs_addons.get(name, False)) != want:
            actions.append(ReconcileAction("set_addon", {"addon": name, "enabled": want}))

    return actions


def is_converged(desired: AgentManifest, observed: dict[str, Any] | None) -> bool:
    """True when no reconcile actions are needed."""
    return not reconcile_plan(desired, observed)
