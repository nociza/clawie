# clawie control-plane redesign — comprehensive plan

> Status: **plan of record / proposed.** Nothing here is implemented yet. This
> document is the agreed target after a ground-up review of the current code.
> It supersedes ad-hoc decisions; phases at the end are the execution order.

## 0. How to read this

Sections 1–3 frame the goal and the principles. Sections 4–13 are the target
design, component by component. Section 14 lists what we deliberately keep.
Section 15 is the **roadmap** — the only section needed to start work. Section
16 tracks open questions.

Every design choice traces back to one of two things: a decision made during
review, or a finding from the code audit. Both are cited inline as
**(decision)** or **(audit: `file`)**.

---

## 1. What clawie is, and the goal

clawie is a **single-host control plane for a fleet of agent runtimes**.
**openclaw is the primary, first-class runtime**; hermes and others come later
**(decision)**. Around each runtime clawie owns provisioning, auth, channels,
delegation between agents, knowledge backup, and observability.

The goal of this redesign is to make four things true at once:

1. **It fully works with openclaw** — including agent-to-agent delegation that
   reaches a *real* agent, not an echo.
2. **A human drives the fleet by talking to one control agent**, which
   self-heals operational problems and escalates code bugs to this repo.
3. **The security posture matches the claims** — today it does not.
4. **Adding hermes is additive**, not surgery.

"Workspace" in this document means the unit clawie provisions and manages: an
agent's full runtime environment (identity + prompts + provider config +
channels + credentials + service + addons). Making that unit a first-class,
declarative artifact is the spine of the redesign.

---

## 2. Current-state assessment

### What is sound and must be preserved

- **Graceful status aggregation** — `status_snapshot` collects each section
  independently and degrades to `{"error": …}` instead of aborting.
- **Event sourcing** — every mutation appends a typed, capped event log.
- **Defensive sysadmin** — `sshd -t` validation before reload; SHA-512 password
  hashing with an `openssl` fallback that refuses silent downgrades.
- **Delegation IPC** — clean length-prefixed framing, cycle/depth detection,
  mailbox fallback, ASCII tree rendering.
- **Drift reconciliation instinct** — `_apply_live_provider_alignment` observes
  the live runtime and aligns state to reality instead of blindly overwriting.

### The cracks this plan closes

| # | Crack | Evidence |
|---|---|---|
| C1 | Delegation has **no brain connected** — every REPL handler echoes; the real LLM is the gateway daemon, which never listens on the delegation socket. Tiers/budgets are simulated (`model_id="fast"`, `len//4`). | `delegation.py:935`, `_service_agents.py:1516` |
| C2 | **Secrets are world-relaxed, systematically.** `_relax_shared_path_permissions` sets all three shared stores (provider-auth, addon-auth, toolchain) to 0o777 dirs / 0o666 files, then symlinks them into every agent home. OAuth access+refresh tokens are world read/write. | `_service_shared.py:167`, `:88` |
| C3 | **clawie patches a vendored binary** (`@anthropic-ai/claude-code/cli.js`, `mode:384`→`438`) to weaken file modes — guaranteed to break on upgrade and a security regression. | `_service_spawn.py:323` |
| C4 | **Prompt injection via world-writable `/tmp`.** Staged prompts (0o666 in a 0o777 dir) are copied into an agent's workspace as core prompts by maintenance. | `_service_prompts.py:345` |
| C5 | **Isolation claim is false.** README says "no agent sees another's secrets," but the shared-store happy path gives all agents one upstream identity, world-readable; the `git` bundle copies the manager's `.ssh`/`.git-credentials` into agent homes. | `README.md:17`, `service.py` `CREDENTIAL_BUNDLE_SPECS` |
| C6 | **Imperative orchestration with hand-rolled rollback** — `switch_agent_provider` is a ~250-line saga; spawn is a long imperative sequence. Correct today, fragile on the next edit. | `_service_agents.py:241` |
| C7 | **State accretion** — `users`/`agents` duality in the hot path (~40×), `ZeroClawService` naming, version skew (`__init__` 0.1.1 vs pyproject 0.1.3). | `store.py`, `service.py:51` |
| C8 | **Provider schema knowledge is smeared**, and auth coupling spans three external formats with no seam. | `_service_agents.py:594`, `auth_sources.py` |
| C9 | **Backup restores knowledge, not a runnable agent**; **resource telemetry is thin** (pid-based, usually 0). | `_service_backup.py:390`, `_service_telemetry.py:127` |

