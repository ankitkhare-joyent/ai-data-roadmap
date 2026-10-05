"""A scripted multi-session agent that uses the memory tool exactly as Claude does, plus a memory guard.

The scripted agent issues the same `memory` tool commands Claude issues (view, create, str_replace, ...),
so the store and the guard are exercised end to end without an API key. claude_demo.py runs the same
backend with the real model.

Memory turns a one-off prompt injection into a persistent one: text an attacker gets in front of the agent
today can be written to memory and obeyed in every future session. The guard tracks where a write came
from. Writes whose content arrived through untrusted tool output are quarantined for human review instead
of landing in memory (the rule-of-two idea from lesson 3, applied to memory).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from agent_memory.store import MemoryStore

PROFILE = "/memories/profile.md"
REMEMBER = re.compile(r"(?:remember that|actually,)\s+(?:my\s+|the\s+)?(.+?)\s+is\s+(?:now\s+)?(.+?)[.!]?$", re.IGNORECASE)
ASK = re.compile(r"what is (?:my|the)\s+(.+?)\?", re.IGNORECASE)


@dataclass
class Turn:
    user: str
    tool_output: str | None = None     # untrusted content the agent reads during the turn (a ticket, a page)


@dataclass
class MemoryGuard:
    enabled: bool = True
    quarantine: list[dict] = field(default_factory=list)

    def allow(self, source: str, content: str) -> bool:
        if not self.enabled or source == "user":
            return True
        self.quarantine.append({"source": source, "content": content})
        return False


class ScriptedAgent:
    def __init__(self, store: MemoryStore, guard: MemoryGuard):
        self.store, self.guard = store, guard
        self.session = uuid.uuid4().hex[:8]
        self.calls: list[dict] = []

    def _mem(self, **cmd):
        self.calls.append(cmd)
        return self.store.execute(cmd, session=self.session, source=cmd.pop("_source", "user"))

    def _facts(self) -> dict[str, str]:
        r = self._mem(command="view", path=PROFILE)
        if r.is_error:
            return {}
        facts = {}
        for line in r.content.split("\n")[1:]:
            m = re.match(r"\s*\d+\t- (.+?): (.+?) \(", line)
            if m:
                facts[m.group(1).lower()] = m.group(2)
        return facts

    def _remember(self, key: str, value: str, source: str) -> None:
        if not self.guard.allow(source, f"{key}: {value}"):
            return
        line = f"- {key.lower()}: {value} (session {self.session})"
        r = self._mem(command="view", path=PROFILE)
        if r.is_error:
            self._mem(command="create", path=PROFILE, file_text=f"# Profile\n{line}\n", _source=source)
            return
        # Find the current line for this key in the view output ("     N<TAB>- key: value (session ..)").
        existing = re.search(rf"^\s*\d+\t(- {re.escape(key.lower())}: .*)$", r.content, re.MULTILINE)
        if existing:      # update in place: one current value per key, never two contradicting lines
            self._mem(command="str_replace", path=PROFILE, old_str=existing.group(1), new_str=line, _source=source)
        else:
            self._mem(command="insert", path=PROFILE, insert_line=1, insert_text=line, _source=source)

    def turn(self, t: Turn) -> str:
        if not self.calls:
            self._mem(command="view", path="/memories")          # memory protocol: always look first
        for text, source in ((t.user, "user"), (t.tool_output, "tool_output")):
            if not text:
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                if m := REMEMBER.search(sentence):
                    self._remember(m.group(1), m.group(2), source)
        if m := ASK.search(t.user):
            value = self._facts().get(m.group(1).lower())
            return value if value else "I don't know."
        return "ok"
