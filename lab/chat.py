"""One streamed chat request through the lesson 4 router, standard library only.

    python3 lab/chat.py TENANT [ROUTE] [URL]     e.g.  python3 lab/chat.py acme prefix_aware

The system prompt is 1,500 words that depend only on TENANT, so the same tenant always shares a prefix.
Prints which backend served it, time to first token, and how many prompt tokens the server had cached.
"""
import json
import sys
import time
import urllib.request

tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
route = sys.argv[2] if len(sys.argv) > 2 else "prefix_aware"
url = sys.argv[3] if len(sys.argv) > 3 else "http://localhost:49480"
system = " ".join(f"{tenant}w{i}" for i in range(1500))
body = {"model": "fleet-8b", "max_tokens": 16, "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": f"q{time.time_ns()}"}]}
req = urllib.request.Request(f"{url}/v1/chat/completions", data=json.dumps(body).encode(),
                             headers={"content-type": "application/json", "x-route": route})
t0 = time.perf_counter()
ttft, cached = None, None
with urllib.request.urlopen(req, timeout=60) as r:
    backend = r.headers.get("x-backend", "?").split("//")[-1]
    for line in r:
        line = line.decode().strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        if ttft is None and chunk.get("choices") and chunk["choices"][0].get("delta", {}).get("content"):
            ttft = (time.perf_counter() - t0) * 1000
        if chunk.get("usage"):
            cached = (chunk["usage"].get("prompt_tokens_details") or {}).get("cached_tokens")
print(f"tenant={tenant:<8} route={route:<13} backend={backend:<10} ttft={ttft:6.0f} ms  cached_prompt_tokens={cached}")