---

## 3. Design principles

1. **The workspace manifest is the source of truth.** Everything else is
   derived or reconciled from it. SQLite becomes a rebuildable cache.
2. **Reconcile, don't orchestrate.** Replace imperative multi-step sagas with
   `reconcile(desired, observed)` that converges and is safe to re-run.
3. **Providers are adapters.** openclaw is the reference adapter; core knows no
   provider's file schema. Adding hermes is a new file.
4. **Trust precedes autonomy.** The security fixes (Phase 2) gate the control
   agent (Phase 4). Never hand an LLM root + repo-write while tokens are
   world-writable.
5. **Gate in code, not in the prompt.** Capability limits and confirmations are
   enforced by the tool/RPC layer, because prompt-only boundaries fail under
   injection.
6. **Notify on version drift; don't chase durability.** Detect unknown openclaw
   versions, degrade to read-only, and tell the human **(decision)**. We accept
   that openclaw upgrades can break clawie as long as the user is warned.
7. **Least privilege at the one chokepoint.** All cross-user mutation flows
   through `_can_manage_linux_user` (root-or-same-user); the control agent gets
   a scoped sudoers allowlist, never blanket root.

---

## 4. Target architecture

```
                 ┌──────────────────────────── human ───────────────────────────┐
                 │  (channel: Telegram/CLI, allowFrom allowlist)                  │
                 ▼                                                                │
        ┌───────────────────┐   nonce-confirmed                                  │
        │   CONTROL AGENT    │   destructive/outward                             │
        │  (role: control,   │──────────────┐                                    │
        │  openclaw runtime) │              ▼                                    │
        └─────────┬──────────┘     ┌──────────────────┐                          │
       control-tool RPC            │   clawied         │  single writer of state │
       (capability-tiered,         │  (control-plane   │  + reconcile loop       │
        code-enforced)──────────▶  │   daemon)         │  + audit (event log)    │
                                   └───────┬──────────┘                          │
                                           │ reconcile(desired, observed)        │
                 ┌─────────────────────────┼──────────────────────────────┐     │
                 ▼                         ▼                              ▼      │
        ProviderAdapter(openclaw)   Workspace manifests           Shared services│
        - render_config             (one per agent, on disk =     - per-agent     │
        - auth sub-seam              source of truth)               secrets (0600) │
        - addon injection                                          - toolchain     │
        - start/stop/readiness                                     - backup repo   │
        - gateway_endpoint                                         - watchdog      │
        - deliver() ───────────────▶ live openclaw gateway ◀── managed agents ────┘
                                       (per-agent endpoint)        (delegation tree
                                                                    rooted at control)
```

Components:

- **Workspace manifest** (§5) — the declarative unit.
- **ProviderAdapter** (§6) — the only place that knows a runtime's internals.
- **Delegation bridge** (§7) — `deliver()` into the live gateway.
- **Security/trust model** (§8) — per-agent secrets; no world-relaxed stores.
- **Control agent** (§9) — the human's interface and self-healer.
- **clawied** (§13) — single-writer daemon hosting reconcile + the control-tool
  RPC surface + the audit log; resolves the SQLite concurrency problem.

`clawied` is the larger structural bet. Phases 0–2 do **not** require it; it
lands with the reconcile/manifest work (Phase 3) and is the natural home for the
control-tool gates (Phase 4).

---

## 5. The workspace manifest (the unit)

