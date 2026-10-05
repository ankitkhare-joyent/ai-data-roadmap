"""Two sessions with the real model: Claude Opus 5.5, the memory tool, this lesson's MemoryStore.

    ANTHROPIC_API_KEY=... python -m agent_memory.claude_demo

Session 1 tells Claude a preference; session 2 is a brand-new conversation that has to recover it from
memory. Old tool results are cleared server-side with context editing, the pairing the memory tool docs
recommend for long-running agents.
"""
from __future__ import annotations

import tempfile

import anthropic

from agent_memory.store import MemoryStore

MODEL = "claude-opus-5-5"


def session(client: anthropic.Anthropic, store: MemoryStore, prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]
    for _ in range(12):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=4096, messages=messages,
            tools=[{"type": "memory_20250818", "name": "memory"}],
            betas=["context-management-2025-06-27"],
            context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
            output_config={"effort": "low"},
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type == "tool_use" and b.name == "memory":
                r = store.execute(dict(b.input), session="claude-demo", source="user")
                print(f"   memory {b.input.get('command')} {b.input.get('path', b.input.get('old_path', ''))}"
                      f"{' ERROR' if r.is_error else ''}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": r.content,
                                "is_error": r.is_error})
        messages.append({"role": "user", "content": results})
    return "(stopped after 12 steps)"


def main() -> None:
    client = anthropic.Anthropic()
    with tempfile.TemporaryDirectory() as base:
        store = MemoryStore(base, "acme", "ankit")
        print("== session 1")
        print(session(client, store, "I run the data platform team. Remember that our Spark jobs run on EMR on "
                                     "EKS in eu-west-1 and that I prefer answers with a short code sample."))
        print("\n== session 2 (new conversation, same memory)")
        print(session(client, store, "Which region do our Spark jobs run in, and how do I like answers?"))


if __name__ == "__main__":
    main()
