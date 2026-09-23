import json

from pydantic import SecretStr

from incident_platform.telemetry import build_mask, trace_id_for


def test_trace_id_is_stable_for_the_same_event():
    # Recomputable after restarts, replays and approval resumes, so nothing is stored.
    assert trace_id_for("inc-1", "evt-1") == trace_id_for("inc-1", "evt-1")


def test_trace_id_differs_per_event():
    assert trace_id_for("inc-1", "evt-1") != trace_id_for("inc-1", "evt-2")


def test_trace_id_differs_per_incident():
    assert trace_id_for("inc-1", "evt-1") != trace_id_for("inc-2", "evt-1")


def test_mask_redacts_every_configured_secret(cfg):
    mask = build_mask(cfg)
    data = "jira and approve and status and slack and siem"
    masked = mask(data=data)
    for secret in ("jira", "approve", "status", "slack", "siem"):
        assert secret not in masked


def test_mask_walks_nested_structures(cfg):
    mask = build_mask(cfg)
    masked = mask(data={"outer": [{"inner": "token is jira"}, ("tuple", "slack")]})
    assert "jira" not in json.dumps(masked)
    assert "slack" not in json.dumps(masked)


def test_mask_redacts_bearer_headers(cfg):
    masked = build_mask(cfg)(data="Authorization: Bearer sk-abc123xyz")
    assert "sk-abc123xyz" not in masked


def test_mask_leaves_evidence_bodies_byte_identical(cfg):
    # Regression guard: full-fidelity tracing is a deliberate decision, not an oversight.
    body = "ERROR checkout p95 latency 2400ms after release v2 at 14:02 UTC"
    assert build_mask(cfg)(data=body) == body


def test_mask_ignores_unset_secrets(cfg):
    # An empty credential must never be treated as a secret: "" matches at every
    # position, which would destroy the payload instead of redacting it.
    assert cfg.litellm_api_key.get_secret_value() == ""
    assert build_mask(cfg)(data="nothing secret here") == "nothing secret here"


def test_mask_output_is_json_serialisable(cfg):
    masked = build_mask(cfg)(data={"a": ("x", "y"), "b": [1, 2.5, None, True]})
    json.dumps(masked)


def test_mask_covers_a_secret_field_added_later(cfg):
    # The guard that matters: a credential added to Settings later must be redacted
    # without anyone remembering to update the mask. Subclassing is how the test states
    # that literally, rather than reusing a field the mask already knew about.
    class Extended(type(cfg)):
        added_later: SecretStr = SecretStr("added-later-secret")

    extended = Extended(**cfg.model_dump(), _env_file=None)
    assert "added-later-secret" not in build_mask(extended)(data="x added-later-secret y")
