"""Calling a remote agent over A2A: discover its card, authenticate, send a task, follow it to the end.

The wrapper is where production concerns live, the same ones you'd put around any remote service call:
a deadline, an auth header, a trace id that ties the remote work to the caller's trace, and a small,
typed result instead of raw protocol events.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.client.errors import A2AClientError
from a2a.helpers import get_artifact_text, get_message_text
from a2a.types import AgentCard, Message, Part, Role, SendMessageRequest, TaskState

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}


@dataclass
class Reply:
    agent: str
    state: str                  # TASK_STATE_COMPLETED, TASK_STATE_INPUT_REQUIRED, ..., or TIMEOUT / ERROR
    text: str
    task_id: str | None = None
    context_id: str | None = None
    seconds: float = 0.0
    events: list[str] = field(default_factory=list)


class RemoteAgent:
    def __init__(self, base_url: str, token: str, traceparent: str | None = None, timeout_s: float = 20):
        self.base_url, self.timeout_s = base_url, timeout_s
        headers = {"Authorization": f"Bearer {token}"}
        if traceparent:
            headers["traceparent"] = traceparent        # W3C trace context: one trace across all agents
        self._http = httpx.AsyncClient(headers=headers, timeout=timeout_s)
        self.card: AgentCard | None = None
        self._client = None

    async def connect(self) -> AgentCard:
        self.card = await A2ACardResolver(self._http, self.base_url).get_agent_card()
        self._client = await create_client(self.card, client_config=ClientConfig(httpx_client=self._http,
                                                                                 streaming=True))
        return self.card

    async def ask(self, text: str, task_id: str | None = None, context_id: str | None = None) -> Reply:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        msg = Message(role=Role.ROLE_USER, message_id=str(uuid.uuid4()), parts=[Part(text=text)],
                      task_id=task_id, context_id=context_id or str(uuid.uuid4()))
        reply = Reply(agent=self.card.name, state="TASK_STATE_SUBMITTED", text="", task_id=task_id,
                      context_id=msg.context_id)
        try:
            async with asyncio.timeout(self.timeout_s):
                async for ev in self._client.send_message(SendMessageRequest(message=msg)):
                    if ev.HasField("task"):
                        reply.task_id, reply.state = ev.task.id, TaskState.Name(ev.task.status.state)
                    elif ev.HasField("status_update"):
                        reply.state = TaskState.Name(ev.status_update.status.state)
                        if ev.status_update.status.HasField("message"):
                            note = get_message_text(ev.status_update.status.message, delimiter=" ")
                            reply.events.append(f"{reply.state}: {note}")
                            if reply.state == "TASK_STATE_INPUT_REQUIRED":
                                reply.text = note
                    elif ev.HasField("artifact_update"):
                        reply.text += get_artifact_text(ev.artifact_update.artifact, delimiter=" ")
                    elif ev.HasField("message"):
                        reply.text, reply.state = get_message_text(ev.message, delimiter=" "), "TASK_STATE_COMPLETED"
                    if reply.state in TERMINAL or reply.state == "TASK_STATE_INPUT_REQUIRED":
                        break
        except TimeoutError:
            reply.state = "TIMEOUT"
        except (httpx.HTTPError, A2AClientError) as e:   # 401/403, connection refused, protocol errors
            reply.state, reply.text = "ERROR", str(e)[:200]
        reply.seconds = loop.time() - t0
        return reply

    async def close(self) -> None:
        if self._client:
            await self._client.close()
        await self._http.aclose()


def new_traceparent() -> str:
    return f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-01"
