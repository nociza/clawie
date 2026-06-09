# Backup & Restore

clawie keeps the knowledge instilled in your agents — core prompts, memory
files, and workspace notes — in a **git repository that is maintained
automatically**. Credentials never enter the repo.

## What gets backed up

| Path in repo | Contents |
|--------------|----------|
| `state/snapshot.json` | Fleet config and agent records, **secrets redacted** (API keys, password hashes, bot tokens) |
| `agents/<id>/prompts/` | Core prompt files from the control plane (SOUL.md, MEMORY.md, DELEGATION.md, ...) |
| `agents/<id>/workspace/` | Knowledge files captured from the live agent workspace: markdown/text notes plus everything under `memory/` |

What is deliberately **excluded**:

- Credential material — auth files, symlinks into the shared auth store,
  key/PEM files, and any file whose name looks credential-like
  (`*token*`, `*secret*`, `*auth*`, ...). A `.gitignore` safety net backs
  this up at the git layer.
- The event log, so commits only happen when knowledge actually changes.
- Files over 1 MiB and non-knowledge formats (binaries, JSON state).

Need a full-fidelity snapshot including credentials? Use
`clawie backup export FILE` — it writes a `0600`-permission local file and is
not meant for git.

## Quick start

```bash
# 1. Create the repo (default: ~/.clawie/backup) and enable continuous backup
clawie backup init --remote git@github.com:you/agent-backup.git

# 2. Take the first snapshot
clawie backup run

# 3. Let maintenance keep it current (backup runs on every pass)
sudo clawie maintenance enable --interval 4
```

Every maintenance pass now syncs credentials, applies staged prompts, **and
commits/pushes knowledge changes**. `clawie status` shows the backup section
alongside everything else.

## Commands

```bash
clawie backup init [PATH] [--remote URL] [--no-auto]
clawie backup run [--message M] [--push|--no-push]
clawie backup status
clawie backup restore [--agent ID] [--no-workspace] [--no-apply-to-disk]
clawie backup export PATH          # full-fidelity local snapshot (secrets included)
clawie backup import PATH [--merge]
```

- `init` creates or adopts the git repo, optionally sets `origin`, and enables
  automatic backups (skip with `--no-auto`). Re-running updates the remote.
- `run` mirrors the current knowledge into the repo and commits **only if
  something changed**. With a remote configured it pushes automatically
  (`--no-push` to skip, `backup_auto_push` config to change the default).
  A failed push is reported but never fails the backup.
- `status` is read-only: repo path, remote, HEAD, commit count, dirty flag,
  last run.
- `restore` writes prompts back into agent state (and agent homes), then
  restores workspace knowledge files. Live workspace files win over
  control-plane prompt copies, so an agent's self-edited `MEMORY.md` comes
  back exactly as it was captured.

## Restore semantics

```bash
clawie backup restore                 # all agents present in local state
clawie backup restore --agent alice   # one agent
```

- Agents present in the backup but missing from local state are skipped with
  a warning — recreate the agent (or `clawie backup import` a snapshot) first.
- `--no-apply-to-disk` updates control-plane state only.
- `--no-workspace` restores core prompts only.
- Restoring to another machine: clone the backup repo to the configured path
  (`clawie backup init PATH`), import a state snapshot if you need agent
  records, then `clawie backup restore`.

## How continuous backup works

`clawie maintenance run` (installed as a cron by `maintenance enable`) ends
each pass with a backup run when `backup_enabled` is set. The maintenance
output and the `maintenance.run` event both record the backup outcome:

```
$ clawie maintenance run
  Auth refresh: ok (codex from /home/you)
  alice: credentials=ok  prompts=ok (none staged)  [ok]
  Backup: ok (commit 3887d3a2c6)
  Total: 1 agents, 0 skipped, 0 errors
```

Backups run as root under cron; clawie restores repo file ownership to the
managing user afterwards so manual `clawie backup run` keeps working.
