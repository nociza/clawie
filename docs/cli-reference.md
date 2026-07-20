# CLI Reference

All commands use `clawie` as the entry point. Use `--config-dir` to override the state directory.
Use `--version` to print the installed package version.
Root-required commands keep the same state root under normal `sudo` by using
`SUDO_USER`; for service accounts or custom deployments, set `CLAWIE_HOME` or
pass the same `--config-dir` to every command.

## status

The single read-only overview of the whole fleet — setup, health, agents,
runtimes, auth, delegation, maintenance, and recent events.

```bash
clawie status                 # full overview
clawie status agents          # one section: setup|health|agents|runtimes|auth|delegation|maintenance|backup|events
clawie status --agent alice   # focus a single agent
clawie status --json          # machine-readable snapshot (for scripting)
clawie status --watch         # live view; refresh until Ctrl-C
clawie status --watch --interval 5
clawie status --refresh       # sample live CPU/memory once
```

`status` is read-only and never requires root. A section that can't be read
(e.g. an unconfigured maintenance cron) degrades to an error note instead of
blanking the whole report. Fatal state-store safety refusals, such as a
non-clawie `--config-dir` that would need permission changes, render the JSON
error details and exit nonzero.

## config

```bash
clawie config show
clawie config set [--provider P] [--auth-mode M] [--api-key K] [--workspace W] [--subscription S] [--interactive]
clawie config set --control-github-repo OWNER/REPO --control-github-token-path PATH [--control-operator HANDLE] [--control-issue-label LABEL] [--control-github-rate-limit-seconds N]
```

## agent

```bash
clawie agent create [AGENT_ID] [--display-name N] [--template T] [--clone-from A] [--channel-strategy new|migrate] [--model-tier fast|balanced|power] [--provider P] [--no-delegation]
clawie agent clone SOURCE TARGET [--display-name N] [--channel-strategy new|migrate] [--model-tier fast|balanced|power] [--provider P] [--no-delegation]
clawie agent list
clawie agent show AGENT_ID
clawie agent delete AGENT_ID
clawie agent purge AGENT_ID
clawie agent create-batch FILE
```

### agent prompt

```bash
clawie agent prompt copy SOURCE TARGET [--no-apply-to-disk]
```

### agent auth

```bash
clawie agent auth show AGENT_ID
clawie agent auth login AGENT_ID
```

### agent provider

```bash
clawie agent provider set AGENT_ID PROVIDER
```

### agent service

```bash
clawie agent service start|stop|restart|status AGENT_ID
clawie agent service apply-prompts AGENT_ID
```

### agent credentials

```bash
clawie agent credentials list
clawie agent credentials show AGENT_ID
clawie agent credentials set AGENT_ID BUNDLE [--include-defaults]
clawie agent credentials sync AGENT_ID [BUNDLE] [--source-home PATH]
clawie agent credentials revoke AGENT_ID BUNDLE
```

### agent addon

```bash
clawie agent addon show AGENT_ID
clawie agent addon enable AGENT_ID ADDON [--login-if-missing]
clawie agent addon disable AGENT_ID ADDON
clawie agent addon apply AGENT_ID ADDON
```

## channel

```bash
clawie channel apply AGENT_ID --preset minimal|growth|enterprise
clawie channel move SOURCE_AGENT TARGET_AGENT
```

## delegation

```bash
clawie delegation submit --parent P --child C [--payload JSON] [--timeout S] [--tier fast|balanced|power]
clawie delegation deliver --agent A --message TEXT [--timeout S] [--tier fast|balanced|power] [--json]
clawie delegation repl --agent-id ID --executor-agent MANAGED_ID [--tier fast|balanced|power]
clawie delegation tree --agent-id ID
clawie delegation tasks [--agent-id ID] [--status S] [--limit N]
clawie delegation status
clawie delegation cleanup
clawie delegation spawn-session --parent P --child C [--timeout S] [--tier fast|balanced|power]
clawie delegation stop-session --parent P --child C
clawie delegation session-agents --parent P
```

## workspace

Published artifacts are copied from a private agent workspace into clawie's
shared published workspace. Authorized viewers receive generated, disposable
materialized projections at `~/.openclaw/workspace/published`; the immutable
canonical copy remains in the manager-private store.

```bash
clawie workspace status
clawie workspace publish PATH [--agent A] [--to B[,C]] [--title T] [--json]
clawie workspace list [--agent A] [--publisher P] [--json]
clawie workspace show PUB_ID [--agent A] [--json]
clawie workspace mount [--agent A|--all]
clawie workspace verify [PUB_ID] [--json]
```

## runtime

```bash
clawie runtime install PROVIDER
clawie runtime create [AGENT_ID] [--user USER] [--source-home PATH] [--skip-config-copy] [--credential-bundle B] [--no-default-credentials] [--password P] [--password-hash H] [--no-global-password] [--from-agent A] [--provider P] [--no-delegation]
clawie runtime detect
clawie runtime status
clawie runtime version
clawie runtime login PROVIDER
clawie runtime service start|stop|restart|status PROVIDER
```

Credential bundles default to empty. Pass `--credential-bundle provider-auth`
only when the runtime should reuse shared provider auth.

## auth

```bash
clawie auth show
clawie auth login PROVIDER
clawie auth import PROVIDER [--from codex|claude|provider] [--source-home PATH]
clawie auth port --from PROVIDER --to PROVIDER   # port sessions between claws
clawie auth apply [AGENT_ID]
```

