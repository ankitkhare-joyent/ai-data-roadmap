"""A deterministic policy layer between the model and its tools.

Prompt injection cannot be reliably detected, so the controls that matter are the ones that still hold
when the model has been fooled. This module enforces three of them in plain code the model can't talk
its way past:

1. **The Agents Rule of Two** (Meta, 2025). In one session an agent may combine at most two of:
     [A] processing untrusted input       (tickets, web pages, emails, tool output from outside)
     [B] access to sensitive data or systems
     [C] changing state or communicating externally
   All three together is the lethal trifecta for exfiltration. Once a session has read untrusted input
   (it is *tainted*), any tool that does [C] needs a human, whatever the model says.
2. **Least privilege per tool**: a tool runs only if the caller's token carries its scope.
3. **Argument constraints**: egress goes only to allow-listed hosts, whatever URL the model produced.

The tool's risk profile comes from its declaration (MCP tool annotations plus two flags of our own), not
from the model's description of what it is about to do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class ToolRisk:
    scope: str                         # OAuth scope the caller's token must carry
    reads_untrusted: bool = False      # [A] output is attacker-controllable (taints the session)
    sensitive: bool = False            # [B] touches sensitive data or systems
    changes_state: bool = False        # [C] side effects or external communication
    egress_url_arg: str | None = None  # name of an argument holding a URL that must be allow-listed


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


@dataclass
class Session:
    """What this agent session has done so far. One per conversation, never shared between users."""
    scopes: frozenset[str]
    tainted_by: list[str] = field(default_factory=list)
    touched_sensitive: bool = False

    @property
    def tainted(self) -> bool:
        return bool(self.tainted_by)


class ToolPolicy:
    def __init__(self, risks: dict[str, ToolRisk], egress_allowlist: set[str]):
        self.risks, self.egress_allowlist = risks, egress_allowlist

    def check(self, session: Session, tool: str, args: dict) -> Decision:
        risk = self.risks.get(tool)
        if risk is None:
            return Decision(Verdict.DENY, f"unknown tool {tool!r}: deny by default")
        if risk.scope not in session.scopes:
            return Decision(Verdict.DENY, f"token lacks scope {risk.scope!r}")
        if risk.egress_url_arg:
            host = urlparse(str(args.get(risk.egress_url_arg, ""))).hostname or ""
            if host not in self.egress_allowlist:
                return Decision(Verdict.DENY, f"egress to {host or 'nowhere'!r} is not allow-listed")

        legs = {
            "A": session.tainted or risk.reads_untrusted,
            "B": session.touched_sensitive or risk.sensitive,
            "C": risk.changes_state,
        }
        if all(legs.values()):
            src = ", ".join(session.tainted_by) or tool
            return Decision(Verdict.NEEDS_APPROVAL,
                            f"rule of two: untrusted input ({src}) + sensitive access + state change")
        if risk.changes_state and session.tainted:
            # Two legs only, but a state change steered by untrusted text still gets a human.
            return Decision(Verdict.NEEDS_APPROVAL, f"state change after untrusted input from {session.tainted_by}")
        return Decision(Verdict.ALLOW, "ok")

    def record(self, session: Session, tool: str) -> None:
        """Call after a tool actually ran, so the session's legs reflect what really happened."""
        risk = self.risks[tool]
        if risk.reads_untrusted and tool not in session.tainted_by:
            session.tainted_by.append(tool)
        if risk.sensitive:
            session.touched_sensitive = True
