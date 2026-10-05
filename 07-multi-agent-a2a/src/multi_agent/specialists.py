"""Two specialist agents served over A2A (a2a-sdk 1.2, protocol 1.0), each owned by a different team.

  fleet-analyst    answers questions about the EC2 fleet (the data platform team's agent)
  runbook-expert   returns the remediation runbook for an issue (the SRE team's agent); asks back with
                   `input-required` when it needs an instance id it wasn't given

What A2A standardises, and what each piece below shows:
  Agent Card   /.well-known/agent-card.json: who I am, my skills, my endpoints, how to authenticate.
               It's how an orchestrator discovers capabilities without sharing code or prompts.
  Task         a unit of work with a lifecycle: submitted -> working -> completed / failed / canceled,
               or input-required when the agent needs more from the caller (multi-turn).
  Artifacts    the outputs of a task, separate from chatty status messages.
  Opaqueness   the caller never sees the specialist's prompt, tools, model or data. That is the point:
               teams can change their agent's internals without breaking anyone.

MCP connects an agent to tools; A2A connects agents to agents, typically across team or company lines.
"""
from __future__ import annotations

import asyncio
import re
import secrets

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Part,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    Task,
    TaskState,
    TaskStatus,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FLEET = {
    "i-0a1b2c3d4e5f60001": {"env": "dev", "type": "m6i.2xlarge", "cpu_p95": 1.8, "owner": "data-eng"},
    "i-0a1b2c3d4e5f60002": {"env": "prod", "type": "r6i.4xlarge", "cpu_p95": 91.0, "owner": "trino"},
    "i-0a1b2c3d4e5f60003": {"env": "test", "type": "c6i.xlarge", "cpu_p95": 0.9, "owner": "platform"},
}
RUNBOOKS = {
    "cpu": "RB-12 High CPU: 1) check the top queries in Trino UI 2) scale the worker pool 3) if a single query, "
           "kill it and notify the owner.",
    "idle": "RB-31 Idle instance: 1) confirm no traffic for 7 days 2) snapshot volumes 3) stop with owner approval.",
}
IID = re.compile(r"i-[0-9a-f]{17}")


class FleetAnalyst(AgentExecutor):
    latency_s = 0.4    # stands in for an LLM + SQL round trip

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        up = await _start(context, event_queue, "Looking up the fleet...")
        await asyncio.sleep(self.latency_s)
        q = context.get_user_input()
        ids = IID.findall(q)
        if ids:
            lines = [f"{i}: {FLEET[i]}" if i in FLEET else f"{i}: not found" for i in ids]
        else:
            hot = [i for i, v in FLEET.items() if v["cpu_p95"] > 80]
            idle = [i for i, v in FLEET.items() if v["cpu_p95"] < 5]
            lines = [f"hot (p95 CPU > 80%): {hot}", f"idle (p95 CPU < 5%): {idle}"]
        await up.add_artifact(parts=[Part(text="\n".join(lines))], name="fleet-facts", last_chunk=True)
        await up.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()


class RunbookExpert(AgentExecutor):
    latency_s = 0.4

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        up = await _start(context, event_queue, "Finding the runbook...")
        await asyncio.sleep(self.latency_s)
        q = context.get_user_input().lower()
        if not IID.search(q):
            # Multi-turn: ask the caller for what we need instead of guessing. The task stays open.
            await up.requires_input(message=up.new_agent_message(
                parts=[Part(text="Which instance id is this about? I need it to pick the right runbook.")]))
            return
        topic = "cpu" if ("cpu" in q or "hot" in q or "slow" in q) else "idle"
        await up.add_artifact(parts=[Part(text=RUNBOOKS[topic])], name="runbook", last_chunk=True)
        await up.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()


async def _start(context: RequestContext, event_queue: EventQueue, note: str) -> TaskUpdater:
    if context.current_task is None:   # first message of a new task
        await event_queue.enqueue_event(Task(id=context.task_id, context_id=context.context_id,
                                             status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                                             history=[context.message]))
    up = TaskUpdater(event_queue=event_queue, task_id=context.task_id, context_id=context.context_id)
    await up.start_work(message=up.new_agent_message(parts=[Part(text=note)]))
    return up


def card(name: str, description: str, skill: AgentSkill, base_url: str, team: str) -> AgentCard:
    return AgentCard(
        name=name, description=description, version="1.0.0",
        provider=AgentProvider(organization=team, url="https://acme.example"),
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"], default_output_modes=["text"],
        skills=[skill],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0",
                                             url=f"{base_url}/a2a/jsonrpc")],
        # Declared in the card so callers know how to authenticate before they call. In production the
        # bearer token is a short-lived JWT for this agent's audience, minted by OpenBao (lesson 3).
        security_schemes={"bearer": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer", bearer_format="JWT"))},
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList()})],
    )


def build_app(kind: str, base_url: str, token: str) -> FastAPI:
    if kind == "fleet":
        executor, c = FleetAnalyst(), card(
            "fleet-analyst", "Answers questions about the EC2 fleet: utilisation, owners, environments.",
            AgentSkill(id="fleet_facts", name="Fleet facts", description="Facts about EC2 instances",
                       tags=["ec2", "fleet"], examples=["Which instances are idle?"]), base_url, "data-platform")
    else:
        executor, c = RunbookExpert(), card(
            "runbook-expert", "Returns the remediation runbook for an operational issue on an instance.",
            AgentSkill(id="runbook", name="Runbook lookup", description="Runbook for an issue",
                       tags=["sre", "runbook"], examples=["Runbook for high CPU on i-..."]), base_url, "sre")
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=c)
    app = FastAPI()
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(agent_card=c),
                              jsonrpc_routes=create_jsonrpc_routes(request_handler=handler, rpc_url="/a2a/jsonrpc"))

    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        # The card is public (that's how discovery works); everything else needs the token.
        if request.url.path.startswith("/.well-known/"):
            return await call_next(request)
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(sent, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="a2a"'})
        request.state.traceparent = request.headers.get("traceparent")
        return await call_next(request)

    return app
