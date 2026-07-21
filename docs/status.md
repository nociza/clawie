# Status

`clawie status` is the single front door for "what's going on across my fleet?"
It is read-only, never requires root, and aggregates every status surface into
one view.

```bash
clawie status
```

## Sections

A full `clawie status` prints these sections, in order:

| Section | What it shows |
|---------|---------------|
| **Setup** | provider, auth mode, workspace, subscription, runtime-installed |
| **Health** | overall health plus the individual `doctor` checks |
| **Agents** | per-agent status, provider, channels, CPU/mem, version (+ provider issues) |
| **Runtimes** | locally-installed provider runtimes and their service/auth state |
| **Auth** | shared provider auth status and whether a login is required |
| **Delegation** | active REPL agents and recent delegation tasks |
| **Maintenance** | the credential-sync cron's enabled state and interval |
| **Backup** | backup repo, remote, HEAD commit, and last run |
| **Events** | the most recent recorded events |

Limit the output to a single section by naming it:

```bash
clawie status agents
clawie status delegation
```

Focus on one agent:

```bash
clawie status --agent alice
```

## JSON output

For scripting and automation, emit the whole snapshot as JSON:

```bash
clawie status --json
clawie status delegation --json
```

The command exits nonzero if the embedded health status is unhealthy or a
state-integrity error is detected. Degraded health and unrelated section probe
errors remain visible but do not make the entire snapshot unusable. Use
`clawie health` when only the health report is needed.

## Live view

`--watch` redraws the overview on an interval until you press `Ctrl-C`. When the
output is piped or redirected (not a TTY), it prints a single snapshot instead.

```bash
clawie status --watch
clawie status --watch --interval 5
```

By default `status` uses cached metrics (a pure read). Pass `--refresh` (implied
by `--watch`) to sample live CPU/memory once. Runtime metrics use `ps` when
available, prefer existing Linux cgroup memory accounting when the host exposes
it, and fall back to `/proc` RSS/memory data.

Pure-read mode does not create the state root or database, run migrations,
repair modes, or persist provider drift observed from live processes. The live
value is shown in the report while desired configuration remains unchanged.

## Resilience

`status` is designed to be useful even on a half-broken system: if one section
can't be read (for example, an unconfigured maintenance cron, or a runtime that
can't be probed), that section degrades to an error note and the rest of the
report still renders. Existing unsafe database paths, including symlinked state,
are surfaced as section errors without modifying the filesystem.

## Migrating from `clawie dashboard`

The old interactive curses dashboard has been retired. `clawie dashboard` is now
a thin, deprecated alias for `clawie status --watch`. All of the data it showed
is available from `clawie status`; the mutating actions it offered (assigning
channels, switching providers, logging in, toggling plugins) are first-class CLI
commands — see the [CLI reference](cli-reference.md).
