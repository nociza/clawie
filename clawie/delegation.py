"""Recursive agent delegation system with Unix domain socket IPC."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import struct
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

DELEGATION_DIR = Path("/tmp/clawie-delegation")

# ---------------------------------------------------------------------------
# Default DELEGATION.md skill content -- auto-loaded for agents with the
# "delegation" plugin enabled.  Agents receive this as a core prompt so they
# know how to delegate work to other agents and participate in recursive
# delegation trees.
# ---------------------------------------------------------------------------

DELEGATION_SKILL_CONTENT = r"""# Delegation Skill

You have access to a **recursive task delegation system** that lets you hand
off work to other agents and receive results, to any depth.  This document
explains the concepts, model tiers, context management, the CLI commands,
and the REPL loop you can use.

---

## 1. Core Concepts

| Term | Meaning |
|---|---|
| **Parent agent** | The agent that initiates a delegation (you, when you delegate). |
| **Child agent** | The agent that receives and executes a delegated task. |
| **REPL** | A long-running loop on a Unix domain socket that listens for incoming tasks. An agent must be running a REPL to accept delegations. |
| **Delegation tree** | The hierarchy of parent → child relationships. Tracked to prevent infinite recursion (max depth 10) and cycles. |
| **Payload** | A JSON object describing what the child should do. |
| **Model tier** | The capability level assigned to an agent: fast, balanced, or power. Determines context budget and intended task complexity. |
| **Context budget** | The token budget allocated to an agent based on its tier. Tracks usage and triggers compaction warnings. |
| **Result** | A JSON object the child returns when the task is complete. |

### How It Works

```
Parent                           Child (REPL running)
  |                                |
  |-- task_submit  ------------>   |
  |                                |-- task_accepted -->
  |   <-- task_accepted ---------  |
  |                                |   (runs handler)
  |                                |   (may recursively delegate to sub-children)
  |   <-- task_result/error -----  |
  |                                |
```

Communication uses **Unix domain sockets** at `/tmp/clawie-delegation/<agent-id>.sock`
with a 4-byte length-prefixed JSON wire protocol.  If sockets are unavailable
(cross-user permissions), a **file-based mailbox** fallback at
`/tmp/clawie-delegation/<agent-id>/inbox/` is used.

---

## 2. Model Tiers

Each agent or delegation task can be assigned a **model tier** that controls
the capability level and context budget.

| Tier | Icon | Context Window | Budget | Speed | Capability | Best For |
|---|---|---|---|---|---|---|
| **fast** | ⚡ | 8K | 4K tokens | 9/10 | 4/10 | Status checks, lookups, validation, simple transforms |
| **balanced** | ⚖ | 32K | 16K tokens | 6/10 | 7/10 | Most tasks — summarization, code generation, moderate analysis |
| **power** | ⭐ | 128K | 64K tokens | 3/10 | 10/10 | Architecture, deep analysis, refactoring, large-payload tasks |

### When to Use Each Tier

- **fast**: The task can be answered in a few sentences, requires no deep reasoning,
  and the payload is small (<1K tokens). Examples: health checks, simple lookups,
  echoing status.
- **balanced** (default): The task requires moderate reasoning, produces a paragraph
  or two, and the payload is medium-sized. This is the default for most work.
- **power**: The task involves complex multi-step reasoning, large inputs, code
  review, architectural decisions, or comprehensive analysis.

### Setting Tiers via CLI

```bash
# Delegate with a specific tier
clawie delegation submit --parent p --child c --tier fast --payload '{}'

# Start a REPL with a tier
clawie delegation repl --agent-id worker --tier power

# Spawn a session agent with a tier
clawie delegation spawn-session --parent p --child c --tier fast

# Set tier on agent creation
clawie agents create my-agent --model-tier fast
```

---

## 3. Context Management

Each agent has a **context budget** based on its tier.  The budget tracks token
usage and prevents context rot in long delegation chains.

### Budget Thresholds

| Threshold | Trigger | Action |
|---|---|---|
| < 75% used | Normal | No action needed |
| ≥ 75% used | Warning | Summarize results more aggressively |
| ≥ 90% used | Compaction | Compact context — drop intermediate detail |

### 4 Rules for Preventing Context Rot

1. **Summarize at depth**: The deeper in the delegation tree, the shorter the result.
2. **Never forward raw results**: Always extract only the fields you need.
3. **Compact early**: If `needs_warning` is true, summarize before delegating more.
4. **Budget children**: Never allocate >50% of your remaining budget to a single child.

### Summarization Depth Rules

| Depth | Result Detail Level |
|---|---|
| 0 (root) | Full detail — complete results |
| 1 | 1–2 paragraphs — key findings and data |
| 2 | 3–5 bullet points — essential facts only |
| 3+ | Single sentence — one-line summary |

### Warning Signs of Context Rot

- Results are getting truncated or lost
- Child agents are returning errors about payload size
- The delegation tree is more than 4 levels deep with full results bubbling up

### Recovery Steps

1. Use `ContextBudget.compact(tokens_freed)` to reclaim space
2. Switch deeper children to the `fast` tier
3. Increase summarization aggressiveness at each depth level

---

## 4. CLI Commands

All delegation commands live under the `clawie delegation` subcommand group.

### Start a REPL (make yourself available for tasks)

```bash
clawie delegation repl --agent-id <your-agent-id> [--tier balanced]
```

This blocks and listens for incoming tasks.  Press **Ctrl+C** to stop.

### Delegate a task to another agent

```bash
clawie delegation submit \
  --parent <your-agent-id> \
  --child  <target-agent-id> \
  --tier fast \
  --payload '{"task": "summarize", "input": "..."}'
```

The child agent **must** already be running a REPL.  The command blocks until
the child returns a result or the timeout expires.

Options:
- `--timeout <seconds>` — max wait time (default 300).
- `--tier fast|balanced|power` — model tier for this task.

