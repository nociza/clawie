# Delegation & Orchestration

clawie includes a recursive task delegation system that lets agents hand off
work through private Unix sockets for same-user session workers and through the
source-pinned runtime gateway for managed cross-user agents.

## How it works

```
Parent                           Child (REPL running)
  |                                |
  |-- task_submit  ------------>   |
  |                                |-- task_accepted -->
  |   <-- task_accepted ---------  |
  |                                |   (runs handler)
  |                                |   (may delegate to sub-children)
  |   <-- task_result/error -----  |
  |                                |
```

Agents communicate through a private, per-OS-user Unix socket directory:
`$CLAWIE_DELEGATION_DIR`, then `$XDG_RUNTIME_DIR/clawie/delegation`, or finally a
UID-scoped directory under the system temp root. Directories are `0700`, sockets
are `0600`, peer UIDs are checked where supported, messages are capped at 16
MiB, and each REPL admits at most 16 concurrent connections. A symlink-safe
file mailbox is available only as a same-user fallback; managed cross-user work
uses the runtime gateway.

Each managed runtime also receives a distinct request-only socket through
`CLAWIE_DELEGATION_SOCKET`. Clawie owns the containing runtime directory, makes
the socket `0600` for the agent's Linux UID, authenticates the peer UID, and
binds that endpoint to one agent ID. The endpoint accepts only recursive
delegation requests: callers cannot spoof their parent identity, confirm control
actions, or invoke generic service methods. A bounded daemon worker pool keeps
recursive requests responsive while the parent delivery is still waiting.

## Model tiers

Every delegation can specify a tier that controls the context budget and signals task complexity:

| Tier | Icon | Context Budget | Speed | Capability | Use for |
|------|------|---------------|-------|------------|---------|
| **fast** | ⚡ | 4K tokens | 9/10 | 4/10 | Status checks, lookups, validation |
| **balanced** | ⚖ | 16K tokens | 6/10 | 7/10 | Most tasks (default) |
| **power** | ⭐ | 64K tokens | 3/10 | 10/10 | Architecture, deep analysis, refactoring |

## CLI usage

### Delegate a task

```bash
clawie delegation submit \
  --parent planner \
  --child worker \
  --tier fast \
  --payload '{"task": "check status"}'
```

A session child must be running a REPL. A managed child with a verified live
gateway is delivered to directly. The command blocks until the result arrives
or the timeout expires.

If `--tier` is omitted, clawie selects `fast`, `balanced`, or `power` from the
task text and payload size. Managed tasks include their active task ID in the
child's task context. A child that recursively submits another CLI task should
preserve the lineage explicitly:

```bash
clawie delegation submit \
  --parent worker \
  --child researcher \
  --parent-task ACTIVE_TASK_ID \
  --payload '{"task": "investigate"}'
```

Inside a managed runtime the CLI automatically uses its request-only socket;
`--parent` remains syntactically required for a consistent command shape, but
the daemon derives the real parent from the authenticated socket binding.
Clawie can infer one unambiguous active task for compatibility, but explicit
`--parent-task` lineage is preferred. The SQLite reservation is atomic across
CLI and daemon processes, preventing concurrent calls from racing cycle, depth,
duplicate-target, or child-count checks.

### Start a REPL

Make an agent available to receive delegations:

```bash
clawie delegation repl --agent-id worker --executor-agent planner --tier balanced
```

`--executor-agent` is required and must name a managed agent with a live
gateway. A REPL without a real execution backend fails closed.

### Spawn session sub-agents

Lightweight sub-agents that run as detached local REPL processes from the CLI
(no root required):

```bash
clawie delegation spawn-session --parent planner --child researcher --tier power
clawie delegation spawn-session --parent planner --child formatter --tier fast

# Delegate to them
clawie delegation submit --parent planner --child researcher --payload '{"task": "analyze"}'

# List session agents
clawie delegation session-agents --parent planner

# Stop one
clawie delegation stop-session --parent planner --child formatter
```

