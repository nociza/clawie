"""Unit tests for the provider-adapter seam (clawie.adapters).

The adapter is pure, so these tests need no openclaw install or network: they
exercise argv construction, JSON parsing, version policy, and config rendering.
"""
from __future__ import annotations

import pytest

from clawie.adapters import (
    AdapterError,
    Endpoint,
    OpenclawAdapter,
    ProviderAdapter,
    Reply,
    Task,
    Version,
    VersionGate,
    adapter_names,
    deep_merge,
    detect_version,
    get_adapter,
)


@pytest.fixture
def adapter() -> OpenclawAdapter:
    return OpenclawAdapter()


# --- Version ---------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026.6.2", Version(2026, 6, 2)),
        ("openclaw 2026.6.2", Version(2026, 6, 2)),
        ("v2026.6.2\n", Version(2026, 6, 2)),
        ("openclaw cli 2026.10.0 (build abc)", Version(2026, 10, 0)),
        ("", None),
        (None, None),
        ("no version here", None),
    ],
)
def test_version_parse(text, expected) -> None:
    assert Version.parse(text) == expected


def test_version_ordering_and_str() -> None:
    assert Version(2026, 6, 0) < Version(2026, 6, 2) < Version(2026, 7, 0)
    assert Version(2026, 6, 2) < Version(2027, 0, 0)
    assert str(Version(2026, 6, 2)) == "2026.6.2"


# --- version policy --------------------------------------------------------

def test_supported_range_bounds(adapter: OpenclawAdapter) -> None:
    low, high = adapter.supported_range()
    assert low == Version(2026, 7, 1)
    assert high == Version(2026, 7, 2)


@pytest.mark.parametrize(
    "version,supported",
    [
        (Version(2026, 7, 1), True),
        (Version(2026, 6, 2), False),
        (Version(2026, 7, 2), False),
        (Version(2026, 5, 9), False),  # below
        (Version(2026, 7, 0), False),  # below pinned release
        (Version(2027, 0, 0), False),  # above
        (None, False),
    ],
)
def test_is_supported(adapter: OpenclawAdapter, version, supported) -> None:
    assert adapter.is_supported(version) is supported


def test_version_gate_supported(adapter: OpenclawAdapter) -> None:
    gate = adapter.version_gate("openclaw 2026.7.1")
    assert gate.supported is True
    assert gate.degraded is False
    assert gate.version == Version(2026, 7, 1)


def test_version_gate_unknown_degrades(adapter: OpenclawAdapter) -> None:
    gate = adapter.version_gate("garbage")
    assert gate.version is None
    assert gate.supported is False
    assert gate.degraded is True
    assert "read-only" in gate.message


def test_version_gate_out_of_band_degrades(adapter: OpenclawAdapter) -> None:
    gate = adapter.version_gate("openclaw 2027.1.0")
    assert gate.version == Version(2027, 1, 0)
    assert gate.degraded is True
    assert "outside the tested range" in gate.message


# --- endpoint + config -----------------------------------------------------

def test_gateway_endpoint_urls(adapter: OpenclawAdapter) -> None:
    ep = adapter.gateway_endpoint(19011)
    assert ep == Endpoint("127.0.0.1", 19011)
    assert ep.ws_url == "ws://127.0.0.1:19011"
    assert ep.http_url == "http://127.0.0.1:19011"


def test_gateway_config_patch(adapter: OpenclawAdapter) -> None:
    patch = adapter.gateway_config_patch(port=19011, token="sekret")
    assert patch == {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": 19011,
            "auth": {"mode": "token", "token": "sekret"},
        }
    }


def test_gateway_config_patch_requires_token(adapter: OpenclawAdapter) -> None:
    with pytest.raises(AdapterError):
        adapter.gateway_config_patch(port=19011, token="")


def test_deep_merge_is_recursive_and_nondestructive() -> None:
    base = {"gateway": {"mode": "remote", "port": 1}, "agents": {"x": 1}}
    patch = {"gateway": {"mode": "local", "auth": {"mode": "token"}}}
    merged = deep_merge(base, patch)
    assert merged["gateway"] == {"mode": "local", "port": 1, "auth": {"mode": "token"}}
    assert merged["agents"] == {"x": 1}
    # base untouched
    assert base["gateway"]["mode"] == "remote"


# --- models ----------------------------------------------------------------

