# Production Verify Proof (Colima systemd VM, wheel artifact)

> Historical evidence only. This run predates the mandatory
> `--exercise-runtime-delivery` gateway challenge, the OpenClaw 2026.7.1 pin,
> and the current hardening changes. It does not accept the current source tree
> or any deployment host.

Date: 2026-06-19 PDT

Environment:

- Host target: Colima Linux VM
- Kernel: `Linux colima 6.8.0-64-generic #67-Ubuntu SMP PREEMPT_DYNAMIC Sun Jun 15 20:23:40 UTC 2025 aarch64 GNU/Linux`
- Init system: `systemd 255 (255.4-1ubuntu8.10)`
- Effective verifier user: `root` through passwordless `sudo`
- Python: `Python 3.12.3`
- Wheel artifact: `dist/clawie-0.1.7-py3-none-any.whl`
- Wheel SHA256: `ec7a8eb1c8a441617e249ec72cc42869978f68e68d12595ecfd09ab5bd6b2a6e`

Purpose:

Exercise
`clawie production verify --exercise-watchdog-restart --all-provider-contracts --json`
on a real Linux/systemd host with two real Linux users, private homes, private
provider-auth files, and the systemd watchdog restart proof. The proof used
`scripts/production_verify_fixture.py`, which runs the built wheel directly on
`PYTHONPATH` through a temporary `clawie` wrapper:

```sh
export PYTHONPATH=/Volumes/Brookline/Projects/Personal/clawie/dist/clawie-0.1.7-py3-none-any.whl
exec /usr/bin/python3 -m clawie "$@"
```

Fixture:

- Temporary state root: `/tmp/clawie-colima-release-proof-017-125641`
- Temporary users: `clawier017125641a`, `clawier017125641b`
- Per-user homes:
  - `/home/clawier017125641a`, mode `0700`
  - `/home/clawier017125641b`, mode `0700`
- Provider-auth files:
  - `/home/clawier017125641a/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
  - `/home/clawier017125641b/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
- Temporary systemd unit: `/etc/systemd/system/clawie-control-watchdog.service`
- Unit `ExecStart`: `/tmp/clawie-wheel-proof-bin-017-125641/clawie --config-dir /tmp/clawie-colima-release-proof-017-125641 clawied run --interval 1`

Pre-flight evidence:

```text
active
enabled
Restart=always
MainPID=2428736
NRestarts=0
ActiveState=active
SubState=running
```

Production verifier result:

```json
{
  "status": "passed",
  "exercise_watchdog_restart": true,
  "all_provider_contracts": true,
  "checks": [
    {
      "name": "doctor",
      "status": "pass",
      "message": "standard health checks are healthy"
    },
    {
      "name": "host_validation",
      "status": "pass",
      "message": "Linux/root host isolation proof passed"
    },
    {
      "name": "watchdog",
      "status": "pass",
      "message": "systemd watchdog proof passed"
    },
    {
      "name": "watchdog_restart_exercise",
      "status": "pass",
      "message": "systemd watchdog restart was exercised"
    },
    {
      "name": "runtime_adapter_openclaw",
      "status": "pass",
      "message": "Provider openclaw adapter contract is verified"
    }
  ]
}
```

Host-validation evidence:

```text
pass Found 2 managed agents across 2 Linux users
pass Linux user exists for release-a: clawier017125641a
pass Home directory is private for release-a: /home/clawier017125641a
pass Credential file is private for release-a: /home/clawier017125641a/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass Linux user exists for release-b: clawier017125641b
pass Home directory is private for release-b: /home/clawier017125641b
pass Credential file is private for release-b: /home/clawier017125641b/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier017125641a cannot read /home/clawier017125641b
pass clawier017125641a cannot read /home/clawier017125641b/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier017125641b cannot read /home/clawier017125641a
pass clawier017125641b cannot read /home/clawier017125641a/.openclaw/agents/main/agent/openclaw-agent.sqlite
```

Watchdog restart evidence:

```text
pass unit file exists: /etc/systemd/system/clawie-control-watchdog.service
pass unit has Restart=always
pass unit ExecStart points at this config directory
pass systemd reports watchdog active
pass systemd reports watchdog enabled
pass systemd loaded Restart=always
pass sent SIGTERM to watchdog MainPID 2428736
pass systemd restarted the watchdog service before_pid=2428736 after_pid=2428801 before_restarts=0 after_restarts=1
```

Cleanup:

After the proof, `clawie control watchdog remove` removed the systemd unit. The
temporary proof users, homes, state root, and wrapper were removed. Follow-up
checks confirmed `/etc/systemd/system/clawie-control-watchdog.service`,
`/tmp/clawie-colima-release-proof-017-125641`, and
`/tmp/clawie-wheel-proof-bin-017-125641` no longer existed, and both temporary
users were absent.

Limitations:

This proves the built `0.1.7` wheel artifact on a local Linux/systemd VM with
disposable agents and private credential copies. Repeat the same aggregate
verifier on the actual deployment host before treating a different host as
accepted. Production delegated-task delivery is verified for openclaw; picoclaw,
zeroclaw, and hermes remain gated until their delegated-delivery contracts are
source-pinned.
