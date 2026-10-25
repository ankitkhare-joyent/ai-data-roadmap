# Lab 05 · Batch AI on the lakehouse: incremental, idempotent, and what goes wrong without it

One sitting, about 40 minutes, no Docker. Background: [lesson 5](../05-batch-ai-lakehouse/README.md). Code:
[tables.py](../05-batch-ai-lakehouse/src/lakehouse_ai/tables.py), [enrich.py](../05-batch-ai-lakehouse/src/lakehouse_ai/enrich.py),
[llm.py](../05-batch-ai-lakehouse/src/lakehouse_ai/llm.py), [embed.py](../05-batch-ai-lakehouse/src/lakehouse_ai/embed.py).

After this lab you can explain:

- what an Iceberg append and an upsert actually write, file by file and snapshot by snapshot
- how the job decides which rows need the model, and why a re-run costs nothing
- what replacing UPSERT with APPEND does to a table and to your bill, and how to repair it
- why a vector search can return the wrong tickets even when the index is perfect

## Before you start

```bash
cd ~/workstuff/argocd-terraform-opentofu/ai-data-roadmap/05-batch-ai-lakehouse
mise trust && mise install
mise run reset                  # deletes .lakehouse/ (catalog, tables, vectors)
source ../lab/env.sh            # sets PYTHONPATH=src
export LAKEHOUSE_DIR=.lakehouse
py() { uv run python -c "$1"; }
E='import asyncio; from lakehouse_ai import enrich; r = asyncio.run(enrich.run("scripted")); print({k: r[k] for k in ("rows_considered","rows_called","rows_cached","rows_failed","inserted","updated")})'
```

## 1. An Iceberg table from nothing

```bash
py "
from lakehouse_ai.tables import catalog, table, arrow
from lakehouse_ai.seed import tickets
t = table(catalog(), 'tickets'); t.append(arrow(t, tickets(2000)))
s = t.current_snapshot(); print(s.summary.operation, s.summary.additional_properties)
print('format', t.metadata.format_version, '| spec', t.spec(), '| identifier', t.schema().identifier_field_names())"
find .lakehouse/warehouse/ops/tickets/data -name '*.parquet' | wc -l
ls .lakehouse/warehouse/ops/tickets/metadata
```

**Predict (P1):** 2,000 tickets, one every 17 minutes, partitioned by `day(created_at)`. How many data files does one
append write? And how many files appear under `metadata/`, and what is each one?

## 2. The first run, and the second

```bash
py "$E"          # run 1
py "$E"          # run 2, no new data
py "
from lakehouse_ai.tables import catalog, table
t = table(catalog(), 'tickets').scan(selected_fields=('text',)).to_arrow()
print('distinct texts', len(set(t['text'].to_pylist())), 'of', t.num_rows)"
```

**Predict (P2):** run 1 inserts 2,000 rows. How many model calls does it make, and why not 2,000? What does run 2 do?

## 3. New data and an edit

```bash
py "
from datetime import datetime, UTC
from lakehouse_ai.tables import catalog, table, arrow
from lakehouse_ai.seed import tickets
t = table(catalog(), 'tickets'); t.append(arrow(t, tickets(300, start_id=2001, seed=7, start=datetime(2026, 10, 1, tzinfo=UTC))))"
py "$E"          # run 3
py "
from lakehouse_ai.tables import catalog, table, arrow
t = table(catalog(), 'tickets'); row = t.scan(row_filter=\"ticket_id == 'OPS-000001'\").to_arrow().to_pylist()[0]
print('before:', row['text'][:70])
row['text'] += ' UPDATE: now also failing health checks, production impact'
r = t.upsert(arrow(t, [row]), join_cols=['ticket_id']); print('upsert updated/inserted:', r.rows_updated, r.rows_inserted)"
py "$E"          # run 4
```

**Predict (P3):** what does run 3 consider? And run 4, after one ticket's text changed?

Now look at how the enrichment table got there:

```bash
py "
from lakehouse_ai.tables import catalog, table
e = table(catalog(), 'ticket_enrichment')
print([(s.summary.operation.value, s.summary.additional_properties.get('total-records')) for s in e.snapshots()])
print('first snapshot rows:', e.scan(snapshot_id=e.snapshots()[0].snapshot_id).count())
r = e.scan(row_filter=\"ticket_id == 'OPS-000001'\").to_arrow().to_pylist()[0]; print('OPS-000001 now:', r['category'], r['severity'])"
```

**Predict (P4):** three runs wrote rows. How many snapshots does `ticket_enrichment` have, and what does the
upsert look like among them? What category does the edited ticket get now?

