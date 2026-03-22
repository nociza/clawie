# Runtime Isolation

clawie uses Linux users for agent isolation. Each agent can get its own OS user, home directory, and scoped credentials.

## Create a runtime

```bash
sudo clawie runtime create alice --user alice
```

This:
1. Creates a Linux user `alice` with a home directory
2. Installs the configured provider runtime
3. Copies common config and credentials from the invoking user
4. Bootstraps provider-specific config in the agent home

Options:

| Flag | Description |
|------|-------------|
| `--user` | Linux username (defaults to agent ID) |
| `--source-home` | Copy configs from this path instead of current user |
| `--skip-config-copy` | Don't copy any configs |
| `--credential-bundle` | Additional credential bundles to sync |
| `--no-default-credentials` | Skip the default `provider-auth` bundle |
| `--password` | Set a password for the Linux user |
| `--disable-ssh-login` | Prevent SSH login to this user |

## Credential bundles

Credentials are synced in scoped bundles:

| Bundle | Contents |
|--------|----------|
| `provider-auth` (default) | .codex/auth.json, auth-profiles.json, shared auth links |
| `git` | .gitconfig, .git-credentials, .config/gh, .ssh |

```bash
# Sync with defaults + git
sudo clawie runtime create alice --user alice --credential-bundle git

# Only git, no defaults
sudo clawie runtime create alice --user alice --no-default-credentials --credential-bundle git
```

## Credential management after creation

```bash
# View credential policy
clawie agent credentials show alice

# Add a bundle
clawie agent credentials set alice git --include-defaults

# Sync credentials to agent home
sudo clawie agent credentials sync alice

# Sync from a specific source
sudo clawie agent credentials sync alice git --source-home /home/admin

# Revoke access
sudo clawie agent credentials revoke alice git
```

Revoking removes the credential files from the agent home and updates the policy so they stay revoked until re-enabled.

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
