# Getting Started

## Prerequisites

- **Linux** (Debian/Ubuntu recommended) — clawie uses Linux users, systemd, and Unix domain sockets. Not compatible with macOS or Windows.
- **Python 3.10+**
- **OpenClaw runtime prerequisite:** Node.js `>=22.22.3 <23`, `>=24.15.0 <25`,
  or `>=25.9.0`, plus pnpm on the root service `PATH`.
- **Root/sudo** for runtime isolation (optional — agent creation and `clawie status` work without it)
- **Consistent state root** — normal `sudo clawie ...` uses the invoking user's `~/.clawie`; set `CLAWIE_HOME` or `--config-dir` for custom deployments.

See [Requirements & Limitations](requirements.md) for full details.

## Install

```bash
# Unprivileged inspection and definition commands
uv tool install clawie

# Production host from a pinned PyPI release (root-owned immutable tool)
sudo env UV_TOOL_DIR=/opt/clawie/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
  uv tool install 'clawie==X.Y.Z' --python 3.12

# Production host from a source checkout
sudo ./install.sh
```

Use `uv tool install -e .` only for local development; editable installs keep
executing code from the checkout and are not production artifacts.
Use the system install for any command run through `sudo`, cron, or systemd;
do not execute a user-owned tool environment as root.

Python 3.10 installs `tomli`; Python 3.11+ uses only the standard library.

## Configure

Set your provider, auth mode, and workspace:

```bash
clawie config set --provider openclaw --auth-mode linked --subscription pro --workspace production
```

Or run the interactive setup:

```bash
clawie config set --interactive
```

View current config:

```bash
clawie config show
```

`config set` changes only the options you provide. A later subscription or
workspace update will not reset the provider, auth mode, API URL, or other
existing settings.

## Install a provider runtime

```bash
sudo clawie runtime install openclaw
```

Clawie gives pnpm an explicit root-owned global package directory below
`/var/lib/clawie/toolchain` (or the local state root for an unprivileged
install), so installed launchers remain executable by isolated agent users.

Authorize the manager-side native store once:

```bash
clawie auth login openclaw
```

If an existing Codex session must be adopted first, run `clawie auth import
openclaw --from codex`, then refresh it through the native login command above.

OpenClaw 2026.7.1 is the only source-pinned delegated-task contract. Before
accepting a host, run `production verify` with both live exercises; picoclaw and
zeroclaw lifecycle/auth support is available, but their delivery remains gated.

## Create your first operational agent

```bash
sudo clawie runtime create alice --user alice --template baseline \
  --credential-bundle provider-auth --no-global-password
sudo clawie agent service start alice
```

This creates the `alice` agent record, a dedicated Linux user and home, and its
provider service configuration. The explicit start command launches the
gateway after provisioning. The default baseline enables delegation and
uses the balanced model tier. The home and provider tree stay owner-only;
cross-user management requires sudo/root or the authenticated `clawied` service.

Options:

```bash
sudo clawie runtime create alice \
  --user alice \
  --template baseline \
  --provider openclaw
```

`clawie agent create draft` is a definition-only planning command: it does not
create a Linux user or start a provider. To launch a new runtime using one of
those definitions as a source, use `sudo clawie runtime create NEW_ID
--from-agent draft`.

## Check fleet status

First prove that the gateway can execute a task:

```bash
clawie delegation deliver \
  --agent alice \
  --message 'Reply with exactly: clawie ready'
```

```bash
clawie status
```

This prints a read-only overview of all your agents — status, runtimes, auth,
delegation, and health. Add `--json` for scripting, or `--watch` for a live
view. An unhealthy embedded health result makes `status` exit nonzero.

## Set up continuous backup

Keep your agents' knowledge (prompts, memory, workspace notes) in a git repo
that clawie maintains automatically:

```bash
clawie backup init --remote git@github.com:you/agent-backup.git
clawie backup run                 # first snapshot
sudo clawie maintenance enable    # keep it current on every maintenance pass
```

The root cron requires an immutable root-owned clawie executable. A user-owned
`uv tool` install or editable checkout is intentionally rejected; use the
repository's `sudo ./install.sh` system installation before enabling cron.

Credential-looking content is filtered on a best-effort basis. Automatic remote
pushes are opt-in; review the repository before enabling them with
`clawie backup init --auto-push`. See
[Backup & Restore](backup.md).

## What's next

- [Agent Management](agents.md) — clone agents, manage prompts, configure addons
- [Delegation & Orchestration](delegation.md) — delegate tasks between agents with tiers
- [Providers & Auth](providers.md) — multi-provider setup, shared auth, porting between claws
- [Backup & Restore](backup.md) — git-backed knowledge backup
- [Status](status.md) — fleet overview, `--json`, live `--watch`
