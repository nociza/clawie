# clawie

`clawie` provides `clawie`, a local CLI + terminal dashboard for provider setup,
Linux user spawning, agent provisioning, and channel operations.

Core flows:
- initialize local setup (`provider`, optional API key, subscription, workspace, API URL)
- choose/install provider runtime (`zeroclaw` default, or `openclaw`)
- spawn Linux users and copy current user configs
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

Initialize setup (non-interactive, default provider `zeroclaw`):

```bash
clawie setup \
  --api-key zc_live_1234 \
  --subscription pro \
  --workspace production \
  --api-url https://api.zeroclaw.example/v1
```

Initialize with `openclaw` (no API key required):

```bash
clawie setup --provider openclaw --install-runtime
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
  --channel-strategy new
```

Clone an existing agent (shorthand command):

```bash
clawie agents clone \
  --from-agent alice \
  --agent-id bob \
  --display-name "Bob Lee" \
  --channel-strategy migrate
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
clawie agents list
clawie agents show --agent-id alice
clawie agents delete --agent-id alice
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

Linux user spawning:

```bash
sudo clawie spawn --agent-id sam --linux-user sam
sudo clawie spawn --agent-id sam --linux-user sam --skip-config-copy
```

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
