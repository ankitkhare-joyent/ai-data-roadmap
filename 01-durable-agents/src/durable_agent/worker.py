"""The worker process: polls the task queue and runs workflow and activity code.

Run as many replicas as you like; they share the queue. Killing one mid-run is the point of the lesson:
another (or the same one, restarted) picks the workflow up from its history.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from durable_agent.activities import Activities
from durable_agent.bao import BaoClient
from durable_agent.codec import EncryptionCodec, data_converter
from durable_agent.fleet import Fleet
from durable_agent.models import TASK_QUEUE
from durable_agent.workflow import CostCleanupAgent


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bao = BaoClient()
    codec = await EncryptionCodec.from_openbao(bao)
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"),
                                  namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
                                  data_converter=data_converter(codec))
    acts = Activities(bao, Fleet(), llm_mode=os.environ.get("LLM_MODE", "scripted"))
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CostCleanupAgent],
        activities=[acts.llm_step, acts.run_tool],
        max_concurrent_activities=20,
        # On SIGTERM, stop polling and give running activities this long to finish before exiting.
        graceful_shutdown_timeout=timedelta(seconds=20),
    )
    logging.getLogger("durable_agent").info("worker polling %s (llm_mode=%s)", TASK_QUEUE, acts.llm_mode)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