The CLI records the child process and socket in clawie's state directory, so a
session agent spawned by one command can be listed, delegated to, and stopped by
later commands. In the Python API, `SessionAgentManager` keeps the same behavior
as before: sub-agents run as background threads inside the current process and
stop when that process exits unless you stop them explicitly.

### View the delegation tree

```bash
clawie delegation tree --agent-id planner
```

Output includes tier icons and status:

```
⭐ planner  [running] d=0
├── ⚡ formatter  [completed] d=1
└── ⭐ researcher  [running] d=1
```

### Other commands

```bash
clawie delegation status          # Active REPL agents
clawie delegation tasks           # Task history
clawie delegation cleanup         # Remove stale sockets
```

## Context budgets

Each tier gets a token budget. The system persists estimated payload and result
tokens, records a warning event at 75%, and records a
`delegation.context_compaction_required` event at 90%. An input larger than the
selected budget is rejected before delivery. Clawie exposes compaction pressure
without silently truncating, summarizing, or rewriting a result.

**Summarization depth rules:**

| Depth | Detail level |
|-------|-------------|
| 0 (root) | Full detail |
| 1 | 1-2 paragraphs |
| 2 | 3-5 bullet points |
| 3+ | Single sentence |

**Budget rules:**
- Never allocate >50% of remaining budget to a single child
- For N children, budget each at `remaining / (N + 1)`
- Reserve 25% for aggregating results

## Automatic tier recommendation

The managed CLI/service path calls `recommend_tier()` when `--tier` is omitted.
The same function is available to Python callers:

```python
from clawie.delegation import recommend_tier

recommend_tier("check status", {})              # -> "fast"
recommend_tier("analyze codebase", {})          # -> "power"
recommend_tier("generate report", {})           # -> "balanced"
recommend_tier("simple task", {"data": "x"*40000})  # -> "power" (large payload)
```

## Python API

### Single delegation

```python
from clawie.delegation import DelegationCoordinator

coord = DelegationCoordinator("planner", model_tier="power")
result = coord.delegate("worker", {"task": "analyze"}, model_tier="fast")
```

### Parallel fan-out

```python
results = coord.delegate_many([
    {"child_id": "w1", "payload": {"task": "part-a"}, "model_tier": "fast"},
    {"child_id": "w2", "payload": {"task": "part-b"}, "model_tier": "power"},
], timeout=120.0)
```

### Custom REPL handler

```python
from clawie.delegation import AgentREPL, Message

def handler(msg: Message, repl: AgentREPL) -> dict:
    print(f"Tier: {repl.model_tier}, Budget left: {repl.context_budget.tokens_remaining}")
    return {"result": "done"}

repl = AgentREPL("worker", handler=handler, model_tier="fast")
repl.start()
```

### Recursive delegation from a handler

```python
def planning_handler(msg: Message, repl: AgentREPL) -> dict:
    sub = repl.delegate("leaf", {"task": "sub-work"}, depth=msg.depth + 1, model_tier="fast")
    return {"combined": sub}
```

### Session agents

```python
from clawie.delegation import SessionAgentManager

mgr = SessionAgentManager("my-agent")
mgr.spawn("worker-1", model_tier="fast")
mgr.spawn("worker-2", model_tier="power")

result = mgr.delegate("worker-1", {"task": "quick check"})

for line in mgr.tree_lines():
    print(line)

mgr.stop_all()
```

## Safety limits

- Max recursion depth: 10 by default; manifests can lower it per agent
- Max children per agent: 50
- Cycle detection prevents A -> B -> A loops
- A child can participate in only one active tree, preventing ambiguous lineage
- Abandoned active tasks expire after their timeout plus a short grace period
- Socket cleanup only removes dead socket entries owned within the private runtime directory

## Error handling

| Scenario | Behavior |
|----------|----------|
| Child not running | Connection error returned immediately |
| Child crashes | Connection error; task marked failed |
| Handler exception | `task_error` with exception message |
| Timeout | Timeout error; task marked failed |
| Depth at or beyond the configured limit | `ValueError` before delegation starts |
| Cycle detected | `ValueError` before delegation starts |
| Input exceeds selected budget | `ValueError` before delivery starts |
| Budget >= 90% after result | Persisted compaction-required event; result preserved |