Each agent is a directory with a declarative `agent.toml` describing it fully:

```toml
id          = "alice"
role        = "worker"          # or "control"
provider    = "openclaw"
model_tier  = "balanced"

[prompts]                       # by reference; content lives in files
dir = "prompts/"                # SOUL.md, IDENTITY.md, AGENTS.md, ...

[[channels]]
kind = "telegram"
name = "alice-tg"
allow_from = ["@you"]

[credentials]
provider_auth = { ref = "codex:alice", scope = "agent" }   # not "shared" by default
bundles       = []              # explicit opt-in only

[addons]
display = { enabled = false }

[limits]
delegation_depth = 10
gateway_timeout  = 300
```

Properties this buys:

- **Source of truth on disk.** SQLite/`clawied` cache is rebuildable from the
  manifests; the `users`/`agents` duality (C7) disappears.
- **Reproducible agents.** Re-provision = read manifest → reconcile.
- **Backup that restores a runnable agent** (§11), not just notes (C9).
- **Diff-able changes.** "Switch provider" = edit `provider`, reconcile —
  retiring the saga (C6).

The on-disk layout (`prompts/`, captured `workspace/` knowledge) mirrors what
backup already collects, so backup becomes "commit the manifest tree."

---

## 6. Provider adapter contract

One interface; openclaw is the reference implementation. Anything a future
hermes would override lives here; everything else stays in core.

```python
class ProviderAdapter(Protocol):
    name: str
    def detect_version(self) -> Version | None: ...
    def supported_range(self) -> tuple[Version, Version]: ...

    # provisioning (render only the minimal keys; let the runtime own defaults)
    def render_config(self, ws: Workspace) -> dict[Path, bytes]: ...
    def render_addon_integration(self, ws, addon, ctx) -> dict[Path, bytes]: ...

    # auth sub-seam (see below)
    def write_provider_auth(self, ws, profiles: list[AuthProfile]) -> None: ...

    # lifecycle
    def start(self, ws) -> ServiceHandle: ...
    def stop(self, ws) -> None: ...
    def readiness(self, ws) -> Readiness: ...
    def process_signature(self, ws) -> str: ...   # for liveness detection

    # the bridge
    def gateway_endpoint(self, ws) -> Endpoint: ...
    def deliver(self, ws, task: Task, *, timeout: float) -> Reply: ...

    def tier_to_model(self, tier: str) -> str: ...
```

### Auth sub-seam (audit: `auth_sources.py`)

Auth couples to **three** external formats. Split it so each side is a small,
version-pinned unit:

- **Upstream credential sources** — readers for `codex/auth.json` (with JWT
  expiry decode) and `claude/.credentials.json`. These are *not* openclaw; they
  are the model backends. One module, contract-tested.
- **Provider auth-store writer** — drives openclaw's own auth surface, **not**
  hand-written JSON. As of openclaw 2026.6.2, auth lives in each agent's
  `openclaw-agent.sqlite`; the `auth-profiles.json` files and `openai-codex`
  profile ids clawie writes today are **legacy migration input** that `openclaw
  doctor --fix` rewrites to the canonical `openai` route. So
  `write_provider_auth` should call `openclaw models auth login/paste-token/order`
  (and use SecretRef `keyRef`/`tokenRef` for static keys) instead of writing the
  deprecated JSON. **This is a live version-drift bug to fix in Phase 0**, not a
  future risk. hermes drives its own auth CLI.

`render_config`, `render_addon_integration`, and `write_provider_auth` together
hold *all* of openclaw's schema knowledge currently smeared across
`_service_agents.py` and `_service_addons.py` (C8, audit: `_service_addons.py`).

### Version gate (decision: notify-on-upgrade)

`detect_version()` + `supported_range()` drive policy: on an **untested**
version, the adapter **degrades that agent to read-only** (no config writes) and
raises a typed `UnsupportedVersion` that surfaces as a status warning and a
control-agent notification (§9, §10). We never silently write a stale schema
over a working agent.

