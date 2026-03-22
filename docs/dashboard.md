# Dashboard

The terminal dashboard gives you a real-time view of your entire agent fleet.

```bash
clawie dashboard
```

Focus on a single agent:

```bash
clawie dashboard --agent-id alice
```

## Views

Press `v` to cycle between three overview modes:

### Agents view

Shows all agents with:
- Status (running, stopped, ready)
- Provider
- Auth status
- Service health
- Channel count

### Channels view

Shows the global channel pool across all agents:
- Channel kind and name
- Enabled/disabled state
- Which agent owns each channel

Use `Tab` to switch focus between the channel list and agent list, `a` to assign a channel, `c` to assign and connect.

### Delegation view

Shows the delegation system state:
- **Left panel**: ASCII delegation trees with tier icons (⚡⚖⭐) and status
- **Right panel**: Active REPL sockets and recent delegation tasks

## Navigation

| Key | Action |
|-----|--------|
| `j`/`k` or arrows | Move selection |
| `Enter` | Open agent detail |
| `v` | Cycle overview mode |
| `Tab` | Switch detail section |
| `Space`/`Enter` | Run action on selected row |
| `b`/`Esc` | Back to overview |
| `r` | Refresh |
| `q` | Quit |

## Agent detail

Press `Enter` on an agent to see its full detail view with three sections:

### Channels section
- `n`: Add a channel
- `N`: Add and link a channel
- `c`/`l`: Link a channel to the provider
- `u`: Unlink a channel
- `s`: Resync channels from the live provider

### Plugins section
- Toggle plugins on/off

### Settings section
- Provider switching
- Auth login/refresh
- Autostart toggle
- Model tier cycling (fast -> balanced -> power)
- Core prompt editing
- Credential bundle management
- Addon enable/disable/apply

## Other overview controls

| Key | Action |
|-----|--------|
| `a` | Toggle autostart for selected agent |
| `d` | Purge selected agent (with confirmation) |
