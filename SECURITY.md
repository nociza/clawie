# Security policy

## Supported versions

Clawie is currently a Beta package. Security fixes are applied to the latest
`0.1.x` release and the default branch. Older releases are not supported.

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** flow in this repository's
Security tab. Include the affected version, deployment model, reproduction
steps, and impact. Do not put credentials, tokens, private agent content, or
unpatched vulnerability details in a public issue.

You should receive an acknowledgement within three business days. The project
will validate the report, coordinate a fix and disclosure timeline, and credit
the reporter when requested.

## Deployment boundary

Clawie relies on Linux users, private filesystem permissions, authenticated Unix
sockets, and a source-pinned OpenClaw adapter. This is same-host user isolation,
not a container, VM, or hostile multi-tenant boundary. A release or deployment
is not accepted until its exact artifact and host pass:

```bash
sudo clawie production verify \
  --exercise-watchdog-restart \
  --exercise-runtime-delivery \
  --all-provider-contracts \
  --json
```
