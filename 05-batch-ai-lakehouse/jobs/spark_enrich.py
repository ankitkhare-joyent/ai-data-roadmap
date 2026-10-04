"""Reference: the same incremental enrichment as a Spark job on EMR on EKS, writing with MERGE INTO.

Not run in this lesson (no Spark cluster locally); it mirrors src/lakehouse_ai/enrich.py line for line.
Submit with spark-submit or an EMR on EKS StartJobRun, with the Iceberg runtime and Glue catalog:

  --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions
  --conf spark.sql.catalog.glue=org.apache.iceberg.spark.SparkCatalog
  --conf spark.sql.catalog.glue.type=glue
  --conf spark.sql.catalog.glue.warehouse=s3://acme-lake/warehouse

Rate limits are the new shuffle: total concurrency = executors x cores x PER_TASK_CONCURRENCY must stay
under your Anthropic (or gateway) limits. Route through the lesson 2 gateway with a team virtual key, and
for large backfills prefer the Batch API from an Airflow task instead (half the price, no rate juggling).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pandas as pd
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

PROMPT_VERSION = "triage-v1"
MODEL = os.environ.get("MODEL", "claude-haiku-4-5")
PER_TASK_CONCURRENCY = int(os.environ.get("PER_TASK_CONCURRENCY", "8"))
OUT_SCHEMA = ("ticket_id string, prompt_version string, model string, text_hash string, category string, "
              "severity string, instance_ids array<string>, summary string, input_tokens int, output_tokens int, "
              "error string")


def enrich_partition(batches):
    """Runs on executors. One async client per task, a semaphore for concurrency, the gateway as base_url."""
    import anthropic

    from lakehouse_ai.llm import SCHEMA, SYSTEM, validate   # ship src/ as a zip with --py-files

    client = anthropic.AsyncAnthropic(base_url=os.environ.get("GATEWAY_URL"),   # virtual key in ANTHROPIC_API_KEY
                                      max_retries=4)

    async def run(df: pd.DataFrame) -> pd.DataFrame:
        sem = asyncio.Semaphore(PER_TASK_CONCURRENCY)

        async def one(row):
            async with sem:
                try:
                    msg = await client.messages.create(
                        model=MODEL, max_tokens=1024, system=SYSTEM,
                        messages=[{"role": "user", "content": f"<ticket>\n{row.text}\n</ticket>"}],
                        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
                    res = validate(json.loads(next(b.text for b in msg.content if b.type == "text")))
                    return {**res, "input_tokens": msg.usage.input_tokens,
                            "output_tokens": msg.usage.output_tokens, "error": None}
                except Exception as e:  # noqa: BLE001  one bad row must not fail the task
                    return {"category": None, "severity": None, "instance_ids": [], "summary": None,
                            "input_tokens": 0, "output_tokens": 0, "error": str(e)[:500]}

        results = await asyncio.gather(*(one(r) for r in df.itertuples()))
        out = pd.DataFrame(results)
        out.insert(0, "ticket_id", df["ticket_id"].values)
        out.insert(1, "prompt_version", PROMPT_VERSION)
        out.insert(2, "model", MODEL)
        out.insert(3, "text_hash", df["text_hash"].values)
        return out

    for pdf in batches:
        yield asyncio.run(run(pdf))


def main() -> None:
    spark = SparkSession.builder.appName("ticket-enrichment").getOrCreate()
    hash_udf = F.udf(lambda t: hashlib.sha256(t.strip().encode()).hexdigest()[:32])
    tickets = spark.table("glue.ops.tickets").select("ticket_id", "text").withColumn("text_hash", hash_udf("text"))
    done = (spark.table("glue.ops.ticket_enrichment")
            .where((F.col("prompt_version") == PROMPT_VERSION) & (F.col("model") == MODEL))
            .select("ticket_id", F.col("text_hash").alias("done_hash")))
    todo = (tickets.join(done, "ticket_id", "left")
            .where(F.col("done_hash").isNull() | (F.col("done_hash") != F.col("text_hash")))
            .drop("done_hash")
            .dropDuplicates(["text_hash"])          # pay once per distinct text, fan out after
            .repartition(64))                       # partitions x concurrency = total in-flight requests

    results = todo.mapInPandas(enrich_partition, schema=OUT_SCHEMA).cache()
    results.where("error is not null").writeTo("glue.ops.enrichment_errors").append()
    good = results.where("error is null").drop("error")
    # Fan the per-text result back out to every ticket with that text, then MERGE: idempotent on re-runs.
    fanned = tickets.select("ticket_id", "text_hash").join(good.drop("ticket_id"), "text_hash") \
        .withColumn("run_id", F.lit(spark.sparkContext.applicationId)) \
        .withColumn("enriched_at", F.current_timestamp())
    fanned.createOrReplaceTempView("new_enrichment")
    spark.sql("""
        MERGE INTO glue.ops.ticket_enrichment t
        USING new_enrichment s
        ON t.ticket_id = s.ticket_id AND t.prompt_version = s.prompt_version
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)


if __name__ == "__main__":
    main()