## 4. Quality, and where it fails

```bash
py "
from lakehouse_ai.evaluate import evaluate; print(evaluate('scripted'))
from lakehouse_ai.tables import catalog, table
c = catalog(); g = {r['ticket_id']: r for r in table(c, 'tickets').scan(selected_fields=('ticket_id','text','gold_category')).to_arrow().to_pylist()}
for r in table(c, 'ticket_enrichment').scan(selected_fields=('ticket_id','category')).to_arrow().to_pylist():
    if g[r['ticket_id']]['gold_category'] == 'security' and r['category'] == 'outage': print(g[r['ticket_id']]['text'][:90]); break"
```

**Predict (P5):** the top confusion is security labelled as outage. Before you read the example: why would keyword
rules call a security ticket an outage?

## 5. Vectors in Lance

```bash
py "
from lakehouse_ai import embed
import lance
print('rows', embed.build())
ds = lance.dataset(embed.lance_path()); print('versions', len(ds.versions()), '| indices', [i['name'] if isinstance(i, dict) else i.name for i in ds.list_indices()])
for q in ('security group open to the internet', 'security group allows port 22, lock it down'):
    print('--', q)
    for h in embed.search(q, k=3): print(round(h['_distance'], 3), h['text'][:60])" 2>&1 | grep -v WARN
```

**Predict (P6):** which tickets come back for the first query? If they're wrong, is the IVF_PQ index to blame?
Check against an exact, brute-force search:

```bash
py "
import lance, numpy as np
from lakehouse_ai import embed
ds = lance.dataset(embed.lance_path()); q = embed.embed(['security group open to the internet'])[0]
t = ds.to_table(columns=['text', 'vector']); V = np.stack(t['vector'].to_numpy(zero_copy_only=False))
for i in np.argsort(-(V @ q))[:3]: print('exact cosine distance', round(1 - float((V @ q)[i]), 3), t['text'][int(i)].as_py()[:55])"
```

## Break it, then fix it

**Break 1: APPEND instead of UPSERT.** It's the one-line "simplification" people make:

```bash
sed -i '' 's/    upserted = enrichment.upsert(arrow(enrichment, rows), join_cols=\["ticket_id", "prompt_version"\]) if rows else None/    enrichment.append(arrow(enrichment, rows)) if rows else None; upserted = None   # LAB BREAK/' \
  src/lakehouse_ai/enrich.py
py "
import asyncio
from lakehouse_ai import enrich
from lakehouse_ai.tables import catalog, table, arrow
t = table(catalog(), 'tickets'); row = t.scan(row_filter=\"ticket_id == 'OPS-000002'\").to_arrow().to_pylist()[0]
row['text'] += ' (reopened)'; t.upsert(arrow(t, [row]), join_cols=['ticket_id'])
asyncio.run(enrich.run('scripted'))
e = table(catalog(), 'ticket_enrichment'); print('rows for OPS-000002:', e.scan(row_filter=\"ticket_id == 'OPS-000002'\").count(), '| total', e.scan().count())
r = asyncio.run(enrich.run('scripted')); print('next run considers', r['rows_considered'])"
```

**Predict (P7):** how many enrichment rows does OPS-000002 have now? And the next run, with no new data: does it
consider zero rows?

**Fix:** revert the code, then repair the data. Reverting alone doesn't remove the duplicate:

```bash
git checkout -- src/lakehouse_ai/enrich.py
py "
import asyncio
from lakehouse_ai import enrich
from lakehouse_ai.tables import catalog, table
e = table(catalog(), 'ticket_enrichment'); e.delete(\"ticket_id == 'OPS-000002'\")
r = asyncio.run(enrich.run('scripted')); print({k: r[k] for k in ('rows_considered','inserted')})
print('rows for OPS-000002:', table(catalog(), 'ticket_enrichment').scan(row_filter=\"ticket_id == 'OPS-000002'\").count())"
```

**Break 2: the model returns garbage for some rows.** Make every fifth answer schema-invalid for one run:

```bash
py "
import asyncio
from datetime import datetime, UTC
from lakehouse_ai import enrich, llm
from lakehouse_ai.tables import catalog, table, arrow
from lakehouse_ai.seed import tickets
t = table(catalog(), 'tickets'); t.append(arrow(t, tickets(50, start_id=3001, seed=99, start=datetime(2026, 10, 20, tzinfo=UTC))))
real = llm.Scripted.run
async def flaky(self, items):
    out = await real(self, items)
    for i, k in enumerate(list(out)):
        if i % 5 == 0: out[k] = llm.Outcome(None, 300, 20, error='invalid enum in {category: weather}', raw='{\"category\": \"weather\"}')
    return out
llm.Scripted.run = flaky
r = asyncio.run(enrich.run('scripted')); print({k: r[k] for k in ('rows_considered','rows_called','rows_failed','inserted')})
print('dead letters:', table(catalog(), 'enrichment_errors').scan().count())
llm.Scripted.run = real
r = asyncio.run(enrich.run('scripted')); print('next run:', {k: r[k] for k in ('rows_considered','inserted','rows_failed')})"
```