### View the delegation tree

```bash
clawie delegation tree --agent-id <root-agent-id>
```

Shows the hierarchy with tier icons: ⚡fast, ⚖balanced, ⭐power.

### List delegation task history

```bash
clawie delegation tasks [--agent-id <id>] [--status pending|running|completed|failed] [--limit 20]
```

### Check which agents are running REPLs

```bash
clawie delegation status
```

### Clean up stale sockets

```bash
clawie delegation cleanup
```

---

## 5. Parallel Delegation and Orchestration

### Automatic Tier-Based Orchestration

When decomposing a complex task, assign tiers based on sub-task complexity:

```python
from clawie.delegation import DelegationCoordinator, recommend_tier

coord = DelegationCoordinator("planner-agent", model_tier="power")

# Auto-recommend tiers for sub-tasks
tasks = [
    {"child_id": "w1", "payload": {"task": "check status"}, "model_tier": "fast"},
    {"child_id": "w2", "payload": {"task": "analyze codebase"}, "model_tier": "power"},
    {"child_id": "w3", "payload": {"task": "format output"}, "model_tier": "fast"},
]
results = coord.delegate_many(tasks, timeout=120.0)
```

### Context Budget Allocation Rules

- **Never allocate >50% of remaining budget to a single child**
- For fan-out with N children, budget each child at `remaining / (N + 1)` tokens
- Reserve at least 25% of budget for aggregating results

### Fan-Out with Mixed Tiers

Use `recommend_tier(description, payload)` to automatically select:

```python
from clawie.delegation import recommend_tier

tier = recommend_tier("check health status", {"target": "api"})  # -> "fast"
tier = recommend_tier("refactor authentication module", {})       # -> "power"
```

---

## 6. Using the REPL Loop

When you run `clawie delegation repl --agent-id <id> [--tier balanced]`, an
**AgentREPL** starts that:

1. Binds a Unix domain socket at `/tmp/clawie-delegation/<id>.sock`.
2. Loops: accepts a connection → reads a `task_submit` message → dispatches to
   the handler → sends back `task_accepted` then `task_result` or `task_error`.
3. Tracks token usage in its context budget.

### Custom handlers with tier awareness

```python
from clawie.delegation import AgentREPL, Message

def my_handler(msg: Message, repl: AgentREPL) -> dict:
    # Access the tier and budget
    print(f"Running at tier: {repl.model_tier}")
    print(f"Budget remaining: {repl.context_budget.tokens_remaining}")

    task = msg.payload.get("task")
    if task == "analyze":
        return {"analysis": "result data"}
    return {"error": "unknown task"}

repl = AgentREPL("worker-agent", handler=my_handler, model_tier="fast")
repl.start()
```

### Recursive delegation with tiers

```python
def planning_handler(msg: Message, repl: AgentREPL) -> dict:
    sub_result = repl.delegate(
        child_id="leaf-worker",
        payload={"task": "sub-analyze", "data": msg.payload.get("data")},
        depth=msg.depth + 1,
        timeout=60.0,
        model_tier="fast",     # sub-task uses a lighter tier
    )
    return {"combined": sub_result}
```

---

## 7. Error Handling

| Scenario | What happens |
|---|---|
| Child not running | `clawie delegation submit` returns a connection error immediately. |
| Child crashes mid-task | Parent receives a connection error; task marked `failed`. |
| Handler raises an exception | Child sends `task_error` with the exception message. |
| Timeout exceeded | Parent receives a timeout error; task marked `failed`. |
| Recursion too deep (>10) | `ValueError` raised before delegation starts. |
| Cycle detected (A→B→A) | `ValueError` raised before delegation starts. |
| Context budget exceeded | Warning at 75%, compaction triggered at 90%. Results summarized. |

---

## 8. Session Sub-Agents (No Root Required)

Spawn lightweight sub-agents **within your current session**:

```bash
# Spawn with a specific tier
clawie delegation spawn-session --parent <your-id> --child <child-id> --tier fast

# Delegate to the session agent
clawie delegation submit --parent <your-id> --child <child-id> --payload '{"task":"work"}'

# List session agents
clawie delegation session-agents --parent <your-id>

# Stop a session agent
clawie delegation stop-session --parent <your-id> --child <child-id>
```

### Programmatic Usage

```python
from clawie.delegation import SessionAgentManager

mgr = SessionAgentManager("my-agent")
mgr.spawn("worker-1", model_tier="fast")
mgr.spawn("worker-2", model_tier="power")

result = mgr.delegate("worker-1", {"task": "analyze"})

for line in mgr.tree_lines():
    print(line)  # Shows tier icons in the tree

mgr.stop_all()
```

---

## 9. Dashboard Delegation View

The clawie dashboard (``clawie dashboard``) has a **delegation** overview mode.
Press **v** to cycle through: agents → channels → delegation.

The delegation view shows:
- **Left panel**: ASCII tree with tier icons (⚡⚖⭐) next to each node
- **Right panel**: Active REPL sockets and recent delegation tasks with tier column
- **Settings**: Model tier setting cycles through fast → balanced → power

---

## 10. Disabling This Skill

If you do not need delegation capabilities, you can disable this skill:

```bash
# When creating an agent:
clawie agents create --agent-id <id> --no-delegation

# When spawning:
clawie spawn --agent-id <id> --no-delegation
```

When disabled, this DELEGATION.md prompt is not included in the agent's core
prompts, and the agent will not have delegation instructions loaded.
"""
MAX_RECURSION_DEPTH = 10
MAX_CHILDREN_PER_AGENT = 50
DEFAULT_TIMEOUT = 300.0
HEARTBEAT_INTERVAL = 30.0
POLL_INTERVAL = 0.1  # 100ms select() poll
MSG_HEADER_SIZE = 4  # 4-byte big-endian length prefix


# ---------------------------------------------------------------------------
# Model Tiers & Context Budgets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelTier:
    """Describes a model tier's capabilities and resource constraints."""

    name: str
    model_id: str
    context_window: int
    cost_per_1k_input: float
    cost_per_1k_output: float
    speed_rating: int  # 1-10
    capability_rating: int  # 1-10
    default_context_budget: int


