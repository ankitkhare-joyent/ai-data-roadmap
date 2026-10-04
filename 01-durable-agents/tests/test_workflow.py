"""Workflow tests on Temporal's time-skipping test server: a 24-hour approval timeout takes milliseconds.

The real activities run against a temp fleet file with the scripted LLM, so these tests exercise the
whole loop (approvals, rejections, timeouts, retries, idempotency) without Docker or an API key.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WorkflowHandle, WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from durable_agent.activities import Activities
from durable_agent.fleet import Fleet
from durable_agent.models import TASK_QUEUE, AgentRequest, Decision
from durable_agent.workflow import CostCleanupAgent

DEV, TEST, GPU, PROD = ("i-0a1b2c3d4e5f60001", "i-0a1b2c3d4e5f60003", "i-0a1b2c3d4e5f60004", "i-0a1b2c3d4e5f60002")


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture
def fleet(tmp_path):
    return Fleet(str(tmp_path / "fleet.json"))


async def start(env, fleet, timeout=timedelta(hours=24)) -> tuple[Worker, WorkflowHandle]:
    acts = Activities(bao=None, fleet=fleet, llm_mode="scripted")
    queue = f"{TASK_QUEUE}-{uuid.uuid4()}"            # one queue per test: no cross-test task stealing
    worker = Worker(env.client, task_queue=queue, workflows=[CostCleanupAgent],
                    activities=[acts.llm_step, acts.run_tool])
    handle = await env.client.start_workflow(
        CostCleanupAgent.run, AgentRequest(goal="clean up", requested_by="test",
                                     approval_timeout_s=int(timeout.total_seconds())),
        id=f"wf-{uuid.uuid4()}", task_queue=queue)
    return worker, handle


async def wait_pending(handle, n: int):
    for _ in range(200):
        st = await handle.query(CostCleanupAgent.status)
        if len(st.pending) == n:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {n} pending approvals, got {len(st.pending)}")


def state(fleet, iid):
    return next(i["state"] for i in json.loads(fleet.path.read_text())["instances"] if i["instance_id"] == iid)


async def test_approve_some_reject_one(env, fleet):
    worker, h = await start(env, fleet)
    async with worker:
        st = await wait_pending(h, 3)                    # dev, test and GPU dev boxes; prod never proposed
        ids = {p.call.input["instance_id"]: p.call.id for p in st.pending}
        assert PROD not in ids
        await h.execute_update(CostCleanupAgent.decide, Decision(ids[DEV], True, "ankit"))
        await h.execute_update(CostCleanupAgent.decide, Decision(ids[TEST], True, "ankit"))
        await h.execute_update(CostCleanupAgent.decide, Decision(ids[GPU], False, "ankit", "training next week"))
        result = await h.result()
    assert result.outcome == "completed" and result.llm_calls == 3
    assert len(result.actions) == 2
    assert (state(fleet, DEV), state(fleet, TEST), state(fleet, GPU), state(fleet, PROD)) == \
        ("stopped", "stopped", "running", "running")


async def test_timeout_rejects_everything(env, fleet):
    worker, h = await start(env, fleet, timeout=timedelta(hours=24))
    async with worker:
        await wait_pending(h, 3)
        result = await h.result()                         # time-skipping fires the 24h timer immediately
    assert result.actions == []
    assert state(fleet, DEV) == "running"


async def test_validator_refuses_bad_updates(env, fleet):
    worker, h = await start(env, fleet)
    async with worker:
        st = await wait_pending(h, 3)
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await h.execute_update(CostCleanupAgent.decide, Decision("toolu_nope", True, "ankit"))
        assert "nothing pending" in str(e.value.cause)
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await h.execute_update(CostCleanupAgent.decide, Decision(st.pending[0].call.id, True, " "))
        assert "reviewer" in str(e.value.cause)
        await h.terminate("test done")


async def test_retry_after_lost_response_does_not_double_stop(env, fleet, monkeypatch):
    monkeypatch.setenv("FLEET_FLAKY_STOPS", "1")          # first attempt of each stop "loses" its response
    worker, h = await start(env, fleet)
    async with worker:
        st = await wait_pending(h, 3)
        for p in st.pending:
            await h.execute_update(CostCleanupAgent.decide, Decision(p.call.id, True, "ankit"))
        result = await h.result()
    assert len(result.actions) == 3
    stops = json.loads(fleet.path.read_text())["stops"]
    assert len(stops) == 3                                 # one record per action despite the retries


def test_fleet_refuses_prod(fleet):
    from durable_agent.fleet import FleetError
    with pytest.raises(FleetError, match="prod"):
        fleet.stop(PROD, "k1")
