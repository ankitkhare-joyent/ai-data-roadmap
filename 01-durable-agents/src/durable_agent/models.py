"""Data passed between the client, the workflow and the activities.

Everything here is serialised into Temporal's event history, so keep it small and JSON-friendly.
Plain dataclasses work with Temporal's default converter; no framework types cross the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_QUEUE = "durable-agent"

# Tools whose side effects a human must approve before they run.
RISKY_TOOLS = frozenset({"stop_instance"})


@dataclass
class AgentRequest:
    goal: str
    requested_by: str
    max_turns: int = 12
    approval_timeout_s: int = 24 * 3600     # plain int: the default JSON converter has no timedelta


@dataclass
class LlmInput:
    messages: list[dict]       # the whole conversation so far, in Messages API shape
    system: str


@dataclass
class ToolCall:
    id: str                    # tool_use id from Claude, unique within the run
    name: str
    input: dict


@dataclass
class LlmOutput:
    content: list[dict]        # assistant content blocks, appended to history unchanged
    stop_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ToolRequest:
    call: ToolCall
    # workflow_id + tool_use id: the same logical action always carries the same key, however many times
    # Temporal retries the activity, so the downstream system can deduplicate it.
    idempotency_key: str


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False


@dataclass
class PendingApproval:
    call: ToolCall
    asked_at: str              # ISO timestamp from workflow.now(), deterministic on replay


@dataclass
class Decision:
    tool_use_id: str
    approved: bool
    reviewer: str
    reason: str = ""


@dataclass
class AgentStatus:
    phase: str                 # thinking | running_tools | awaiting_approval | done
    turn: int
    pending: list[PendingApproval]
    llm_calls: int
    input_tokens: int
    output_tokens: int


@dataclass
class AgentResult:
    outcome: str               # completed | refused | turn_limit
    answer: str
    actions: list[str]
    llm_calls: int
    input_tokens: int
    output_tokens: int
