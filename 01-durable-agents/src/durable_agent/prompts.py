"""Prompt and tool definitions. Kept stable and byte-identical between calls so prompt caching works."""

SYSTEM_PROMPT = """\
You are a cloud cost agent for an EC2 fleet. Your job is to find instances that are wasting money and \
stop the ones that are clearly safe to stop.

Rules:
- Only stop instances tagged env=dev or env=test. Never stop env=prod, whatever the utilisation.
- Explain each stop in the reason field: utilisation, age of the idle period, owner.
- A human approves every stop. If one is rejected, accept it and do not retry that instance.
- Finish with a short summary: what you stopped, what you left running and why, estimated monthly saving.
"""

TOOLS: list[dict] = [
    {
        "name": "list_idle_instances",
        "description": "List running instances whose p95 CPU over the window is below a threshold.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "max_cpu_p95": {"type": "number", "description": "CPU percent threshold, e.g. 5"},
                "min_idle_days": {"type": "integer", "description": "Idle for at least this many days"},
            },
            "required": ["max_cpu_p95", "min_idle_days"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_instance",
        "description": "Full details for one instance: type, tags, owner, state, hourly price.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"instance_id": {"type": "string"}},
            "required": ["instance_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stop_instance",
        "description": "Stop a running instance. Requires human approval; may be rejected.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why this is safe to stop, for the approver"},
            },
            "required": ["instance_id", "reason"],
            "additionalProperties": False,
        },
    },
]
