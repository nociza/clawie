# Changelog

All notable changes to clawie are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## 0.1.9

Security and robustness fixes from a full audit. These change behavior on top of
the code proven in the 0.1.8 wheel proof, so 0.1.9 must be re-verified with
`clawie production verify` against its exact artifact before it is treated as
accepted on any host.

### Security

- **Virtual-display VNC is no longer exposed on all interfaces.** The `display`
  addon previously started `x11vnc` with `-listen 0.0.0.0` and no password, and
  `websockify` bound every interface — an unauthenticated remote desktop of the
  agent session. Both now bind localhost only; reach them through an SSH tunnel.
- **Spawn password hashes no longer pass through argv.** `_set_password_hash`
  used `usermod -p <hash>`, exposing the crypt hash via world-readable
  `/proc/<pid>/cmdline`. It now feeds the hash to `chpasswd -e` on stdin.

### Fixed

- **Upgrades from a pre-SQLite install no longer crash.** Legacy-JSON migration
  recursed (`ensure()` → `_migrate_legacy_json()` → `write_config()` →
  `ensure()`) until `RecursionError` on every command. The migration is now
  reentrancy-guarded and retires the legacy `config.json`/`state.json` files so
  they cannot be re-imported and clobber later changes.
- **`clawie status` exits nonzero on a corrupt/unreadable database.** SQLite
  corruption signatures (and a wholly unreadable store) are now treated as fatal,
  so `status --json` is a trustworthy monitoring gate as documented.
- **Delegation timeouts stop the work for real.** The delivery runner now launches
  the provider command in its own session and reaps the whole process group on
  timeout; previously only the `sudo` wrapper was killed and the provider-CLI
  grandchild kept running, duplicating side effects on retry.
- **Backup git calls are bounded.** Every `git` invocation (including the network
  `push`) now has a timeout, so a dead remote or hung filesystem can no longer
  wedge the maintenance daemon indefinitely.

### Docs

- Corrected `auth import` synopsis (`--from` is required).
- Clarified that production acceptance is version- and host-specific.
