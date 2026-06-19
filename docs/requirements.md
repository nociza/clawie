# Requirements & Limitations

## System requirements

| Requirement | Detail |
|-------------|--------|
| **Operating system** | Linux (Debian/Ubuntu recommended). Uses `useradd`, systemd, Unix domain sockets, `/tmp`, and `apt-get`. Not compatible with macOS or Windows. |
| **Python** | 3.10 or later |
| **Dependencies** | Stdlib on Python 3.11+. Python 3.10 installs `tomli` for TOML parsing. |
| **Terminal** | UTF-8 encoding and color support for `clawie status` output. Minimum 80x24 recommended. |

## Root / sudo

Most read-only operations work without root. These require `sudo`:

| Operation | Why |
|-----------|-----|
| `runtime create` | Calls `useradd` to create a Linux user |
| `agent credentials sync` | Copies files into another user's home directory |
| `agent credentials revoke` | Removes files from another user's home directory |
| `agent provider set` | Writes provider config, restarts services in agent user's context |
| `agent auth login` | Writes auth files to agent home |
| `auth apply` | Copies private provider-auth files into agent homes |
| `agent addon enable/apply` | Writes config into agent home |
| SSH login disable | Modifies `/etc/ssh/sshd_config.d/` and reloads sshd |
| `control watchdog install/remove/verify` | Writes root-owned systemd unit files and runs `systemctl`; `verify --exercise-restart` intentionally kills the watchdog service process to prove restart behavior |

When run with `sudo`, clawie reads `SUDO_USER` and uses the invoking user's
`~/.clawie` state directory instead of `/root/.clawie`.

## Provider runtimes

Provider runtimes are external tools installed separately. They are only needed if you want to run agents — agent creation and `clawie status` work without them.