DEFAULT_MODEL_TIERS: dict[str, ModelTier] = {
    "fast": ModelTier(
        name="fast",
        model_id="fast",
        context_window=8_000,
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.002,
        speed_rating=9,
        capability_rating=4,
        default_context_budget=4_000,
    ),
    "balanced": ModelTier(
        name="balanced",
        model_id="balanced",
        context_window=32_000,
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        speed_rating=6,
        capability_rating=7,
        default_context_budget=16_000,
    ),
    "power": ModelTier(
        name="power",
        model_id="power",
        context_window=128_000,
        cost_per_1k_input=0.015,
        cost_per_1k_output=0.075,
        speed_rating=3,
        capability_rating=10,
        default_context_budget=64_000,
    ),
}

VALID_TIER_NAMES: tuple[str, ...] = ("fast", "balanced", "power")
DEFAULT_TIER: str = "balanced"


def get_model_tier(name: str) -> ModelTier:
    """Return the ``ModelTier`` for *name*, raising ``ValueError`` if unknown."""
    tier = DEFAULT_MODEL_TIERS.get(name)
    if tier is None:
        raise ValueError(
            f"unknown model tier {name!r}; valid tiers: {', '.join(VALID_TIER_NAMES)}"
        )
    return tier


def estimate_tokens(text: str) -> int:
    """Rough token estimate: len(text) / 4."""
    return max(0, len(text) // 4)


def estimate_payload_tokens(payload: dict[str, Any] | Any) -> int:
    """Estimate token count for a JSON-serializable payload."""
    try:
        raw = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError):
        raw = str(payload)
    return estimate_tokens(raw)


@dataclass
class ContextBudget:
    """Tracks token usage against a budget for a single agent."""

    total_budget: int = 16_000
    tokens_used: int = 0
    payload_tokens: int = 0
    result_tokens: int = 0
    compaction_count: int = 0
    tier: str = DEFAULT_TIER

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.total_budget - self.tokens_used)

    @property
    def usage_ratio(self) -> float:
        if self.total_budget <= 0:
            return 1.0
        return self.tokens_used / self.total_budget

    @property
    def needs_warning(self) -> bool:
        return self.usage_ratio >= 0.75

    @property
    def needs_compaction(self) -> bool:
        return self.usage_ratio >= 0.90

    def record_payload(self, tokens: int) -> None:
        self.payload_tokens += tokens
        self.tokens_used += tokens

    def record_result(self, tokens: int) -> None:
        self.result_tokens += tokens
        self.tokens_used += tokens

    def compact(self, tokens_freed: int) -> None:
        freed = min(tokens_freed, self.tokens_used)
        self.tokens_used -= freed
        self.compaction_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_budget": self.total_budget,
            "tokens_used": self.tokens_used,
            "payload_tokens": self.payload_tokens,
            "result_tokens": self.result_tokens,
            "compaction_count": self.compaction_count,
            "tier": self.tier,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextBudget":
        return cls(
            total_budget=int(data.get("total_budget", 16_000)),
            tokens_used=int(data.get("tokens_used", 0)),
            payload_tokens=int(data.get("payload_tokens", 0)),
            result_tokens=int(data.get("result_tokens", 0)),
            compaction_count=int(data.get("compaction_count", 0)),
            tier=str(data.get("tier", DEFAULT_TIER)),
        )

    @classmethod
    def for_tier(cls, tier_name: str) -> "ContextBudget":
        tier = DEFAULT_MODEL_TIERS.get(tier_name)
        budget = tier.default_context_budget if tier else 16_000
        return cls(total_budget=budget, tier=tier_name)


def recommend_tier(task_description: str, payload: dict[str, Any] | None = None) -> str:
    """Heuristic tier selection based on task description and payload size."""
    desc = task_description.lower()
    payload_tokens = estimate_payload_tokens(payload or {})

    # Large payloads need power tier
    if payload_tokens > 8000:
        return "power"

    # Keywords suggesting simple/fast tasks
    fast_keywords = (
        "check", "status", "ping", "list", "count", "echo", "health",
        "version", "simple", "quick", "lookup", "validate",
    )
    if any(kw in desc for kw in fast_keywords) and payload_tokens < 1000:
        return "fast"

    # Keywords suggesting complex/power tasks
    power_keywords = (
        "analyze", "refactor", "architect", "design", "review", "plan",
        "migrate", "optimize", "debug", "investigate", "comprehensive",
    )
    if any(kw in desc for kw in power_keywords):
        return "power"

    # Medium payload or no strong signal → balanced
    return "balanced"


# ---------------------------------------------------------------------------
# Message protocol
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """Wire-format envelope for delegation IPC."""

    msg_type: str  # task_submit, task_accepted, task_result, task_error, etc.
    msg_id: str = ""
    task_id: str = ""
    parent_agent_id: str = ""
    child_agent_id: str = ""
    depth: int = 0
    timestamp: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    model_tier: str = ""
    context_budget: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.msg_id:
            self.msg_id = uuid.uuid4().hex
        if not self.timestamp:
            self.timestamp = time.time()

    def encode(self) -> bytes:
        """Serialize to length-prefixed UTF-8 JSON."""
        body = json.dumps(asdict(self), sort_keys=True).encode("utf-8")
        return struct.pack("!I", len(body)) + body

    @classmethod
    def decode(cls, data: bytes) -> "Message":
        """Deserialize from raw JSON bytes (no length prefix)."""
        obj = json.loads(data.decode("utf-8"))
        return cls(**{k: v for k, v in obj.items() if k in cls.__dataclass_fields__})


