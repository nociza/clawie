"""Control-agent capability gate (Phase 4).

The enforcement heart of the control agent. Every control verb has a fixed
capability tier, and the gate decides **in code** whether a request executes —
because a prompt-only boundary fails under injection (Principle 6). Read and
safe-heal actions are autonomous; destructive and outward (repo) actions cannot
execute without a nonce confirmation from an allowlisted operator.

Pure: no I/O, no LLM. The control runtime calls ``authorize`` before acting and
only proceeds on an ``ALLOW`` decision. The LLM can *request* a destructive
action but can never mint the confirmation it didn't get from a human — and the
confirmation is bound to the exact verb+args, so a poisoned log line or a stray
"yes" cannot self-approve.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    READ = "read"
    SAFE_HEAL = "safe_heal"
    DESTRUCTIVE = "destructive"
    OUTWARD = "outward"


# verb -> tier. Unknown verbs fail closed (DESTRUCTIVE → confirmation required).
VERB_TIERS: dict[str, Tier] = {
    # read (autonomous)
    "status": Tier.READ,
    "logs": Tier.READ,
    "list": Tier.READ,
    "tree": Tier.READ,
    "version": Tier.READ,
    # safe-heal (autonomous, logged)
    "restart": Tier.SAFE_HEAL,
    "apply_prompts": Tier.SAFE_HEAL,
    "sync_auth": Tier.SAFE_HEAL,
    "backup": Tier.SAFE_HEAL,
    "reconcile": Tier.SAFE_HEAL,
    # destructive (confirm)
    "delete_agent": Tier.DESTRUCTIVE,
    "purge_agent": Tier.DESTRUCTIVE,
    "set_credentials": Tier.DESTRUCTIVE,
    "revoke_credentials": Tier.DESTRUCTIVE,
    "set_provider": Tier.DESTRUCTIVE,
    # outward — repo writes (preview + confirm)
    "open_issue": Tier.OUTWARD,
    "open_pr": Tier.OUTWARD,
}

AUTONOMOUS_TIERS = (Tier.READ, Tier.SAFE_HEAL)
CONFIRM_TIERS = (Tier.DESTRUCTIVE, Tier.OUTWARD)


class Decision(str, Enum):
    ALLOW = "allow"
    PENDING_CONFIRMATION = "pending_confirmation"
    DENY = "deny"


@dataclass(frozen=True)
class GateResult:
    decision: Decision
    tier: Tier
    verb: str
    nonce: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def _norm(verb: str) -> str:
    return str(verb).strip().lower()


def tier_for(verb: str) -> Tier:
    return VERB_TIERS.get(_norm(verb), Tier.DESTRUCTIVE)


@dataclass
class _Pending:
    verb: str
    tier: Tier
    fingerprint: str
    created_at: float


class ControlGate:
    """In-code capability gate for control-agent actions.

    - read / safe-heal verbs → ``ALLOW`` immediately (caller logs the action).
    - destructive / outward verbs → ``PENDING_CONFIRMATION`` with a nonce; the
      caller shows the nonce to a human, who echoes it via :meth:`confirm`. A
      ``confirm`` with the right (unused) nonce, from an allowlisted confirmer,
      for the same verb+args, yields ``ALLOW``.
    """

    def __init__(self, allowlist: list[str] | None = None, *, ttl_seconds: float = 300.0) -> None:
        self._allowlist = {str(x).strip() for x in (allowlist or []) if str(x).strip()}
        self._ttl = float(ttl_seconds)
        self._pending: dict[str, _Pending] = {}

    @staticmethod
    def _fingerprint(verb: str, args: dict | None) -> str:
        return f"{verb}:{json.dumps(args or {}, sort_keys=True)}"

    def authorize(self, verb: str, args: dict | None = None) -> GateResult:
        nverb = _norm(verb)
        tier = tier_for(nverb)
        if tier in AUTONOMOUS_TIERS:
            return GateResult(Decision.ALLOW, tier, nverb, reason="autonomous tier")
        nonce = secrets.token_hex(8)
        self._pending[nonce] = _Pending(
            nverb, tier, self._fingerprint(nverb, args), time.monotonic()
        )
        return GateResult(
            Decision.PENDING_CONFIRMATION,
            tier,
            nverb,
            nonce=nonce,
            reason="confirmation required: echo the nonce from an allowlisted operator",
        )

    def confirm(
        self, nonce: str, *, confirmer: str, verb: str, args: dict | None = None
    ) -> GateResult:
        nverb = _norm(verb)
        token = str(nonce).strip()
        pending = self._pending.pop(token, None)  # one-shot: consume on any outcome
        if pending is None:
            return GateResult(Decision.DENY, tier_for(nverb), nverb, reason="unknown or used nonce")
        if time.monotonic() - pending.created_at > self._ttl:
            return GateResult(Decision.DENY, pending.tier, nverb, reason="confirmation expired")
        if self._allowlist and str(confirmer).strip() not in self._allowlist:
            return GateResult(Decision.DENY, pending.tier, nverb, reason="confirmer not on allowlist")
        if pending.verb != nverb:
            return GateResult(Decision.DENY, pending.tier, nverb, reason="verb mismatch")
        if pending.fingerprint != self._fingerprint(nverb, args):
            return GateResult(Decision.DENY, pending.tier, nverb, reason="args changed since request")
        return GateResult(Decision.ALLOW, pending.tier, nverb, reason="confirmed by operator")

    def pending_count(self) -> int:
        return len(self._pending)
