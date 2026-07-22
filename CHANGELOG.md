# Changelog

All notable changes to clawie are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## 0.1.9

Security and robustness fixes from a full audit. The exact 0.1.9 wheel recorded
in the [nw2-clawies production proof](docs/proofs/production-verify-nw2-clawies-systemd-wheel-0.1.9-2026-07-21.md)
passed live delivery, watchdog restart, host isolation, and cleanup. Other
artifacts and deployment hosts still require their own production verification.

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
- **Status no longer overstates linked-auth readiness.** Health output now says
  that the auth mode is configured and directs users to the separate credential
  readiness result instead of implying that a linked session exists.
- **Agent creation output distinguishes uninspected state.** A newly defined
  agent reports auth as `not checked` and labels the channel value as its
  discovery source, avoiding contradictory output between `create` and `show`.
- **Agent cloning is non-destructive by default.** `agent clone` now mints new
  channel names unless `--channel-strategy migrate` is explicit. Migration help
  and output state clearly that matching channel ownership moves away from the
  source, and `fix-permissions` is no longer missing from agent command usage.
- **Re-selecting the current model tier is event-idempotent.** An explicit
  no-op no longer writes a misleading `agent.model_tier.changed` event.
- **Runtime installation works with pnpm 11.** Clawie now passes an explicit
  global executable directory and treats `PNPM_HOME` as the toolchain root,
  preventing pnpm from appending a second `/bin` and rejecting the install.
- **OpenClaw delivery uses GPT-5.6 Sol.** All current model tiers now request
  the explicit flagship identifier `openai/gpt-5.6-sol`; production evidence
  also records the exact delivery model and requires the imported Codex-linked
  account to be confirmed ready by OpenClaw's live CLI status surface.

### Docs

- Corrected `auth import` synopsis (`--from` is required).
- Clarified that production acceptance is version- and host-specific.
