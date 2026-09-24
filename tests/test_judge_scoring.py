from incident_platform.judge.scoring import (
    cited_evidence,
    deterministic_scores,
    score_id_for,
    verdict_value,
)

RESOLVED = {
    "incident_id": "inc-1",
    "status": "resolved",
    "verified": True,
    "rounds": 3,
    "rca_valid": True,
    "rca": {"hypothesis": "release v2 regression", "confidence": 0.9, "evidence_ids": ["e1"]},
    "evidence": [
        {"evidence_id": "e1", "tool": "metrics", "observation": "p95 2400ms"},
        {"evidence_id": "e2", "tool": "logs", "observation": "unrelated noise"},
    ],
}


def test_deterministic_scores_for_a_resolved_incident():
    names = {s.name: s for s in deterministic_scores(RESOLVED)}
    assert names["outcome_verified"].value == 1
    assert names["outcome_verified"].data_type == "BOOLEAN"
    assert names["outcome_status"].value == "resolved"
    assert names["outcome_status"].data_type == "CATEGORICAL"
    assert names["investigation_rounds"].value == 3
    assert names["investigation_rounds"].data_type == "NUMERIC"
    assert names["rca_citations_valid"].value == 1


def test_missing_sources_are_omitted_not_defaulted():
    # An incident routed to manual_review before verify never sets verified. Recording that
    # as false would corrupt every average built on it.
    partial = {"status": "manual_review", "rounds": 6}
    names = {s.name for s in deterministic_scores(partial)}
    assert names == {"outcome_status", "investigation_rounds"}


def test_false_values_are_still_recorded():
    names = {s.name: s for s in deterministic_scores({**RESOLVED, "verified": False})}
    assert names["outcome_verified"].value == 0


def test_cited_evidence_excludes_uncited_items():
    # The leak risk: the obvious implementation passes state["evidence"] wholesale.
    cited = cited_evidence(RESOLVED)
    assert [e["evidence_id"] for e in cited] == ["e1"]


def test_cited_evidence_is_empty_without_an_rca():
    assert cited_evidence({"rca": {}, "evidence": RESOLVED["evidence"]}) == []


def test_score_id_is_stable_and_distinct():
    assert score_id_for("inc-1", "outcome_verified") == score_id_for("inc-1", "outcome_verified")
    assert score_id_for("inc-1", "outcome_verified") != score_id_for("inc-1", "outcome_status")
    assert score_id_for("inc-1", "outcome_verified") != score_id_for("inc-2", "outcome_verified")


def test_verdict_values():
    assert verdict_value("supported") == 1.0
    assert verdict_value("partially_supported") == 0.5
    assert verdict_value("unsupported") == 0.0


def test_score_ids_do_not_collide_across_argument_boundaries():
    # A flat "{incident}:{name}" seed collides here, and the failure would be silent:
    # two distinct scores would merge into one upsert.
    assert score_id_for("a:b", "c") != score_id_for("a", "b:c")
