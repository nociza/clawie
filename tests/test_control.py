"""Tests for the control-agent capability gate (Phase 4)."""
from __future__ import annotations

import pytest

from clawie.control import ControlGate, Decision, Tier, tier_for


@pytest.mark.parametrize(
    "verb,tier",
    [
        ("status", Tier.READ),
        ("version", Tier.READ),
        ("restart", Tier.SAFE_HEAL),
        ("backup", Tier.SAFE_HEAL),
        ("delete_agent", Tier.DESTRUCTIVE),
        ("set_provider", Tier.DESTRUCTIVE),
        ("open_pr", Tier.OUTWARD),
        ("open_issue", Tier.OUTWARD),
    ],
)
def test_tier_for_known(verb: str, tier: Tier) -> None:
    assert tier_for(verb) is tier


def test_tier_for_unknown_is_fail_closed() -> None:
    assert tier_for("rm_rf_everything") is Tier.DESTRUCTIVE


def test_autonomous_verbs_allow_immediately() -> None:
    gate = ControlGate(allowlist=["@op"])
    for verb in ("status", "restart", "sync_auth", "backup"):
        result = gate.authorize(verb)
        assert result.allowed is True
        assert result.decision is Decision.ALLOW
        assert result.nonce == ""


def test_destructive_requires_confirmation() -> None:
    gate = ControlGate(allowlist=["@op"])
    result = gate.authorize("delete_agent", {"agent_id": "alice"})
    assert result.decision is Decision.PENDING_CONFIRMATION
    assert result.allowed is False
    assert result.nonce
    assert gate.pending_count() == 1


def test_confirm_happy_path() -> None:
    gate = ControlGate(allowlist=["@op"])
    args = {"agent_id": "alice"}
    pending = gate.authorize("delete_agent", args)
    confirmed = gate.confirm(pending.nonce, confirmer="@op", verb="delete_agent", args=args)
    assert confirmed.allowed is True
    assert confirmed.decision is Decision.ALLOW
    assert gate.pending_count() == 0  # consumed


def test_confirm_outward_verb() -> None:
    gate = ControlGate(allowlist=["@op"])
    pending = gate.authorize("open_pr", {"title": "fix"})
    assert pending.tier is Tier.OUTWARD
    confirmed = gate.confirm(pending.nonce, confirmer="@op", verb="open_pr", args={"title": "fix"})
    assert confirmed.allowed is True


def test_confirm_unknown_nonce_denied() -> None:
    gate = ControlGate()
    result = gate.confirm("deadbeef", confirmer="@op", verb="delete_agent")
    assert result.decision is Decision.DENY
    assert "unknown" in result.reason


def test_confirm_is_one_shot() -> None:
    gate = ControlGate(allowlist=["@op"])
    args = {"agent_id": "alice"}
    pending = gate.authorize("delete_agent", args)
    assert gate.confirm(pending.nonce, confirmer="@op", verb="delete_agent", args=args).allowed
    # reuse of the same nonce is denied
    again = gate.confirm(pending.nonce, confirmer="@op", verb="delete_agent", args=args)
    assert again.decision is Decision.DENY


def test_confirm_rejects_non_allowlisted_confirmer() -> None:
    gate = ControlGate(allowlist=["@op"])
    pending = gate.authorize("purge_agent", {"agent_id": "x"})
    result = gate.confirm(pending.nonce, confirmer="@attacker", verb="purge_agent", args={"agent_id": "x"})
    assert result.decision is Decision.DENY
    assert "allowlist" in result.reason


def test_confirm_rejects_changed_args() -> None:
    """A poisoned/altered payload between request and confirm is rejected."""
    gate = ControlGate(allowlist=["@op"])
    pending = gate.authorize("delete_agent", {"agent_id": "alice"})
    result = gate.confirm(
        pending.nonce, confirmer="@op", verb="delete_agent", args={"agent_id": "EVERYONE"}
    )
    assert result.decision is Decision.DENY
    assert "args changed" in result.reason


def test_confirm_rejects_verb_mismatch() -> None:
    gate = ControlGate(allowlist=["@op"])
    pending = gate.authorize("delete_agent", {"agent_id": "alice"})
    result = gate.confirm(pending.nonce, confirmer="@op", verb="open_pr", args={"agent_id": "alice"})
    assert result.decision is Decision.DENY


def test_confirm_expired_denied() -> None:
    gate = ControlGate(allowlist=["@op"])
    pending = gate.authorize("delete_agent", {"agent_id": "alice"})
    gate._ttl = -1.0  # force any elapsed time to be "expired"
    result = gate.confirm(pending.nonce, confirmer="@op", verb="delete_agent", args={"agent_id": "alice"})
    assert result.decision is Decision.DENY
    assert "expired" in result.reason


def test_empty_allowlist_does_not_restrict_confirmer() -> None:
    gate = ControlGate()  # no allowlist configured
    pending = gate.authorize("delete_agent", {})
    result = gate.confirm(pending.nonce, confirmer="anyone", verb="delete_agent", args={})
    assert result.allowed is True