---

## 7. Delegation bridge (close the loop)

**Receiving side stops echoing and calls the adapter into the live gateway**
**(decision: live gateway endpoint).**

```
parent ──`clawie delegation submit --child c`──▶ clawied
   DelegationCoordinator (keep: tree, depth/cycle, task record, budget)
     └─ resolve c → OpenclawAdapter.deliver(ws_c, task, timeout)
          └─ connect c's gateway endpoint (request/response)
             gateway runs task in c's persistent context, returns reply (+usage)
   ◀─ reply ─────────────────────────────────────────────────────────────────
```

Design details (verified against openclaw 2026.6.2 — see Appendix A):

- **The endpoint is a loopback port, not a socket clawie invents.** The gateway
  is one always-on process exposing a single multiplexed port (default `18789`,
  `gateway.bind: "loopback"`) that serves both a WebSocket control/RPC plane and
  OpenAI-compatible HTTP. `render_config` assigns a **unique `gateway.port` per
  agent** (the way clawie already allocates VNC ports for the display addon) and
  `gateway_endpoint()` returns `127.0.0.1:<port>`.
- **`deliver()` has three fidelity levels, all real:** (1) **native WS RPC**
  (primary) — `connect` → `hello-ok`, then the `agent` RPC paired with
  `agent.wait`; runs are two-stage (`accepted` ack → streamed `agent` events →
  final `ok|error`), mapping **1:1 onto clawie's existing
  `task_accepted`→`task_result` protocol**, and openclaw explicitly recommends
  this path for external apps; (2) **OpenAI HTTP** (fallback) — `POST
  /v1/responses`, model `openclaw/<agentId>`, `Authorization: Bearer`; (3) **CLI**
  (bootstrap) — `openclaw agent --agent <id> --session-key
  agent:<id>:clawie:<task_id> --message <payload> --json --timeout <n>`.
- **Auth to the gateway.** `render_config` provisions `gateway.auth.mode="token"`
  + a **per-agent `gateway.auth.token`** (or `OPENCLAW_GATEWAY_TOKEN` in
  `~/.openclaw/.env`). On loopback a same-host backend client authenticating with
  that token uses openclaw's reserved internal control-plane path and skips device
  pairing. That token is a **full-operator** credential → a per-agent 0o600 secret
  under Phase 2, never a shared/world-relaxed file.
- **Session-scoped, never a heartbeat.** Each delegation runs in a dedicated
  session key (`agent:<id>:clawie:<task_id>`) so it doesn't pollute the human
  channel history and isn't mistaken for a `HEARTBEAT_OK` poll.
- **Budgets become real.** openclaw reports usage/cost via `sessions.usage` /
  `usage.cost`; budgets consume that instead of the `len//4` heuristic, and tier
  → model uses real ids (`openai/gpt-5.4`, not the legacy `openai-codex/*`) (C1).
- **Echo REPL is demoted** to a `loopback` adapter for tests and the no-gateway
  dev path, preserving the existing delegation test suite.
- **openclaw already has recursive primitives.** Its `tasks.*` ledger
  (`parentTaskId`, `childSessionKey`, `flowId`) and native subagents are a
  delegation model clawie's tree can ride on or converge with later.
- **The bridge is forward-durable** via protocol negotiation (§10), unlike the
  config-write surface.

---

## 8. Security & trust model — the gating phase

This is the load-bearing section; Phase 4 must not ship before it.

