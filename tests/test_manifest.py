"""Tests for the workspace manifest + reconcile core (Phase 3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from clawie.manifest import (
    AgentManifest,
    ChannelSpec,
    CredentialRef,
    ManifestError,
    ReconcileAction,
    is_converged,
    reconcile_plan,
)


def test_manifest_defaults() -> None:
    m = AgentManifest(id="alice")
    assert m.provider == "openclaw"
    assert m.role == "worker"
    assert m.model_tier == "balanced"
    assert m.display_name == "alice"


@pytest.mark.parametrize("bad_id", ["", "..", "-bad", "a/b", "x" * 65])
def test_manifest_rejects_bad_id(bad_id: str) -> None:
    with pytest.raises(ManifestError):
        AgentManifest(id=bad_id)


@pytest.mark.parametrize("field,value", [("role", "admin"), ("model_tier", "turbo")])
def test_manifest_rejects_invalid_enums(field: str, value: str) -> None:
    with pytest.raises(ManifestError):
        AgentManifest(id="alice", **{field: value})


def test_manifest_round_trip_dict_and_json() -> None:
    m = AgentManifest(
        id="alice",
        provider="openclaw",
        role="control",
        model_tier="power",
        channels=[ChannelSpec("telegram", "alice-tg", ("@you",))],
        credentials=[CredentialRef("codex", "agent")],
        addons={"display": True},
        limits={"gateway_timeout": 300},
    )
    again = AgentManifest.from_dict(m.to_dict())
    assert again.to_dict() == m.to_dict()
    assert AgentManifest.from_json(m.to_json()).to_dict() == m.to_dict()


def test_manifest_credentials_are_by_reference_never_inline() -> None:
    m = AgentManifest(id="alice", credentials=[CredentialRef("codex", "agent")])
    payload = m.to_dict()
    # only a name + scope — no token/secret fields
    assert payload["credentials"] == [{"name": "codex", "scope": "agent"}]


def test_manifest_write_read_file(tmp_path: Path) -> None:
    m = AgentManifest(id="alice", model_tier="fast")
    path = m.write(tmp_path / "agent.json")
    assert path.exists()
    assert AgentManifest.read(path).to_dict() == m.to_dict()


def test_from_dict_requires_id() -> None:
    with pytest.raises(ManifestError):
        AgentManifest.from_dict({"provider": "openclaw"})


# --- reconcile -------------------------------------------------------------

def test_reconcile_noop_when_in_sync() -> None:
    m = AgentManifest(id="a", provider="openclaw", model_tier="balanced")
    observed = {"provider": "openclaw", "model_tier": "balanced", "channels": [], "addons": {}}
    assert reconcile_plan(m, observed) == []
    assert is_converged(m, observed) is True


def test_reconcile_provider_and_tier() -> None:
    m = AgentManifest(id="a", provider="openclaw", model_tier="power")
    observed = {"provider": "hermes", "model_tier": "balanced"}
    plan = reconcile_plan(m, observed)
    kinds = [a.kind for a in plan]
    assert "set_provider" in kinds
    assert "set_model_tier" in kinds
    sp = next(a for a in plan if a.kind == "set_provider")
    assert sp.detail == {"from": "hermes", "to": "openclaw"}


def test_reconcile_channels_add_and_remove() -> None:
    m = AgentManifest(id="a", channels=[ChannelSpec("telegram", "keep"), ChannelSpec("slack", "new")])
    observed = {
        "provider": "openclaw",
        "model_tier": "balanced",
        "channels": [{"kind": "telegram", "name": "keep"}, {"kind": "email", "name": "stale"}],
    }
    plan = reconcile_plan(m, observed)
    ensures = [a.detail for a in plan if a.kind == "ensure_channel"]
    removes = [a.detail for a in plan if a.kind == "remove_channel"]
    assert {"kind": "slack", "name": "new"} in ensures
    assert {"kind": "telegram", "name": "keep"} not in ensures  # already present
    assert {"kind": "email", "name": "stale"} in removes


def test_reconcile_addons() -> None:
    m = AgentManifest(id="a", addons={"display": True, "gws": False})
    observed = {"provider": "openclaw", "model_tier": "balanced", "addons": {"display": False}}
    plan = reconcile_plan(m, observed)
    set_addons = {a.detail["addon"]: a.detail["enabled"] for a in plan if a.kind == "set_addon"}
    assert set_addons["display"] is True
    assert "gws" not in set_addons  # already disabled/absent -> no action


def test_reconcile_handles_empty_observed() -> None:
    m = AgentManifest(id="a", provider="openclaw", model_tier="fast")
    plan = reconcile_plan(m, None)
    assert any(a.kind == "set_provider" for a in plan)
    assert isinstance(plan[0], ReconcileAction)
