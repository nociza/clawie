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
- Install shared addon CLIs and attach them to agents.
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

2. Install the provider runtime you want to use:

```bash
clawie runtime install picoclaw
```

3. Select provider (open/pico/zero):

```bash
clawie config set --provider picoclaw --subscription pro --workspace production
```

4. Create an agent:

```bash
clawie agent create \
  alice \
  --display-name "Alice" \
  --template baseline \
  --channel-strategy new
```

5. Spawn an isolated Linux runtime for that claw:

```bash
sudo clawie runtime create alice --user alice
```

6. Operate the fleet:

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
clawie agent service start|stop|restart|status
clawie agent credentials list|show|set|sync|revoke
clawie agent addon show|enable|disable|apply
clawie channel apply|move
clawie runtime create|detect|install|status|login
clawie runtime service start|stop|restart|status
clawie auth show|login|import|apply
clawie addon list|show|install
clawie addon auth show|login|import
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
sudo clawie agent provider set alice picoclaw
sudo clawie agent service restart alice
clawie agent clone alice bob --channel-strategy migrate
clawie agent delete bob
clawie agent prompt copy alice bob
clawie agent credentials list
clawie agent credentials show alice
clawie agent credentials set alice git --include-defaults
sudo clawie agent credentials sync alice
sudo clawie agent credentials revoke alice git
clawie addon list
clawie addon install gws
clawie addon auth show gws
sudo clawie addon auth login gws
sudo clawie agent addon enable alice gws
clawie agent addon show alice

# new agents start with no channels; add them explicitly
clawie agent create alice --template baseline
clawie channel apply alice --preset growth

# channels
clawie channel apply alice --preset growth
clawie channel move alice bob

# runtime + health
clawie runtime install picoclaw
clawie runtime install openclaw
sudo clawie runtime create alice --user alice
clawie runtime detect
clawie runtime status
clawie runtime login zeroclaw
clawie runtime service status zeroclaw
clawie auth show
clawie auth login picoclaw
clawie auth import picoclaw --from codex
sudo clawie auth apply
clawie addon auth import gws --source-home /home/azicon
sudo clawie agent addon apply alice gws
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
- `Tab` or `Left` / `Right`: switch detail section (`Channels` -> `Plugins` -> `Settings`)
- `Space` / `Enter`: run action for selected row
- In `Channels` overview mode:
  - `Tab`: switch focus between channel list and target agent list
  - `a`: assign selected channel to selected agent
  - `c`: assign + run provider channel connect command for selected agent
- `a`: toggle selected agent autostart
- In the detail `Channels` section:
  - `n`: add a channel to the selected agent
  - `N`: add a channel and immediately link it with the provider
  - `c` / `l`: link the selected assigned channel
  - `u`: unlink the selected assigned channel
  - `s`: resync the selected agent's channels from the live provider runtime
- In the detail `Settings` section:
  - use `channel ...` rows to sync, add, link, and unlink channels for the selected agent
  - use auth rows to inspect linked-auth state and run re-login
  - use provider rows to switch a managed agent between configured providers
  - use service rows to run `<provider> service start|stop|restart|status`
  - use `prompt ...` rows to edit/sync/write core prompts
  - use `cred ...` rows to toggle bundle policy, sync credentials, and revoke credential access
  - use `addon ...` rows to enable, disable, reapply, and log in shared addons such as `gws`
- `d`: purge selected agent (requires confirmation)
- `b` or `Esc`: back to overview
- `r`: refresh, `q`: quit

## Provider Switching

- Single-line managed cutover:

```bash
sudo clawie agent provider set AGENT_ID openclaw
sudo clawie agent provider set AGENT_ID picoclaw
sudo clawie agent provider set AGENT_ID zeroclaw
```

- `agent provider set` is a real cutover for managed agents with a Linux user:
  it validates the target provider, writes provider-specific prompt files,
  installs the target runtime when needed, bootstraps provider-native config,
  migrates live channel settings into the target home before start, stops the
  old provider service, starts the new one, and replays enabled non-CLI channels.
- The same single command also handles the failure cases that previously needed
  manual cleanup:
  - auto-import shared linked auth from the invoking user's Codex/provider
    session when possible
  - repair stale shared auth store formats and relink native provider auth files
  - repair ownership and symlinks under the target agent home
  - migrate Telegram bot config and heal legacy reachability defaults
  - restart the target provider if it is already running so repaired config/auth
    is actually applied to the live process
  - stop lingering runtimes from other providers before declaring success
  - verify the target provider is both live and post-start ready before success
- If state already says the target provider, the same command still reconciles
  runtime drift by restarting the target provider when it is already live, or
  starting it when it is not, and then stopping other installed provider
  services for that agent user.
- Provider-specific post-start checks are built in:
  - `openclaw`: `openclaw models status`
  - `picoclaw`: `picoclaw auth status`
  - `zeroclaw`: `zeroclaw auth status`
- `runtime create` and runtime `start|restart` actions also ensure the selected
  provider runtime is installed before trying to attach it to an agent or user.
- Managed-agent cutovers require `sudo/root` when the agent Linux user differs
  from the current shell user.
- Linked auth remains on disk in the agent home. Use `clawie agent auth show`
  to inspect it and `sudo clawie agent auth login AGENT_ID` if the new provider
  needs a fresh login.
- Fail-fast cases are explicit. `agent provider set` will stop before a partial
  success if the target runtime cannot be installed, the available linked auth
  is stale/missing and cannot be auto-imported, the migrated Telegram token is
  unresolved/invalid, or the target provider fails its post-start readiness
  probe.

## Shared Auth

- `clawie auth ...` manages the shared provider-auth store used across agents.
- `clawie auth login PROVIDER` runs the provider login flow against the shared
  auth home instead of one agent home, then links eligible agents to it.
- `clawie auth import PROVIDER --from codex|provider|claude` seeds that shared
  auth home from an existing app/provider login.
- `picoclaw` shared auth is written in the provider's native
  `.picoclaw/auth.json` format, so imported Codex subscription auth is usable by
  the real `picoclaw` binary without an extra login step.
- `clawie auth apply [AGENT_ID]` reapplies shared auth links to one agent or
  all eligible managed agents.
- `clawie agent auth show` and `clawie agent auth login` use the shared auth
  store automatically once that agent has shared provider auth linked.

## Shared Addons

- `clawie addon ...` is the central console for shared addon tools.
- `clawie addon install gws` installs the official Google Workspace CLI package
  (`@googleworkspace/cli`) and makes `gws` available on the shared runtime path.
- `clawie addon auth login gws` runs `gws auth setup` or `gws auth login`
  against a shared config dir, auto-installs `gcloud` into Clawie's shared
  toolchain if setup needs it, then exports portable plaintext credentials so
  every enabled agent can reuse the same Google Workspace session.
- `clawie addon auth import gws --source-home PATH` or `--from-agent AGENT_ID`
  imports an existing `~/.config/gws` config into that shared store.
- `clawie agent addon enable AGENT_ID gws` links the shared `gws` config into
  the target agent home at `~/.config/gws`.
- If shared `gws` credentials do not exist yet, enable will tell you to log in
  first, or you can pass `--login-if-missing` to perform the shared login flow
  immediately.

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

- `provider-auth` (default): shared auth files like `.codex/auth.json` and
  provider `auth-profiles.json` files. These are linked from a shared auth
  store so one login can be reused across agents.
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
