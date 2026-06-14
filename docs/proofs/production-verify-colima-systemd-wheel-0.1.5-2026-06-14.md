# Production Verify Proof (Colima systemd VM, wheel artifact)

Date: 2026-06-14

Environment:

- Host target: Colima Linux VM
- Kernel: `Linux colima 6.8.0-64-generic #67-Ubuntu SMP PREEMPT_DYNAMIC Sun Jun 15 20:23:40 UTC 2025 aarch64 GNU/Linux`
- Init system: `systemd 255 (255.4-1ubuntu8.10)`
- Effective verifier user: `root` through passwordless `sudo`
- Python: `Python 3.12.3`
- Wheel artifact: `dist/clawie-0.1.5-py3-none-any.whl`
- Wheel SHA256: `e20648a259d2afb43fa52f7c123df4776f34755f6e3bceeb63227313077c4e31`

Purpose:

Exercise
`clawie production verify --exercise-watchdog-restart --all-provider-contracts --json`
on a real Linux/systemd host with two real Linux users, private homes, private
provider-auth files, and the systemd watchdog restart proof. The proof used a
temporary wrapper that ran the built wheel directly on `PYTHONPATH`:

```sh
export PYTHONPATH=/Volumes/Brookline/Projects/Personal/clawie/dist/clawie-0.1.5-py3-none-any.whl
exec /usr/bin/python3 -m clawie "$@"
```

Fixture:

- Temporary state root: `/tmp/clawie-colima-release-proof-015-152217`
- Temporary users: `clawier152217a`, `clawier152217b`
- Per-user homes:
  - `/home/clawier152217a`, mode `0700`
  - `/home/clawier152217b`, mode `0700`
- Provider-auth files:
  - `/home/clawier152217a/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
  - `/home/clawier152217b/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
- Temporary systemd unit: `/etc/systemd/system/clawie-control-watchdog.service`
- Unit `ExecStart`: `/tmp/clawie-wheel-proof-bin-015-152217/clawie --config-dir /tmp/clawie-colima-release-proof-015-152217 clawied run --interval 1`

Pre-flight evidence:

```text
active
enabled
Restart=always
MainPID=375961
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
pass Linux user exists for release-a: clawier152217a
pass Home directory is private for release-a: /home/clawier152217a
pass Credential file is private for release-a: /home/clawier152217a/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass Linux user exists for release-b: clawier152217b
pass Home directory is private for release-b: /home/clawier152217b
pass Credential file is private for release-b: /home/clawier152217b/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier152217a cannot read /home/clawier152217b
pass clawier152217a cannot read /home/clawier152217b/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawier152217b cannot read /home/clawier152217a
pass clawier152217b cannot read /home/clawier152217a/.openclaw/agents/main/agent/openclaw-agent.sqlite
```

Watchdog restart evidence:

```text
pass unit file exists: /etc/systemd/system/clawie-control-watchdog.service
pass unit has Restart=always
pass unit ExecStart points at this config directory
pass systemd reports watchdog active
pass systemd reports watchdog enabled
pass systemd loaded Restart=always
pass sent SIGTERM to watchdog MainPID 375961
pass systemd restarted the watchdog service before_pid=375961 after_pid=376039 before_restarts=0 after_restarts=1
```

Cleanup:

After the proof, `clawie control watchdog remove` removed the systemd unit. The
temporary proof users, homes, state root, and wrapper were removed, and
`/etc/systemd/system/clawie-control-watchdog.service` no longer existed.

Limitations:

This proves the built `0.1.5` wheel artifact on a local Linux/systemd VM with
disposable agents and private credential copies. Repeat the same aggregate
verifier on the actual deployment host before treating a different host as
accepted.