**Predict (P8):** 50 new tickets, every fifth model answer invalid. Does the job fail? How many tickets end in the
dead-letter table, and what does the next normal run do with them?

## Check your predictions

**P1.** **24** data files: the 2,000 tickets span 24 days, and an append writes one file per partition it touches
(`added-data-files: 24`, `changed-partition-count: 24`, about 155 KB in total). `metadata/` holds four files: two
table metadata JSONs (`00000-…` from `create_table`, `00001-…` from the append), one manifest list (`snap-….avro`)
and one manifest (`….avro`) listing the 24 data files. Every commit writes a new metadata JSON; the catalog just
moves its pointer.

**P2.** **1,818** calls and **182** cached: 1,818 distinct texts among 2,000 tickets (duplicate reports), and the job
pays once per distinct text. Run 2 considers **0** rows, makes no calls and writes no enrichment snapshot (it does
record the run in `enrichment_runs`).

**P3.** Run 3: `rows_considered 300, rows_called 276, rows_cached 24, inserted 300`. Run 4: `rows_considered 1,
rows_called 1, updated 1`. The anti-join compares each ticket's current text hash with the hash stored next to its
enrichment, so an edited ticket is the only new work.

**P4.** Four snapshots: `[('append', '2000'), ('append', '2300'), ('overwrite', '2299'), ('append', '2300')]`. The
upsert is one transaction that commits two snapshots: an overwrite that drops the old row (2,299) and an append
that adds the new one (2,300). Readers never see 2,299 because both land in one atomic commit, but time travel can.
The first snapshot still shows 2,000 rows. The edited ticket is still `access medium`: the keyword rules saw
"IAM role" and "AccessDenied" first and ignored "production impact". A real model would likely raise the
severity, which is exactly what an eval on edited tickets would test.

**P5.** `Security group on i-… allows 0.0.0.0/0 on port 22, please lock down`. The outage rule matches `down`, in
"lock down", and outage rules are checked first. Accuracy is 0.926, and all 171 errors are this one confusion. You
would never find it without labelled data.

**P6.** IAM access tickets (`Please add me to the IAM role…`), wrong. The index isn't to blame: the exact search
returns the same tickets. The hashing embedder is lexical: the query shares "to the" with the IAM tickets as much
as it shares "security group" with the right ones, and the security tickets carry extra tokens (`0.0.0.0/0`,
`port 22`) that dilute their vectors. The second query, worded like the tickets, finds `Security group on …
allows 0.0.0.0/0 on port 22` at distance about 1.18. Two details: Lance's reported distance for this index is about
twice the cosine distance (it's the squared L2 distance between unit vectors, 2 × (1 − cos)), and the index only
helps speed, never relevance. Retrieval quality is bounded by the embedding model.

**P7.** **2** rows for OPS-000002 (`total 2301`). And the next run considers **1** row again: the job's lookup keeps
whichever of the two rows it reads last, its hash differs from the current text, so every run re-enriches the ticket
and appends another duplicate. Duplicates grow, and so does the bill. After the revert and the targeted delete:
`{'rows_considered': 1, 'inserted': 1}` and one row again. Idempotency belongs in the write (MERGE/UPSERT on the
business key), not in the hope that nobody re-runs the job.

**P8.** It doesn't fail: `{'rows_considered': 50, 'rows_called': 45, 'rows_failed': 12, 'inserted': 38}`, and the
dead-letter table holds 12 rows with the error and the raw output. 9 of the 45 distinct texts failed, which took 12
tickets with them (duplicates share an answer). The next normal run considers exactly those 12 and inserts them:
failed rows have no enrichment, so the incremental selection retries them for free.

## What to remember

- Every Iceberg commit is a new metadata file and a pointer swap; snapshots make runs reproducible and auditable.
- Select work by comparing input hashes with what you already produced, per prompt version and model.
- Write with UPSERT/MERGE on the business key. APPEND turns re-runs into duplicates and repeat spend.
- Bad model output is data: dead-letter it, and let the next run retry.
- Vector search quality is the embedder's; the index only trades a little recall for speed.

Next: [Lab 06 · Agent memory](06-agent-memory.md).
