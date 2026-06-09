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

## Live view

`--watch` redraws the overview on an interval until you press `Ctrl-C`. When the
output is piped or redirected (not a TTY), it prints a single snapshot instead.

```bash
clawie status --watch
clawie status --watch --interval 5
```

By default `status` uses cached metrics (a pure read). Pass `--refresh` (implied
by `--watch`) to sample live CPU/memory once.

## Resilience

`status` is designed to be useful even on a half-broken system: if one section
can't be read (for example, an unconfigured maintenance cron, or a runtime that
can't be probed), that section degrades to an error note and the rest of the
report still renders.

## Migrating from `clawie dashboard`

The old interactive curses dashboard has been retired. `clawie dashboard` is now
a thin, deprecated alias for `clawie status --watch`. All of the data it showed
is available from `clawie status`; the mutating actions it offered (assigning
channels, switching providers, logging in, toggling plugins) are first-class CLI
commands — see the [CLI reference](cli-reference.md).
