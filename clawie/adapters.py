"""Provider adapters: the seam between clawie core and a runtime.

openclaw is the reference adapter. Everything runtime-specific — version policy,
gateway endpoint, config rendering, credential setup, task delivery, and the
tier→model map — lives behind :class:`ProviderAdapter` so core knows no runtime's
internals and adding a new runtime (hermes, …) is a new file, not surgery into
the service mixins.

Adapters are deliberately **pure**: methods build argv lists, parse text, or
return config patches. All subprocess execution, port allocation, token
persistence, and filesystem work stay in the service layer, which keeps the whole
seam unit-testable without a runtime installed.

Verified against openclaw ``2026.7.1`` (github.com/openclaw/openclaw @ ``2d2ddc43``);
see ``docs/design/control-plane.md`` Appendix A for the primary-source facts this
encodes (loopback gateway port, token auth, ``agent`` run JSON, protocol v4,
``openclaw models auth`` as the auth surface, canonical ``openai/*`` model ids).
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


class AdapterError(RuntimeError):
    """Base error for adapter-level failures."""


class UnsupportedVersionError(AdapterError):
    """Raised when a runtime version is outside the adapter's tested range and the
    caller asked to treat that as fatal rather than degrade-and-notify."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class Version:
    """A comparable ``major.minor.patch`` version."""

    major: int
    minor: int
    patch: int

    _RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

    @classmethod
    def parse(cls, text: str | None) -> "Version | None":
        """Extract the first ``N.N.N`` from *text* (e.g. ``"openclaw 2026.6.2"``)."""
        if not text:
            return None
        match = cls._RE.search(str(text))
        if not match:
            return None
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Endpoint:
    """A resolved local gateway endpoint."""

    host: str
    port: int

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class Task:
    """A unit of work to deliver to an agent."""

    task_id: str
    message: str
    tier: str = "balanced"


@dataclass(frozen=True)
class Reply:
    """A parsed terminal result from an agent run."""

    ok: bool
    output: str
    usage: dict[str, Any] = field(default_factory=dict)
    delivery_status: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VersionGate:
    """Outcome of checking a detected runtime version against the tested range."""

    version: Version | None
    supported: bool
    message: str

    @property
    def degraded(self) -> bool:
        """True when writes should degrade to read-only (unknown/untested version)."""
        return not self.supported


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ProviderAdapter(Protocol):
    """The contract every runtime adapter implements.

    Methods are pure: they return argv lists, parse text, or return data. The
    service layer runs the commands and persists the results.
    """

    name: str

    # version policy
    def version_command(self) -> list[str]: ...
    def supported_range(self) -> tuple[Version, Version]: ...
    def is_supported(self, version: Version | None) -> bool: ...
    def version_gate(self, version_output: str | None) -> VersionGate: ...

    # endpoint + config
    def gateway_endpoint(self, port: int, host: str = ...) -> Endpoint: ...
    def gateway_config_patch(self, *, port: int, token: str) -> dict[str, Any]: ...

    # models
    def tier_to_model(self, tier: str) -> str: ...

    # delivery (the bridge)
    def runtime_agent_id(self, managed_agent_id: str) -> str: ...
    def deliver_command(
        self, agent_id: str, task: Task, *, timeout: float, openclaw_bin: str = ...
    ) -> list[str]: ...
    def parse_reply(self, stdout: str) -> Reply: ...

    # auth (replaces hand-written auth files)
    def auth_login_command(
        self, provider: str, *, profile_id: str = ..., method: str = ..., openclaw_bin: str = ...
    ) -> list[str]: ...
    def readiness_command(self, openclaw_bin: str = ...) -> list[str]: ...


# ---------------------------------------------------------------------------
# openclaw — the reference adapter
# ---------------------------------------------------------------------------

