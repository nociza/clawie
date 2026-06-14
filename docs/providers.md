# Providers & Auth

clawie supports three providers. Each agent runs on exactly one provider at a time, but you can switch with a single command.

## Supported providers

| Provider | Runtime | Default Auth | Install |
|----------|---------|-------------|---------|
| **zeroclaw** | zeroclaw-agent | linked | brew |
| **picoclaw** | picoclaw-agent | linked | brew |
| **openclaw** | openclaw-agent | none | pnpm |

## Setup

```bash
# Set the global provider
clawie config set --provider picoclaw --subscription pro --workspace production

# Install a runtime
clawie runtime install picoclaw

# Override provider per agent
clawie agent create alice --provider zeroclaw
```

## Auth modes

| Mode | Description |
|------|-------------|
| `none` | No authentication (openclaw default) |
| `linked` | Shared auth store; one login for all agents |
| `api_key` | Direct API key |

## Shared auth

The shared auth store lets you authenticate once and share credentials across agents:

```bash
# Login to a provider's shared auth
clawie auth login picoclaw

# Import from an existing app session
clawie auth import picoclaw --from codex

# View shared auth status
clawie auth show

# Apply shared auth to all eligible agents
sudo clawie auth apply
```

Shared auth is stored in the provider's native format, so imported credentials work without extra login steps.

## Porting auth between claws

Already authorized one claw (e.g. openclaw) and want the same session on
another? `auth port` translates the shared auth store between provider
formats — no new login required:

```bash
# Move every usable session from openclaw's store into picoclaw's
clawie auth port --from openclaw --to picoclaw

# Works in any direction
clawie auth port --from picoclaw --to zeroclaw
```

Porting:

- reads every profile from the source provider's shared store (including
  picoclaw's native `auth.json`),
- merges them into the target store in the target's native format, keeping
  the active profile active,
- copies provider auth into all agents that consume the shared provider-auth bundle, and
- tells you which agents need a service restart to pick up the new auth.

If the source store is empty you'll get a pointer to `clawie auth login` /
`clawie auth import ... --from codex` instead of a silent no-op.

## Per-agent auth

```bash
clawie agent auth show alice
sudo clawie agent auth login alice
```

Agents with shared auth enabled use their private copy of the shared store.

## Provider switching

Switch a managed agent to a different provider in one command:

```bash
sudo clawie agent provider set alice picoclaw
```

This handles the full cutover:
- Validates the target provider
- Writes provider-specific prompts
- Installs the runtime if needed
- Migrates config and channels
- Stops the old provider, starts the new one
- Imports shared auth when available
- Verifies readiness with provider-specific health checks

If the agent is already on the target provider, the command reconciles any drift by restarting the runtime.

## Provider health checks

Each provider has a built-in readiness check:

| Provider | Check command |
|----------|--------------|
| openclaw | `openclaw models status` |
| picoclaw | `picoclaw auth status` |
| zeroclaw | `zeroclaw auth status` |
