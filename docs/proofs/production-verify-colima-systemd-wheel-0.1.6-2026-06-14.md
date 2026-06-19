# Production Verify Proof (Colima systemd VM, wheel artifact)

Date: 2026-06-14 PDT

Environment:

- Host target: Colima Linux VM
- Kernel: `Linux colima 6.8.0-64-generic #67-Ubuntu SMP PREEMPT_DYNAMIC Sun Jun 15 20:23:40 UTC 2025 aarch64 GNU/Linux`
- Init system: `systemd 255 (255.4-1ubuntu8.10)`
- Effective verifier user: `root` through passwordless `sudo`
- Python: `Python 3.12.3`
- Wheel artifact: `dist/clawie-0.1.6-py3-none-any.whl`
- Wheel SHA256: `b615ec852d9ece0a24edb9217a8bf5286024c5bfb62a5305b4adeb69c008488e`

Purpose:

Exercise
`clawie production verify --exercise-watchdog-restart --all-provider-contracts --json`
on a real Linux/systemd host with two real Linux users, private homes, private
provider-auth files, and the systemd watchdog restart proof. The proof used
`scripts/production_verify_fixture.py`, which runs the built wheel directly on
`PYTHONPATH` through a temporary `clawie` wrapper:

```sh
export PYTHONPATH=/Volumes/Brookline/Projects/Personal/clawie/dist/clawie-0.1.6-py3-none-any.whl
exec /usr/bin/python3 -m clawie "$@"
```

Fixture:

- Temporary state root: `/tmp/clawie-colima-release-proof-016-183317`
- Temporary users: `clawier016183317a`, `clawier016183317b`
- Per-user homes:
  - `/home/clawier016183317a`, mode `0700`
  - `/home/clawier016183317b`, mode `0700`
- Provider-auth files:
  - `/home/clawier016183317a/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
  - `/home/clawier016183317b/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
- Temporary systemd unit: `/etc/systemd/system/clawie-control-watchdog.service`
- Unit `ExecStart`: `/tmp/clawie-wheel-proof-bin-016-183317/clawie --config-dir /tmp/clawie-colima-release-proof-016-183317 clawied run --interval 1`

Pre-flight evidence:

```text
active
enabled
Restart=always
MainPID=438210
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
pass Linux user exists for release-a: clawier016183317a
pass Home directory is private for release-a: /home/clawier016183317a
pass Credential file is private for release-a: /home/clawier016183317a/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass Linux user exists for release-b: clawier016183317b
pass Home directory is private for release-b: /home/clawier016183317b
pass Credential file is private for release-b: /home/clawier016183317b/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier016183317a cannot read /home/clawier016183317b
pass clawier016183317a cannot read /home/clawier016183317b/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier016183317b cannot read /home/clawier016183317a
pass clawier016183317b cannot read /home/clawier016183317a/.openclaw/agents/main/agent/openclaw-agent.sqlite
```

Watchdog restart evidence:

```text
pass unit file exists: /etc/systemd/system/clawie-control-watchdog.service
pass unit has Restart=always
pass unit ExecStart points at this config directory
pass systemd reports watchdog active
pass systemd reports watchdog enabled
pass systemd loaded Restart=always
pass sent SIGTERM to watchdog MainPID 438210
pass systemd restarted the watchdog service before_pid=438210 after_pid=438275 before_restarts=0 after_restarts=1
```

Cleanup:

After the proof, `clawie control watchdog remove` removed the systemd unit. The
temporary proof users, homes, state root, and wrapper were removed. Follow-up
checks confirmed `/etc/systemd/system/clawie-control-watchdog.service`,
`/tmp/clawie-colima-release-proof-016-183317`, and
`/tmp/clawie-wheel-proof-bin-016-183317` no longer existed, and both temporary
users were absent.

Limitations:

This proves the built `0.1.6` wheel artifact on a local Linux/systemd VM with
disposable agents and private credential copies. Repeat the same aggregate
verifier on the actual deployment host before treating a different host as
accepted. Production delegated-task delivery is verified for openclaw; picoclaw,
zeroclaw, and hermes remain gated until their delegated-delivery contracts are
source-pinned.
