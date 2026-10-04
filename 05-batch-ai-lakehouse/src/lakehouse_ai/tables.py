"""The Iceberg catalog and table definitions.

Locally: PyIceberg's SQL catalog (SQLite) over a folder, so the lesson runs with no services at all.
Production: the same code with `load_catalog("glue", **{"type": "glue"})` (AWS Glue, which you run
today) or a REST catalog such as Lakekeeper (learn-ai-agent). Only `catalog()` changes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import DayTransform
from pyiceberg.types import (
    DoubleType,
    IntegerType,
    ListType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

NS = "ops"

TICKETS = Schema(
    NestedField(1, "ticket_id", StringType(), required=True),
    NestedField(2, "created_at", TimestamptzType(), required=True),
    NestedField(3, "account", StringType()),
    NestedField(4, "instance_id", StringType()),
    NestedField(5, "text", StringType(), required=True),
    NestedField(6, "gold_category", StringType()),   # human labels on a sample, for evals
    NestedField(7, "gold_severity", StringType()),
    identifier_field_ids=[1],
)

ENRICHMENT = Schema(
    NestedField(1, "ticket_id", StringType(), required=True),
    NestedField(2, "prompt_version", StringType(), required=True),
    NestedField(3, "model", StringType(), required=True),
    NestedField(4, "text_hash", StringType(), required=True),   # re-enrich when the ticket text changes
    NestedField(5, "category", StringType()),
    NestedField(6, "severity", StringType()),
    NestedField(7, "instance_ids", ListType(8, StringType(), element_required=True)),
    NestedField(9, "summary", StringType()),
    NestedField(10, "input_tokens", IntegerType()),
    NestedField(11, "output_tokens", IntegerType()),
    NestedField(12, "run_id", StringType(), required=True),
    NestedField(13, "enriched_at", TimestamptzType(), required=True),
    identifier_field_ids=[1, 2],
)

RUNS = Schema(
    NestedField(1, "run_id", StringType(), required=True),
    NestedField(2, "started_at", TimestamptzType(), required=True),
    NestedField(3, "finished_at", TimestamptzType(), required=True),
    NestedField(4, "llm_mode", StringType()),
    NestedField(5, "model", StringType()),
    NestedField(6, "prompt_version", StringType()),
    NestedField(7, "tickets_snapshot_id", LongType()),    # exactly which data this run read
    NestedField(8, "rows_considered", IntegerType()),
    NestedField(9, "rows_cached", IntegerType()),
    NestedField(10, "rows_called", IntegerType()),
    NestedField(11, "rows_failed", IntegerType()),
    NestedField(12, "input_tokens", LongType()),
    NestedField(13, "output_tokens", LongType()),
    NestedField(14, "est_cost_usd", DoubleType()),
)

ERRORS = Schema(
    NestedField(1, "run_id", StringType(), required=True),
    NestedField(2, "ticket_id", StringType(), required=True),
    NestedField(3, "error", StringType()),
    NestedField(4, "raw", StringType()),
)


def catalog(root: str | None = None) -> Catalog:
    root_path = Path(root or os.environ.get("LAKEHOUSE_DIR", ".lakehouse")).resolve()
    (root_path / "warehouse").mkdir(parents=True, exist_ok=True)
    cat = SqlCatalog("local", uri=f"sqlite:///{root_path}/catalog.db", warehouse=f"file://{root_path}/warehouse")
    try:
        cat.create_namespace(NS)
    except NamespaceAlreadyExistsError:
        pass
    return cat


def table(cat: Catalog, name: str) -> Table:
    try:
        return cat.load_table(f"{NS}.{name}")
    except NoSuchTableError:
        pass
    schema = {"tickets": TICKETS, "ticket_enrichment": ENRICHMENT, "enrichment_runs": RUNS,
              "enrichment_errors": ERRORS}[name]
    spec = PartitionSpec()
    if name == "tickets":
        spec = PartitionSpec(PartitionField(source_id=2, field_id=1000, transform=DayTransform(), name="created_day"))
    return cat.create_table(f"{NS}.{name}", schema=schema, partition_spec=spec,
                            properties={"format-version": "2", "write.parquet.compression-codec": "zstd"})


def arrow(tbl: Table, rows: list[dict]) -> pa.Table:
    """Rows to an Arrow table with exactly the table's schema (required fields stay non-nullable).

    Use the *table's* schema, not the definition above: the catalog assigns its own field ids on create.
    """
    return pa.Table.from_pylist(rows, schema=tbl.schema().as_arrow())