def recv_message(sock: socket.socket, timeout: float | None = None) -> Message:
    """Read one length-prefixed message from *sock*."""
    if timeout is not None:
        sock.settimeout(timeout)
    header = _recv_exact(sock, MSG_HEADER_SIZE)
    (length,) = struct.unpack("!I", header)
    body = _recv_exact(sock, length)
    return Message.decode(body)


def send_message(sock: socket.socket, msg: Message) -> None:
    """Write one length-prefixed message to *sock*."""
    sock.sendall(msg.encode())


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed while reading")
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# DelegationBus -- Unix socket IPC manager
# ---------------------------------------------------------------------------

class DelegationBus:
    """Manages Unix domain sockets for agent IPC."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._server: socket.socket | None = None
        self._connections: dict[str, socket.socket] = {}
        self._lock = threading.Lock()

    @property
    def socket_path(self) -> Path:
        return DELEGATION_DIR / f"{self.agent_id}.sock"

    def listen(self) -> None:
        """Bind a server socket for this agent."""
        DELEGATION_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(DELEGATION_DIR), 0o1777)
        except OSError:
            pass

        path = self.socket_path
        if path.exists():
            path.unlink()

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(str(path))
        self._server.listen(16)
        self._server.setblocking(False)
        try:
            os.chmod(str(path), 0o777)
        except OSError:
            pass

    def accept(self, timeout: float = POLL_INTERVAL) -> socket.socket | None:
        """Non-blocking accept via select() with *timeout* seconds poll."""
        srv = self._server
        if srv is None:
            return None
        try:
            ready, _, _ = select.select([srv], [], [], timeout)
            if ready:
                conn, _ = srv.accept()
                return conn
        except (OSError, ValueError):
            return None
        return None

    def connect(self, child_id: str) -> socket.socket:
        """Connect to a child agent's socket."""
        path = DELEGATION_DIR / f"{child_id}.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(path))
        with self._lock:
            old = self._connections.pop(child_id, None)
            if old:
                try:
                    old.close()
                except OSError:
                    pass
            self._connections[child_id] = sock
        return sock

    def send_and_recv(
        self,
        child_id: str,
        msg: Message,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Message:
        """Send *msg* to *child_id* and wait for a response."""
        with self._lock:
            sock = self._connections.get(child_id)
        if sock is None:
            sock = self.connect(child_id)
        send_message(sock, msg)
        return recv_message(sock, timeout=timeout)

    def close(self) -> None:
        """Tear down server and all connections."""
        with self._lock:
            for sock in self._connections.values():
                try:
                    sock.close()
                except OSError:
                    pass
            self._connections.clear()

        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

        path = self.socket_path
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# FileMailbox -- JSON file fallback for cross-user durability
# ---------------------------------------------------------------------------

class FileMailbox:
    """File-based message queue for cases where Unix sockets fail."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._inbox = DELEGATION_DIR / agent_id / "inbox"

    def ensure(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(self._inbox.parent), 0o1777)
            os.chmod(str(self._inbox), 0o1777)
        except OSError:
            pass

    def send(self, target_agent_id: str, msg: Message) -> Path:
        """Write a message file into *target_agent_id*'s inbox."""
        inbox = DELEGATION_DIR / target_agent_id / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(inbox.parent), 0o1777)
            os.chmod(str(inbox), 0o1777)
        except OSError:
            pass
        filename = f"{msg.timestamp:.6f}-{msg.msg_id}.json"
        path = inbox / filename
        path.write_text(json.dumps(asdict(msg), sort_keys=True), encoding="utf-8")
        return path

    def poll(self) -> list[Message]:
        """Read and consume all pending messages (oldest first)."""
        self.ensure()
        messages: list[Message] = []
        files = sorted(self._inbox.glob("*.json"))
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                messages.append(
                    Message(**{k: v for k, v in data.items() if k in Message.__dataclass_fields__})
                )
                path.unlink()
            except (json.JSONDecodeError, OSError, TypeError):
                try:
                    path.unlink()
                except OSError:
                    pass
        return messages


# ---------------------------------------------------------------------------
# DelegationTree -- Thread-safe recursive state tracker
# ---------------------------------------------------------------------------

@dataclass
class TreeNode:
    agent_id: str
    parent_id: str
    task_id: str
    depth: int = 0
    status: str = "pending"
    children: list[str] = field(default_factory=list)
    model_tier: str = ""


