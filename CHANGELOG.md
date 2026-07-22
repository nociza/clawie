# Changelog

All notable changes to clawie are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## 0.1.20

### Added

- **Guided WeChat and WhatsApp onboarding.** New `channel wechat` and
  `channel whatsapp` setup, status, pairing-list, and pairing-approve commands
  install version-pinned maintained plugins, require a real terminal for live
  QR login, recover an already-healthy saved login without relinking, and only
  commit channel ownership after live health passes.
- **Durable logical agent rename.** `agent rename` changes the Clawie identity
  while preserving the Linux sandbox, home, service, and credentials. Published
  workspace aliases keep historical immutable artifacts accessible after the
  rename.
- **Rollback-safe gateway credential rotation.** `agent service
  rotate-gateway-token` changes only the private loopback gateway credential,
  validates the restarted runtime before commit, restores the exact prior
  config and service state on failure, and never returns either token.

### Fixed

- Failed QR logins now restore the gateway's prior running/stopped intent, and
  an unlinked WhatsApp account points directly to fresh QR setup instead of an
  ineffective restart loop.
- New OpenClaw homes no longer acquire a streaming-only phantom Telegram
  channel; existing placeholders are ignored and safely removed during the
  next reconciliation.
- `workspace publish RELATIVE_PATH --agent AGENT` now resolves the path inside
  the selected agent's workspace as documented, while preserving traversal,
  symlink, special-file, and workspace-boundary checks.

### Security

- QR setup is serialized per managed home, fails before mutation without an
  interactive terminal, keeps provider errors secret-free, scopes plugin
  rollback, and retains the managed-user isolation boundary.
- Agent rename and published-workspace alias updates are transactional across
  SQLite state and the publication catalog, with compensation on partial
  failure.

## 0.1.13

### Fixed

- **Concurrent Telegram setup is now fail-closed.** A per-agent, cross-process
  lock serializes the complete preflight/write/restart/probe/commit transaction.
  A contending setup exits before token validation or any machine mutation and
  directs the operator to wait and rerun status. The lock is held on the
  already-open managed-home directory, so it creates no lock-file residue.

## 0.1.12

### Fixed

- **Telegram setup is now replacement-safe and rollback-safe.** Clawie validates
  a candidate token with Telegram before the first mutation, refuses bot
  identity changes unless `--replace` is explicit, and commits channel
  ownership only after the gateway and Telegram probe are healthy. Failures
  after mutation restore the exact prior token/configuration contents and the
  prior running or stopped service intent; a failed registry commit rolls back
  the live replacement as well.
- **Telegram channels no longer collide across agents.** Existing channel names
  are preserved, new names are agent-scoped, and ownership conflicts fail
  before mutation instead of silently moving another agent's channel.
- **Telegram diagnostics are observational.** `status` and `pairing-list` no
  longer trigger managed-agent reconciliation or state writes, and unhealthy
  token remediation now includes the required `--replace` confirmation.

### Security

- Bot API transport/provider failures remain token-redacted, token validation
  is bounded, source and destination files remain private and symlink-safe, and
  setup health waits reject negative, non-finite, or unbounded values.

## 0.1.11

### Added

- **Telegram now has a guided, production-safe onboarding and recovery path.**
  `clawie channel telegram setup` reads BotFather tokens only from a hidden
  prompt, private file, or stdin; installs a managed-user-owned `0600` token
  file; reconciles and restarts the OpenClaw agent; and requires configured,
  running, connected, live-probe health before succeeding. Dedicated `status`,
  `pairing-list`, and `pairing-approve` commands provide secret-free,
  actionable diagnostics for the full first-message journey.

### Security

- Telegram tokens are rejected on argv, never copied into Clawie state or
  events, and never echoed from provider stderr. File input rejects symlinks,
  extra hard links, oversized content, and group/world-readable modes.

## 0.1.10

### Fixed

- **OpenClaw Telegram `tokenFile` configurations now start correctly.** Clawie
  recognizes OpenClaw's private token-file setting, validates that it references
  a bounded, private, managed-user-owned regular file, and preserves the file
  reference instead of copying the bot token into `openclaw.json`. Newly managed
  bots also retain Telegram's secure pairing policy instead of being opened to
  every sender by default.
- **Independent Clawie installations no longer collide on gateway ports.** Port
  allocation now checks live host listeners as well as the current state store,
  and service startup requires a successful gateway RPC handshake before it is
  reported as running.

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
