"""Tool poisoning and rug pulls: treat MCP tool definitions like dependencies.

A tool's description goes straight into the model's context, so a malicious or compromised MCP server can
inject instructions through it ("tool poisoning"), or ship a harmless description on day one and change
it later ("rug pull"). Defences: review tool definitions once, pin them by hash like a lockfile, refuse
to start when a pinned definition changes, and scan descriptions for injection text.
"""
from __future__ import annotations

import hashlib
import json

from secure_agent import guard


def fingerprint(spec: dict) -> str:
    canonical = json.dumps({k: spec.get(k) for k in ("name", "description", "input_schema")}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def pin(specs: list[dict]) -> dict[str, str]:
    return {s["name"]: fingerprint(s) for s in specs}


def verify(specs: list[dict], pins: dict[str, str]) -> list[str]:
    """Problems that should stop the agent from starting. Empty list means safe to load."""
    problems = []
    for s in specs:
        name = s["name"]
        if name not in pins:
            problems.append(f"{name}: new tool, not reviewed")
        elif pins[name] != fingerprint(s):
            problems.append(f"{name}: definition changed since it was pinned (possible rug pull)")
        text, hidden = guard.normalise(s.get("description", ""))
        hits = guard.scan(text).injection_hits
        if hits or hidden:
            problems.append(f"{name}: description looks like an injection ({hits or 'hidden unicode'})")
    return problems
