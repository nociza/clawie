# clawie

`clawie` is the central command center for your claw army.
It gives you one local CLI + terminal dashboard to provision, isolate, and run
many claws from one place.

## Philosophy

- Central control plane for the whole fleet.
- Linux-style isolation: each claw can run in its own Linux user runtime.
- Cross-provider support: open, pico, and zero (`openclaw`, `picoclaw`, `zeroclaw`).
- Authorize once, use all: reuse discovered credentials/config across flows.

## What It Handles

- Setup provider, auth, workspace, and API settings.
- Create, clone, and manage agents.
- Bootstrap or migrate channels between agents.
- Spawn per-agent Linux users with optional config/credential copy.
- Detect installed local claw runtimes and transfer state.
- Monitor health/events with CLI monitor and dashboard.
- Export/import local state snapshots.

## Install

```bash
uv tool install clawie
# from this repo:
uv tool install -e .
```

## Quick Start

1. Initialize setup:

```bash
clawie setup --interactive
```

2. Select provider (open/pico/zero):

```bash
clawie setup --provider zeroclaw --subscription pro --workspace production
```

3. Create an agent:

```bash
clawie agents create \
  --agent-id alice \
  --display-name "Alice" \
  --template baseline \
  --channel-strategy new
```

4. Spawn an isolated Linux runtime for that claw:

```bash
sudo clawie spawn --agent-id alice --linux-user alice
```

5. Operate the fleet:

```bash
clawie dashboard
# or
clawie monitor
```

## Common Commands

```bash
# setup
clawie setup --status
clawie setup --provider openclaw --auth-mode none --install-runtime
clawie setup --provider zeroclaw --auth-mode api_key --api-key zc_live_1234

# agents
clawie list
clawie agents show --agent-id alice
clawie agents clone --from-agent alice --agent-id bob --channel-strategy migrate
clawie agents delete --agent-id bob

# channels
clawie channels bootstrap --agent-id alice --preset growth
clawie channels migrate --from-agent alice --to-agent bob

# runtime + health
clawie claws detect
clawie doctor
clawie events list --limit 50

# state backups
clawie state export --output backup.json
clawie state import --input backup.json
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
- `d`: purge selected agent (requires confirmation)
- `b` or `Esc`: back to overview
- `r`: refresh, `q`: quit

## Isolation and Credential Reuse

`spawn` is built around the Linux `user` model. Each claw can get an isolated
OS user/home/runtime, while Clawie can copy common config/credential paths from
the invoking user (unless `--skip-config-copy` is set).

It also detects installed claw runtimes in the source home and carries relevant
credential/state paths, enabling an "authorize once, use all" workflow across
providers.

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
clawie agents batch-create --file agents.json
```

## Config and State

- State directory: `~/.clawie`
- SQLite DB: `~/.clawie/clawie.db`

Override config root per command:

```bash
clawie --config-dir /tmp/clawie-dev setup --status
```

## Development

```bash
uv run clawie --help
uv run python -m clawie --help
uv run --with pytest pytest -q
```