OPENCLAW_DEFAULT_PORT = 18789
OPENCLAW_PROTOCOL_MIN = 4
OPENCLAW_PROTOCOL_MAX = 4


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *patch* into a copy of *base* (patch wins on conflicts)."""
    out = dict(base)
    for key, value in patch.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = deep_merge(existing, value)
        else:
            out[key] = value
    return out


class GatewayCliAdapter:
    """Base adapter for gateway + CLI agent runtimes (openclaw, hermes, …).

    Subclasses set the class attributes below; every method here is runtime-
    agnostic. This is the seam in practice: a new runtime is a subclass plus a
    registry entry, not surgery into the service mixins.
    """

    name = "gateway"
    binary = "gateway"  # the runtime's CLI entrypoint on PATH
    MIN_SUPPORTED = Version(0, 0, 0)
    MAX_SUPPORTED_EXCLUSIVE = Version(9999, 0, 0)
    PROTOCOL_MIN = 1
    PROTOCOL_MAX = 1
    TIER_MODELS: dict[str, str] = {}
    DEFAULT_MODEL = ""
    CONTRACT_VERIFIED = True

    # --- version policy ------------------------------------------------------

    def version_command(self) -> list[str]:
        return [self.binary, "--version"]

    def supported_range(self) -> tuple[Version, Version]:
        return (self.MIN_SUPPORTED, self.MAX_SUPPORTED_EXCLUSIVE)

    def is_supported(self, version: Version | None) -> bool:
        if version is None:
            return False
        return self.MIN_SUPPORTED <= version < self.MAX_SUPPORTED_EXCLUSIVE

    def version_gate(self, version_output: str | None) -> VersionGate:
        """Classify a detected version. Unknown or out-of-band → degrade + notify."""
        version = Version.parse(version_output)
        low, high = self.supported_range()
        if version is None:
            return VersionGate(
                None,
                False,
                f"could not detect {self.name} version; config writes degraded to "
                "read-only until a supported version is confirmed",
            )
        if self.is_supported(version):
            return VersionGate(version, True, f"{self.name} {version} is within the tested range")
        return VersionGate(
            version,
            False,
            f"{self.name} {version} is outside the tested range "
            f"[{low}, {high}); config writes degraded to read-only — upgrade clawie "
            f"or pin {self.name}, then re-run",
        )

    # --- endpoint + config ---------------------------------------------------

    def gateway_endpoint(self, port: int, host: str = "127.0.0.1") -> Endpoint:
        return Endpoint(host=host, port=int(port))

    def gateway_config_patch(self, *, port: int, token: str) -> dict[str, Any]:
        """The ``openclaw.json`` patch that makes an agent's gateway addressable:
        local mode, loopback bind, a deterministic per-agent port, and token auth.
        """
        if not token:
            raise AdapterError("gateway auth token is required")
        return {
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "port": int(port),
                "auth": {"mode": "token", "token": str(token)},
            }
        }

    @staticmethod
    def new_gateway_token() -> str:
        """A fresh per-agent gateway auth token (URL-safe, ~256 bits of entropy)."""
        return secrets.token_urlsafe(32)

    # --- models --------------------------------------------------------------

    def tier_to_model(self, tier: str) -> str:
        model = str(self.TIER_MODELS.get(str(tier).strip().lower(), self.DEFAULT_MODEL) or "").strip()
        if not model:
            raise AdapterError(
                f"{self.name} has no verified model mapping; runtime writes are disabled "
                "until the adapter contract is pinned"
            )
        return model

    # --- delivery (the bridge) ----------------------------------------------

    @staticmethod
    def session_key(agent_id: str, task_id: str) -> str:
        """A dedicated session per delegated task, so it doesn't pollute the
        human channel history and isn't mistaken for a HEARTBEAT poll."""
        return f"agent:{agent_id}:clawie:{task_id}"

    def runtime_agent_id(self, managed_agent_id: str) -> str:
        """Map a Clawie agent ID to the provider runtime's internal agent ID."""
        return str(managed_agent_id).strip()

    def deliver_command(
        self, agent_id: str, task: Task, *, timeout: float, openclaw_bin: str = ""
    ) -> list[str]:
        """Bootstrap delivery path: one agent turn via the gateway, JSON out.

        The native WS ``agent``+``agent.wait`` path supersedes this for streaming
        and richer control, but the CLI is the trivially-correct first cut and
        the verified fallback.
        """
        managed_id = str(agent_id).strip()
        if not managed_id:
            raise AdapterError("agent_id is required")
        runtime_id = self.runtime_agent_id(managed_id)
        if not runtime_id:
            raise AdapterError(f"{self.name} runtime agent_id is required")
        cmd = [
            openclaw_bin or self.binary,
            "agent",
            "--agent",
            runtime_id,
            "--session-key",
            self.session_key(runtime_id, task.task_id),
            "--message",
            task.message,
            "--json",
            "--timeout",
            str(int(timeout)),
        ]
        model = self.tier_to_model(task.tier)
        if model:
            cmd += ["--model", model]
        return cmd

    def parse_reply(self, stdout: str) -> Reply:
        """Parse the JSON from ``openclaw agent --json``.

        Shape (Appendix A / docs/cli/agent.md): the gateway response wraps the
        reply under ``result`` (``result.payloads`` and ``result.meta``), while
        older/embedded responses may put those fields at the top level.
        Diagnostics go to stderr, so stdout is reserved for the JSON object.
        """
        text = str(stdout or "").strip()
        if not text:
            return Reply(ok=False, output="", error="empty response from openclaw agent")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return Reply(ok=False, output="", error=f"unparseable agent JSON: {exc}")
        if not isinstance(data, dict):
            return Reply(ok=False, output="", error="agent JSON was not an object", raw={})

        raw_result = data.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        payloads = data.get("payloads")
        if not isinstance(payloads, list):
            payloads = result.get("payloads", [])
        parts: list[str] = []
        payload_error = False
        if isinstance(payloads, list):
            for item in payloads:
                if isinstance(item, dict):
                    payload_error = payload_error or item.get("isError") is True
                    chunk = str(item.get("text", "") or "").strip()
                    if chunk:
                        parts.append(chunk)
        output = "\n".join(parts)

        delivery = data.get("deliveryStatus") or result.get("deliveryStatus") or {}
        if isinstance(delivery, dict):
            delivery_status = str(delivery.get("status", ""))
        else:
            delivery_status = str(delivery or "")

        raw_meta = data.get("meta")
        if not isinstance(raw_meta, dict):
            raw_meta = result.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        usage = {}
        for src in (meta.get("usage"), result.get("usage"), data.get("usage")):
            if isinstance(src, dict) and src:
                usage = src
                break

        error = str(data.get("error", result.get("error", "")) or "")
        status = str(data.get("status", "") or "")
        if payload_error and not error:
            error = output or "agent reply contained an error payload"
        ok = not error and status in {"", "ok"}
        if not ok and not error:
            error = status or "agent run did not complete"
        return Reply(
            ok=ok,
            output=output,
            usage=usage,
            delivery_status=delivery_status,
            error="" if ok else error,
            raw={**data, "meta": meta},
        )

    # --- auth (replaces hand-written auth-profiles.json) ---------------------

    def auth_login_command(
        self,
        provider: str,
        *,
        profile_id: str = "",
        method: str = "",
        set_default: bool = False,
        agent: str = "",
        openclaw_bin: str = "",
    ) -> list[str]:
        """Drive the runtime's own auth surface instead of writing legacy JSON.

        openclaw stores auth in each agent's ``openclaw-agent.sqlite``; the
        ``auth-profiles.json`` + ``openai-codex`` ids clawie used to write are
        legacy migration input (Appendix A).
        """
        prov = str(provider).strip().lower()
        if not prov:
            raise AdapterError("provider is required")
        cmd = [openclaw_bin or self.binary, "models", "auth", "login", "--provider", prov]
        if profile_id:
            cmd += ["--profile-id", str(profile_id)]
        if method:
            cmd += ["--method", str(method)]
        if set_default:
            cmd += ["--set-default"]
        if agent:
            cmd += ["--agent", str(agent)]
        return cmd

    def auth_paste_token_command(
        self, provider: str, *, openclaw_bin: str = ""
    ) -> list[str]:
        prov = str(provider).strip().lower()
        if not prov:
            raise AdapterError("provider is required")
        return [openclaw_bin or self.binary, "models", "auth", "paste-token", "--provider", prov]

    def readiness_command(self, openclaw_bin: str = "") -> list[str]:
        return [openclaw_bin or self.binary, "models", "status", "--json"]

    def gateway_status_command(self, openclaw_bin: str = "") -> list[str]:
        return [openclaw_bin or self.binary, "gateway", "status", "--json"]


