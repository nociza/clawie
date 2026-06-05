# clawie

<img width="2466" height="1536" alt="clawie" src="https://github.com/user-attachments/assets/e458bb38-b506-4fd6-a43e-53cd49649592" />

A local control plane for provisioning, orchestrating, and monitoring a fleet of AI agents from one CLI.

## Why clawie

You have multiple agents across providers. Each needs its own config, credentials, channels, and runtime. clawie gives you one place to manage all of it — create agents, delegate tasks between them, isolate their environments, and monitor everything from one CLI.

## Core capabilities

**Agent orchestration** — Delegate tasks across agents in recursive trees with automatic tier-based routing. Fast agents handle lookups, power agents handle analysis, balanced agents handle everything else.

**Multi-provider fleet** — Run agents on openclaw, picoclaw, or zeroclaw. Switch providers with a single command. Authorize once, share credentials across agents.

**Linux isolation** — Each agent gets its own Linux user, home directory, and credential scope. No agent sees another's secrets.

**Unified status** — One read-only `clawie status` command shows agent status, runtimes, auth, delegation trees, and health across your entire fleet — with `--json` for scripting and `--watch` for a live view.

## Requirements

- **Linux** (Debian/Ubuntu recommended). Uses `useradd`, systemd, Unix domain sockets, and `/tmp` — no macOS or Windows support.
- **Python 3.10+**
- **No external Python dependencies** — stdlib only.
- **Root/sudo** required for runtime isolation (`runtime create`, `credentials sync`, `provider set`, `auth apply`). Agent creation and `clawie status` work without root.
- **Provider runtimes** (optional): Homebrew for zeroclaw/picoclaw, pnpm or npm for openclaw.
- **Terminal**: UTF-8 with color support for `status` output.

## Install

```bash
uv tool install clawie        # from PyPI
uv tool install -e .          # from source
```

## Quick start

```bash
clawie config set --provider picoclaw --subscription pro
clawie agent create alice --template baseline
clawie status
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

# Status
clawie status
```

## Status

`clawie status` is the read-only front door to the whole fleet:

```bash
clawie status                 # full overview
clawie status agents          # one section
clawie status --agent alice   # focus a single agent
clawie status --json          # machine-readable, for scripting
clawie status --watch         # live view; refresh until Ctrl-C
```

It aggregates setup, health, agents, runtimes, auth, delegation, maintenance,
and recent events — and degrades gracefully if any one section can't be read.
`clawie dashboard` is a deprecated alias for `clawie status --watch`.

## Limitations

- **Linux only** — no macOS or Windows. Relies on Linux users, systemd, and Unix sockets.
- **Single machine** — all agent communication is over localhost Unix sockets. No network/multi-host delegation.
- **User-level isolation, not container-level** — agents get separate Linux users and home directories, but share the same kernel, `/tmp`, and localhost. No Docker/VM boundary.
- **Delegation depth capped at 10**, max 50 children per agent, 5-minute default timeout.
- **SQLite storage** — single-writer, not designed for concurrent multi-process access to the same state directory.
- **Token estimation is approximate** — uses a chars/4 heuristic, not a real tokenizer.

See [docs/requirements.md](docs/requirements.md) for full details.

## Documentation

Full documentation is in [`docs/`](docs/), deployable to GitHub Pages:

- [Getting Started](docs/getting-started.md)
- [Requirements & Limitations](docs/requirements.md)
- [Agent Management](docs/agents.md)
- [Delegation & Orchestration](docs/delegation.md)
- [Providers & Auth](docs/providers.md)
- [Runtime Isolation](docs/runtime.md)
- [Status](docs/status.md)
- [CLI Reference](docs/cli-reference.md)
- [Python API](docs/python-api.md)

## Development

```bash
uv run clawie --help
uv run --with pytest pytest -q
```

## License

MIT