| Fix | From → To | Source |
|---|---|---|
| **Stop world-relaxing stores** | `_relax_shared_path_permissions` (0o777/0o666 on provider-auth, addon-auth, toolchain) → per-agent links/copies at **0o600**, or a 0o700 broker process that hands tokens to the agent user only | audit: `_service_shared.py:167` (C2) |
| **Per-agent auth by default** | one shared upstream identity for all → each agent references its own profile (`scope="agent"`); shared is explicit opt-in | C5 |
| **Remove the binary patch** | rewriting `claude-code/cli.js` modes → per-agent `CLAUDE_CONFIG_DIR` with 0o600 credentials | `_service_spawn.py:323` (C3) |
| **Close the `/tmp` inject vector** | staging at 0o666 in 0o777 `/tmp` dir, picked up as core prompts → stage inside the manageable boundary (agent-owned 0o700), validate provenance/ownership before apply | `_service_prompts.py:345` (C4) |
| **Scope the git bundle** | copying the manager's `.ssh`/`.git-credentials` into agent homes → per-agent deploy keys / no implicit identity copy | C5 |
| **Reconcile the claims** | README "no agent sees another's secrets" → make it true, or state the real model plainly | `README.md:17` |
| **Control-plane token** | (new) the control agent's GitHub token is a dedicated **0o600** secret **outside** every shared/relaxed store; never a credential bundle or addon | §9, C2 |

The single enforcement chokepoint, `_can_manage_linux_user` (root-or-same-user,
`_service_shared.py:272`), is where the control agent's **scoped sudoers
allowlist** (specific `clawie`/`clawied` verbs, not all) is wired — so even a
hijacked control agent cannot `userdel` the fleet.

openclaw's own guidance reinforces the model: operator scopes are "a guardrail
inside one trusted operator domain, **not hostile multi-tenant isolation**… run
separate Gateways under separate OS users or hosts" for strong separation.
clawie's one-gateway-per-Linux-user design **is** that separation — the OS-user
boundary is the real isolation; gateway scopes are the intra-agent guardrail.
Phase 2 must additionally treat each agent's `gateway.auth.token` as a per-agent
0o600 secret, since it is a full-operator credential.

---

## 9. Control agent

A privileged **`role: control` openclaw workspace** — not a new runtime. It
reuses the gateway, channels, prompts, delegation, and status. It sits at the
**root of the delegation tree** (human → control agent → fleet).

### Faculties (reuse existing machinery)

| Faculty | Mechanism |
|---|---|
| Senses | `clawie status --json`, `daemon.log`, the drift detector |
| Hands | restart / re-apply prompts / re-sync auth / backup / create-clone via control-tool RPC |
| Voice | its channel (`allowFrom` allowlist) |
| Escalation | file issue / open PR to `nociza/clawie` |

### Authority: diagnose-and-confirm (decision), enforced in code

Each control verb carries a fixed **capability tier**, checked by the
clawied RPC layer (not the prompt):

| Tier | Verbs | Behavior |
|---|---|---|
| `read` | status, logs, list, tree | autonomous, logged |
| `safe-heal` | restart service, re-apply prompts, re-sync auth, backup run | autonomous, logged |
| `destructive` | delete/purge, credential set/revoke, provider switch | **pending-confirmation object**; resolves only on a **nonce echoed by an allowlisted human** |
| `outward` | open issue / open PR | **preview (issue body / diff) → human approval** before submit |

A destructive/outward call cannot execute without the confirmation token,
regardless of what the model "decided" — this is the prompt-injection defense
(Principle 5). Nonce-based confirmation prevents a poisoned log line or stray
"yes" from self-approving.

**openclaw enforces a second layer.** Connect the control agent to each gateway
with a **scoped device token** — `operator.read` to diagnose, `operator.write`
for safe-heal — and **withhold `operator.admin`**. openclaw then refuses
`config.*`, `update.*`, and `exec.approvals.*` at the gateway no matter what the
model attempts, independent of clawie's code-enforced gates. Caveat: a **shared
gateway token/password is full-operator scope** (openclaw honors narrower scopes
only for device-token/identity-bearing connects), so the control agent must use a
scoped device token, not the shared secret.

### Repo escalation (decision)

- **Two-tier "fix bugs":** operational issues → self-heal; **code bugs (incl.
  the accepted openclaw-upgrade breakage) → escalate, never hot-patch the
  running control plane.**