class OpenclawAdapter(GatewayCliAdapter):
    """Reference adapter — the openclaw gateway runtime (verified 2026.7.1)."""

    name = "openclaw"
    binary = "openclaw"
    # Source-pinned production band. A future patch is deliberately rejected
    # until its CLI and JSON delivery contract has been exercised and pinned.
    MIN_SUPPORTED = Version(2026, 7, 1)
    MAX_SUPPORTED_EXCLUSIVE = Version(2026, 7, 2)
    PROTOCOL_MIN = OPENCLAW_PROTOCOL_MIN
    PROTOCOL_MAX = OPENCLAW_PROTOCOL_MAX
    # Canonical ``openai/*`` ids — NOT the legacy ``openai-codex/*`` ids that
    # `openclaw doctor --fix` rewrites (Appendix A).
    TIER_MODELS = {
        "fast": "openai/gpt-5.6-sol",
        "balanced": "openai/gpt-5.6-sol",
        "power": "openai/gpt-5.6-sol",
    }
    DEFAULT_MODEL = "openai/gpt-5.6-sol"

    def runtime_agent_id(self, managed_agent_id: str) -> str:
        """Each isolated Clawie Linux user owns OpenClaw's default agent."""
        if not str(managed_agent_id).strip():
            return ""
        return "main"


