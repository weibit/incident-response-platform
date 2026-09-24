from uuid import uuid4

from incident_platform.judge.main import judge_incident, score_event
from incident_platform.models import Envelope, Verdict

RESOLVED = {
    "incident_id": "inc-1",
    "input_event_id": "evt-1",
    "status": "resolved",
    "verified": True,
    "rounds": 3,
    "rca_valid": True,
    "rca": {"hypothesis": "release v2 regression", "confidence": 0.9, "evidence_ids": ["e1"]},
    "evidence": [{"evidence_id": "e1", "tool": "metrics", "observation": "p95 2400ms"}],
}


class FakeJudge:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    async def faithfulness(self, hypothesis, confidence, cited):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class Recorder:
    def __init__(self):
        self.scores = []

    def __call__(self, **kwargs):
        self.scores.append(kwargs)


def _supported():
    return Verdict(label="supported", reason="metrics show it")


async def test_writes_five_scores_for_a_judged_incident(store, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("incident_platform.judge.main.record_score", recorder)
    await store.save_state(RESOLVED)
    await judge_incident(store, FakeJudge(_supported()), "inc-1")
    names = {s["name"] for s in recorder.scores}
    assert names == {
        "outcome_verified",
        "outcome_status",
        "investigation_rounds",
        "rca_citations_valid",
        "rca_faithfulness",
    }
    faith = next(s for s in recorder.scores if s["name"] == "rca_faithfulness")
    assert faith["value"] == 1.0
    assert faith["comment"].startswith("supported:")
    assert all(s["score_id"] for s in recorder.scores)


async def test_a_failing_judge_still_writes_the_deterministic_scores(store, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("incident_platform.judge.main.record_score", recorder)
    await store.save_state(RESOLVED)
    await judge_incident(store, FakeJudge(error=RuntimeError("gateway down")), "inc-1")
    names = {s["name"] for s in recorder.scores}
    assert "rca_faithfulness" not in names
    assert len(names) == 4
    assert store.audits[-1][1]["faithfulness"] is None


async def test_an_incident_without_an_rca_is_not_judged(store, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("incident_platform.judge.main.record_score", recorder)
    await store.save_state({**RESOLVED, "status": "manual_review", "rca": {}, "verified": False})
    judge = FakeJudge(_supported())
    await judge_incident(store, judge, "inc-1")
    assert judge.calls == 0
    assert store.audits[-1][1]["rca_present"] is False


async def test_scores_are_skipped_without_a_trace_to_attach_to(store, monkeypatch):
    # A score naming a trace that does not exist is dropped silently, so do not pretend.
    recorder = Recorder()
    monkeypatch.setattr("incident_platform.judge.main.record_score", recorder)
    await store.save_state({k: v for k, v in RESOLVED.items() if k != "input_event_id"})
    await judge_incident(store, FakeJudge(_supported()), "inc-1")
    assert recorder.scores == []
    assert store.audits[-1][0] == "judge.verdict"


async def test_a_missing_state_is_audited_not_crashed(store, monkeypatch):
    monkeypatch.setattr("incident_platform.judge.main.record_score", Recorder())
    await judge_incident(store, FakeJudge(_supported()), "unknown-incident")
    assert store.audits[-1][0] == "judge.state_missing"


async def test_a_malformed_event_is_skipped_not_raised(store):
    # Unguarded, this would be a guaranteed infinite redelivery: the offset is never
    # committed, so the broker hands back the same bad message forever.
    await score_event(store, FakeJudge(_supported()), b"{not json")
    assert store.audits == []


async def test_an_unparseable_envelope_is_skipped(store):
    await score_event(store, FakeJudge(_supported()), b'{"incident_id": "not-a-uuid"}')
    assert store.audits == []


async def test_a_failure_inside_scoring_is_skipped(store, monkeypatch):
    def boom(**_):
        raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr("incident_platform.judge.main.record_score", boom)
    await store.save_state(RESOLVED)
    event = Envelope(
        incident_id=uuid4(),
        correlation_id=uuid4(),
        event_type="incident.resolved",
        source="worker",
        payload={},
    )
    # Must not raise: an unreachable collector cannot be allowed to wedge the consumer.
    await score_event(store, FakeJudge(_supported()), event.model_dump_json().encode())


async def test_a_collector_failure_still_writes_the_audit_row(store, monkeypatch):
    # The audit row is the durable floor. Losing scores to an outage is expected; losing the
    # record that judging happened is not.
    def boom(**_):
        raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr("incident_platform.judge.main.record_score", boom)
    await store.save_state(RESOLVED)
    await judge_incident(store, FakeJudge(_supported()), "inc-1")
    assert store.audits[-1][0] == "judge.verdict"
    assert store.audits[-1][1]["faithfulness"] == "supported"


async def test_an_unparseable_message_is_logged_without_an_incident(store):
    # Distinct from a scoring failure: there is no incident id to report, and it is expected
    # noise rather than a bug worth an error-level line.
    await score_event(store, FakeJudge(_supported()), b"{not json")
    assert store.audits == []


async def test_a_well_formed_event_is_scored(store, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("incident_platform.judge.main.record_score", recorder)
    incident_id = uuid4()
    await store.save_state({**RESOLVED, "incident_id": str(incident_id)})
    event = Envelope(
        incident_id=incident_id,
        correlation_id=uuid4(),
        event_type="incident.resolved",
        source="worker",
        payload={},
    )
    await score_event(store, FakeJudge(_supported()), event.model_dump_json().encode())
    assert {s["name"] for s in recorder.scores} >= {"outcome_status", "rca_faithfulness"}
