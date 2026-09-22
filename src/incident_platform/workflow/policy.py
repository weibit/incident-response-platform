from datetime import UTC, datetime

from incident_platform.models import RCA, Action, Evidence, Plan

OBSERVATIONAL = {"logs", "metrics", "kubernetes", "deployments", "config_diffs", "topology"}


def valid_rca(rca: dict, evidence: list[dict], mock_mode: bool) -> bool:
    result = RCA.model_validate(rca)
    by_id = {e.evidence_id: e for e in map(Evidence.model_validate, evidence)}
    cited = [by_id.get(key) for key in result.evidence_ids]
    return (
        result.confidence >= 0.75
        and len(cited) >= 2
        and all(
            e is not None
            and e.trusted
            and (mock_mode or not e.simulated)
            and abs((datetime.now(UTC) - e.observed_at).total_seconds()) <= 1800
            for e in cited
        )
        and len({e.tool for e in cited if e}) >= 2
        # Runbooks and prior incidents are procedure and precedent, not observations of
        # this incident. At least one citation must come from an observational tool.
        and any(e is not None and e.tool in OBSERVATIONAL for e in cited)
    )


def policy(plan: Plan, allowed_targets: list[str]) -> str:
    # Risk comes from server-side rules, never an LLM's self-reported risk score.
    if plan.target not in allowed_targets or not plan.rollback or not plan.verification:
        return "deny"
    return {Action.OBSERVE: "allow", Action.RESTART: "approval", Action.ROLLBACK: "approval"}.get(
        plan.action, "deny"
    )


async def authorized_execution(state, store, cfg):
    plan = Plan.model_validate(state["plan"])
    decision = policy(plan, cfg.allowed_targets)
    if not state.get("rca_valid") or decision == "deny":
        raise PermissionError("Plan denied")
    if decision == "approval":
        approval = await store.approved(state["incident_id"], plan.plan_id)
        if (
            not approval
            or approval["plan"] != state["plan"]
            or not approval["decision"]
            or approval["decision"].get("approved") is not True
        ):
            raise PermissionError("Exact current plan needs unexpired approval")
    if not cfg.mock_mode:
        # Fail closed until a scoped executor with remote idempotency/reconciliation is installed.
        raise NotImplementedError("Production mutation adapter is intentionally unconfigured")
    return await store.simulate_execution(state)
