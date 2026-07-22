# Production Verify Proof (0.1.9 wheel, nw2-clawies)

Date: 2026-07-21 PDT

## Accepted artifact

- Artifact: `dist/clawie-0.1.9-py3-none-any.whl`
- Installed version: `clawie 0.1.9`
- SHA-256: `a2ab9cd786d6e500691519d42b749863ac37846ad32444c4e14e1c035e6d8591`
- Reproducible-build epoch: `SOURCE_DATE_EPOCH=1784592000`
- Classification: `Development Status :: 5 - Production/Stable`
- Verified delivery runtime: OpenClaw `2026.7.1`
- Verified model identifier: `openai/gpt-5.6-sol`

The fixture executed the wheel directly rather than the source checkout. Its
temporary root-owned wrapper exported the wheel as `PYTHONPATH` and ran
`/usr/bin/python3 -m clawie`.

## Host

- Target: `nw2-clawies` over Tailscale SSH
- Operating system: Debian GNU/Linux 13 (trixie), x86-64 KVM guest
- Kernel: Linux `6.12.95+deb13-amd64`
- Init: systemd `257.13`
- Python: `3.13.5`
- Node: `22.22.3`
- Package manager: pnpm `11.15.1`
- Effective verifier: root through passwordless `sudo`
- Auth input: real Codex-linked auth copied privately from `/home/admin`; no
  credential values are included in this record

The pinned Node and pnpm prerequisites were checksum-verified or installed from
the signed npm registry metadata into root-owned, non-group/world-writable
paths. The fixture installed OpenClaw through Clawie's public runtime command.

The fixture also required OpenClaw's live CLI status—not file presence alone—to
confirm the imported Codex-linked session. The sanitized evidence reported
`auth_status: ready`, `auth_profile: openai:default`, `account_present: true`,
`source: cli`, and `login_required: false`. The account identifier and all token
values were excluded from the proof.

## Compatibility gate

The complete local release suite passed with coverage above the configured 71%
floor. Ruff, mypy, Bandit's medium/high gate, lock validation, dependency audit,
wheel build, and wheel/sdist smoke tests also passed.

The Python 3.10, 3.11, 3.12, 3.13, and 3.14 test matrix passed with 638 tests
on every interpreter. The Python lock contained no known vulnerabilities or
adverse project statuses. A resolver-specific review of the deployed pnpm tree
confirmed `fast-uri 3.1.4`, `hono 4.12.31`, and `protobufjs 7.6.5`; those are
newer than the vulnerable versions reported by an independently resolved npm
audit graph. The remaining `@hono/node-server 1.19.14` advisory concerns a
Windows path separator, outside Clawie's Linux-only platform. The actual pnpm
tree—not a separately resolved npm tree—is the artifact relevant to this host.

## Journey

The public fixture was run from a root-owned `0700` input directory after the
wheel hash was rechecked on the host:

```bash
sudo env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  python3 production_verify_fixture.py \
  --wheel clawie-0.1.9-py3-none-any.whl \
  --version 0.1.9 \
  --source-home /home/admin \
  --auth-source codex
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
7. Removed the watchdog, purged both agents and users, and deleted all temporary
   state and wrapper paths.

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
  "default_model": "openai/gpt-5.6-sol",
  "delivery_model": "openai/gpt-5.6-sol",
  "readiness_json": true,
  "delivery_challenge_verified": true,
  "transport": "gateway"
}
```

Live delivery returned the unique challenge through the gateway. Embedded
fallback or any fallback marker would have failed the proof.

## Isolation and supervision evidence

- Two real Linux users were found and their home directories were private.
- Both `.codex/auth.json` and OpenClaw's native
  `agents/main/agent/openclaw-agent.sqlite` were private in each home.
- Each agent user was unable to read the other agent's home or credential
  files.
- The watchdog unit was active and enabled with `Restart=always`.
- The verifier sent `SIGTERM` to watchdog PID `7745`; systemd restarted it as
  PID `7902`, with `NRestarts` increasing from 0 to 1.
- The OpenClaw adapter used the isolated runtime's internal `main` agent and
  parsed the source-pinned nested gateway response envelope.

## Cleanup and postflight evidence

Cleanup returned `ok: true`:

- `clawier019gojc13wa` absent
- `clawier019gojc13wb` absent
- watchdog and alert units absent
- `/tmp/clawie-release-proof-019-gojc_13w` absent
- `/root/clawie-wheel-proof-bin-019-j39rdp2x` absent
- no failed systemd units

No fixture user, home, watchdog unit, state root, or wrapper was retained. The
root-owned OpenClaw `2026.7.1` toolchain intentionally remains installed at
`/var/lib/clawie/toolchain` for subsequent production use; an independent
postflight check found no non-symlink path owned outside root or writable by
group/other.

## Scope

This accepts the exact wheel hash above on `nw2-clawies` for the source-pinned
OpenClaw delivery surface. It does not certify a different artifact or
deployment host. Each production host must run the same aggregate verifier.
picoclaw and zeroclaw remain outside the verified delegated delivery surface
until their contracts are source-pinned and exercised.
