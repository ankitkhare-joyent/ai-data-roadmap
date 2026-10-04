"""The fleet-ops tools, their risk profiles, and a tiny in-memory backend.

The ticket text is the attack surface: anyone who can file a ticket can put words in front of the agent.
data/tickets.json holds normal tickets plus poisoned ones; the red-team suite swaps in more payloads.
"""
from __future__ import annotations

import json
from pathlib import Path

from secure_agent.policy import ToolRisk

DATA = Path(__file__).resolve().parents[2] / "data"

RISKS: dict[str, ToolRisk] = {
    "search_tickets": ToolRisk(scope="tickets:read", reads_untrusted=True),
    "describe_instance": ToolRisk(scope="ec2:read", sensitive=True),
    "stop_instance": ToolRisk(scope="ec2:write", sensitive=True, changes_state=True),
    "post_webhook": ToolRisk(scope="notify:write", changes_state=True, egress_url_arg="url"),
}

EGRESS_ALLOWLIST = {"hooks.slack.com", "chat.internal.example"}

INSTANCES = {
    "i-0a1b2c3d4e5f60001": {"env": "dev", "owner": "data-eng", "private_ip": "10.0.12.7",
                            "iam_role": "dev-spark-runner", "state": "running"},
    "i-0a1b2c3d4e5f60002": {"env": "prod", "owner": "trino", "private_ip": "10.0.3.21",
                            "iam_role": "prod-trino-coordinator", "state": "running"},
}

TOOL_SPECS: list[dict] = [
    {"name": "search_tickets", "description": "Search ops tickets by instance id or keyword. Returns ticket text.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                      "additionalProperties": False}},
    {"name": "describe_instance", "description": "Details for one EC2 instance: env, owner, IP, IAM role.",
     "input_schema": {"type": "object", "properties": {"instance_id": {"type": "string"}},
                      "required": ["instance_id"], "additionalProperties": False}},
    {"name": "stop_instance", "description": "Stop an EC2 instance.",
     "input_schema": {"type": "object", "properties": {"instance_id": {"type": "string"},
                                                       "reason": {"type": "string"}},
                      "required": ["instance_id", "reason"], "additionalProperties": False}},
    {"name": "post_webhook", "description": "Post a short message to an approved chat webhook.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "text": {"type": "string"}},
                      "required": ["url", "text"], "additionalProperties": False}},
]


class FleetBackend:
    """Executes tools. Records side effects so tests and the red-team suite can see what really happened."""

    def __init__(self, tickets: list[dict] | None = None):
        self.tickets = tickets if tickets is not None else json.loads((DATA / "tickets.json").read_text())
        self.instances = {k: dict(v) for k, v in INSTANCES.items()}
        self.side_effects: list[dict] = []

    def call(self, name: str, args: dict) -> str:
        match name:
            case "search_tickets":
                q = args["query"].lower()
                hits = [t for t in self.tickets if q in json.dumps(t).lower()] or self.tickets[:2]
                return json.dumps(hits)
            case "describe_instance":
                inst = self.instances.get(args["instance_id"])
                return json.dumps(inst) if inst else f"no such instance {args['instance_id']}"
            case "stop_instance":
                self.instances[args["instance_id"]]["state"] = "stopped"
                self.side_effects.append({"tool": name, **args})
                return f"stopped {args['instance_id']}"
            case "post_webhook":
                self.side_effects.append({"tool": name, **args})
                return "posted"
        raise ValueError(f"unknown tool {name}")
