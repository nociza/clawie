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

When run with `sudo`, clawie reads `SUDO_USER` and uses the invoking user's `~/.clawie` state directory instead of `/root/.clawie`.

## Provider runtimes

Provider runtimes are external tools installed separately. They are only needed if you want to run agents — agent creation and `clawie status` work without them.

| Provider | Install method | Requires |
|----------|---------------|----------|
| zeroclaw | `brew install zeroclaw` | [Homebrew](https://brew.sh) (Linuxbrew) |
| picoclaw | `brew install picoclaw` | [Homebrew](https://brew.sh) (Linuxbrew) |
| openclaw | `pnpm add -g openclaw` | pnpm or npm, Node.js |

clawie can install these for you with `clawie runtime install <provider>`, but the underlying package manager must already be available on `PATH`.

Additional tools that may be auto-installed:
- **gcloud SDK** — downloaded automatically if needed for Google Workspace addon auth setup
- **fnm** — Node version manager, used for managing Node.js versions

## Storage

| Item | Location | Notes |
|------|----------|-------|
| State database | `~/.clawie/clawie.db` | SQLite |
| Override | `CLAWIE_HOME` env var or `--config-dir` flag | |
| Fallback | `/tmp/clawie-<uid>/clawie.db` | If home directory is not writable |
| Event history | Capped at 2,000 entries | Auto-trimmed on each write |
| Shared provider-auth cache | `~/.clawie/shared-provider-auth` or `/var/lib/clawie/provider-auth` | Manager-side cache; directories `0700`, files `0600` |
| Shared addon-auth cache | `~/.clawie/shared-addon-auth` or `/var/lib/clawie/addon-auth` | Manager-side cache; directories `0700`, files `0600` |
| Shared toolchain | `~/.clawie/shared-toolchain` or `/var/lib/clawie/toolchain` | Shared executable cache; directories `0755`, files non-world-writable |

The SQLite database uses default journaling. It is designed for single-process access. Running multiple clawie processes against the same state directory concurrently is not supported.

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
- `crypt` module for password hashing

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

### SQLite single-writer

The state database is SQLite with default journaling. It does not use WAL mode. Running multiple clawie processes against the same `~/.clawie` directory simultaneously may cause locking errors.

### No real tokenizer

Token counts for context budgets use `len(text) // 4` as an approximation. For most English text and JSON payloads this is reasonable, but it can be inaccurate for non-Latin scripts, heavily encoded data, or very short strings.

### Socket path length

Unix domain socket paths are limited to 108 bytes on Linux. The path format is `/tmp/clawie-delegation/<agent-id>.sock`, leaving approximately 80 characters for the agent ID. Longer IDs will fail to bind.
