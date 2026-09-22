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
