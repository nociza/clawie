# clawie

<img width="2466" height="1536" alt="clawie" src="https://github.com/user-attachments/assets/e458bb38-b506-4fd6-a43e-53cd49649592" />

A local control plane for provisioning, orchestrating, and monitoring a fleet of AI agents from one CLI.

## Why clawie

You have multiple agents across providers. Each needs its own config, credentials, channels, and runtime. clawie gives you one place to manage all of it — create agents, delegate tasks between them, isolate their environments, and monitor everything from one CLI.

## Core capabilities

**Agent orchestration** — Delegate tasks across agents in recursive trees with automatic tier-based routing. Fast agents handle lookups, power agents handle analysis, balanced agents handle everything else.

**Provider-aware fleet** — The delegated-task contract is source-pinned for
OpenClaw 2026.7.1. A deployment is accepted only after the verifier completes a
live challenge through that host's gateway; picoclaw and zeroclaw delivery
remain gated. Authorize once, copy private credential material into eligible
agent homes, and port sessions between claws with `clawie auth port`.

**Linux isolation** — Each agent gets its own Linux user and home directory. Credential files are copied into agent homes with private modes; agents do not read or mutate shared auth/cache files.

**Continuous knowledge backup** — Agent manifests, prompts, and memory are mirrored into a git repo on every maintenance pass, with secrets redacted and credentials excluded. Restore one agent or the whole fleet with `clawie backup restore`.

**Unified status** — One read-only `clawie status` command shows agent status, runtimes, auth, delegation trees, backup, and health across your entire fleet — with `--json` for scripting and `--watch` for a live view.

## Requirements

- **Linux** (Debian/Ubuntu recommended). Uses `useradd`, systemd, Unix domain sockets, and `/tmp` — no macOS or Windows support.
- **Python 3.10+**
- **Python dependencies** — stdlib on Python 3.11+; Python 3.10 installs `tomli` for TOML parsing.
- **Root/sudo** required for runtime isolation (`runtime create`, `credentials sync`, `provider set`, `auth apply`). Agent creation and `clawie status` work without root.
- **State root under sudo** — normal `sudo clawie ...` uses the invoking user's `~/.clawie` via `SUDO_USER`. For service accounts or custom layouts, set `CLAWIE_HOME` or pass `--config-dir` consistently.
- **Provider runtimes** (optional): Homebrew for zeroclaw/picoclaw, pnpm or npm for openclaw.
- **Terminal**: UTF-8. Colors are automatic on TTYs and can be disabled with
  `--no-color` or `NO_COLOR`.

## Install

```bash
uv tool install clawie        # from PyPI
uv tool install .             # immutable install from source
sudo ./install.sh             # system install for sudo/runtime automation
```

Use the root-owned system install for operational agents, cron, and the
systemd watchdog. A user-owned tool environment is appropriate for unprivileged
definition and inspection commands, but must not be executed as root.

## Quick start

```bash
clawie config set --provider openclaw --subscription pro
sudo clawie runtime install openclaw
sudo clawie runtime create alice --user alice --template baseline
clawie status
```

Later `config set` calls update only the options supplied; omitted provider,
auth, workspace, subscription, and API settings are preserved.

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
clawie delegation submit --parent planner --child researcher --payload '{"task":"analyze"}'
clawie delegation stop-session --parent planner --child researcher

# See the delegation tree
clawie delegation tree --agent-id planner
```

Tiers include context budgets that track token usage and trigger compaction warnings to prevent context rot in deep delegation chains.

## Key commands

```bash
# Definition-only agents (no Linux user or service)
clawie agent create alice --model-tier balanced
clawie agent clone alice bob --channel-strategy migrate
clawie agent list
clawie agent show alice

# Operational isolated agent
sudo clawie runtime create worker --user worker --provider openclaw

# Delegation
clawie delegation submit --parent p --child c --tier fast --payload '{}'
clawie delegation spawn-session --parent p --child c --tier power
clawie delegation tree --agent-id p
clawie delegation status

# Providers & runtime
clawie config set --provider openclaw
sudo clawie runtime create alice --user alice
clawie runtime detect

# Credentials
clawie auth login picoclaw                        # authorize the manager-side store once
clawie auth import openclaw --from codex          # adopt an existing session
clawie auth port --from openclaw --to picoclaw    # port sessions between claws
sudo clawie auth apply                            # copy private auth files into eligible agents

# Backup (git-backed, continuously maintained)
clawie backup init --remote git@github.com:you/agent-backup.git
clawie backup run
clawie backup restore --agent alice

# Status
clawie status

# Production acceptance for the configured host
sudo clawie production verify --exercise-watchdog-restart --exercise-runtime-delivery --json

# Release acceptance for the verified delivery surface
sudo clawie production verify --exercise-watchdog-restart --exercise-runtime-delivery --all-provider-contracts --json
```

Backup collection is staged before replacement. Incomplete reads preserve the
last complete snapshot and return nonzero; failed remote pushes also return
nonzero so cron and monitoring cannot report false durability.

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
backup, and recent events — and degrades gracefully if any one section can't
be read. `clawie dashboard` is a deprecated alias for `clawie status --watch`.

## Backup

Agent knowledge — core prompts, `MEMORY.md`, and workspace notes — lives in a
git repo that clawie commits to automatically on every maintenance pass. Remote
pushes require an explicit `--push` or `backup init --auto-push`:

```bash
clawie backup init --remote git@github.com:you/agent-backup.git
sudo clawie maintenance enable        # backup now runs every pass
clawie backup status                  # repo, HEAD, last run
clawie backup restore --agent alice   # bring knowledge back after a loss
```

Secrets are redacted from the snapshot, credential-looking content is filtered
on a best-effort basis, and automatic remote pushes are opt-in. Review a backup
before enabling pushes. Missing local agent records are recreated from the backed-up
manifest before prompts and workspace knowledge are restored; `clawie backup
export` exists for full-fidelity local snapshots.
See [docs/backup.md](docs/backup.md).

## Limitations

- **Linux only** — no macOS or Windows. Relies on Linux users, systemd, and Unix sockets.
- **Single machine** — all agent communication is over localhost Unix sockets. No network/multi-host delegation.
- **User-level isolation, not container-level** — agents get separate Linux users and home directories, but share the same kernel, `/tmp`, and localhost. No Docker/VM boundary.
- **Delegation depth capped at 10**, max 50 children per agent, 5-minute default timeout; manifests can lower depth and gateway timeout limits per agent.
- **SQLite storage** — uses WAL, a busy timeout, and revision-based compare-and-swap for JSON state/config snapshots so stale writers fail instead of silently losing updates. `clawied` hosts manifest reconciliation, mutating service operations, and a capability-gated control RPC.
- **Acceptance is host-specific** — earlier records under `docs/proofs/` are historical evidence, not evidence for the hardened tree or a new host. Run both destructive exercises in `production verify` on the exact built artifact and deployment host. The package remains Beta until that fresh Linux/systemd acceptance is recorded.
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
- [Backup & Restore](docs/backup.md)
- [Status](docs/status.md)
- [CLI Reference](docs/cli-reference.md)
- [Python API](docs/python-api.md)

## Development

```bash
uv run clawie --help
uv run --with pytest pytest -q
```

## License

Apache-2.0
