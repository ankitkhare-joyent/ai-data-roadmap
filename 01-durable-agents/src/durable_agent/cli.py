"""Operator CLI: start a run, watch it, approve or reject actions, read the result.

    python -m durable_agent.cli start  [--goal "..."] [--id cleanup-1]
    python -m durable_agent.cli status  cleanup-1
    python -m durable_agent.cli approve cleanup-1 <tool_use_id|all> --reviewer ankit
    python -m durable_agent.cli reject  cleanup-1 <tool_use_id> --reviewer ankit --reason "owner on leave"
    python -m durable_agent.cli result  cleanup-1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy

from durable_agent.bao import BaoClient
from durable_agent.codec import EncryptionCodec, data_converter
from durable_agent.models import TASK_QUEUE, AgentRequest, Decision
from durable_agent.workflow import CostCleanupAgent

DEFAULT_GOAL = "Find EC2 instances idle for at least 7 days (p95 CPU under 5%) and stop the ones that are safe to stop."


async def connect() -> Client:
    codec = await EncryptionCodec.from_openbao(BaoClient())
    return await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"),
                                data_converter=data_converter(codec))


def show(obj) -> None:
    print(json.dumps(asdict(obj), indent=2, default=str))


async def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="durable_agent.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--goal", default=DEFAULT_GOAL)
    s.add_argument("--id", default="cleanup-1")
    s.add_argument("--by", default=os.environ.get("USER", "operator"))
    for name in ("status", "result"):
        sub.add_parser(name).add_argument("id")
    for name in ("approve", "reject"):
        d = sub.add_parser(name)
        d.add_argument("id")
        d.add_argument("tool_use_id")
        d.add_argument("--reviewer", required=True)
        d.add_argument("--reason", default="")
    a = p.parse_args()

    client = await connect()
    if a.cmd == "start":
        # The workflow id is the business key: starting the same id twice is refused while one is running,
        # which makes "start" itself idempotent.
        h = await client.start_workflow(CostCleanupAgent.run, AgentRequest(goal=a.goal, requested_by=a.by),
                                        id=a.id, task_queue=TASK_QUEUE,
                                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        print(f"started {h.id} run={h.result_run_id}")
        return

    h = client.get_workflow_handle_for(CostCleanupAgent.run, a.id)   # typed: result() returns AgentResult
    if a.cmd == "status":
        show(await h.query(CostCleanupAgent.status))
    elif a.cmd == "result":
        show(await h.result())
    else:
        st = await h.query(CostCleanupAgent.status)
        ids = [x.call.id for x in st.pending] if a.tool_use_id == "all" else [a.tool_use_id]
        for tid in ids:
            try:
                msg = await h.execute_update(CostCleanupAgent.decide,
                                             Decision(tid, a.cmd == "approve", a.reviewer, a.reason))
                print(f"{tid}: {msg}")
            except WorkflowUpdateFailedError as e:
                print(f"{tid}: refused: {e.cause}")


if __name__ == "__main__":
    asyncio.run(main())
