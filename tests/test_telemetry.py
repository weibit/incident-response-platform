import json

import pytest
from langfuse import get_client
from langfuse._client.resource_manager import LangfuseResourceManager
from pydantic import SecretStr

from incident_platform.telemetry import REDACTED, build_mask, init_tracing, trace_id_for


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


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.checked = False

    def auth_check(self):
        self.checked = True
        return True


def test_init_tracing_returns_none_when_disabled(cfg, monkeypatch):
    monkeypatch.setattr(
        "incident_platform.telemetry.Langfuse",
        lambda **kwargs: pytest.fail("client must not be constructed when disabled"),
    )
    assert init_tracing(cfg) is None


def test_init_tracing_passes_a_mask(cfg, monkeypatch):
    built = []
    monkeypatch.setattr(
        "incident_platform.telemetry.Langfuse",
        lambda **kwargs: built.append(FakeClient(**kwargs)) or built[-1],
    )
    client = init_tracing(cfg.model_copy(update={"langfuse_enabled": True}))
    # Callability alone would be satisfied by any lambda; assert it is the real mask.
    secret = cfg.jira_webhook_token.get_secret_value()
    assert client.kwargs["mask"](data=f"x {secret} y") == f"x {REDACTED} y"
    assert client.checked is True


def test_init_tracing_survives_a_failing_auth_check(cfg, monkeypatch):
    class Unreachable(FakeClient):
        def auth_check(self):
            raise ConnectionError("collector unreachable")

    monkeypatch.setattr("incident_platform.telemetry.Langfuse", Unreachable)
    # Tracing is never a reason the worker fails to start.
    assert init_tracing(cfg.model_copy(update={"langfuse_enabled": True})) is not None


def test_get_client_returns_a_masked_client_after_init(cfg, monkeypatch):
    # The justification for init_tracing: evaluation.py calls the SDK's bare get_client(),
    # and that call must redact. The SDK shares configuration through a resource manager
    # rather than returning the same object, so identity is not the property worth pinning.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    LangfuseResourceManager.reset()
    try:
        init_tracing(cfg.model_copy(update={"langfuse_enabled": True}))
        secret = cfg.jira_webhook_token.get_secret_value()
        assert get_client()._mask(data=f"leak {secret}") == "leak [redacted]"
    finally:
        LangfuseResourceManager.reset()
