# Getting Started

## Prerequisites

- **Linux** (Debian/Ubuntu recommended) — clawie uses Linux users, systemd, and Unix domain sockets. Not compatible with macOS or Windows.
- **Python 3.10+**
- **Root/sudo** for runtime isolation (optional — agent creation and the dashboard work without it)

See [Requirements & Limitations](requirements.md) for full details.

## Install

```bash
# From PyPI
uv tool install clawie

# From source
uv tool install -e .
```

Python 3.10 installs `tomli`; Python 3.11+ uses only the standard library.

## Configure

Set your provider and workspace:

```bash
clawie config set --provider picoclaw --subscription pro --workspace production
```

Or run the interactive setup:

```bash
clawie config set --interactive
```

View current config:

```bash
clawie config show
```

## Install a provider runtime

```bash
clawie runtime install picoclaw
```

Supported providers: `openclaw`, `picoclaw`, `zeroclaw`.

## Create your first agent

```bash
clawie agent create alice --template baseline
```

This creates an agent named `alice` with the default baseline template, delegation enabled, and balanced model tier.

Options:

```bash
clawie agent create alice \
  --display-name "Alice" \
  --template baseline \
  --channel-strategy new \
  --model-tier power \
  --provider picoclaw
```

## Check fleet status

```bash
clawie status
```

This prints a read-only overview of all your agents — status, runtimes, auth, delegation, and health. Add `--json` for scripting, or `--watch` for a live view.

## Create an isolated runtime

For full Linux-level isolation (requires root):

```bash
sudo clawie runtime create alice --user alice
```

This creates a dedicated Linux user, copies credentials, and installs the provider runtime. See [Runtime Isolation](runtime.md) for details.

## Set up continuous backup

Keep your agents' knowledge (prompts, memory, workspace notes) in a git repo
that clawie maintains automatically:

```bash
clawie backup init --remote git@github.com:you/agent-backup.git
clawie backup run                 # first snapshot
sudo clawie maintenance enable    # keep it current on every maintenance pass
```

Credentials are never written to the backup repo. See [Backup & Restore](backup.md).

## What's next

- [Agent Management](agents.md) — clone agents, manage prompts, configure addons
- [Delegation & Orchestration](delegation.md) — delegate tasks between agents with tiers
- [Providers & Auth](providers.md) — multi-provider setup, shared auth, porting between claws
- [Backup & Restore](backup.md) — git-backed knowledge backup
- [Status](status.md) — fleet overview, `--json`, live `--watch`
