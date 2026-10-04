"""Embeddings into Lance, joined back to Iceberg by ticket_id.

Why Lance next to Iceberg: Iceberg is the system of record for tables (and v3 adds VARIANT, row lineage
and deletion vectors). Lance is a columnar format built for ML access patterns: fast random access, vector
indexes stored with the data, cheap column additions, multimodal blobs. A common 2026 layout keeps
facts in Iceberg and vectors and media in Lance, keyed by the same id.

The embedder here is a deterministic hashing trick so the lesson runs offline; swap in a real model
(bge, e5, or a hosted embedding API) in jobs/ray_enrich.py for production.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

os.environ.setdefault("LANCE_LOG", "error")   # Lance warns that 2,300 rows is a tiny index; we know

import lance
import numpy as np
import pyarrow as pa

from lakehouse_ai.tables import catalog, table

DIM = 256


def embed(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        words = re.findall(r"[a-z0-9]+", t.lower())
        for a, b in zip(words, words[1:] + [""], strict=True):   # unigrams + bigrams
            for tok in (a, f"{a}_{b}"):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
                out[i, h % DIM] += 1.0 if (h >> 63) == 0 else -1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms == 0, 1, norms)


def lance_path(root: str | None = None) -> str:
    return str(Path(root or os.environ.get("LAKEHOUSE_DIR", ".lakehouse")).resolve() / "lance" / "tickets.lance")


def build(root: str | None = None) -> int:
    cat = catalog(root)
    t = table(cat, "tickets").scan(selected_fields=("ticket_id", "text")).to_arrow()
    vecs = embed(t["text"].to_pylist())
    data = pa.table({"ticket_id": t["ticket_id"], "text": t["text"],
                     "vector": pa.FixedSizeListArray.from_arrays(pa.array(vecs.ravel()), DIM)})
    ds = lance.write_dataset(data, lance_path(root), mode="overwrite")
    # IVF_PQ: cluster vectors into partitions, compress with product quantisation. Search probes a few
    # partitions instead of scanning everything. Needs a few hundred rows per partition to train well.
    ds.create_index("vector", index_type="IVF_PQ", num_partitions=8, num_sub_vectors=16, metric="cosine")
    return ds.count_rows()


def search(query: str, k: int = 5, root: str | None = None) -> list[dict]:
    ds = lance.dataset(lance_path(root))
    res = ds.to_table(nearest={"column": "vector", "q": embed([query])[0], "k": k, "nprobes": 4},
                      columns=["ticket_id", "text", "_distance"])
    return res.to_pylist()
