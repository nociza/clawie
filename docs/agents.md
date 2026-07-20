# Agent Management

Agents are the core unit in clawie. Each agent has a provider, channels,
credentials, core prompts, plugins, and an optional Linux runtime. `agent
create` creates only the control-plane definition; for a runnable isolated
agent, use `sudo clawie runtime create ID --user USER` instead.

## Create

```bash
clawie agent create alice --template baseline
```

If `AGENT_ID` is omitted, clawie picks an unused default name at random.
This command does not create a Linux user or start a provider service.

Options:

| Flag | Description |
|------|-------------|
| `--display-name` | Human-readable name |
| `--template` | Template to base config on (default: `baseline`) |
| `--channel-strategy` | `new` (mint fresh names) or `migrate` (keep source names) |
| `--model-tier` | Default tier: `fast`, `balanced`, `power` |
| `--provider` | Override provider for this agent |
| `--no-delegation` | Disable delegation skill |
| `--clone-from` | Clone config from an existing agent |

## Clone

Copy an existing agent's configuration into a new one:

```bash
clawie agent clone alice bob --channel-strategy migrate
```

This copies channels, prompts, defaults, and addons. Use `--channel-strategy new` to mint fresh channel names instead of migrating.

## List and inspect

```bash
clawie agent list
clawie agent show alice
```

## Prompts

Copy core prompt files between agents:

```bash
clawie agent prompt copy alice bob
```

Core prompts include SOUL.md, IDENTITY.md, AGENTS.md, TOOLS.md, MEMORY.md, and DELEGATION.md. These are seeded automatically based on the provider. Manifests with `role: control` add a marked control RPC block to AGENTS.md and TOOLS.md.

## Credentials

Credential sync uses a bundle model. Available bundles:

| Bundle | Contents |
|--------|----------|
| `provider-auth` | Provider auth files copied privately into the agent home (.codex/auth.json plus provider-native auth stores such as OpenClaw's openclaw-agent.sqlite) |
| `git` | .gitconfig, .git-credentials, .config/gh, .ssh |

```bash
clawie agent credentials list
clawie agent credentials show alice
clawie agent credentials set alice provider-auth
clawie agent credentials set alice git
sudo clawie agent credentials sync alice
sudo clawie agent credentials revoke alice git
```

## Addons

Addons extend agents with shared tools:

```bash
# Install a shared addon
clawie addon install gws

# Authenticate
sudo clawie addon auth login gws

# Enable for an agent
sudo clawie agent addon enable alice gws

# View addon status
clawie agent addon show alice
```

Available addons include `gws` (Google Workspace) and `display` (virtual display with browser automation).

## Model tier

Each agent has a default model tier that controls delegation budgets:

```bash
clawie agent create alice --model-tier fast
```

This creates a control-plane definition, not a Linux runtime. Use `sudo clawie
runtime create` for an operational isolated agent. Set the tier programmatically
via the service API.

## Delete

```bash
clawie agent delete alice          # Remove a definition-only record
sudo clawie agent purge alice      # Remove metadata + Linux user + credentials
```

`delete` refuses records with a `linux_user`, preventing an active runtime and
credential copies from becoming invisible or unmanaged. Use confirmed `purge`
for every provisioned runtime.

## Batch provisioning

Create multiple agents from a JSON file:

```json
[
  {"agent_id": "maria", "template": "baseline", "channel_strategy": "new"},
  {"agent_id": "dan", "clone_from": "maria", "channel_strategy": "migrate"}
]
```

```bash
clawie agent create-batch agents.json
```
