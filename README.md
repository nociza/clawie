# clawie

<img width="2466" height="1536" alt="clawie" src="https://github.com/user-attachments/assets/e458bb38-b506-4fd6-a43e-53cd49649592" />

A local control plane for provisioning, orchestrating, and monitoring a fleet of AI agents from one CLI.

## Why clawie

You have multiple agents across providers. Each needs its own config, credentials, channels, and runtime. clawie gives you one place to manage all of it — create agents, delegate tasks between them, isolate their environments, and monitor everything from a terminal dashboard.

## Core capabilities

**Agent orchestration** — Delegate tasks across agents in recursive trees with automatic tier-based routing. Fast agents handle lookups, power agents handle analysis, balanced agents handle everything else.

**Multi-provider fleet** — Run agents on openclaw, picoclaw, or zeroclaw. Switch providers with a single command. Authorize once, share credentials across agents.

**Linux isolation** — Each agent gets its own Linux user, home directory, and credential scope. No agent sees another's secrets.

**Terminal dashboard** — Real-time TUI showing agent status, delegation trees, channels, and health across your entire fleet.

## Install

```bash
uv tool install clawie        # from PyPI
uv tool install -e .          # from source
```

## Quick start

```bash
clawie config set --provider picoclaw --subscription pro
clawie agent create alice --template baseline
clawie dashboard
```

## Agent orchestration

Agents delegate work to each other through a recursive task system with three model tiers:

| Tier | Budget | Use for |
|------|--------|---------|
| **fast** ⚡ | 4K tokens | Status checks, lookups, validation |
| **balanced** ⚖ | 16K tokens | Most tasks (default) |
| **power** ⭐ | 64K tokens | Architecture, deep analysis, refactoring |

```bash
# Delegate with a tier
clawie delegation submit --parent planner --child worker --tier fast --payload '{"task":"check"}'

# Spawn session sub-agents on the fly
clawie delegation spawn-session --parent planner --child researcher --tier power

# See the delegation tree
clawie delegation tree --agent-id planner
```

Tiers include context budgets that track token usage and trigger compaction warnings to prevent context rot in deep delegation chains.

## Key commands

```bash
# Agents
clawie agent create alice --model-tier balanced
clawie agent clone alice bob --channel-strategy migrate
clawie agent list
clawie agent show alice

# Delegation
clawie delegation submit --parent p --child c --tier fast --payload '{}'
clawie delegation spawn-session --parent p --child c --tier power
clawie delegation tree --agent-id p
clawie delegation status

# Providers & runtime
clawie config set --provider picoclaw
sudo clawie runtime create alice --user alice
clawie runtime detect

# Dashboard
clawie dashboard
```

## Dashboard

Launch with `clawie dashboard`. Press `v` to cycle views:

- **Agents** — status, provider, auth, service health per agent
- **Channels** — all channels across agents, assign/move with keyboard
- **Delegation** — live delegation trees with tier icons, active sockets, task history

Navigate with arrow keys, `Enter` to drill into an agent, `Tab` to switch sections, `q` to quit.

## Documentation

Full documentation is in [`docs/`](docs/), deployable to GitHub Pages:

- [Getting Started](docs/getting-started.md)
- [Agent Management](docs/agents.md)
- [Delegation & Orchestration](docs/delegation.md)
- [Providers & Auth](docs/providers.md)
- [Runtime Isolation](docs/runtime.md)
- [Dashboard](docs/dashboard.md)
- [CLI Reference](docs/cli-reference.md)
- [Python API](docs/python-api.md)

## Development

```bash
uv run clawie --help
uv run --with pytest pytest -q
```

## License

MIT
