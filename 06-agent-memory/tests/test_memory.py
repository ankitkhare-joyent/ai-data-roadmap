import os
import time

import pytest

from agent_memory import context, evals
from agent_memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path, "acme", "ankit")


@pytest.mark.parametrize("path", [
    "/memories/../../etc/passwd", "/memories/%2e%2e/%2e%2e/secret", "/etc/passwd", "memories/x",
    "/memoriesX/evil", "/memories/..\\..\\x",
])
def test_traversal_is_refused(store, path):
    r = store.execute({"command": "create", "path": path, "file_text": "x"})
    assert r.is_error


def test_symlink_escape_is_refused(store, tmp_path):
    os.symlink(tmp_path, store.root / "link")
    assert store.execute({"command": "view", "path": "/memories/link/acme"}).is_error


def test_users_are_isolated(tmp_path):
    a, b = MemoryStore(tmp_path, "acme", "ankit"), MemoryStore(tmp_path, "acme", "priya")
    a.execute({"command": "create", "path": "/memories/x.md", "file_text": "ankit only"})
    assert b.execute({"command": "view", "path": "/memories/x.md"}).is_error


def test_documented_return_strings(store):
    assert store.execute({"command": "create", "path": "/memories/n.md", "file_text": "a\nb\na"}).content == \
        "File created successfully at: /memories/n.md"
    view = store.execute({"command": "view", "path": "/memories/n.md"}).content
    assert view.startswith("Here's the content of /memories/n.md with line numbers:\n     1\ta")
    dup = store.execute({"command": "str_replace", "path": "/memories/n.md", "old_str": "a", "new_str": "c"})
    assert dup.is_error and "Multiple occurrences" in dup.content
    assert store.execute({"command": "delete", "path": "/memories"}).is_error
    listing = store.execute({"command": "view", "path": "/memories"}).content
    assert "excluding hidden items" in listing and "/memories/n.md" in listing


def test_secrets_redacted_and_writes_audited(store):
    store.execute({"command": "create", "path": "/memories/p.md", "file_text": "token: abc123"}, session="s1")
    assert "abc123" not in (store.root / "p.md").read_text()
    assert '"session": "s1"' in store.audit.read_text()


def test_size_cap(store):
    r = store.execute({"command": "create", "path": "/memories/big.md", "file_text": "x" * 25_000})
    assert r.is_error and "capped" in r.content


def test_expiry(store):
    store.execute({"command": "create", "path": "/memories/old.md", "file_text": "x"})
    old = time.time() - 100 * 86400
    os.utime(store.root / "old.md", (old, old))
    assert store.expire(90) == ["/memories/old.md"]


def test_context_strategies_bound_the_window_and_offload_is_cheapest():
    runs = {n: context.simulate(n, context.Session()) for n in ("none", "clear", "offload", "compact")}
    assert runs["none"].peak_context > 3 * runs["offload"].peak_context
    assert runs["offload"].billed_input_usd < min(r.billed_input_usd for n, r in runs.items() if n != "offload")
    assert runs["offload"].cache_breaks == 0 and runs["clear"].cache_breaks > 0


def test_memory_evals_pass_with_guard():
    assert evals.main() == 0