class DelegationTree:
    """Tracks the recursive delegation hierarchy."""

    def __init__(self) -> None:
        self._nodes: dict[str, TreeNode] = {}
        self._lock = threading.Lock()

    def register(
        self,
        agent_id: str,
        parent_id: str,
        task_id: str,
        depth: int = 0,
        model_tier: str = "",
    ) -> TreeNode:
        """Add a delegation node. Raises on depth overflow or cycle."""
        if depth >= MAX_RECURSION_DEPTH:
            raise ValueError(
                f"max recursion depth ({MAX_RECURSION_DEPTH}) exceeded at depth={depth}"
            )
        # Cycle detection: walk ancestry from parent_id upward
        with self._lock:
            visited: set[str] = set()
            current = parent_id
            while current:
                if current == agent_id:
                    raise ValueError(
                        f"delegation cycle detected: {agent_id} already in ancestry"
                    )
                if current in visited or current not in self._nodes:
                    break
                visited.add(current)
                current = self._nodes[current].parent_id

            # Check max children
            parent_node = self._nodes.get(parent_id)
            if parent_node and len(parent_node.children) >= MAX_CHILDREN_PER_AGENT:
                raise ValueError(
                    f"max children ({MAX_CHILDREN_PER_AGENT}) exceeded for {parent_id}"
                )

            node = TreeNode(
                agent_id=agent_id,
                parent_id=parent_id,
                task_id=task_id,
                depth=depth,
                status="pending",
                model_tier=model_tier,
            )
            self._nodes[agent_id] = node
            if parent_node:
                parent_node.children.append(agent_id)
            return node

    def update_status(self, agent_id: str, status: str) -> None:
        with self._lock:
            node = self._nodes.get(agent_id)
            if node:
                node.status = status

    def get_node(self, agent_id: str) -> TreeNode | None:
        with self._lock:
            return self._nodes.get(agent_id)

    def get_subtree(self, root_id: str) -> dict[str, Any]:
        """Return a nested dict for the subtree rooted at *root_id*."""
        with self._lock:
            return self._subtree_locked(root_id)

    def _subtree_locked(self, root_id: str) -> dict[str, Any]:
        node = self._nodes.get(root_id)
        if not node:
            return {}
        return {
            "agent_id": node.agent_id,
            "parent_id": node.parent_id,
            "task_id": node.task_id,
            "depth": node.depth,
            "status": node.status,
            "children": [self._subtree_locked(cid) for cid in node.children],
        }

    def remove(self, agent_id: str) -> None:
        with self._lock:
            node = self._nodes.pop(agent_id, None)
            if node:
                parent = self._nodes.get(node.parent_id)
                if parent and agent_id in parent.children:
                    parent.children.remove(agent_id)

    def all_agents(self) -> list[str]:
        with self._lock:
            return list(self._nodes.keys())

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                aid: {
                    "agent_id": n.agent_id,
                    "parent_id": n.parent_id,
                    "task_id": n.task_id,
                    "depth": n.depth,
                    "status": n.status,
                    "children": list(n.children),
                    "model_tier": n.model_tier,
                }
                for aid, n in self._nodes.items()
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DelegationTree":
        tree = cls()
        for aid, raw in data.items():
            node = TreeNode(
                agent_id=raw.get("agent_id", aid),
                parent_id=raw.get("parent_id", ""),
                task_id=raw.get("task_id", ""),
                depth=raw.get("depth", 0),
                status=raw.get("status", "pending"),
                children=list(raw.get("children", [])),
                model_tier=raw.get("model_tier", ""),
            )
            tree._nodes[aid] = node
        return tree


# ---------------------------------------------------------------------------
# AgentREPL -- RLM REPL loop (blocking, per agent)
# ---------------------------------------------------------------------------

TaskHandler = Callable[[Message, "AgentREPL"], dict[str, Any]]


def _default_handler(msg: Message, repl: "AgentREPL") -> dict[str, Any]:
    """Echo handler for testing -- returns the payload as-is."""
    return dict(msg.payload)