## addon

```bash
clawie addon list
clawie addon show ADDON
clawie addon install ADDON
clawie addon auth show ADDON
clawie addon auth login ADDON
clawie addon auth import ADDON [--source-home PATH] [--from-agent A]
```

## maintenance

```bash
clawie maintenance status
clawie maintenance enable [--interval N]   # install the maintenance cron (root)
clawie maintenance disable                 # remove the cron (root)
clawie maintenance run                     # sync credentials, write prompts, run backup
```

Each pass refreshes shared auth, syncs private credential copies into agent
homes, writes configured prompts directly when it has permission, and — when
backup is enabled — commits knowledge changes to the backup repo.

## clawied

```bash
clawie clawied reconcile [--manifest PATH|--agent ID] [--dry-run] [--json]
clawie clawied run [--once] [--interval SECONDS] [--dry-run] [--json]
clawie clawied status [--json]
clawie clawied stop [--json]
```

`clawied run` is a foreground reconcile loop intended for a process supervisor.
It writes `clawied.pid`, `clawied.lock`, `clawied-status.json`, and a local Unix
command socket. `clawied reconcile`, `clawied run --once`, `clawied status`, and
`clawied stop` use that socket when the daemon is running and fall back to local
execution when it is not. Mutating CLI service operations, including setup,
shared auth, addon/runtime install and auth, agent lifecycle, credential,
provider, prompt, service, channel, backup/import, delegation, and maintenance
commands, also route through `clawied` when it is running, using a whitelisted
service RPC surface. `clawied` also hosts the control-tool RPC used by the
control agent: read/safe-heal verbs execute autonomously, destructive and
outward verbs return a nonce and execute only after matching confirmation.
Confirmed `open_issue` creates a GitHub issue using the configured private token
file, with local dedupe and rate limiting. Confirmed `open_pr` creates a draft
GitHub pull request from an existing branch using the same private token and
local guardrails; it does not push branches or merge changes.

## control

```bash
clawie control request VERB [--args-json JSON] [--json]
clawie control confirm VERB --nonce NONCE [--args-json JSON] [--json]
clawie control watchdog install [--interval SECONDS] [--notify-command CMD] [--no-start]
clawie control watchdog status
sudo clawie control watchdog verify [--exercise-restart] [--timeout SECONDS] [--json]
clawie control watchdog remove
```

`control request` and `control confirm` are thin daemon clients for the
capability-gated control RPC. Isolated control workspaces receive a request-only
socket; confirmation is accepted only on the manager socket and the confirmer
is derived from the Unix peer's OS username or `uid:<number>`. Empty operator
allowlists fail closed. Read and safe-heal verbs execute immediately;
destructive and outward verbs return a nonce and only execute after `confirm`
echoes the same verb and args from an allowlisted local principal.

The watchdog commands manage a root-owned systemd unit that runs
`clawie clawied run` with `Restart=always`. `--notify-command` writes a separate
systemd `OnFailure` alert unit for an out-of-band notification command.
`control watchdog install --no-start` writes the unit files without enabling or
starting the service; `watchdog status` still reports the unit-file presence
separately from systemd enabled/active state.
`watchdog verify --exercise-restart --json` is the target-host proof command:
it checks the loaded unit and, when requested, kills the watchdog process and
waits for systemd to restart it.

## dashboard (deprecated)

```bash
clawie dashboard [AGENT_ID] [--refresh N]
```

Deprecated alias for `clawie status --watch` (a single snapshot when output is
not a TTY). Use `clawie status` instead.

## health & events

```bash
clawie health
sudo clawie health --host-validate --json
sudo clawie production verify [--exercise-watchdog-restart] [--exercise-runtime-delivery] [--watchdog-timeout SECONDS] [--all-provider-contracts] [--json]
clawie event list [--limit N]
```

`health --host-validate` is the production host-isolation proof. It requires
Linux, root, and at least two managed Linux-user agents; it exits nonzero unless
the cross-user checks pass.
`production verify` aggregates the release proof gates: standard health,
target-host host validation, target-host watchdog verification, source-pinned
runtime checks, and live gateway challenge delivery. By default, checks cover
the configured provider and any providers already assigned to agents; add
`--all-provider-contracts` for package release acceptance across every verified
production delivery provider. A production pass requires both
`--exercise-watchdog-restart` and `--exercise-runtime-delivery`; without them
the command exits nonzero. The older wheel has a historical Colima
Linux/systemd proof recorded in
[`docs/proofs/production-verify-colima-systemd-wheel-0.1.7-2026-06-19.md`](proofs/production-verify-colima-systemd-wheel-0.1.7-2026-06-19.md);
that predates mandatory live delivery and does not accept the current tree or a
different host.

## backup

Git-backed knowledge backup (see [Backup & Restore](backup.md)):

```bash
clawie backup init [PATH] [--remote URL] [--no-auto] [--auto-push|--no-auto-push]
clawie backup run [--message M] [--push|--no-push]
clawie backup status
clawie backup restore [--agent ID] [--no-workspace] [--no-apply-to-disk]
```

Full-fidelity local snapshots (credentials included; file is chmod `0600`):

```bash
clawie backup export PATH
clawie backup import PATH [--merge]
```

## Global flags

| Flag | Description |
|------|-------------|
| `--config-dir` | Override state directory (default: `~/.clawie`) |
| `--version` | Show the installed clawie version and exit |