class HermesAdapter(GatewayCliAdapter):
    """Scaffold adapter for the planned hermes runtime.

    hermes is intended as a second first-class runtime. Its exact contract has
    not been verified against hermes source, so the gateway+CLI shape inherited
    from the base is a starting point, not a guarantee. The supported range is
    intentionally empty, so the version gate degrades hermes agents to read-only
    until a real, pinned hermes version + model map is filled in here.
    """

    name = "hermes"
    binary = "hermes"
    MIN_SUPPORTED = Version(9999, 0, 0)
    MAX_SUPPORTED_EXCLUSIVE = Version(9999, 0, 1)
    TIER_MODELS = {"fast": "", "balanced": "", "power": ""}
    DEFAULT_MODEL = ""
    CONTRACT_VERIFIED = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, ProviderAdapter] = {
    OpenclawAdapter.name: OpenclawAdapter(),
    HermesAdapter.name: HermesAdapter(),
}


def get_adapter(name: str) -> ProviderAdapter:
    """Return the adapter for *name*, raising ``AdapterError`` if unknown."""
    token = str(name).strip().lower()
    adapter = _ADAPTERS.get(token)
    if adapter is None:
        choices = ", ".join(sorted(_ADAPTERS))
        raise AdapterError(f"no adapter for runtime {name!r}; known: {choices}")
    return adapter


def adapter_names() -> list[str]:
    return sorted(_ADAPTERS)


def detect_version(adapter: ProviderAdapter, run: Callable[[list[str]], str]) -> VersionGate:
    """Run *adapter*'s version command via *run* and return the gate outcome.

    *run* takes an argv list and returns captured stdout (the service injects a
    subprocess runner; tests inject a stub). Failures degrade rather than raise.
    """
    try:
        output = run(adapter.version_command())
    except Exception as exc:  # noqa: BLE001 - detection must never abort the caller
        return VersionGate(None, False, f"openclaw version probe failed: {exc}")
    return adapter.version_gate(output)
