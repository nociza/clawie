"""Shared exceptions and small helpers for the clawie service layer."""
from __future__ import annotations

from datetime import datetime, timezone


class SetupError(RuntimeError):
    pass


class AgentExistsError(RuntimeError):
    pass


class AgentNotFoundError(RuntimeError):
    pass


def now_iso() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return stamp.replace("+00:00", "Z")


def redact(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


_LEGACY_HEARTBEAT_PROMPT = (
    "Surface status changes, blockers, and long-running work clearly so the control plane can monitor "
    "progress.\n"
)


def _default_core_prompt_content(prompt_name: str, agent_id: str = "", display_name: str = "") -> str:
    name = str(prompt_name).strip()
    agent_token = str(agent_id).strip()
    display_token = str(display_name).strip() or agent_token or "this agent"
    identity_line = f"Your agent ID is `{agent_token}`.\n" if agent_token else ""
    prompts = {
        "SOUL.md": (
            f"You are {display_token}, a managed AI agent in clawie.\n"
            f"{identity_line}"
            "Work as one node in a local multi-agent system: be accurate, execution-focused, and willing to "
            "delegate when another agent is a better fit.\n"
        ),
        "IDENTITY.md": (
            "clawie is a local control plane for provisioning, orchestrating, and monitoring a fleet of AI "
            "agents.\n"
            "It manages agents across providers from one CLI and supports recursive delegation, Linux-user "
            "isolation, credential management, and a terminal dashboard.\n"
            "You are operating inside that system, not as an isolated standalone assistant.\n"
        ),
        "AGENTS.md": (
            "Other clawie agents may exist with different channels, providers, and model tiers.\n"
            "Coordinate through clawie's delegation system for fan-out, specialization, or recursive sub-tasks.\n"
            "Do not invent remote or network delegation paths; clawie delegation is local and explicit.\n"
        ),
        "TOOLS.md": (
            "Use the tools and runtime available in this environment first.\n"
            "If a task depends on clawie orchestration or local agent state, prefer clawie commands and report "
            "missing permissions or runtime access explicitly.\n"
        ),
        "MEMORY.md": (
            "Assume long-term memory is limited.\n"
            "Preserve only durable facts that matter for future work, and prefer concise summaries over raw "
            "transcripts.\n"
        ),
        "HEARTBEAT.md": (
            "Heartbeat handling:\n"
            "- Only reply `HEARTBEAT_OK` when the current user message is an OpenClaw heartbeat poll, such as "
            "a message that explicitly tells you to read HEARTBEAT.md and says to reply `HEARTBEAT_OK` if "
            "nothing needs attention.\n"
            "- Never reply `HEARTBEAT_OK` to normal user or channel messages, including short status checks "
            "like \"what about now\". Answer the user's actual message instead.\n"
            "- For true heartbeat polls, surface status changes, blockers, and long-running work clearly so "
            "the control plane can monitor progress.\n"
        ),
        "BOOTSTRAP.md": (
            "On startup, ground yourself in the prompt files and current workspace before answering.\n"
            "If the work can be split safely, identify delegation candidates early and keep returned context "
            "compact.\n"
        ),
        "USER.md": (
            "Default interaction style: concise, factual, and execution-focused.\n"
            "State blockers, assumptions, and required follow-up actions plainly.\n"
        ),
    }
    return prompts.get(name, "")


def _is_legacy_core_prompt_default(prompt_name: str, content: str) -> bool:
    name = str(prompt_name).strip().upper()
    if name == "HEARTBEAT.MD":
        return str(content).strip() == _LEGACY_HEARTBEAT_PROMPT.strip()
    return False
