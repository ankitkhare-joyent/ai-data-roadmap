"""Walk through what a gateway gives you, as a platform team and as an app team.

Admin calls use LiteLLM's management API with the master key. App calls use the normal Anthropic SDK,
pointed at the gateway, with a virtual key and a model alias: the app code is identical to calling
Anthropic directly, which is what makes a gateway cheap to adopt.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import anthropic
import httpx

GW = os.environ.get("GATEWAY_URL", "http://localhost:49400")
FAKE = os.environ.get("FAKE_URL")
MASTER = Path("/bootstrap/admin/master_key").read_text().strip()
admin = httpx.Client(base_url=GW, headers={"Authorization": f"Bearer {MASTER}"}, timeout=30)
run = uuid.uuid4().hex[:6]


def step(title: str) -> None:
    print(f"\n== {title}")


def post(path: str, body: dict) -> dict:
    r = admin.post(path, json=body)
    r.raise_for_status()
    return r.json()


def app_client(key: str) -> anthropic.Anthropic:
    # max_retries=0 so the demo shows the gateway's own retry/fallback behaviour, not the SDK's.
    return anthropic.Anthropic(base_url=GW, api_key=key, max_retries=0, timeout=60)


def ask(client: anthropic.Anthropic, model: str, text: str, **kw):
    return client.messages.with_raw_response.create(
        model=model, max_tokens=512, messages=[{"role": "user", "content": text}], **kw)


step("1. Platform team: teams with budgets, keys with limits and model allow-lists")
de = post("/team/new", {"team_alias": f"data-eng-{run}", "max_budget": 5.0, "budget_duration": "30d",
                        "models": ["smart", "fast", "cheap"]})["team_id"]
an = post("/team/new", {"team_alias": f"analytics-{run}", "max_budget": 1.0, "budget_duration": "30d",
                        "models": ["fast", "cheap"]})["team_id"]
de_key = post("/key/generate", {"team_id": de, "key_alias": f"airflow-agent-{run}", "rpm_limit": 60})["key"]
an_key = post("/key/generate", {"team_id": an, "key_alias": f"superset-bot-{run}", "rpm_limit": 5})["key"]
tiny_key = post("/key/generate", {"team_id": de, "key_alias": f"intern-{run}", "max_budget": 0.004})["key"]
print(f"teams: data-eng ($5/30d: smart, fast, cheap), analytics ($1/30d: fast, cheap)")
print(f"keys:  airflow-agent (60 rpm), superset-bot (5 rpm), intern ($0.004 budget)   e.g. {an_key[:8]}...")

step("2. Model allow-list: analytics asks for the expensive model")
try:
    ask(app_client(an_key), "smart", "hello")
    print("unexpected: allowed")
except anthropic.APIStatusError as e:
    print(f"refused with HTTP {e.status_code}: {str(e.message)[:110]}")

step("3. Fallback: 'smart' (Opus) is overloaded half the time; the gateway falls back to 'fast'")
de_client = app_client(de_key)
for i in range(6):
    raw = ask(de_client, "smart", f"Summarise yesterday's failed DAG runs, request {i} {run}",
              extra_body={"cache": {"no-cache": True}})
    msg = raw.parse()
    served = msg.content[0].text.split("]")[0].removeprefix("[served by ")
    print(f"  request {i}: served by {served:<20} cost ${float(raw.headers.get('x-litellm-response-cost', 0)):.4f}")
if FAKE:
    print("  provider saw:", httpx.get(f"{FAKE}/stats").json())

step("4. Rate limit: superset-bot fires 8 requests at a 5 rpm key")
codes = []
for i in range(8):
    try:
        ask(app_client(an_key), "cheap", f"ping {i} {run}", extra_body={"cache": {"no-cache": True}})
        codes.append(200)
    except anthropic.APIStatusError as e:
        codes.append(e.status_code)
print(f"  status codes: {codes}")

step("5. Budget: the intern key has $0.004; one 'fast' answer costs about $0.0045")
for i in range(3):
    try:
        raw = ask(app_client(tiny_key), "fast", f"explain partitioning, take {i} {run}",
                  extra_body={"cache": {"no-cache": True}})
        print(f"  call {i}: ok, cost ${float(raw.headers.get('x-litellm-response-cost', 0)):.4f}")
    except anthropic.APIStatusError as e:
        print(f"  call {i}: HTTP {e.status_code}: {str(e.message)[:100]}")

step("6. Cache: the same deterministic prompt twice")
prompt = f"Classify this log line as INFO/WARN/ERROR: 'executor lost, retrying stage 3' ({run})"
for label in ("first ", "second"):
    t = time.perf_counter()
    raw = ask(de_client, "cheap", prompt)
    print(f"  {label}: {1000 * (time.perf_counter() - t):6.0f} ms   cost ${float(raw.headers.get('x-litellm-response-cost', 0)):.5f}"
          f"   cache hit: {raw.headers.get('x-litellm-cache-key') is not None}")

step("7. Spend, per team and per key (what finance asks you for)")
time.sleep(3)  # spend is written asynchronously in batches
for name, tid in (("data-eng", de), ("analytics", an)):
    info = admin.get("/team/info", params={"team_id": tid}).json()["team_info"]
    print(f"  {name:<10} spend ${info.get('spend', 0):.4f} of ${info.get('max_budget')}")
for name, key in (("airflow-agent", de_key), ("superset-bot", an_key), ("intern", tiny_key)):
    info = admin.get("/key/info", params={"key": key}).json()["info"]
    print(f"  {name:<14} spend ${info.get('spend', 0):.4f}")
