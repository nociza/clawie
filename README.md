# clawie

`clawie` provides `clawie`, a local CLI + terminal dashboard for ZeroClaw-style
setup, user provisioning, and channel operations.

Core flows:
- initialize local setup (`api_key`, subscription, workspace, API URL)
- create or clone users with channel strategies (`new` or `migrate`)
- bootstrap or migrate channels between users
- inspect health, events, and dashboard snapshots
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
clawie setup init --interactive
```

Initialize setup (non-interactive):

```bash
clawie setup init \
  --api-key zc_live_1234 \
  --subscription pro \
  --workspace production \
  --api-url https://api.zeroclaw.example/v1
```

Check setup:

```bash
clawie setup status
```

Create a user from a template:

```bash
clawie users create \
  --user-id alice \
  --display-name "Alice Kim" \
  --template baseline \
  --channel-strategy new
```

Clone an existing user (shorthand command):

```bash
clawie users clone \
  --from-user alice \
  --user-id bob \
  --display-name "Bob Lee" \
  --channel-strategy migrate
```

Launch the dashboard:

```bash
clawie dashboard
```

## Command Highlights

User operations:

```bash
clawie users list
clawie users show --user-id alice
clawie users delete --user-id alice
```

Create/clone with explicit channels:

```bash
clawie users create --user-id sam --channel-strategy new --channel chat:ops --channel email:inbox
clawie users clone --from-user alice --user-id bob --channels-file channels.json
```

Channel operations:

```bash
clawie channels bootstrap --user-id alice --preset growth
clawie channels bootstrap --user-id alice --preset enterprise --replace
clawie channels migrate --from-user alice --to-user bob
clawie channels migrate --from-user alice --to-user bob --replace
```

Diagnostics and events:

```bash
clawie doctor
clawie events list --limit 50
```

State snapshots:

```bash
clawie state export --output backup.json
clawie state import --input backup.json
clawie state import --input backup.json --merge
```

## Batch Provisioning

Create `users.json`:

```json
[
  {
    "user_id": "maria",
    "display_name": "Maria",
    "template": "baseline",
    "channel_strategy": "new"
  },
  {
    "user_id": "dan",
    "display_name": "Dan",
    "clone_from": "maria",
    "channel_strategy": "migrate"
  }
]
```

Run:

```bash
clawie users batch-create --file users.json
```

## Config and State

Defaults:
- config directory: `~/.config/clawie`
- config file: `~/.config/clawie/config.json`
- state file: `~/.config/clawie/state.json`

You can override the config root for any command:

```bash
clawie --config-dir /tmp/clawie-dev setup status
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