| Provider | Install method | Requires | Production delegated-task delivery |
|----------|---------------|----------|----------------------------------|
| openclaw | `pnpm add -g openclaw` | pnpm or npm, Node.js | verified |
| picoclaw | `brew install picoclaw` | [Homebrew](https://brew.sh) (Linuxbrew) | gated until source-pinned |
| zeroclaw | `brew install zeroclaw` | [Homebrew](https://brew.sh) (Linuxbrew) | gated until source-pinned |

clawie can install these for you with `clawie runtime install <provider>`, but the underlying package manager must already be available on `PATH`.

Additional tools that may be auto-installed:
- **gcloud SDK** — downloaded automatically if needed for Google Workspace addon auth setup
- **fnm** — Node version manager, used for managing Node.js versions

## Storage

| Item | Location | Notes |
|------|----------|-------|
| State database | `~/.clawie/clawie.db` | SQLite; state root `0700`, DB files `0600`, symlink roots/DB files rejected |
| Override | `CLAWIE_HOME` env var or `--config-dir` flag | Must point at a dedicated clawie state directory |
| Fallback | `/tmp/clawie-<uid>/clawie.db` | If home directory is not writable |
| Event history | Capped at 2,000 entries | Auto-trimmed on each write |
| Shared provider-auth cache | `~/.clawie/shared-provider-auth` or `/var/lib/clawie/provider-auth` | Manager-side cache; directories `0700`, files `0600` |
| Shared addon-auth cache | `~/.clawie/shared-addon-auth` or `/var/lib/clawie/addon-auth` | Manager-side cache; directories `0700`, files `0600` |
| Shared toolchain | `~/.clawie/shared-toolchain` or `/var/lib/clawie/toolchain` | Shared executable cache; directories `0755`, files non-world-writable |

The SQLite database stores agent records in an `agents` table and returns an
agents-only state surface; old `users` tables migrate once into `agents`. It
uses WAL journaling and a 30-second busy timeout so read-only commands and
maintenance jobs can coexist more reliably. `clawied` now hosts manifest
reconcile cycles under an advisory loop lock, exposes a local command socket for
`clawied` operations, routes mutating CLI service operations through a
whitelisted service RPC when the daemon is running, and hosts the
capability-gated control-tool RPC for read/safe-heal and confirmed destructive
actions. `clawie control request` and `clawie control confirm` expose that RPC
to a live `role: control` workspace; applying a control-role manifest seeds the
workspace prompts with the daemon-backed command path.

Root-required commands should operate on the same state root as normal user
commands. Plain `sudo clawie ...` does this by resolving `SUDO_USER` back to the
invoking user's `~/.clawie` and preserving that user's ownership for state DB
files; custom service-account deployments should set `CLAWIE_HOME` or pass the
same `--config-dir` everywhere.
To avoid damaging shared directories, clawie refuses to repair permissions on an
explicit non-empty `--config-dir` unless it already looks like a clawie state
directory. Use a dedicated empty directory for first-time custom state roots.

`clawie control watchdog install` writes a systemd supervisor for
`clawie clawied run` with `Restart=always`. An optional `--notify-command`
creates a separate `OnFailure` alert unit. The unit rendering is test-covered,
and `sudo clawie control watchdog verify --exercise-restart --json` is the
target-host proof that systemd actually restarts the daemon.

`sudo clawie production verify --exercise-watchdog-restart --json` aggregates
the configured-host gates into one target-host report: standard health,
Linux/root host validation, watchdog restart verification, and configured
runtime adapter contract checks. Package release acceptance should add
`--all-provider-contracts` so every verified production delivery provider has a
source-pinned delivery adapter contract. Running without
`--exercise-watchdog-restart` is a non-destructive dry check and cannot produce
a production pass. The built wheel has a Colima Linux/systemd proof recorded in
[`docs/proofs/production-verify-colima-systemd-wheel-0.1.6-2026-06-14.md`](proofs/production-verify-colima-systemd-wheel-0.1.6-2026-06-14.md);
repeat the same verifier on any different deployment host before accepting that
host.

## Delegation system

| Limit | Value |
|-------|-------|
| Max recursion depth | 10 levels |
| Max children per agent | 50 |
| Default timeout | 300 seconds (5 minutes) |
| Socket location | `/tmp/clawie-delegation/` |
| Socket permissions | `0o1777` (sticky bit, world-readable) |
| Wire protocol | 4-byte length-prefixed JSON over Unix domain sockets |
| Polling interval | 100ms |
| Heartbeat interval | 30 seconds |
| Agent ID length limit | ~80 characters (Unix socket path limit is 108 bytes) |

A file-based mailbox fallback at `/tmp/clawie-delegation/<agent-id>/inbox/` is used when socket connections fail due to cross-user permission issues.

### Context budgets

| Tier | Context window | Token budget |
|------|---------------|-------------|
| fast | 8,000 | 4,000 |
| balanced | 32,000 | 16,000 |
| power | 128,000 | 64,000 |

Token estimation uses a `len(text) / 4` heuristic. This is a rough approximation, not a real tokenizer.

## Limitations

### Linux only

clawie relies on Linux-specific system calls and tools throughout:
- `useradd` / `usermod` for agent isolation
- `systemctl` for service management
- Unix domain sockets (`AF_UNIX`) for agent IPC
- `/tmp` for socket and mailbox files
- `pwd` module for user lookup
- `crypt` module or `openssl` executable for SHA512 password hashing

There is no platform detection or fallback for macOS or Windows.

### Single machine only

All agent communication uses Unix domain sockets on localhost. There is no network transport, remote delegation, or multi-machine coordination.

### User-level isolation, not container-level

Agent isolation uses Linux users:
- Each agent gets a dedicated OS user and home directory
- File permissions prevent agents from reading each other's home directories
- Credential files are copied into agent homes with private modes

What is **not** isolated:
- Agents share the same kernel, network stack, and hardware
- `/tmp/clawie-delegation/` is world-readable (socket files are mode `0o777`)
- Provider-auth and addon-auth caches can hold one shared upstream identity. Copy isolation prevents cross-agent file access, but shared credentials still represent the same upstream account unless you configure separate source credentials.
- Any process running as root can access all agent files
- No network namespace, cgroup, or seccomp restrictions
- No Docker, Podman, or VM boundary

This is defense-in-depth at the user level, not a security sandbox.

### SQLite write serialization

The state database is SQLite with WAL mode and a 30-second busy timeout. This improves read/write coexistence for normal CLI use and maintenance, but SQLite still permits only one writer at a time. High-frequency concurrent mutation against the same `~/.clawie` directory can still queue behind the writer or fail if the timeout is exceeded.

### No real tokenizer

Token counts for context budgets use `len(text) // 4` as an approximation. For most English text and JSON payloads this is reasonable, but it can be inaccurate for non-Latin scripts, heavily encoded data, or very short strings.

### Socket path length

Unix domain socket paths are limited to 108 bytes on Linux. The path format is `/tmp/clawie-delegation/<agent-id>.sock`, leaving approximately 80 characters for the agent ID. Longer IDs will fail to bind.
