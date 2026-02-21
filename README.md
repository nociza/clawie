# clawie

`clawie` provides `clawie`, a local CLI + terminal dashboard for provider setup,
Linux user spawning, agent provisioning, and channel operations.

Core flows:
- initialize local setup (`provider`, optional API key, subscription, workspace, API URL)
- choose/install provider runtime (`zeroclaw`, `picoclaw`, or `openclaw`)
- spawn Linux users and copy current user configs
- auto-detect installed claws in source home and transfer credentials/state
- create or clone agents with channel strategies (`new` or `migrate`)
- bootstrap or migrate channels between agents
- inspect health, events, and htop-like monitor snapshots
- export/import local state snapshots

## Install

From package index:

```bash
uv tool install clawie
```

From this repository:

```bash
uv tool install -e .
```

## Quick Start

Initialize setup (interactive):

```bash
clawie setup --interactive
```

Initialize setup (non-interactive, default provider `zeroclaw` with linked auth):

```bash
clawie setup --subscription pro --workspace production
```

Set a global default password for future spawned Linux users:

```bash
clawie setup --spawn-password 'ChangeMe123!'
```

Use explicit API key auth (any provider that supports it):

```bash
clawie setup \
  --provider zeroclaw \
  --auth-mode api_key \
  --api-key zc_live_1234
```

Initialize with `openclaw` (no API key required):

```bash
clawie setup --provider openclaw --auth-mode none --install-runtime
```

Check setup:

```bash
clawie setup --status
```

Create an agent from a template:

```bash
clawie agents create \
  --agent-id alice \
  --display-name "Alice Kim" \
  --template baseline \
  --channel-strategy new \
  --provider zeroclaw
```

Clone an existing agent (shorthand command):

```bash
clawie agents clone \
  --from-agent alice \
  --agent-id bob \
  --display-name "Bob Lee" \
  --channel-strategy migrate \
  --provider picoclaw
```

Launch the htop-like monitor:

```bash
clawie monitor
```

Spawn a Linux user and copy current configs:

```bash
sudo clawie spawn --agent-id alice --linux-user alice
```

## Command Highlights

Agent operations:

```bash
clawie list
clawie agents list
clawie agents show --agent-id alice
clawie agents delete --agent-id alice
sudo clawie purge alice
sudo clawie purge alice --yes
```

Create/clone with explicit channels:

```bash
clawie agents create --agent-id sam --channel-strategy new --channel chat:ops --channel email:inbox
clawie agents clone --from-agent alice --agent-id bob --channels-file channels.json
```

Channel operations:

```bash
clawie channels bootstrap --agent-id alice --preset growth
clawie channels bootstrap --agent-id alice --preset enterprise --replace
clawie channels migrate --from-agent alice --to-agent bob
clawie channels migrate --from-agent alice --to-agent bob --replace
```

Diagnostics and events:

```bash
clawie doctor
clawie events list --limit 50
```

Monitor and dashboard:

```bash
clawie monitor
clawie dashboard
```

Dashboard controls:
- Local claws found in current user home (for example `~/.zeroclaw`) are shown
  in the same list as agents, marked as `(current-user)`.
- `j` / `k` or arrow keys: move selection
- `Enter`: open selected agent detail page
- `Tab`: switch detail section (channels/plugins/settings)
- `Space` / `Enter`: run action for selected row
- `a`: toggle selected agent autostart
- In Settings, use service rows to run `<provider> service start|stop|restart|status`
  The dashboard sets per-user runtime bus env automatically and retries once
  after bootstrapping user linger/service manager when running as root.
  If user bus remains unavailable, Clawie falls back to provider `daemon` mode
  and tracks `fallback_pid` for start/stop/status operations.
- `d`: purge selected agent (requires confirmation)
- `b` or `Esc`: back to overview
- `r`: refresh, `q`: quit

Discover installed claws:

```bash
clawie claws detect
clawie claws detect --source-home /home/azicon
```

Linux user spawning:

```bash
sudo clawie spawn --agent-id sam --linux-user sam
sudo clawie spawn --agent-id sam --linux-user sam --skip-config-copy
sudo clawie spawn --agent-id pico1 --linux-user pico1 --provider picoclaw
sudo clawie spawn --agent-id sam --linux-user sam --password 'AgentSpecific123!'
sudo clawie spawn --agent-id sam --linux-user sam --password-hash '$6$...'
sudo clawie spawn --agent-id sam --linux-user sam --no-global-password
```

`spawn` automatically ports common credential/config locations from the invoking
user to the new Linux user, including Clawie config, provider config dirs, and
Codex/OpenAI config dirs when present.
It also auto-detects installed claw runtimes in the source home and includes
their credential/state paths during transfer.
When source claw config contains configured channels (for example
`~/.zeroclaw/config.toml` with `channels_config.telegram`), spawn imports those
channel kinds automatically instead of falling back to baseline-only channels.
When run via `sudo`, Clawie reuses the invoking user's Clawie state by default
instead of creating a separate `/root/.clawie` setup.

State snapshots:

```bash
clawie state export --output backup.json
clawie state import --input backup.json
clawie state import --input backup.json --merge
```

## Batch Provisioning

Create `agents.json`:

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

Run:

```bash
clawie agents batch-create --file agents.json
```

## Config and State

Defaults:
- state directory: `~/.clawie`
- SQLite DB: `~/.clawie/clawie.db`

You can override the config root for any command:

```bash
clawie --config-dir /tmp/clawie-dev setup --status
```

## Development

Run from source:

```bash
uv run clawie --help
uv run python -m clawie --help
```

Run tests:

```bash
uv run --with pytest pytest -q
```

## Notes

This project currently stores data locally. Integration point for service behavior:
`clawie/service.py`.
