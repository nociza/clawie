# Linux Host-Validation Proof (Container)

Date: 2026-06-14

Environment:

- Docker container: `nice_joliot`
- Kernel: `Linux colima 6.8.0-64-generic #67-Ubuntu SMP PREEMPT_DYNAMIC Sun Jun 15 20:23:40 UTC 2025 aarch64 GNU/Linux`
- Effective user: `root` (`uid=0`)
- `/proc`: available
- `python3`: `/usr/local/bin/python3`
- `sudo`: `/usr/bin/sudo`
- `useradd`: `/usr/sbin/useradd`

Purpose:

Exercise `ClawieService.host_validation_report()` on Linux as root with two real
OS users, private homes, private provider-auth files, and cross-user read denial.

Fixture:

- Temporary users: `clawiepfa`, `clawiepfb`
- Temporary config dir: `/tmp/clawie-host-proof-config-*`
- Per-user homes: `/home/clawiepfa`, `/home/clawiepfb`, mode `0700`
- Provider-auth files:
  - `/home/clawiepfa/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`
  - `/home/clawiepfb/.openclaw/agents/main/agent/openclaw-agent.sqlite`, mode `0600`

Result:

```json
{
  "status": "passed",
  "check_count": 11,
  "failures": [],
  "cross_user_passes": [
    "clawiepfa cannot read /home/clawiepfb",
    "clawiepfa cannot read /home/clawiepfb/.openclaw/agents/main/agent/openclaw-agent.sqlite",
    "clawiepfb cannot read /home/clawiepfa",
    "clawiepfb cannot read /home/clawiepfa/.openclaw/agents/main/agent/openclaw-agent.sqlite"
  ]
}
```

Full check list:

```text
pass Found 2 managed agents across 2 Linux users
pass Linux user exists for proof-a: clawiepfa
pass Home directory is private for proof-a: /home/clawiepfa
pass Credential file is private for proof-a: /home/clawiepfa/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass Linux user exists for proof-b: clawiepfb
pass Home directory is private for proof-b: /home/clawiepfb
pass Credential file is private for proof-b: /home/clawiepfb/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawiepfa cannot read /home/clawiepfb
pass clawiepfa cannot read /home/clawiepfb/.openclaw/agents/main/agent/openclaw-agent.sqlite
pass clawiepfb cannot read /home/clawiepfa
pass clawiepfb cannot read /home/clawiepfa/.openclaw/agents/main/agent/openclaw-agent.sqlite
```

Limitations:

This proves the Linux/root host-validation code path and OS permission checks in
an isolated Linux container. It is not a substitute for acceptance on the target
production host with real provisioned agents, and it does not prove systemd
watchdog restart behavior.
