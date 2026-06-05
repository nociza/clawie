# Dashboard (deprecated)

The interactive terminal dashboard has been retired in favor of a simple,
read-only, scriptable [`clawie status`](status.md) command.

`clawie dashboard` still works as a thin alias:

```bash
clawie dashboard            # == clawie status --watch (single snapshot when not a TTY)
clawie dashboard alice      # focus one agent
```

Use [`clawie status`](status.md) instead. It provides the same fleet overview
(agents, runtimes, auth, delegation, maintenance, health, events), plus `--json`
output and per-section scoping. The mutating actions the old dashboard offered
are first-class CLI commands — see the [CLI reference](cli-reference.md).
