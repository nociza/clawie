# clawie

A local control plane for provisioning, orchestrating, and monitoring a fleet of AI agents.

## What is clawie?

clawie manages multiple AI agents across providers from one CLI. It handles agent creation, recursive task delegation, Linux-level isolation, credential management, and real-time monitoring — all from your terminal.

## Key features

- **Agent orchestration** with recursive delegation, model tiers, and context budgets
- **Multi-provider support** for openclaw, picoclaw, and zeroclaw
- **Runtime isolation** via per-agent Linux users and scoped credentials
- **Terminal dashboard** for real-time fleet monitoring
- **Addon ecosystem** for extending agents with tools like Google Workspace
- **Channel management** for connecting agents to Telegram, Slack, email, and more

## Quick start

```bash
uv tool install clawie
clawie config set --provider picoclaw --subscription pro
clawie agent create alice --template baseline
clawie dashboard
```

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](getting-started.md) | Install, configure, create your first agent |
| [Agent Management](agents.md) | Create, clone, configure, delete agents |
| [Delegation & Orchestration](delegation.md) | Task delegation, model tiers, context budgets |
| [Providers & Auth](providers.md) | Provider setup, auth modes, shared credentials |
| [Runtime Isolation](runtime.md) | Linux users, credential bundles, security model |
| [Dashboard](dashboard.md) | TUI controls, views, navigation |
| [CLI Reference](cli-reference.md) | Every command and flag |
| [Python API](python-api.md) | Programmatic usage from Python |