def test_tier_to_model_uses_canonical_openai_ids(adapter: OpenclawAdapter) -> None:
    assert adapter.tier_to_model("fast") == "openai/gpt-5.5"
    assert adapter.tier_to_model("balanced") == "openai/gpt-5.6"
    assert adapter.tier_to_model("power") == "openai/gpt-5.6"
    # unknown tier falls back to a real default, never the legacy openai-codex id
    model = adapter.tier_to_model("nonsense")
    assert model == "openai/gpt-5.6"
    for value in adapter.TIER_MODELS.values():
        assert not value.startswith("openai-codex/")


# --- delivery --------------------------------------------------------------

def test_session_key_is_task_scoped(adapter: OpenclawAdapter) -> None:
    assert adapter.session_key("ops", "t123") == "agent:ops:clawie:t123"


def test_deliver_command_structure(adapter: OpenclawAdapter) -> None:
    task = Task(task_id="t123", message="summarize logs", tier="fast")
    cmd = adapter.deliver_command("ops", task, timeout=90.0)
    assert cmd[:2] == ["openclaw", "agent"]
    assert "--agent" in cmd and cmd[cmd.index("--agent") + 1] == "ops"
    assert cmd[cmd.index("--session-key") + 1] == "agent:ops:clawie:t123"
    assert cmd[cmd.index("--message") + 1] == "summarize logs"
    assert "--json" in cmd
    # timeout coerced to int string
    assert cmd[cmd.index("--timeout") + 1] == "90"
    # fast tier maps to a real model
    assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.5"


def test_deliver_command_respects_custom_bin(adapter: OpenclawAdapter) -> None:
    cmd = adapter.deliver_command(
        "ops", Task("t1", "hi"), timeout=10, openclaw_bin="/opt/openclaw/bin/openclaw"
    )
    assert cmd[0] == "/opt/openclaw/bin/openclaw"


def test_deliver_command_requires_agent(adapter: OpenclawAdapter) -> None:
    with pytest.raises(AdapterError):
        adapter.deliver_command("  ", Task("t1", "hi"), timeout=10)


# --- parse_reply -----------------------------------------------------------

def test_parse_reply_success(adapter: OpenclawAdapter) -> None:
    stdout = (
        '{"payloads":[{"text":"Report ready","mediaUrl":null},{"text":"line two"}],'
        '"meta":{"durationMs":1200,"usage":{"input":100,"output":40}},'
        '"deliveryStatus":{"status":"sent","succeeded":true}}'
    )
    reply = adapter.parse_reply(stdout)
    assert reply.ok is True
    assert reply.output == "Report ready\nline two"
    assert reply.usage == {"input": 100, "output": 40}
    assert reply.delivery_status == "sent"
    assert reply.error == ""


def test_parse_reply_nested_result_delivery(adapter: OpenclawAdapter) -> None:
    stdout = '{"payloads":[{"text":"ok"}],"result":{"deliveryStatus":{"status":"suppressed"}}}'
    reply = adapter.parse_reply(stdout)
    assert reply.ok is True
    assert reply.delivery_status == "suppressed"


def test_parse_reply_empty_is_error(adapter: OpenclawAdapter) -> None:
    reply = adapter.parse_reply("   ")
    assert reply.ok is False
    assert "empty" in reply.error


def test_parse_reply_unparseable_is_error(adapter: OpenclawAdapter) -> None:
    reply = adapter.parse_reply("not json {")
    assert reply.ok is False
    assert "unparseable" in reply.error


def test_parse_reply_explicit_error_field(adapter: OpenclawAdapter) -> None:
    reply = adapter.parse_reply('{"error":"model refused"}')
    assert reply.ok is False
    assert reply.error == "model refused"


def test_parse_reply_in_flight_is_not_ok(adapter: OpenclawAdapter) -> None:
    reply = adapter.parse_reply('{"status":"in_flight","payloads":[]}')
    assert reply.ok is False
    assert reply.error == "in_flight"


# --- auth ------------------------------------------------------------------

def test_auth_login_command_drives_openclaw_cli(adapter: OpenclawAdapter) -> None:
    cmd = adapter.auth_login_command(
        "openai", profile_id="openai:default", method="cli", set_default=True, agent="ops"
    )
    assert cmd == [
        "openclaw", "models", "auth", "login",
        "--provider", "openai",
        "--profile-id", "openai:default",
        "--method", "cli",
        "--set-default",
        "--agent", "ops",
    ]
    # never the legacy id
    assert "openai-codex" not in cmd


def test_auth_login_requires_provider(adapter: OpenclawAdapter) -> None:
    with pytest.raises(AdapterError):
        adapter.auth_login_command("")


