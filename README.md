# clawie
<img width="2466" height="1536" alt="clawie" src="https://github.com/user-attachments/assets/e458bb38-b506-4fd6-a43e-53cd49649592" />

`clawie` is the central command center for your claw army.
It gives you one local CLI + terminal dashboard to provision, isolate, and run
many claws from one place.


## Philosophy

- Central control plane for the whole fleet.
- Linux-style isolation: each claw can run in its own Linux user runtime.
- Cross-provider support: open, pico, and zero (`openclaw`, `picoclaw`, `zeroclaw`).
- Authorize once, use all: reuse discovered credentials/config across flows.

## What It Handles

- Configure provider, auth, workspace, and API settings.
- Create, clone, inspect, and delete agents.
- Copy agent prompts and manage credential bundle policy.
- Apply channel presets and move channels between agents.
- Create per-agent Linux runtimes with optional config/credential copy.
- Detect installed local claw runtimes and transfer state.
- Inspect health/events from the CLI dashboard.
- Export/import local state snapshots.

## Install

```bash
uv tool install clawie
# from this repo:
uv tool install -e .
```

## Quick Start

1. Initialize config:

```bash
clawie config set --interactive
```

2. Select provider (open/pico/zero):

```bash
clawie config set --provider picoclaw --subscription pro --workspace production
```

3. Create an agent:

```bash
clawie agent create \
  alice \
  --display-name "Alice" \
  --template baseline \
  --channel-strategy new
```

4. Spawn an isolated Linux runtime for that claw:

```bash
sudo clawie runtime create alice --user alice
```

5. Operate the fleet:

```bash
clawie dashboard
```

## Command Layout

```bash
clawie config set|show
clawie agent create|clone|list|show|delete|purge|create-batch
clawie agent prompt copy
clawie agent auth show|login
clawie agent provider set
clawie agent credentials list|show|set|sync|revoke
clawie channel apply|move
clawie runtime create|detect|status|login
clawie dashboard
clawie health
clawie event list
clawie backup export|import
```

## Common Commands

```bash
# config
clawie config show
clawie config set --provider openclaw --auth-mode none --install-runtime
clawie config set --provider picoclaw --auth-mode api_key --api-key pico_live_1234

# agents
clawie agent list
clawie agent show alice
clawie agent auth show alice
sudo clawie agent auth login alice
clawie agent provider set alice picoclaw
clawie agent clone alice bob --channel-strategy migrate
clawie agent delete bob
clawie agent prompt copy alice bob
clawie agent credentials list
clawie agent credentials show alice
clawie agent credentials set alice git --include-defaults
sudo clawie agent credentials sync alice
sudo clawie agent credentials revoke alice git

# channels
clawie channel apply alice --preset growth
clawie channel move alice bob

# runtime + health
sudo clawie runtime create alice --user alice
clawie runtime detect
clawie runtime status
clawie runtime login zeroclaw
clawie health
clawie event list --limit 50

# state backups
clawie backup export backup.json
clawie backup import backup.json
```

## Dashboard Controls

- Local claws found in current user home (for example `~/.zeroclaw`) are shown
  in the same list as agents, marked as `(current-user)`.
- `v`: switch overview between `Agents` and `Channels` mode
- `j` / `k` or arrow keys: move selection
- `Enter`: open selected agent detail page
- `Tab`: switch detail section (channels/plugins/settings)
- `Space` / `Enter`: run action for selected row
- In `Channels` overview mode:
  - `Tab`: switch focus between channel list and target agent list
  - `a`: assign selected channel to selected agent
  - `c`: assign + run provider channel connect command for selected agent
- `a`: toggle selected agent autostart
- In Settings, use service rows to run `<provider> service start|stop|restart|status`
- In Settings, use `cred ...` rows to toggle bundle policy, sync credentials, and revoke credential access.
- `d`: purge selected agent (requires confirmation)
- `b` or `Esc`: back to overview
- `r`: refresh, `q`: quit

## Isolation and Credential Reuse

`runtime create` is built around the Linux `user` model. Each claw can get an
isolated OS user/home/runtime, while Clawie can copy common config/credential
paths from the invoking user (unless `--skip-config-copy` is set).

It also detects installed claw runtimes in the source home and carries relevant
credential/state paths, enabling an "authorize once, use all" workflow across
providers.

Credential sync is bundle-based. By default, Clawie syncs `provider-auth`.
You can opt in additional bundles (for example `git`) during runtime creation
with `--credential-bundle`, or later with
`clawie agent credentials set/sync/revoke`.

Available credential bundles:

- `provider-auth` (default): provider and model auth/config paths like `.codex`,
  `.openai`, and provider state dirs.
- `git`: `.gitconfig`, `.git-credentials`, `.config/gh`, `.ssh`.

Runtime examples:

```bash
# default behavior: sync provider-auth
sudo clawie runtime create alice --user alice

# include git credentials as well
sudo clawie runtime create alice --user alice --credential-bundle git

# disable defaults and only sync git
sudo clawie runtime create alice --user alice \
  --no-default-credentials \
  --credential-bundle git
```

Revoke behavior:

- `clawie agent credentials revoke` removes credential files for the selected
  bundle(s) from the agent Linux home.
- Revoked bundles are also removed from that agent's credential policy so access
  stays revoked until explicitly re-enabled.

Simple one-command sync from the main user to an agent:

```bash
sudo clawie agent credentials sync sandbox git --source-home /home/azicon
```

When run with `sudo`, Clawie reuses the invoking user's Clawie state by default
instead of creating separate `/root/.clawie` state.

## Batch Provisioning

```json
[
  {
    "agent_id": "maria",
    "display_name": "Maria",
    "template": "baseline",
    "channel_strategy": "new"
  },
  {
    "agent_id": "dan",
    "display_name": "Dan",
    "clone_from": "maria",
    "channel_strategy": "migrate"
  }
]
```

```bash
clawie agent create-batch agents.json
```

## Config and State

- State directory: `~/.clawie`
- SQLite DB: `~/.clawie/clawie.db`

Override config root per command:

```bash
clawie --config-dir /tmp/clawie-dev config show
```

## Development

```bash
uv run clawie --help
uv run python -m clawie --help
uv run --with pytest pytest -q
```
