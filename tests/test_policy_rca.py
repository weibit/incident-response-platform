from datetime import UTC, datetime

from incident_platform.workflow.policy import valid_rca


def evidence(evidence_id, tool, trusted=True):
    return {
        "evidence_id": evidence_id,
        "tool": tool,
        "observation": "text",
        "observed_at": datetime.now(UTC).isoformat(),
        "trusted": trusted,
        "simulated": True,
    }


def rca(*ids, confidence=0.85):
    return {"hypothesis": "h", "confidence": confidence, "evidence_ids": list(ids)}


def test_two_observational_sources_are_valid():
    found = [evidence("a", "metrics"), evidence("b", "deployments")]
    assert valid_rca(rca("a", "b"), found, True) is True


def test_one_observational_plus_one_retrieval_is_valid():
    found = [evidence("a", "metrics"), evidence("b", "runbooks")]
    assert valid_rca(rca("a", "b"), found, True) is True


def test_retrieval_only_citations_are_rejected():
    # A runbook is procedure and a prior incident is precedent. Neither observes
    # this incident, so together they cannot establish a root cause.
    found = [evidence("a", "runbooks"), evidence("b", "similar_incidents")]
    assert valid_rca(rca("a", "b"), found, True) is False


def test_untrusted_empty_retrieval_cannot_supply_the_second_source():
    found = [evidence("a", "metrics"), evidence("b", "similar_incidents", trusted=False)]
    assert valid_rca(rca("a", "b"), found, True) is False


def test_knowledge_is_not_observational():
    # The whole retrieval corpus is procedure and precedent. Collapsing two tool names
    # into one must not accidentally promote it to an observation of this incident.
    from incident_platform.workflow.policy import OBSERVATIONAL

    assert "knowledge" not in OBSERVATIONAL


def test_knowledge_only_citations_are_rejected():
    found = [evidence("a", "knowledge"), evidence("b", "knowledge")]
    assert valid_rca(rca("a", "b"), found, True) is False


def test_one_observational_plus_knowledge_is_valid():
    found = [evidence("a", "metrics"), evidence("b", "knowledge")]
    assert valid_rca(rca("a", "b"), found, True) is True


def test_the_tool_sets_still_partition_exactly():
    # Three declarations must agree: the analyst's vocabulary, the observational set, and
    # the retrieval mapping. Nothing enforces this at runtime, so it is asserted here.
    from incident_platform.integrations.knowledge_backend import TOOL_CALLS
    from incident_platform.integrations.tools import READ_TOOLS
    from incident_platform.workflow.policy import OBSERVATIONAL

    assert OBSERVATIONAL | set(TOOL_CALLS) == set(READ_TOOLS)
    assert not (OBSERVATIONAL & set(TOOL_CALLS))
