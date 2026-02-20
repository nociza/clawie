# clawie

`clawie` installs the `clawctl` Linux CLI + terminal dashboard for ZeroClaw operations.

Core flows:
- setup ZeroClaw API/subscription/workspace
- one-click user spin-out from template or cloned user config
- choose per-user channel strategy (`new` or `migrate`)
- unified dashboard for all user agents
- extra ops features: health checks, event feed, batch provisioning, state import/export

## Install with uv

From package index:

```bash
uv tool install clawie
```

From this repository during development:

```bash
uv tool install -e .
```

## Quick Start

Interactive setup:

```bash
clawctl setup init --interactive
```

Non-interactive setup:

```bash
clawctl setup init \
  --api-key zc_live_1234 \
  --subscription pro \
  --workspace production
```

Create a user from template:

```bash
clawctl users create \
  --user-id alice \
  --display-name "Alice Kim" \
  --template baseline \
  --channel-strategy new
```

One-click clone a user config:

```bash
clawctl users clone \
  --from-user alice \
  --user-id bob \
  --display-name "Bob Lee" \
  --channel-strategy migrate
```

Channel operations:

```bash
clawctl channels bootstrap --user-id alice --preset growth
clawctl channels migrate --from-user alice --to-user bob
```

Unified dashboard:

```bash
clawctl dashboard
```

## Batch Provisioning Example

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
clawctl users batch-create --file users.json
```

## Other Useful Commands

```bash
clawctl setup status
clawctl users list
clawctl users show --user-id alice
clawctl doctor
clawctl events list --limit 50
clawctl state export --output backup.json
clawctl state import --input backup.json --merge
```

## Config and State Paths

By default:
- config directory: `~/.config/clawctl`
- config file: `~/.config/clawctl/config.json`
- state file: `~/.config/clawctl/state.json`

Override with `--config-dir` on any command.

## Notes

This scaffold stores data locally and is ready to wire into real ZeroClaw APIs. Integration point: `src/clawctl/service.py`.
