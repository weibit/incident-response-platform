from incident_platform.judge.main import judge_incident
from incident_platform.models import Verdict

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
