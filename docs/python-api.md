# Python API

clawie can be used programmatically from Python. The main entry points are `ZeroClawService` for agent/fleet operations and the `delegation` module for orchestration.

## Service API

```python
from clawie.store import StateStore
from clawie.service import ZeroClawService

store = StateStore()  # Uses ~/.clawie by default
service = ZeroClawService(store)
```

### Agent operations

```python
# Create
agent = service.create_agent(
    agent_id="alice",
    display_name="Alice",
    template="baseline",
    clone_from=None,
    channel_strategy="new",
    channels=None,
    agent_version="1.0.0",
)

# List and inspect
agents = service.list_agents()
agent = service.get_agent("alice")

# Delete
service.delete_agent("alice")
```

### Model tiers

```python
new_tier = service.set_agent_model_tier("alice", "fast")
# Or cycle: fast -> balanced -> power -> fast
new_tier = service.set_agent_model_tier("alice", "")
```

### Delegation

```python
# Submit a task
result = service.delegate_task(
    parent_id="planner",
    child_id="worker",
    payload={"task": "analyze"},
    timeout=60.0,
    model_tier="fast",
)

# Spawn session agents
info = service.spawn_session_agent(
    parent_id="planner",
    child_id="researcher",
    model_tier="power",
)

# Stop session agents
service.stop_session_agent("planner", "researcher")

# Start a blocking REPL
service.start_agent_repl("worker", model_tier="balanced")
```

### Configuration

```python
service.setup(
    provider="picoclaw",
    api_key="",
    subscription="pro",
    workspace="production",
    api_url="https://api.example.com/v1",
)
```

### Backup/restore

```python
# Git-backed knowledge backup
service.backup_init("~/backups/agents", remote="git@github.com:you/agents.git")
result = service.backup_run()                  # {"changed", "commit", "pushed", ...}
status = service.backup_status()               # read-only repo/last-run view
service.backup_restore("alice")                # restore prompts + workspace knowledge

# Full-fidelity state snapshots (credentials included)
service.export_state("backup.json")
service.import_state("backup.json", merge=False)
```

### Credential porting

```python
result = service.port_shared_auth("openclaw", "picoclaw")
result["profiles"]                 # ported profile ids
result["restart_required_agents"]  # agents that should be restarted
```

## Delegation module

For direct orchestration without the service layer:

### DelegationCoordinator

```python
from clawie.delegation import DelegationCoordinator

coord = DelegationCoordinator("planner", model_tier="power")

# Single delegation
result = coord.delegate("worker", {"task": "check"}, model_tier="fast")

# Parallel fan-out
results = coord.delegate_many([
    {"child_id": "w1", "payload": {"task": "a"}, "model_tier": "fast"},
    {"child_id": "w2", "payload": {"task": "b"}, "model_tier": "power"},
], timeout=120.0)

coord.shutdown_all()
```

### AgentREPL

```python
from clawie.delegation import AgentREPL, Message

def handler(msg: Message, repl: AgentREPL) -> dict:
    return {"processed": msg.payload}

repl = AgentREPL("worker", handler=handler, model_tier="fast")
repl.start()           # Blocking
# repl.start_background()  # Non-blocking (daemon thread)
```

### SessionAgentManager

```python
from clawie.delegation import SessionAgentManager

mgr = SessionAgentManager("parent")
mgr.spawn("w1", model_tier="fast")
mgr.spawn("w2", model_tier="power")

result = mgr.delegate("w1", {"task": "check"})
agents = mgr.list_agents()
lines = mgr.tree_lines()

mgr.stop_agent("w1")
mgr.stop_all()
```

### Context budgets

```python
from clawie.delegation import ContextBudget

budget = ContextBudget.for_tier("fast")  # 4K total
budget.record_payload(1000)
budget.record_result(500)

print(budget.tokens_remaining)  # 2500
print(budget.usage_ratio)       # 0.375
print(budget.needs_warning)     # False (< 75%)
print(budget.needs_compaction)  # False (< 90%)

budget.compact(500)  # Free 500 tokens
```

### Tier recommendation

```python
from clawie.delegation import recommend_tier, get_model_tier

tier_name = recommend_tier("check status", {})  # "fast"
tier = get_model_tier(tier_name)
print(tier.context_window)       # 8000
print(tier.default_context_budget)  # 4000
```

### Token estimation

```python
from clawie.delegation import estimate_tokens, estimate_payload_tokens

tokens = estimate_tokens("Hello world")  # ~2
tokens = estimate_payload_tokens({"key": "value"})  # rough estimate
```

## State store

Direct database access for advanced use:

```python
from clawie.store import StateStore

store = StateStore(config_dir="/path/to/state")
store.ensure()  # Create schema

config = store.read_config()
state = store.read_state()

# Delegation tasks
store.write_delegation_task(
    task_id="t1",
    parent_agent_id="planner",
    child_agent_id="worker",
    model_tier="fast",
)
tasks = store.read_delegation_tasks(parent_agent_id="planner")
```

## Status

```python
# Read-only aggregate snapshot of the whole fleet.
snapshot = service.status_snapshot()                    # all sections
agents = service.status_snapshot(sections=["agents"])   # a single section
focused = service.status_snapshot(agent_id="alice")     # focus one agent
```
