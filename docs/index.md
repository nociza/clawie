# clawie

A local control plane for provisioning, orchestrating, and monitoring a fleet of AI agents.

## What is clawie?

clawie manages multiple AI agents across providers from one CLI. It handles agent creation, recursive task delegation, Linux-level isolation, credential management, and real-time monitoring — all from your terminal.

## Key features

- **Agent orchestration** with recursive delegation, model tiers, and context budgets
- **Provider-aware support** with a source-pinned OpenClaw delivery contract and mandatory live host proof, plus picoclaw/zeroclaw lifecycle and auth migration support
- **Runtime isolation** via per-agent Linux users and scoped credentials
- **Continuous knowledge backup** to a git repo — prompts and agent memory, secrets excluded
- **Credential porting** between claws: authorize once, move sessions across providers
- **Unified `clawie status`** for read-only fleet monitoring (with `--json` and `--watch`)
- **Addon ecosystem** for extending agents with tools like Google Workspace
- **Channel management** for connecting agents to Telegram, Slack, email, and more

## Quick start

```bash
sudo ./install.sh  # from a release checkout; installs a root-owned system copy
clawie config set --provider openclaw --subscription pro
sudo clawie runtime install openclaw
sudo clawie runtime create alice --user alice --template baseline
clawie status
```

## Requirements

Linux with Python 3.10+. Python 3.10 installs `tomli`; Python 3.11+ uses only the standard library. Root/sudo needed for runtime isolation. See [Requirements & Limitations](requirements.md) for full details.

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](getting-started.md) | Install, configure, create your first agent |
| [Requirements & Limitations](requirements.md) | System requirements, root needs, constraints |
| [Agent Management](agents.md) | Create, clone, configure, delete agents |
| [Delegation & Orchestration](delegation.md) | Task delegation, model tiers, context budgets |
| [Providers & Auth](providers.md) | Provider setup, auth modes, shared credentials, porting |
| [Runtime Isolation](runtime.md) | Linux users, credential bundles, security model |
| [Backup & Restore](backup.md) | Continuous git-backed knowledge backup |
| [Status](status.md) | Fleet overview, `--json`, live `--watch` |
| [CLI Reference](cli-reference.md) | Every command and flag |
| [Python API](python-api.md) | Programmatic usage from Python |
| [Releasing](releasing.md) | Exact artifact proof and trusted PyPI publishing |
