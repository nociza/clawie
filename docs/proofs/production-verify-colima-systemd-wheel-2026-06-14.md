# Production Verify Proof (Colima systemd VM, wheel artifact)

Date: 2026-06-14

Environment:

- Host target: Colima Linux VM
- Kernel: `Linux colima 6.8.0-64-generic #67-Ubuntu SMP PREEMPT_DYNAMIC Sun Jun 15 20:23:40 UTC 2025 aarch64 GNU/Linux`
- Init system: `systemd`
- Effective verifier user: `root` through passwordless `sudo`
- Python: `/usr/bin/python3`
- Wheel artifact: `dist/clawie-0.1.4-py3-none-any.whl`

Purpose:

Exercise
`clawie production verify --exercise-watchdog-restart --all-provider-contracts --json`
on a real Linux/systemd host with two real Linux users, private homes, private
provider-auth files, and the systemd watchdog restart proof. Colima did not have
`ensurepip`, so the proof used a temporary wrapper that ran the built wheel
directly on `PYTHONPATH`:

```sh
export PYTHONPATH=/Volumes/Brookline/Projects/Personal/clawie/dist/clawie-0.1.4-py3-none-any.whl
exec /usr/bin/python3 -m clawie "$@"
```

Fixture:

- Temporary state root: `/tmp/clawie-colima-release-proof-4151bd`
- Temporary users: `clawier4151bda`, `clawier4151bdb`
- Per-user homes:
  - `/home/clawier4151bda`, mode `0700`
  - `/home/clawier4151bdb`, mode `0700`
- Provider-auth files:
  - `/home/clawier4151bda/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
  - `/home/clawier4151bdb/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
- Temporary systemd unit: `/etc/systemd/system/clawie-control-watchdog.service`
- Unit `ExecStart`: `/tmp/clawie-wheel-proof-bin/clawie --config-dir /tmp/clawie-colima-release-proof-4151bd clawied run --interval 1`

Pre-flight evidence:

```text
active
enabled
Restart=always
MainPID=182148
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
pass Linux user exists for release-a: clawier4151bda
pass Home directory is private for release-a: /home/clawier4151bda
pass Credential file is private for release-a: /home/clawier4151bda/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass Linux user exists for release-b: clawier4151bdb
pass Home directory is private for release-b: /home/clawier4151bdb
pass Credential file is private for release-b: /home/clawier4151bdb/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier4151bda cannot read /home/clawier4151bdb
pass clawier4151bda cannot read /home/clawier4151bdb/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier4151bdb cannot read /home/clawier4151bda
pass clawier4151bdb cannot read /home/clawier4151bda/.openclaw/agents/main/agent/openclaw-agent.sqlite
```

Watchdog restart evidence:

```text
pass unit file exists: /etc/systemd/system/clawie-control-watchdog.service
pass unit has Restart=always
pass unit ExecStart points at this config directory
pass systemd reports watchdog active
pass systemd reports watchdog enabled
pass systemd loaded Restart=always
pass sent SIGTERM to watchdog MainPID 182148
pass systemd restarted the watchdog service before_pid=182148 after_pid=182276 before_restarts=0 after_restarts=1
```

Cleanup:

After the proof, `clawie control watchdog remove` removed the systemd unit. The
temporary proof users, homes, state root, and wrapper were removed, and
`/etc/systemd/system/clawie-control-watchdog.service` no longer existed.

Limitations:

This proves the built wheel artifact on a local Linux/systemd VM with disposable
agents and private credential copies. Repeat the same aggregate verifier on the
actual deployment host before treating a different host as accepted.
