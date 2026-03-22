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

No external Python dependencies — stdlib only.

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

## Launch the dashboard

```bash
clawie dashboard
```

This opens a real-time terminal UI showing all your agents, their status, delegation trees, and channels. Press `q` to quit, `v` to switch views.

## Create an isolated runtime

For full Linux-level isolation (requires root):

```bash
sudo clawie runtime create alice --user alice
```

This creates a dedicated Linux user, copies credentials, and installs the provider runtime. See [Runtime Isolation](runtime.md) for details.

## What's next

- [Agent Management](agents.md) — clone agents, manage prompts, configure addons
- [Delegation & Orchestration](delegation.md) — delegate tasks between agents with tiers
- [Providers & Auth](providers.md) — multi-provider setup and shared auth
- [Dashboard](dashboard.md) — TUI navigation and controls
