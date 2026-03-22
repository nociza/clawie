# CLI Reference

All commands use `clawie` as the entry point. Use `--config-dir` to override the state directory.

## config

```bash
clawie config show
clawie config set [--provider P] [--auth-mode M] [--api-key K] [--workspace W] [--subscription S] [--interactive]
```

## agent

```bash
clawie agent create AGENT_ID [--display-name N] [--template T] [--clone-from A] [--channel-strategy new|migrate] [--model-tier fast|balanced|power] [--provider P] [--no-delegation]
clawie agent clone SOURCE TARGET [--display-name N] [--channel-strategy new|migrate] [--model-tier fast|balanced|power] [--provider P] [--no-delegation]
clawie agent list
clawie agent show AGENT_ID
clawie agent delete AGENT_ID
clawie agent purge AGENT_ID
clawie agent create-batch FILE
```

### agent prompt

```bash
clawie agent prompt copy SOURCE TARGET [--apply-to-disk]
```

### agent auth

```bash
clawie agent auth show AGENT_ID
clawie agent auth login AGENT_ID
```

### agent provider

```bash
clawie agent provider set AGENT_ID PROVIDER
```

### agent service

```bash
clawie agent service start|stop|restart|status AGENT_ID
```

### agent credentials

```bash
clawie agent credentials list
clawie agent credentials show AGENT_ID
clawie agent credentials set AGENT_ID BUNDLE [--include-defaults]
clawie agent credentials sync AGENT_ID [BUNDLE] [--source-home PATH]
clawie agent credentials revoke AGENT_ID BUNDLE
```

### agent addon

```bash
clawie agent addon show AGENT_ID
clawie agent addon enable AGENT_ID ADDON [--login-if-missing]
clawie agent addon disable AGENT_ID ADDON
clawie agent addon apply AGENT_ID ADDON
```

## channel

```bash
clawie channel apply AGENT_ID --preset minimal|growth|enterprise
clawie channel move SOURCE_AGENT TARGET_AGENT
```

## delegation

```bash
clawie delegation submit --parent P --child C [--payload JSON] [--timeout S] [--tier fast|balanced|power]
clawie delegation repl --agent-id ID [--tier fast|balanced|power]
clawie delegation tree --agent-id ID
clawie delegation tasks [--agent-id ID] [--status S] [--limit N]
clawie delegation status
clawie delegation cleanup
clawie delegation spawn-session --parent P --child C [--timeout S] [--tier fast|balanced|power]
clawie delegation stop-session --parent P --child C
clawie delegation session-agents --parent P
```

## runtime

```bash
clawie runtime install PROVIDER
clawie runtime create AGENT_ID --user USER [--source-home PATH] [--skip-config-copy] [--credential-bundle B] [--no-default-credentials] [--password P] [--disable-ssh-login]
clawie runtime detect
clawie runtime status
clawie runtime login PROVIDER
clawie runtime service start|stop|restart|status PROVIDER
```

## auth

```bash
clawie auth show
clawie auth login PROVIDER
clawie auth import PROVIDER [--from SOURCE]
clawie auth apply [AGENT_ID]
```

## addon

```bash
clawie addon list
clawie addon show ADDON
clawie addon install ADDON
clawie addon auth show ADDON
clawie addon auth login ADDON
clawie addon auth import ADDON [--source-home PATH] [--from-agent A]
```

## dashboard

```bash
clawie dashboard [--agent-id ID] [--refresh N]
```

## health & events

```bash
clawie health
clawie event list [--limit N]
```

## backup

```bash
clawie backup export PATH
clawie backup import PATH [--merge]
```

## Global flags

| Flag | Description |
|------|-------------|
| `--config-dir` | Override state directory (default: `~/.clawie`) |
