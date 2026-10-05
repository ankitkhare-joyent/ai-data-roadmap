import pytest

from multi_agent import specialists
from multi_agent.demo import specialists as running_specialists
from multi_agent.orchestrator import Budget, Orchestrator
from multi_agent.remote import RemoteAgent

Q = "Which instance is hot right now, and what should I do to fix it?"


async def test_orchestrator_resolves_follow_up_and_answers():
    async with running_specialists() as (urls, tokens):
        res = await Orchestrator(urls, tokens).answer(Q)
    states = [(s.agent, s.state) for s in res.spans]
    assert ("runbook-expert", "TASK_STATE_INPUT_REQUIRED") in states
    assert states[-1] == ("runbook-expert", "TASK_STATE_COMPLETED")
    assert res.spans[0].task_id == res.spans[-1].task_id or res.spans[1].task_id == res.spans[-1].task_id
    assert "RB-12" in res.answer and "i-0a1b2c3d4e5f60002" in res.answer


async def test_budget_caps_delegations():
    async with running_specialists() as (urls, tokens):
        res = await Orchestrator(urls, tokens, budget=Budget(max_delegations=2)).answer(Q)
    assert res.delegations == 2 and "needs more information" in res.answer   # follow-up skipped


async def test_slow_specialist_times_out_without_failing_the_request(monkeypatch):
    monkeypatch.setattr(specialists.FleetAnalyst, "latency_s", 3.0)
    async with running_specialists() as (urls, tokens):
        res = await Orchestrator(urls, tokens, timeout_s=1).answer(Q)
    assert any(s.agent == "fleet-analyst" and s.state == "TIMEOUT" for s in res.spans)
    assert "unavailable (TIMEOUT)" in res.answer


async def test_wrong_token_is_rejected_but_card_is_public():
    async with running_specialists() as (urls, _tokens):
        r = RemoteAgent(urls["fleet-analyst"], token="not-the-token")
        card = await r.connect()
        assert card.name == "fleet-analyst" and "bearer" in card.security_schemes
        reply = await r.ask("Which instances are idle?")
        await r.close()
    assert reply.state == "ERROR" and "401" in reply.text


@pytest.mark.parametrize("q,expected", [("Draft a haiku", []), ("which instances are idle?", ["fleet-analyst"])])
def test_rule_planner(q, expected):
    from multi_agent.orchestrator import RulePlanner
    assert RulePlanner().plan(q, {"fleet-analyst": 1, "runbook-expert": 1}) == expected