class AgentREPL:
    """
    Blocking REPL loop for a single agent.

    Listens on a Unix domain socket, dispatches incoming tasks to a handler,
    and optionally delegates sub-tasks recursively.
    """

    def __init__(
        self,
        agent_id: str,
        handler: TaskHandler | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        model_tier: str = DEFAULT_TIER,
    ) -> None:
        self.agent_id = agent_id
        self.handler = handler or _default_handler
        self.timeout = timeout
        self.model_tier = model_tier
        self.bus = DelegationBus(agent_id)
        self.tree = DelegationTree()
        self.context_budget = ContextBudget.for_tier(model_tier)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Listen on socket, loop: accept -> dispatch -> respond."""
        self._running = True
        try:
            self.bus.listen()
        except (OSError, AttributeError):
            self._running = False
            return
        while self._running:
            conn = self.bus.accept(timeout=POLL_INTERVAL)
            if conn is None:
                continue
            try:
                self._handle_connection(conn)
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def start_background(self) -> threading.Thread:
        """Start the REPL loop in a daemon thread."""
        t = threading.Thread(target=self.start, daemon=True)
        t.start()
        self._thread = t
        return t

    def _handle_connection(self, conn: socket.socket) -> None:
        """Process a single incoming connection."""
        msg = recv_message(conn, timeout=self.timeout)
        if msg.msg_type == "task_submit":
            self._handle_task(conn, msg)
        elif msg.msg_type == "heartbeat":
            ack = Message(
                msg_type="heartbeat_ack",
                task_id=msg.task_id,
                parent_agent_id=msg.child_agent_id,
                child_agent_id=msg.parent_agent_id,
            )
            send_message(conn, ack)
        elif msg.msg_type == "shutdown":
            self._running = False
            ack = Message(
                msg_type="shutdown",
                task_id=msg.task_id,
                parent_agent_id=self.agent_id,
            )
            send_message(conn, ack)
        elif msg.msg_type == "task_cancel":
            # Acknowledge cancellation
            ack = Message(
                msg_type="task_error",
                task_id=msg.task_id,
                parent_agent_id=self.agent_id,
                child_agent_id=msg.parent_agent_id,
                payload={"error": "cancelled"},
            )
            send_message(conn, ack)

    def _handle_task(self, conn: socket.socket, msg: Message) -> None:
        """Send TASK_ACCEPTED, run handler with timeout, send result/error."""
        # Track inbound payload tokens
        self.context_budget.record_payload(estimate_payload_tokens(msg.payload))

        # Send acceptance
        accepted = Message(
            msg_type="task_accepted",
            task_id=msg.task_id,
            parent_agent_id=self.agent_id,
            child_agent_id=msg.parent_agent_id,
            depth=msg.depth,
        )
        send_message(conn, accepted)

        # Run handler in thread with timeout
        result_holder: list[dict[str, Any] | None] = [None]
        error_holder: list[str | None] = [None]

        def _run() -> None:
            try:
                result_holder[0] = self.handler(msg, self)
            except Exception as exc:
                error_holder[0] = str(exc)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=self.timeout)

        if worker.is_alive():
            # Timeout
            response = Message(
                msg_type="task_error",
                task_id=msg.task_id,
                parent_agent_id=self.agent_id,
                child_agent_id=msg.parent_agent_id,
                depth=msg.depth,
                payload={"error": f"handler timed out after {self.timeout}s"},
            )
        elif error_holder[0] is not None:
            response = Message(
                msg_type="task_error",
                task_id=msg.task_id,
                parent_agent_id=self.agent_id,
                child_agent_id=msg.parent_agent_id,
                depth=msg.depth,
                payload={"error": error_holder[0]},
            )
        else:
            result_payload = result_holder[0] or {}
            self.context_budget.record_result(estimate_payload_tokens(result_payload))
            response = Message(
                msg_type="task_result",
                task_id=msg.task_id,
                parent_agent_id=self.agent_id,
                child_agent_id=msg.parent_agent_id,
                depth=msg.depth,
                payload=result_payload,
            )

        send_message(conn, response)

    def delegate(
        self,
        child_id: str,
        payload: dict[str, Any],
        depth: int = 0,
        timeout: float | None = None,
        model_tier: str = "",
    ) -> dict[str, Any]:
        """
        Delegate a sub-task to *child_id* (the RLM llm_query() equivalent).

        Can be called from within a handler for recursive delegation.
        """
        tier = model_tier or self.model_tier
        task_id = uuid.uuid4().hex
        self.tree.register(child_id, self.agent_id, task_id, depth=depth, model_tier=tier)

        msg = Message(
            msg_type="task_submit",
            task_id=task_id,
            parent_agent_id=self.agent_id,
            child_agent_id=child_id,
            depth=depth,
            payload=payload,
            model_tier=tier,
        )

        try:
            sock = self.bus.connect(child_id)
            send_message(sock, msg)

            # Wait for acceptance
            accepted = recv_message(sock, timeout=timeout or self.timeout)
            if accepted.msg_type == "task_error":
                self.tree.update_status(child_id, "failed")
                raise RuntimeError(accepted.payload.get("error", "task rejected"))
            self.tree.update_status(child_id, "running")

            # Wait for result
            result = recv_message(sock, timeout=timeout or self.timeout)
            if result.msg_type == "task_error":
                self.tree.update_status(child_id, "failed")
                raise RuntimeError(result.payload.get("error", "task failed"))
            self.tree.update_status(child_id, "completed")
            return dict(result.payload)
        except ConnectionError as exc:
            self.tree.update_status(child_id, "failed")
            self.tree.remove(child_id)
            raise RuntimeError(f"child {child_id} unreachable: {exc}") from exc

    def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        self.bus.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ---------------------------------------------------------------------------
# DelegationCoordinator -- High-level orchestrator for planning agents
# ---------------------------------------------------------------------------

class DelegationCoordinator:
    """Orchestrates delegation from a planning agent to workers."""

    def __init__(
        self,
        agent_id: str,
        bus: DelegationBus | None = None,
        tree: DelegationTree | None = None,
        model_tier: str = DEFAULT_TIER,
    ) -> None:
        self.agent_id = agent_id
        self.bus = bus or DelegationBus(agent_id)
        self.tree = tree or DelegationTree()
        self.model_tier = model_tier
        self.context_budget = ContextBudget.for_tier(model_tier)

    def delegate(
        self,
        child_id: str,
        payload: dict[str, Any],
        depth: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        model_tier: str = "",
    ) -> dict[str, Any]:
        """Register in tree, connect, send task, wait for result."""
        tier = model_tier or self.model_tier
        task_id = uuid.uuid4().hex
        self.tree.register(child_id, self.agent_id, task_id, depth=depth, model_tier=tier)
        self.tree.update_status(child_id, "connecting")

        payload_tokens = estimate_payload_tokens(payload)
        self.context_budget.record_payload(payload_tokens)

        msg = Message(
            msg_type="task_submit",
            task_id=task_id,
            parent_agent_id=self.agent_id,
            child_agent_id=child_id,
            depth=depth,
            payload=payload,
            model_tier=tier,
        )

        try:
            sock = self.bus.connect(child_id)
            send_message(sock, msg)

            # Wait for acceptance
            accepted = recv_message(sock, timeout=timeout)
            if accepted.msg_type == "task_error":
                self.tree.update_status(child_id, "failed")
                return {"error": accepted.payload.get("error", "rejected")}
            self.tree.update_status(child_id, "running")

            # Wait for result
            result = recv_message(sock, timeout=timeout)
            if result.msg_type == "task_error":
                self.tree.update_status(child_id, "failed")
                return {"error": result.payload.get("error", "failed")}

            self.tree.update_status(child_id, "completed")
            return dict(result.payload)
        except (ConnectionError, OSError) as exc:
            self.tree.update_status(child_id, "failed")
            self.tree.remove(child_id)
            return {"error": f"connection failed: {exc}"}
        except TimeoutError:
            self.tree.update_status(child_id, "timeout")
            return {"error": f"timed out after {timeout}s"}

    def delegate_many(
        self,
        tasks: list[dict[str, Any]],
        timeout: float = DEFAULT_TIMEOUT,
        model_tier: str = "",
    ) -> list[dict[str, Any]]:
        """
        Parallel delegation via threads.

        Each entry in *tasks* must have 'child_id' and 'payload' keys,
        and optionally 'depth' and 'model_tier'.
        """
        results: list[dict[str, Any]] = [{}] * len(tasks)
        threads: list[threading.Thread] = []

        def _run(idx: int, child_id: str, payload: dict[str, Any], depth: int, tier: str) -> None:
            results[idx] = self.delegate(child_id, payload, depth=depth, timeout=timeout, model_tier=tier)

        for i, task in enumerate(tasks):
            child_id = str(task["child_id"])
            payload = dict(task.get("payload", {}))
            depth = int(task.get("depth", 0))
            tier = str(task.get("model_tier", model_tier or self.model_tier))
            t = threading.Thread(target=_run, args=(i, child_id, payload, depth, tier), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=timeout)

        return results

    def shutdown_child(self, child_id: str) -> None:
        """Send shutdown to a specific child."""
        msg = Message(
            msg_type="shutdown",
            parent_agent_id=self.agent_id,
            child_agent_id=child_id,
        )
        try:
            sock = self.bus.connect(child_id)
            send_message(sock, msg)
            recv_message(sock, timeout=5.0)
        except (ConnectionError, OSError, TimeoutError):
            pass

    def shutdown_all(self) -> None:
        """Cascade shutdown deepest-first."""
        agents = self.tree.all_agents()
        # Sort by depth descending (deepest first)
        nodes = []
        for aid in agents:
            node = self.tree.get_node(aid)
            if node:
                nodes.append((node.depth, aid))
        nodes.sort(reverse=True)

        for _, aid in nodes:
            self.shutdown_child(aid)
        self.bus.close()

    def persist(self, store: Any) -> None:
        """Save tree to SQLite via store."""
        store.write_delegation_tree(self.agent_id, self.tree.to_dict())

    def restore(self, store: Any) -> None:
        """Load tree from SQLite via store."""
        data = store.read_delegation_tree(self.agent_id)
        if data:
            self.tree = DelegationTree.from_dict(data)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def cleanup_stale_sockets(max_age_seconds: float = 600.0) -> list[str]:
    """Remove socket files older than *max_age_seconds*. Returns removed paths."""
    removed: list[str] = []
    if not DELEGATION_DIR.exists():
        return removed
    now = time.time()
    for path in DELEGATION_DIR.glob("*.sock"):
        try:
            age = now - path.stat().st_mtime
            if age > max_age_seconds:
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
    return removed


def list_active_agents() -> list[dict[str, Any]]:
    """List agents with active sockets."""
    agents: list[dict[str, Any]] = []
    if not DELEGATION_DIR.exists():
        return agents
    for path in sorted(DELEGATION_DIR.glob("*.sock")):
        agent_id = path.stem
        try:
            stat = path.stat()
            # Try connecting to see if it's alive
            alive = False
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect(str(path))
                alive = True
                sock.close()
            except (ConnectionRefusedError, OSError):
                pass
            agents.append({
                "agent_id": agent_id,
                "socket": str(path),
                "alive": alive,
                "age_seconds": round(time.time() - stat.st_mtime, 1),
            })
        except OSError:
            pass
    return agents


# ---------------------------------------------------------------------------
# ASCII tree rendering
# ---------------------------------------------------------------------------

_STATUS_ICONS: dict[str, str] = {
    "running": "\u25cf",   # ●
    "completed": "\u2713", # ✓
    "failed": "\u2717",    # ✗
    "pending": "\u25cb",   # ○
    "timeout": "\u29d6",   # ⧖
    "connecting": "\u2026",# …
}

_TIER_ICONS: dict[str, str] = {
    "fast": "\u26a1",      # ⚡
    "balanced": "\u2696",  # ⚖
    "power": "\u2b50",     # ⭐
}


def render_tree_ascii(
    tree_data: dict[str, Any],
    root_id: str | None = None,
) -> list[str]:
    """
    Render a delegation tree (from DelegationTree.to_dict() or
    DelegationTree.get_subtree()) as ASCII art lines.

    Supports both flat-dict format (to_dict) and nested format (get_subtree).
    Returns a list of pre-formatted strings.
    """
    if not tree_data:
        return ["(empty tree)"]

    # Detect format: nested (has "children" list of dicts) vs flat (agent_id -> node)
    if "agent_id" in tree_data and "children" in tree_data:
        return _render_nested(tree_data)

    # Flat format from to_dict()
    return _render_flat(tree_data, root_id)


def _render_nested(node: dict[str, Any], prefix: str = "", is_last: bool = True) -> list[str]:
    """Render a nested subtree dict as ASCII lines."""
    agent_id = node.get("agent_id", "?")
    status = node.get("status", "pending")
    depth = node.get("depth", 0)
    icon = _STATUS_ICONS.get(status, "?")
    tier = node.get("model_tier", "")
    tier_icon = _TIER_ICONS.get(tier, "")
    children = node.get("children", [])

    connector = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "  # └── or ├──
    if not prefix:
        connector = ""

    tier_part = f" {tier_icon}{tier}" if tier else ""
    label = f"{connector}{icon} {agent_id}  [{status}] d={depth}{tier_part}"
    lines = [f"{prefix}{label}"]

    child_prefix = prefix + ("    " if is_last else "\u2502   ")  # │
    for i, child in enumerate(children):
        if isinstance(child, dict):
            lines.extend(_render_nested(child, child_prefix, i == len(children) - 1))
    return lines


def _render_flat(
    nodes: dict[str, Any],
    root_id: str | None = None,
) -> list[str]:
    """Render a flat to_dict() format as ASCII lines."""
    # Find roots: nodes whose parent_id is empty or not in the dict
    if root_id and root_id in nodes:
        roots = [root_id]
    else:
        roots = [
            aid for aid, n in nodes.items()
            if not n.get("parent_id") or n["parent_id"] not in nodes
        ]
        roots.sort()

    if not roots:
        return ["(empty tree)"]

    lines: list[str] = []
    for i, rid in enumerate(roots):
        lines.extend(_render_flat_node(nodes, rid, "", i == len(roots) - 1))
    return lines


def _render_flat_node(
    nodes: dict[str, Any],
    agent_id: str,
    prefix: str,
    is_last: bool,
) -> list[str]:
    node = nodes.get(agent_id, {})
    status = node.get("status", "pending")
    depth = node.get("depth", 0)
    icon = _STATUS_ICONS.get(status, "?")
    tier = node.get("model_tier", "")
    tier_icon = _TIER_ICONS.get(tier, "")
    children = node.get("children", [])

    connector = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
    if not prefix:
        connector = ""

    tier_part = f" {tier_icon}{tier}" if tier else ""
    label = f"{connector}{icon} {agent_id}  [{status}] d={depth}{tier_part}"
    lines = [f"{prefix}{label}"]

    child_prefix = prefix + ("    " if is_last else "\u2502   ")
    real_children = [c for c in children if c in nodes]
    for i, cid in enumerate(real_children):
        lines.extend(_render_flat_node(nodes, cid, child_prefix, i == len(real_children) - 1))
    return lines


# ---------------------------------------------------------------------------
# SessionAgentManager -- lightweight in-process sub-agents
# ---------------------------------------------------------------------------

class SessionAgentManager:
    """
    Manages lightweight sub-agents within a single process session.

    Unlike ``spawn_linux_user`` (which requires root and creates real Linux
    users), session agents run as background threads in the current process.
    They share the same user, filesystem, and environment but each gets its
    own AgentREPL with a unique Unix socket.
    """

    def __init__(self, parent_agent_id: str) -> None:
        self.parent_agent_id = parent_agent_id
        self.coordinator = DelegationCoordinator(parent_agent_id)
        self._agents: dict[str, AgentREPL] = {}
        self._lock = threading.Lock()
        # Register parent as root so the tree renders properly
        self.coordinator.tree.register(parent_agent_id, "", "root", depth=0)
        self.coordinator.tree.update_status(parent_agent_id, "running")

    def spawn(
        self,
        child_id: str,
        handler: TaskHandler | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        model_tier: str = DEFAULT_TIER,
    ) -> dict[str, Any]:
        """
        Spawn a lightweight sub-agent with its own REPL.

        Returns a dict with agent info.  The sub-agent is immediately ready
        to receive delegated tasks.
        """
        with self._lock:
            if child_id in self._agents:
                raise ValueError(f"session agent already exists: {child_id}")

        repl = AgentREPL(child_id, handler=handler, timeout=timeout, model_tier=model_tier)
        thread = repl.start_background()

        with self._lock:
            self._agents[child_id] = repl

        # Register in coordinator tree
        depth = 0
        parent_node = self.coordinator.tree.get_node(self.parent_agent_id)
        if parent_node:
            depth = parent_node.depth + 1
        task_id = uuid.uuid4().hex
        try:
            self.coordinator.tree.register(
                child_id, self.parent_agent_id, task_id, depth=depth,
                model_tier=model_tier,
            )
            self.coordinator.tree.update_status(child_id, "running")
        except ValueError:
            repl.stop()
            with self._lock:
                self._agents.pop(child_id, None)
            raise

        return {
            "agent_id": child_id,
            "parent_id": self.parent_agent_id,
            "depth": depth,
            "status": "running",
            "session": True,
            "model_tier": model_tier,
        }

    def delegate(
        self,
        child_id: str,
        payload: dict[str, Any],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """Delegate a task to a session sub-agent."""
        with self._lock:
            if child_id not in self._agents:
                raise ValueError(f"session agent not found: {child_id}")

        # Use the bus directly instead of coordinator.delegate() to avoid
        # re-registering the child in the tree (spawn already registered it).
        task_id = uuid.uuid4().hex
        msg = Message(
            msg_type="task_submit",
            task_id=task_id,
            parent_agent_id=self.parent_agent_id,
            child_agent_id=child_id,
            depth=1,
            payload=payload,
        )
        try:
            sock = self.coordinator.bus.connect(child_id)
            send_message(sock, msg)
            accepted = recv_message(sock, timeout=timeout)
            if accepted.msg_type == "task_error":
                self.coordinator.tree.update_status(child_id, "failed")
                return {"error": accepted.payload.get("error", "rejected")}
            self.coordinator.tree.update_status(child_id, "running")
            result = recv_message(sock, timeout=timeout)
            if result.msg_type == "task_error":
                self.coordinator.tree.update_status(child_id, "failed")
                return {"error": result.payload.get("error", "failed")}
            self.coordinator.tree.update_status(child_id, "completed")
            return dict(result.payload)
        except (ConnectionError, OSError) as exc:
            self.coordinator.tree.update_status(child_id, "failed")
            return {"error": f"connection failed: {exc}"}
        except TimeoutError:
            self.coordinator.tree.update_status(child_id, "timeout")
            return {"error": f"timed out after {timeout}s"}

    def stop_agent(self, child_id: str) -> None:
        """Stop a specific session sub-agent."""
        with self._lock:
            repl = self._agents.pop(child_id, None)
        if repl:
            repl.stop()
            self.coordinator.tree.update_status(child_id, "completed")

    def stop_all(self) -> None:
        """Stop all session sub-agents."""
        with self._lock:
            agents = dict(self._agents)
            self._agents.clear()
        for child_id, repl in agents.items():
            repl.stop()
            self.coordinator.tree.update_status(child_id, "completed")

    def list_agents(self) -> list[dict[str, Any]]:
        """List all session sub-agents."""
        with self._lock:
            result = []
            for child_id, repl in self._agents.items():
                node = self.coordinator.tree.get_node(child_id)
                result.append({
                    "agent_id": child_id,
                    "parent_id": self.parent_agent_id,
                    "depth": node.depth if node else 0,
                    "status": node.status if node else "unknown",
                    "running": repl._running,
                    "session": True,
                    "model_tier": node.model_tier if node else repl.model_tier,
                })
            return result

    def tree_data(self) -> dict[str, Any]:
        """Return the full delegation tree dict."""
        return self.coordinator.tree.to_dict()

    def tree_lines(self) -> list[str]:
        """Render the delegation tree as ASCII art."""
        data = self.coordinator.tree.to_dict()
        if not data:
            return ["(no agents)"]
        return render_tree_ascii(data, root_id=self.parent_agent_id)
