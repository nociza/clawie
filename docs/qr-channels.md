# WeChat and WhatsApp

Clawie provides one guided, fail-closed journey for OpenClaw channels that use
a live QR code. Run these commands in an interactive terminal on the gateway
host. A non-interactive shell exits before installing a plugin or changing
credentials, services, or Clawie state.

## WeChat

```bash
sudo clawie channel wechat setup bidao
```

Clawie installs Tencent Weixin's maintained
`@tencent-weixin/openclaw-weixin` package into only the agent's private
OpenClaw home, records the exact resolved version, enables it, and starts QR
login. Scan with WeChat and confirm on the phone. The integration supports
direct chats and media; the plugin does not advertise group-chat support.

## WhatsApp

```bash
sudo clawie channel whatsapp setup whatsclaw
```

Clawie installs the official `@openclaw/whatsapp` package, pins the resolved
version, and starts WhatsApp Web's QR login. A separate assistant number is
recommended so ordinary messages to a personal account do not all become
agent inputs.

## Pair the sender

Both channels default to OpenClaw's secure pairing policy. After the account is
connected, send it a direct message and approve the displayed code:

```bash
sudo clawie channel wechat pairing-list bidao
sudo clawie channel wechat pairing-approve bidao CODE

sudo clawie channel whatsapp pairing-list whatsclaw
sudo clawie channel whatsapp pairing-approve whatsclaw CODE
```

Then prove the complete listener path:

```bash
sudo clawie channel wechat status bidao
sudo clawie channel whatsapp status whatsclaw
```

Status exits nonzero unless the plugin is installed and enabled and the
channel is configured, running, connected, and passing its live probe. Account
identifiers and credentials are not emitted by the stable status contract.

## Failure and recovery

- A missing TTY fails before mutation.
- A failed plugin install triggers a scoped uninstall attempt and never assigns
  Clawie channel ownership.
- A QR cancellation leaves an inactive pinned plugin but no Clawie channel
  assignment; rerun setup when ready.
- If login succeeds but postflight health is slow, credentials are preserved
  to avoid silently unlinking the phone. Run `status`, restart only that agent,
  and rerun setup only if status recommends relinking.
- Clawie serializes channel setup per managed home so two onboarding commands
  cannot overwrite one another.
