# Runtime Isolation

clawie uses Linux users for agent isolation. Each agent can get its own OS user, home directory, and scoped credentials.

## Create a runtime

```bash
sudo clawie runtime create alice --user alice
```

If the agent ID is omitted, clawie picks an unused default name at random. For
runtime creation, the implicit Linux username is the lowercase form of that name
unless `--user` is provided.

This:
1. Creates a Linux user `alice` with a home directory
2. Installs the configured provider runtime
3. Copies common config from the invoking user
4. Bootstraps provider-specific config in the agent home

Options:

| Flag | Description |
|------|-------------|
| `--user` | Linux username (defaults to agent ID) |
| `--source-home` | Copy configs from this path instead of current user |
| `--skip-config-copy` | Don't copy any configs |
| `--credential-bundle` | Credential bundle to sync; use `provider-auth` only when this agent should reuse shared provider auth |
| `--no-default-credentials` | Compatibility flag; default credential bundles are empty |
| `--password` | Set a password for the Linux user |
| `--password-hash` | Set a pre-hashed Linux password |
| `--no-global-password` | Ignore the configured global spawn password |
| `--from-agent` | Clone state from an existing agent |
| `--provider` | Override the configured provider |
| `--no-delegation` | Disable the delegation skill for the spawned agent |

Spawned users are denied SSH login automatically.

## Credential bundles

Credentials are synced in scoped bundles:

| Bundle | Contents |
|--------|----------|
| `provider-auth` | .codex/auth.json and provider auth stores copied into the agent home as private files |
| `git` | .gitconfig, .git-credentials, .config/gh, .ssh |

```bash
# Default: no credential bundles, so the agent authenticates independently
sudo clawie runtime create alice --user alice

# Explicitly reuse shared provider auth
sudo clawie runtime create alice --user alice --credential-bundle provider-auth

# Explicitly copy git credentials
sudo clawie runtime create alice --user alice --credential-bundle git
```

## Credential management after creation

```bash
# View credential policy
clawie agent credentials show alice

# Opt into shared provider auth
clawie agent credentials set alice provider-auth

# Add git credentials
clawie agent credentials set alice git

# Sync credentials to agent home
sudo clawie agent credentials sync alice

# Sync from a specific source
sudo clawie agent credentials sync alice git --source-home /home/admin

# Revoke access
sudo clawie agent credentials revoke alice git
```

Revoking removes the credential files from the agent home and updates the policy so they stay revoked until re-enabled. Shared provider-auth and addon-auth caches are manager-side only; agents receive owned copies rather than symlinks to shared credential files.

`clawie health` verifies this host isolation surface: the shared provider-auth
store must be private, copied provider-auth files must not be symlinks, and
copied provider-auth files must not be group/world-readable.

For release/production validation, run the stronger Linux/root host proof:

```bash
sudo clawie health --host-validate --json
```

That command requires Linux with `/proc`, root, and at least two managed
Linux-user agents. It checks that the users exist, homes are private, credential
files are private and not symlinks, and one agent user cannot read another
agent's sensitive paths. A skipped or failed report is not production evidence.
The repository includes a Linux/root container proof in
[`docs/proofs/host-validation-linux-container-2026-06-14.md`](proofs/host-validation-linux-container-2026-06-14.md);
the built wheel also has a Colima Linux/systemd aggregate proof in
[`docs/proofs/production-verify-colima-systemd-wheel-2026-06-14.md`](proofs/production-verify-colima-systemd-wheel-2026-06-14.md).
Repeat the verifier on any different deployment host before accepting that host.

For full target-host acceptance, run the aggregate verifier:

```bash
sudo clawie production verify --exercise-watchdog-restart --json
```

It combines standard health, host isolation validation, watchdog restart
verification, and configured runtime adapter contract checks into one report.
For package release acceptance, add `--all-provider-contracts` so every verified
production delivery provider is checked for a source-pinned adapter.
The aggregate verifier exits nonzero unless `--exercise-watchdog-restart`
actually proves restart behavior.

## Detect installed runtimes

```bash
clawie runtime detect
clawie runtime status
```

These scan for installed provider runtimes and report their status.

## Runtime services

```bash
clawie runtime service status zeroclaw
clawie runtime service start zeroclaw
clawie runtime service stop zeroclaw
clawie runtime service restart zeroclaw
```

## sudo behavior

When run with `sudo`, clawie uses the invoking user's state directory (`~/.clawie`) instead of `/root/.clawie`, so you don't end up with split state.

## Security model

Runtime isolation is **user-level, not container-level**. Each agent gets a separate Linux user and home directory, and file permissions prevent cross-agent access.

What is **not** isolated:
- Agents share the same kernel, network stack, and hardware
- `/tmp/clawie-delegation/` is world-accessible for IPC sockets
- Any root process can access all agent files
- No network namespace, cgroup, seccomp, or container boundary

This provides defense-in-depth at the OS user level, not a security sandbox. If you need stronger isolation, run clawie inside a VM or container.
