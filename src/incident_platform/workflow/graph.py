import hashlib
import json

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt

from incident_platform.integrations.tools import read_tools
from incident_platform.models import IncidentInput, IncidentState, Plan
from incident_platform.workflow.policy import authorized_execution, policy, valid_rca


def build_graph(cfg, store, analyst, backend, checkpointer):
    async def emit(state, kind, payload):
        await store.emit(state, kind, payload)

    async def normalize(state):
        incident = IncidentInput.model_validate(state["raw"])
        await emit(state, "incident.created", {"service": incident.service})
        return {
            "incident": incident.model_dump(mode="json"),
            "evidence": [],
            "hypotheses": [],
            "rounds": 0,
            "status": "investigating",
        }

    async def enrich(state):
        tools = read_tools(backend, state["incident"]["service"])
        evidence = await tools["topology"].ainvoke({"query": "Owner and dependency map"})
        return {"evidence": [evidence]}

    async def triage(state):
        result = await analyst.triage(state)
        # Deterministic rule cannot be downgraded by the model.
        if state["incident"]["severity"] in {"critical", "high"}:
            result = result.model_copy(
                update={"severity": state["incident"]["severity"], "investigate": True}
            )
        await emit(state, "incident.triaged", result.model_dump())
        return {"triage": result.model_dump()}

    async def investigate(state):
        step = await analyst.investigate(state)
        update = {
            "rounds": state["rounds"] + 1,
            "hypotheses": [*state["hypotheses"], step.hypothesis],
            "rca": {},
            "rca_valid": False,
        }
        if step.tool:
            tools = read_tools(backend, state["incident"]["service"])
            payload = {"query": step.query}
            if step.tool == "knowledge":
                payload["kind"] = step.kind
            evidence = await tools[step.tool].ainvoke(payload)
            update["evidence"] = [*state["evidence"], evidence]
        elif step.conclusion:
            update["rca"] = step.conclusion.model_dump()
        return update

    def investigation_route(state):
        if state.get("rca"):
            return "validate_rca"
        return "investigate" if state["rounds"] < cfg.max_investigation_rounds else "manual"

    async def validate(state):
        valid = valid_rca(state["rca"], state["evidence"], cfg.mock_mode)
        if valid:
            await emit(state, "incident.rca_completed", state["rca"])
        return {"rca_valid": valid}

    async def plan(state):
        draft = await analyst.plan(state)
        content = json.dumps(draft.model_dump(mode="json"), sort_keys=True)
        plan_id = hashlib.sha256(
            f"{state['incident_id']}:{state['rounds']}:{content}".encode()
        ).hexdigest()
        result = Plan(**draft.model_dump(), plan_id=plan_id).model_dump(mode="json")
        await emit(state, "incident.mitigation_proposed", result)
        return {"plan": result, "approval": {}}

    async def assess(state):
        decision = policy(Plan.model_validate(state["plan"]), cfg.allowed_targets)
        if decision == "approval":
            await store.request_approval(state, cfg.approval_ttl_seconds)
        await store.audit(
            state["incident_id"],
            "policy.decision",
            {"plan_id": state["plan"]["plan_id"], "decision": decision},
        )
        return {"decision": decision}

    def approve(state):
        decision = interrupt(
            {
                "incident_id": state["incident_id"],
                "plan": state["plan"],
                "instruction": "Approve or reject this exact plan via Jira/Slack callback",
            }
        )
        return {"approval": decision}

    async def execute(state):
        result = await authorized_execution(state, store, cfg)
        await emit(state, "incident.mitigated", result)
        return {"execution": result}

    async def verify(state):
        healthy = await backend.verify(state["incident"]["service"], state["execution"])
        # A fresh investigation must gather fresh observations, not reuse the old RCA.
        return {
            "verified": healthy,
            "evidence": state["evidence"] if healthy else [],
            "rca": state["rca"] if healthy else {},
        }

    async def resolve(state):
        await emit(state, "incident.resolved", {"simulated": cfg.mock_mode, "verified": True})
        return {"status": "resolved"}

    async def manual(state):
        await emit(
            state, "incident.manual_review", {"reason": "Review or additional evidence required"}
        )
        return {"status": "manual_review"}

    async def denied(state):
        return {"status": "denied"}

    graph = StateGraph(IncidentState)
    nodes = {
        "normalize": normalize,
        "enrich": enrich,
        "triage": triage,
        "investigate": investigate,
        "validate_rca": validate,
        "plan": plan,
        "policy": assess,
        "approval": approve,
        "execute": execute,
        "verify": verify,
        "resolve": resolve,
        "manual": manual,
        "denied": denied,
    }
    for name, fn in nodes.items():
        graph.add_node(
            name,
            fn,
            retry_policy=RetryPolicy(max_attempts=3, retry_on=(TimeoutError, ConnectionError)),
        )
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "enrich")
    graph.add_edge("enrich", "triage")
    graph.add_conditional_edges(
        "triage", lambda s: "investigate" if s["triage"]["investigate"] else "manual"
    )
    graph.add_conditional_edges("investigate", investigation_route)
    graph.add_conditional_edges(
        "validate_rca",
        lambda s: (
            "plan"
            if s["rca_valid"]
            else ("investigate" if s["rounds"] < cfg.max_investigation_rounds else "manual")
        ),
    )
    graph.add_edge("plan", "policy")
    graph.add_conditional_edges(
        "policy",
        lambda s: {"allow": "execute", "approval": "approval", "deny": "denied"}[s["decision"]],
    )
    graph.add_conditional_edges(
        "approval",
        lambda s: (
            "execute"
            if (
                s["approval"].get("approved") is True
                and s["approval"].get("plan_id") == s["plan"]["plan_id"]
            )
            else "denied"
        ),
    )
    graph.add_edge("execute", "verify")
    graph.add_conditional_edges(
        "verify",
        lambda s: (
            "resolve"
            if s["verified"]
            else ("investigate" if s["rounds"] < cfg.max_investigation_rounds else "manual")
        ),
    )
    for name in ("resolve", "manual", "denied"):
        graph.add_edge(name, END)
    return graph.compile(checkpointer=checkpointer)
