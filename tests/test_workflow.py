from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from incident_platform.integrations.llm import Analyst
from incident_platform.integrations.tools import MockEvidenceBackend
from incident_platform.models import RCA, Action, Envelope, Plan, Triage
from incident_platform.worker.main import process_event
from incident_platform.workflow.graph import build_graph
from incident_platform.workflow.policy import authorized_execution, policy, valid_rca


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (Action.OBSERVE, "allow"),
        (Action.RESTART, "approval"),
        (Action.ROLLBACK, "approval"),
        (Action.SHELL, "deny"),
    ],
)
def test_policy(action, expected):
    plan = Plan(
        plan_id="p",
        action=action,
        target="checkout",
        rationale="test",
        rollback="undo",
        verification="SLO",
    )
    assert policy(plan, ["checkout"]) == expected
    assert policy(plan, ["other-service"]) == "deny"


async def test_pause_rebuild_resume_and_replay(cfg, store, incident):
    saver = InMemorySaver()
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), saver)
    event = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        event_type="incident.created",
        source="jira",
        payload=incident["raw"],
    )
    await process_event(store, graph, cfg, event)
    state = await store.get_state(incident["incident_id"])
    assert state["status"] == "awaiting_approval"
    assert not store.executions
    plan_id = state["plan"]["plan_id"]
    await store.decide(incident["incident_id"], plan_id, True, "jira-automation")
    resumed_graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), saver)
    approval = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        event_type="incident.approval",
        source="gateway",
        payload=store.approvals[plan_id]["decision"],
    )
    await process_event(store, resumed_graph, cfg, approval)
    await process_event(store, resumed_graph, cfg, approval)
    await process_event(store, resumed_graph, cfg, event)
    assert (await store.get_state(incident["incident_id"]))["status"] == "resolved"
    assert len(store.executions) == 1
    assert "incident.resolved" in [kind for kind, _ in store.events]


async def test_rejection(cfg, store, incident):
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), InMemorySaver())
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    state = await graph.ainvoke(incident, config)
    assert state["__interrupt__"]
    state = await graph.ainvoke(
        Command(resume={"plan_id": state["plan"]["plan_id"], "approved": False}), config
    )
    assert state["status"] == "denied"
    assert not store.executions


async def test_forged_resume_cannot_execute(cfg, store, incident):
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), InMemorySaver())
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    state = await graph.ainvoke(incident, config)
    with pytest.raises(PermissionError):
        await graph.ainvoke(
            Command(resume={"plan_id": state["plan"]["plan_id"], "approved": True}), config
        )
    assert not store.executions


async def test_expired_and_changed_plan(cfg, store, incident):
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), InMemorySaver())
    state = await graph.ainvoke(incident, {"configurable": {"thread_id": incident["incident_id"]}})
    plan_id = state["plan"]["plan_id"]
    await store.decide(incident["incident_id"], plan_id, True, "human")
    store.approvals[plan_id]["expires"] = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(PermissionError):
        await authorized_execution(state, store, cfg)
    store.approvals[plan_id]["expires"] = datetime.now(UTC) + timedelta(seconds=60)
    state["plan"] = {**state["plan"], "rationale": "Different plan"}
    with pytest.raises(PermissionError):
        await authorized_execution(state, store, cfg)


async def test_investigation_budget(cfg, store, incident):
    cfg.max_investigation_rounds = 1
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), InMemorySaver())
    state = await graph.ainvoke(incident, {"configurable": {"thread_id": incident["incident_id"]}})
    assert state["status"] == "manual_review"
    assert not store.executions


async def test_rca_citations_and_simulated_evidence():
    backend = MockEvidenceBackend()
    evidence = [
        (await backend.query(t, "checkout", "q")).model_dump(mode="json")
        for t in ["logs", "metrics"]
    ]
    rca = RCA(
        hypothesis="h", confidence=0.9, evidence_ids=[e["evidence_id"] for e in evidence]
    ).model_dump()
    assert valid_rca(rca, evidence, True)
    assert not valid_rca(rca, evidence, False)
    rca["evidence_ids"] = [str(uuid4())]
    assert not valid_rca(rca, evidence, True)


async def test_low_risk_auto_allow(cfg, store, incident):
    class ObserveAnalyst(Analyst):
        async def plan(self, state):
            return (await super().plan(state)).model_copy(update={"action": Action.OBSERVE})

    graph = build_graph(cfg, store, ObserveAnalyst(cfg), MockEvidenceBackend(), InMemorySaver())
    state = await graph.ainvoke(incident, {"configurable": {"thread_id": incident["incident_id"]}})
    assert state["status"] == "resolved"
    assert not store.approvals


async def test_triage_rule_overrides_model(cfg, store, incident):
    class DismissiveAnalyst(Analyst):
        async def triage(self, state):
            return Triage(severity="low", investigate=False, reason="model dismissed")

    graph = build_graph(cfg, store, DismissiveAnalyst(cfg), MockEvidenceBackend(), InMemorySaver())
    state = await graph.ainvoke(incident, {"configurable": {"thread_id": incident["incident_id"]}})
    assert state["triage"]["investigate"] is True
    assert state["triage"]["severity"] == "high"


async def test_failed_verification_loops_then_escalates(cfg, store, incident):
    class ObserveAnalyst(Analyst):
        async def plan(self, state):
            return (await super().plan(state)).model_copy(update={"action": Action.OBSERVE})

    class UnhealthyBackend(MockEvidenceBackend):
        async def verify(self, service, execution):
            return False

    graph = build_graph(cfg, store, ObserveAnalyst(cfg), UnhealthyBackend(), InMemorySaver())
    state = await graph.ainvoke(incident, {"configurable": {"thread_id": incident["incident_id"]}})
    assert state["status"] == "manual_review"
    assert state["rounds"] == cfg.max_investigation_rounds
    assert "incident.resolved" not in [kind for kind, _ in store.events]


async def test_followup_does_not_overwrite_suspended_plan(cfg, store, incident):
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), InMemorySaver())
    event = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        event_type="incident.created",
        source="jira",
        payload=incident["raw"],
    )
    await process_event(store, graph, cfg, event)
    first_plan = (await store.get_state(incident["incident_id"]))["plan"]
    followup = event.model_copy(
        update={
            "event_id": uuid4(),
            "payload": {**event.payload, "source_event_id": "later", "summary": "updated"},
        }
    )
    await process_event(store, graph, cfg, followup)
    assert (await store.get_state(incident["incident_id"]))["plan"] == first_plan
    assert store.audits[-1][0] == "incident.followup_recorded"


async def test_live_executor_fails_closed(cfg, store, incident):
    cfg.mock_mode = False
    state = {
        **incident,
        "rca_valid": True,
        "plan": {
            "plan_id": "p",
            "action": "observe",
            "target": "checkout",
            "rationale": "r",
            "rollback": "r",
            "verification": "v",
        },
    }
    with pytest.raises(NotImplementedError):
        await authorized_execution(state, store, cfg)
    assert not store.executions


async def test_read_tool_transient_failure_retries(cfg, store, incident):
    class FlakyBackend(MockEvidenceBackend):
        failures = 0

        async def query(self, tool, service, query):
            if tool == "topology" and self.failures == 0:
                self.failures += 1
                raise ConnectionError("temporary read failure")
            return await super().query(tool, service, query)

    backend = FlakyBackend()
    graph = build_graph(cfg, store, Analyst(cfg), backend, InMemorySaver())
    state = await graph.ainvoke(incident, {"configurable": {"thread_id": incident["incident_id"]}})
    assert state["__interrupt__"]
    assert backend.failures == 1
    assert not store.executions
