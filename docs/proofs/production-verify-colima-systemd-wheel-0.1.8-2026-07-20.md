# Production Verify Proof (0.1.8 wheel, Colima systemd VM)

Date: 2026-07-20 PDT

## Accepted artifact

- Artifact: `dist/clawie-0.1.8-py3-none-any.whl`
- Installed version: `clawie 0.1.8`
- SHA-256: `49068363669459f782fe19c6524b554f423dbf63c420a78f4aeae60b182106de`
- Classification: `Development Status :: 5 - Production/Stable`
- Verified delivery runtime: OpenClaw `2026.7.1`
- Verified model identifier: `openai/gpt-5.5`

The fixture executed the wheel directly rather than the source checkout. Its
temporary wrapper exported the wheel as `PYTHONPATH` and ran
`/usr/bin/python3 -m clawie`.

## Host

- Target: disposable Colima Linux VM
- Kernel: Linux 6.8, aarch64
- Init: systemd 255
- Python: 3.12
- Node: 22.22.3
- Package manager: pnpm 10.17.1
- Effective verifier: root through passwordless `sudo`
- Auth input: real Codex-linked auth copied privately into the fixture; no
  credential values are included in this record

## Journey

The public fixture command was:

```bash
sudo python3 scripts/production_verify_fixture.py \
  --wheel dist/clawie-0.1.8-py3-none-any.whl \
  --version 0.1.8 \
  --source-home /Users/nociza \
  --auth-source codex \
  --interval 1
```

It performed this sequence without synthetic state rows:

1. Initialized a fresh state root and installed pinned OpenClaw through the
   wheel's public runtime command.
2. Imported real linked auth through the public auth command.
3. Created two isolated agents and Linux users with private homes and private
   per-user auth stores.
4. Started both gateways through their exact users' systemd user managers.
5. Installed and started the root-owned clawied watchdog.
6. Ran the aggregate verifier with watchdog restart, live runtime delivery, and
   all verified provider contracts enabled.
7. Removed the watchdog, purged both agents and users, and deleted all
   temporary state and wrapper paths.

## Aggregate result

```json
{
  "status": "passed",
  "exercise_runtime_delivery": true,
  "exercise_watchdog_restart": true,
  "all_provider_contracts": true,
  "checks": [
    {"name": "doctor", "status": "pass"},
    {"name": "host_validation", "status": "pass"},
    {"name": "watchdog", "status": "pass"},
    {"name": "watchdog_restart_exercise", "status": "pass"},
    {"name": "runtime_adapter_openclaw", "status": "pass"}
  ]
}
```

The runtime contract evidence reported:

```json
{
  "adapter": "openclaw",
  "agent_id": "release-a",
  "contract_verified": true,
  "runtime_version": "2026.7.1",
  "supported_range": ["2026.7.1", "2026.7.2"],
  "default_model": "openai/gpt-5.5",
  "readiness_json": true,
  "delivery_challenge_verified": true,
  "transport": "gateway"
}
```

Live delivery returned the unique nonce through the gateway. Embedded fallback
or any fallback marker would have failed the proof.

## Isolation and supervision evidence

- Two real Linux users were found and their home directories were private.
- Both `.codex/auth.json` and OpenClaw's native
  `agents/main/agent/openclaw-agent.sqlite` were private in each home.
- Each agent user was unable to read the other agent's home or credential
  files.
- The watchdog unit was active and enabled with `Restart=always`.
- The verifier sent `SIGTERM` to watchdog PID `2900817`; systemd restarted it
  as PID `2901008`, with `NRestarts` increasing from 0 to 1.
- The OpenClaw adapter used the isolated runtime's internal `main` agent and
  parsed the source-pinned nested gateway response envelope.

## Cleanup evidence

Cleanup returned `ok: true`:

- `clawier018x3dg8v5fa` absent
- `clawier018x3dg8v5fb` absent
- watchdog unit absent
- `/tmp/clawie-release-proof-018-x3dg8v5f` absent
- `/root/clawie-wheel-proof-bin-018-xax2jzi1` absent

No fixture user, home, watchdog unit, state root, or wrapper was retained.

## Scope

This accepts the exact wheel hash above on the recorded Linux/systemd fixture
for the source-pinned OpenClaw delivery surface. It does not certify a different
artifact or deployment host. Each production host must run the same aggregate
verifier. picoclaw, zeroclaw, and hermes remain outside the verified delegated
delivery surface until their contracts are source-pinned and exercised.