- Artifacts carry openclaw+clawie versions, the failing op, `daemon.log` +
  event-log slices, and (for PRs) an adapter-scoped diff.
- **PRs from a branch, never auto-merge.** Issues **deduped by failure
  signature**, rate-limited.
- Token scope: issues + PRs on the one repo, no merge; stored per §8.

### Bootstrapping & audit

- A **dumb watchdog** (systemd unit, not an agent) keeps the control agent alive
  and pings the human **out-of-band** if the control agent itself is down.
- Every control action routes through the **event log** for an audit trail.
- **DR-aware:** the control agent knows backup is knowledge-only until Phase 3
  graduates it (§11) and says so when proposing a restore.

---

## 10. Forward compatibility & versioning (decision)

Policy: **detect, degrade, notify** — not heroic durability.

1. `OpenclawAdapter.detect_version()` runs on every reconcile.
2. Outside `supported_range()` → that agent goes **read-only**; clawie stops
   writing its config.
3. The control agent delivers the notification on the control channel
   ("openclaw upgraded to X, untested; I paused writes to 3 agents; file an
   issue?") and can open the escalation issue.

This is where the **notify-on-upgrade** decision, the **version gate** (§6), and
the **control agent** (§9) converge. Contract tests per adapter (Phase 5) turn a
breaking upgrade into a CI failure rather than a production surprise.

The **delegation bridge specifically is forward-durable**: openclaw's WS protocol
is versioned (`PROTOCOL_VERSION`, currently `4`); `connect` negotiates
`minProtocol`/`maxProtocol`, the server rejects out-of-range, and
`hello-ok.features.methods/events` is a capability-discovery list. The adapter
declares the protocol range it speaks and degrades+notifies on mismatch.
openclaw's docs literally tell integrators to "pin the version you test against"
and "recheck on upgrade" — notify-on-upgrade is openclaw's own recommended
practice. The fragile surface is the **config/auth file write**, which is why
Phase 0 moves auth to the `openclaw models auth` CLI.

---

## 11. Backup & disaster recovery

Today: prompts + `.md`/`memory/` workspace notes, secrets excluded, and
`restore` requires the agent to already exist in local state — i.e.
**knowledge-only** (C9, audit: `_service_backup.py:390`).

Target: because the **manifest** (§5) fully declares an agent, the backup repo
becomes the manifest tree (`agent.toml` + `prompts/` + captured `workspace/`).
Restore = drop manifests → reconcile → runnable fleet (creds re-supplied
separately and still never committed). Until that lands, the control agent
states the limitation explicitly. Full-fidelity local `backup export` (with
secrets, 0o600) stays as-is.

---

## 12. Observability & telemetry

- **Status is the senses** and already degrades gracefully — keep it.
- **Fix resource telemetry (C9):** `collect_metrics` keys CPU/mem off a stored
  `pid` that is usually 0; real liveness comes from `process_signature()` /
  `/proc`. Until reworked, the control agent's self-heal triggers on
  **liveness/auth/drift**, **not** on "CPU pegged." Make resource-based triggers
  a Phase 5 item with a real metrics source.

---

## 13. State, concurrency & `clawied`

The control agent makes an existing latent bug acute: SQLite is single-writer
with default journaling, but a root maintenance cron, the human CLI, and the
control agent can all write `~/.clawie/clawie.db` at once (audit:
`requirements.md` admits this).

**Target:** a long-running local **`clawied`** daemon is the **single writer**.
It owns the DB, runs the reconcile loop, hosts the capability-tiered control-
tool RPC (§9), and writes the audit log. CLI and control agent become clients.
This resolves concurrency, gives the reconcile loop a home, and is the clean
place for the code-enforced gates.

**Interim (before clawied):** WAL + `busy_timeout` + an advisory file lock, and
route the control agent's mutations through a single serialized path.

---

## 14. What we explicitly preserve

- `status_snapshot` graceful degradation and the `--json`/`--watch` surface.
- The event-sourced audit log (now also backing control-agent audit).
- Password hashing safety, `sshd -t` pre-validation, archive path-traversal
  guards (`_extract_tarball_safe`).
- The delegation IPC framing and tree (repurposed as the `loopback` adapter).
- The drift-alignment instinct (`_apply_live_provider_alignment`) — generalized
  into the reconcile loop.

---

## 15. Roadmap

Each phase is independently valuable. **Phase 2 gates Phase 4.**

### Phase 0 — Provider adapter seam + version gate
Extract `ProviderAdapter`; move openclaw config-render / CLI verbs / readiness /
addon injection / auth-store writer out of `_service_agents` + `_service_addons`
into `OpenclawAdapter`. Add `detect_version` + `supported_range` →
degrade-to-read-only + warn. **No behavior change; unblocks everything; delivers
notify-on-upgrade.** Split the upstream credential sources (codex/claude) from
the provider writer. **Also fix the live drift:** stop writing legacy
`auth-profiles.json` / `openai-codex` ids — drive `openclaw models auth` + SQLite
— and have `render_config` assign a per-agent `gateway.port` + `gateway.auth.token`
so Phase 1 has an endpoint to talk to.

### Phase 1 — Real delegation bridge
Implement `OpenclawAdapter.deliver()` over the loopback WS `agent` + `agent.wait`
RPC (OpenAI `/v1/responses` fallback; `openclaw agent --json` CLI bootstrap), each
delegation in its own session key; rewire `DelegationCoordinator` to call it for
managed agents; demote echo REPL to `loopback`; feed real usage from
`sessions.usage` into budgets. **Delivers the headline.** (Needs 0.)

### Phase 2 — Trust (GATING)
De-relax all three shared stores; per-agent secrets at 0o600 (or 0o700 broker);
remove the cli.js patch (per-agent `CLAUDE_CONFIG_DIR`); close the `/tmp`
staging vector; scope the git bundle; reconcile the README claims; per-agent
auth default. **Load-bearing; must precede Phase 4.** (Independent of 0/1.)

### Phase 3 — Workspace manifest + reconcile + `clawied`
Define `agent.toml`; build `reconcile(desired, observed)` to retire the
provider-switch saga and the spawn imperative; stand up `clawied` as single
writer + reconcile host (resolves concurrency); graduate backup to manifest-
based runnable restore; retire `users`/`agents` duality, the `ZeroClawService`
name, and the version skew. (Needs 0; benefits 1.)

### Phase 4 — Control agent
`role: control` workspace; control-tool RPC with code-enforced capability tiers
+ nonce confirmation; autonomous safe-heal; confirmed destructive + outward;
scoped GitHub integration (issues + PRs-from-branch, never auto-merge,
dedupe/rate-limit); 0o600 repo token outside shared stores; systemd watchdog +
out-of-band alerting; full audit; delivers version-drift notices; DR-aware. Scope
the control agent's gateway device token to `operator.read`/`write` without
`operator.admin`, so openclaw blocks config/update mutations as a second layer.
(Needs 0,1,3; **gated by 2**.)

### Phase 5 — Hardening & extensibility
Add **hermes** as the second adapter (proves the seam); contract tests per
adapter in CI; real resource-metrics source for the control agent; split the
3,145-line `cli.py`; give the mixins real boundaries. (Needs the seam.)

```
0 ──▶ 1 ──▶ 3 ──▶ 4 ──▶ 5
            ▲      ▲
2 ──────────┴──gates┘
```

---

## 16. Risks & open questions

- **openclaw gateway protocol (Phase 1): RESOLVED** (Appendix A). Loopback WS RPC
  (`agent` + `agent.wait`) / OpenAI HTTP (`/v1/responses`) / CLI (`openclaw
  agent`), token auth, protocol v4. Remaining detail: the exact `agent` WS param
  schema (`packages/gateway-protocol/src/schema.ts`, `/reference/rpc`) and how
  clawie provisions + rotates the per-agent gateway token and the control agent's
  scoped device token.
- **clawied scope (Phase 3):** introducing a daemon is the biggest structural
  bet. If deferred, the interim WAL+lock path must hold under the control agent.
- **hermes interface (Phase 5):** unknown until its runtime exists; the seam in
  §6 is our best current guess and may need a method added.
- **Per-agent auth UX:** moving off the shared store improves isolation but adds
  per-agent login friction; the control agent should streamline it.
- **Migration:** existing fleets carry the `users`/`agents` shape and shared
  stores; Phase 2/3 need a one-time migration that doesn't strand live agents.

---

## Appendix A — openclaw integration facts (verified)

Verified by reading `github.com/openclaw/openclaw` at commit `e9bd90d2`
(openclaw `2026.6.2`). Primary sources: `docs/gateway/*`, `docs/cli/agent.md`,
`packages/gateway-protocol`.

**Gateway runtime**
- One always-on process, single multiplexed port. Default `18789`; bind default
  `loopback`. Port precedence `--port` → `OPENCLAW_GATEWAY_PORT` → `gateway.port`
  → `18789`. `gateway.mode=local` required to start.
- Same port serves WS control/RPC **and** HTTP: `/v1/models`, `/v1/embeddings`,
  `/v1/chat/completions`, `/v1/responses`, `/tools/invoke`.
- `/v1/models` is agent-first: returns `openclaw`, `openclaw/default`,
  `openclaw/<agentId>`.

**Delivering a task to an agent (the bridge)**
- Native WS: `connect` → `hello-ok`; `agent` RPC + `agent.wait`; two-stage
  `accepted` → streamed `agent` events → final `ok|error`; `deliver=true` for
  outbound, `result.deliveryStatus` reported.
- HTTP: `POST /v1/responses`, model `openclaw/<agentId>`.
- CLI: `openclaw agent --agent <id> --session-key <key> --message <text> --json
  --timeout <n>` (session keys are `agent:<id>:<key>`).
- Sessions/tasks: `sessions.*` (durable threads); `tasks.*` ledger with
  `parentTaskId`/`childSessionKey`/`flowId`; native subagents.

**Auth**
- Gateway connection: `gateway.auth.mode` ∈ `token|password|trusted-proxy|none`;
  `gateway.auth.token`/`OPENCLAW_GATEWAY_TOKEN` or `gateway.auth.password`/
  `OPENCLAW_GATEWAY_PASSWORD`; HTTP uses `Authorization: Bearer`. Loopback still
  requires auth by default; a same-host `gateway-client` backend may skip device
  pairing with the shared token.
- Operator scopes: `operator.read|write|admin|approvals|pairing|talk.secrets`.
  `config.*`/`update.*`/`exec.approvals.*` require `operator.admin`. **Shared-
  secret bearer = full operator scope**; narrower scopes are honored only for
  device-token/identity-bearing connects.
- Model-provider auth now lives in each agent's `openclaw-agent.sqlite`.
  `auth-profiles.json`, `auth-state.json`, and `openai-codex` ids are **legacy**,
  migrated by `openclaw doctor --fix`. Preferred: `openclaw models auth
  login/paste-token/order`, `~/.openclaw/.env` API keys, Claude-CLI reuse, and
  SecretRef `keyRef`/`tokenRef`.

**Protocol versioning**
- `PROTOCOL_VERSION = 4` (`packages/gateway-protocol/src/version.ts`); `connect`
  sends `minProtocol`/`maxProtocol`; server rejects out-of-range;
  `hello-ok.features.methods/events` = capability discovery. Docs instruct
  integrators to pin the tested version and recheck on upgrade.

**Isolation**
- openclaw: operator scopes are an intra-domain guardrail, "not hostile
  multi-tenant isolation… run separate Gateways under separate OS users or hosts"
  for strong separation — validating clawie's one-gateway-per-Linux-user model as
  the real boundary.
