"""Reference: self-hosted batch inference with Ray Data + vLLM on KubeRay, Iceberg in and out.

Not run in this lesson (needs GPUs). Same contract as enrich.py: read the tickets snapshot, keep the rows
that need work, run a structured-output prompt on an open model, UPSERT on (ticket_id, prompt_version).
Submit as a RayJob on the KubeRay cluster you already run (Ray 2.59, image rayproject/ray-llm).

Why Ray Data for this: it streams rows from Iceberg through CPU preprocessing and GPU inference stages
at the same time, keeps the GPUs saturated with continuous batching, and scales the vLLM actor pool
between the bounds you give it. Prefix caching makes the shared system prompt nearly free per row.
"""
from __future__ import annotations

import hashlib
import json

import ray
from ray.data import SaveMode
from ray.data.llm import build_processor, vLLMEngineProcessorConfig

from lakehouse_ai.llm import PROMPT_VERSION, SCHEMA, SYSTEM, validate

MODEL = "Qwen/Qwen3-8B"          # small open model; measure it against Claude on the eval set first
CATALOG = {"name": "glue", "type": "glue", "warehouse": "s3://acme-lake/warehouse"}

config = vLLMEngineProcessorConfig(
    model_source=MODEL,
    engine_kwargs={"enable_prefix_caching": True, "max_model_len": 4096, "gpu_memory_utilization": 0.9},
    batch_size=64,
    concurrency=(1, 8),               # autoscale between 1 and 8 vLLM replicas (one GPU each)
    accelerator_type="L40S",
    should_continue_on_error=True,    # bad rows get __inference_error__ instead of failing the job
)

processor = build_processor(
    config,
    preprocess=lambda row: {
        **row,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"<ticket>\n{row['text']}\n</ticket>"}],
        "sampling_params": {"temperature": 0, "max_tokens": 256,
                            "structured_outputs": {"json": SCHEMA}},   # vLLM constrained decoding
    },
    postprocess=lambda row: {
        "ticket_id": row["ticket_id"], "prompt_version": PROMPT_VERSION, "model": MODEL,
        "text_hash": row["text_hash"], **validate(json.loads(row["generated_text"])),
    },
)

if __name__ == "__main__":
    tickets = ray.data.read_iceberg(table_identifier="ops.tickets", catalog_kwargs=CATALOG,
                                    selected_fields=("ticket_id", "text"))
    tickets = tickets.map(lambda r: {**r, "text_hash": hashlib.sha256(r["text"].strip().encode()).hexdigest()[:32]})
    # Incremental filtering as in enrich.py (anti-join on text_hash) is omitted here for brevity.
    enriched = processor(tickets)
    enriched.write_iceberg(table_identifier="ops.ticket_enrichment", catalog_kwargs=CATALOG,
                           mode=SaveMode.UPSERT, upsert_kwargs={"join_cols": ["ticket_id", "prompt_version"]})