def test_paste_token_and_readiness_commands(adapter: OpenclawAdapter) -> None:
    assert adapter.auth_paste_token_command("openrouter") == [
        "openclaw", "models", "auth", "paste-token", "--provider", "openrouter"
    ]
    assert adapter.readiness_command() == ["openclaw", "models", "status", "--json"]
    assert adapter.gateway_status_command() == ["openclaw", "gateway", "status", "--json"]


# --- registry + detect -----------------------------------------------------

def test_registry() -> None:
    assert "openclaw" in adapter_names()
    assert "hermes" in adapter_names()
    assert isinstance(get_adapter("openclaw"), OpenclawAdapter)
    assert isinstance(get_adapter("OpenClaw"), OpenclawAdapter)
    with pytest.raises(AdapterError):
        get_adapter("ghostclaw")


@pytest.mark.parametrize("name", adapter_names())
def test_adapter_contract(name: str) -> None:
    """Every registered adapter must satisfy the same contract (Phase 5)."""
    adapter = get_adapter(name)
    assert isinstance(adapter, ProviderAdapter)
    assert isinstance(adapter.name, str) and adapter.name
    # version policy
    assert adapter.version_command()[-1] == "--version"
    low, high = adapter.supported_range()
    assert isinstance(low, Version) and isinstance(high, Version) and low <= high
    assert isinstance(adapter.version_gate("0.0.0"), VersionGate)
    # endpoint + config
    ep = adapter.gateway_endpoint(12345)
    assert isinstance(ep, Endpoint) and ep.port == 12345
    patch = adapter.gateway_config_patch(port=12345, token="t")
    assert patch["gateway"]["port"] == 12345
    assert patch["gateway"]["auth"] == {"mode": "token", "token": "t"}
    with pytest.raises(AdapterError):
        adapter.gateway_config_patch(port=1, token="")
    # token + models
    assert len(adapter.new_gateway_token()) >= 16
    has_model_map = any(str(model).strip() for model in adapter.TIER_MODELS.values()) or bool(
        str(adapter.DEFAULT_MODEL).strip()
    )
    if has_model_map:
        assert isinstance(adapter.tier_to_model("balanced"), str)
    else:
        with pytest.raises(AdapterError):
            adapter.tier_to_model("balanced")
    # delivery
    if has_model_map:
        cmd = adapter.deliver_command("a", Task("t1", "hi"), timeout=10)
        assert cmd[0] == adapter.binary and "agent" in cmd and "--json" in cmd
    else:
        with pytest.raises(AdapterError):
            adapter.deliver_command("a", Task("t1", "hi"), timeout=10)
    good = adapter.parse_reply('{"payloads":[{"text":"ok"}]}')
    assert isinstance(good, Reply) and good.ok and good.output == "ok"
    assert adapter.parse_reply("").ok is False
    # auth / readiness
    assert adapter.auth_login_command("openai")[0] == adapter.binary
    assert adapter.readiness_command()[0] == adapter.binary


def test_hermes_adapter_fails_closed_until_contract_is_verified() -> None:
    adapter = get_adapter("hermes")

    gate = adapter.version_gate("hermes 0.1.0")

    assert gate.degraded is True
    assert "read-only" in gate.message
    with pytest.raises(AdapterError, match="no verified model mapping"):
        adapter.tier_to_model("balanced")
    with pytest.raises(AdapterError, match="no verified model mapping"):
        adapter.deliver_command("ops", Task("t1", "hi"), timeout=10)


def test_openclaw_satisfies_protocol(adapter: OpenclawAdapter) -> None:
    assert isinstance(adapter, ProviderAdapter)


def test_detect_version_success(adapter: OpenclawAdapter) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        return "openclaw 2026.7.1"

    gate = detect_version(adapter, run)
    assert calls == [["openclaw", "--version"]]
    assert gate.supported is True
    assert gate.version == Version(2026, 7, 1)


def test_detect_version_probe_failure_degrades(adapter: OpenclawAdapter) -> None:
    def run(argv: list[str]) -> str:
        raise FileNotFoundError("openclaw not installed")

    gate = detect_version(adapter, run)
    assert gate.degraded is True
    assert "probe failed" in gate.message


def test_version_gate_dataclass_shape() -> None:
    gate = VersionGate(Version(2026, 6, 2), True, "ok")
    assert gate.version == Version(2026, 6, 2)
    assert gate.supported is True
    assert gate.degraded is False
