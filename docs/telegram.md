# Telegram

Clawie provides a complete, secret-safe Telegram onboarding and recovery flow
for managed OpenClaw agents. It writes the bot token to the agent's private
home, keeps OpenClaw on its secure pairing policy, restarts the gateway, and
requires a live Telegram API probe before setup succeeds. Setup is a guarded
transaction: it validates the bot identity before its first write, commits
Clawie ownership only after live health passes, and restores the prior files
and running/stopped service state if any later step fails.

## Set up a bot

Create the bot with BotFather, then run the guided setup from an interactive
terminal:

```bash
sudo clawie channel telegram setup teleclaw
```

The token prompt is hidden. Clawie never accepts the token as a positional
argument or option value, so it does not enter shell history or the process
list. The installed token lives at `.openclaw/telegram.token` in the managed
Linux user's home, with mode `0600`; `openclaw.json`, Clawie state, events, and
command output contain only the token-file path.

For automation, provide a private regular file or pipe from a secret manager:

```bash
sudo clawie channel telegram setup teleclaw --token-file /secure/path/telegram.token
secret-manager-read telegram-token | sudo clawie channel telegram setup teleclaw --token-stdin
```

The source file must be non-empty, no larger than 4096 bytes, have one hard
link, not be a symlink, and grant no group or world permissions. Clawie prints
an exact `chmod 600` repair when permissions are too broad.

Clawie refuses to change an already configured bot by default. To intentionally
replace it, supply the new token through a safe input source and add
`--replace`:

```bash
sudo clawie channel telegram setup teleclaw --token-file /secure/path/new-token --replace
```

The replacement token must pass Telegram's `getMe` check before Clawie touches
the existing token, configuration, service, or registry. A typo or revoked
token therefore cannot take a working bot offline. The health wait is bounded
to 0–300 seconds with `--wait-seconds`.

## Pair the first user

New bots use OpenClaw's `pairing` direct-message policy. This is intentional: a
working bot is not made public merely to simplify onboarding.

1. Send `/start` or any message to the bot.
2. List the pending user and pairing code:

   ```bash
   sudo clawie channel telegram pairing-list teleclaw
   ```

3. Approve that code:

   ```bash
   sudo clawie channel telegram pairing-approve teleclaw CODE
   ```

4. Send another message. The bot should now reply normally.

## Health and recovery

Start every investigation with Clawie's stable health gate:

```bash
sudo clawie channel telegram status teleclaw
```

It checks all four conditions that matter—configured, listener running,
account connected, and live Bot API probe—and exits nonzero unless all four
pass. Output is secret-free and includes the exact next command. `status` and
`pairing-list` are read-only with respect to Clawie state; neither performs a
hidden reconciliation. Use `--json` for monitoring.

If Telegram is not replying:

```bash
# 1. Prove runtime and Telegram API health.
sudo clawie channel telegram status teleclaw

# 2. Check whether this sender is still waiting for approval.
sudo clawie channel telegram pairing-list teleclaw

# 3. If status directs you to restart, use the managed service path.
sudo clawie agent service restart teleclaw

# 4. If the token is rejected, rerun setup with the corrected BotFather token.
sudo clawie channel telegram setup teleclaw --replace
```

Setup is idempotent. Rerunning it rewrites the same private token file,
reconciles the Telegram channel, restarts the managed gateway, and repeats the
live probe. A different token always requires `--replace`. Provider stderr is
deliberately not echoed because it can contain request URLs with credentials.
If an automatic rollback itself cannot restore the prior service, Clawie exits
nonzero, says that rollback was incomplete, and prints the safe status command
to run; it never claims success after a partial setup.
