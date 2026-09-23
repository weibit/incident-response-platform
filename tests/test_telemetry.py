from incident_platform.telemetry import trace_id_for


def test_trace_id_is_stable_for_the_same_event():
    # Recomputable after restarts, replays and approval resumes, so nothing is stored.
    assert trace_id_for("inc-1", "evt-1") == trace_id_for("inc-1", "evt-1")


def test_trace_id_differs_per_event():
    assert trace_id_for("inc-1", "evt-1") != trace_id_for("inc-1", "evt-2")


def test_trace_id_differs_per_incident():
    assert trace_id_for("inc-1", "evt-1") != trace_id_for("inc-2", "evt-1")
