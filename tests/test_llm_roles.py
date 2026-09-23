from langchain_core.runnables import RunnableLambda

from incident_platform.config import Settings
from incident_platform.integrations.llm import Analyst
from incident_platform.models import Triage
from incident_platform.telemetry import trace_id_for

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}

STATE = {"incident": {"severity": "high", "service": "checkout"}, "evidence": []}


def _live(**overrides):
    return Settings(**BASE, mock_mode=False, litellm_api_key="gateway", **overrides)


def test_builds_one_model_per_role(monkeypatch):
    built = []

    def fake(cfg, alias):
        built.append(alias)
        return object()

    monkeypatch.setattr("incident_platform.integrations.llm._model", fake)
    analyst = Analyst(
        _live(
            llm_model_triage="incident-triage",
            llm_model_rca="incident-rca",
            llm_model_plan="incident-plan",
        )
    )
    assert built == ["incident-triage", "incident-rca", "incident-plan"]
    assert set(analyst.models) == {"triage", "rca", "plan"}


def test_roles_share_one_alias_when_only_llm_model_is_set(monkeypatch):
    built = []
    monkeypatch.setattr(
        "incident_platform.integrations.llm._model",
        lambda cfg, alias: built.append(alias) or object(),
    )
    Analyst(_live(llm_model="shared-alias"))
    assert built == ["shared-alias", "shared-alias", "shared-alias"]


def test_mock_mode_builds_no_models(cfg):
    # Mock mode must not reach the gateway at all, so no client is constructed.
    assert Analyst(cfg).models == {}


async def test_mock_mode_still_returns_canned_triage(cfg):
    result = await Analyst(cfg).triage(STATE)
    assert result.severity == "high"
    assert result.investigate is True


TRACED_STATE = {
    "incident_id": "inc-1",
    "input_event_id": "evt-1",
    "incident": {"severity": "high", "service": "checkout"},
    "evidence": [],
}


class RecordingModel:
    def __init__(self, result):
        self.result, self.bound = result, None

    def bind(self, **kwargs):
        self.bound = kwargs
        return self

    def with_structured_output(self, schema):
        return RunnableLambda(lambda _: self.result)


def _recording(monkeypatch):
    model = RecordingModel(Triage(severity="high", investigate=True, reason="ok"))
    monkeypatch.setattr("incident_platform.integrations.llm._model", lambda cfg, alias: model)
    return model


async def test_structured_binds_the_derived_trace_id(monkeypatch):
    model = _recording(monkeypatch)
    analyst = Analyst(_live(llm_model="shared-alias"))
    await analyst.structured("triage", Triage, "Assess", TRACED_STATE)
    assert model.bound == {
        "extra_body": {"metadata": {"existing_trace_id": trace_id_for("inc-1", "evt-1")}}
    }


async def test_structured_skips_binding_without_identifiers(monkeypatch):
    model = _recording(monkeypatch)
    analyst = Analyst(_live(llm_model="shared-alias"))
    # A gateway-side trace is never a reason a model call fails.
    await analyst.structured("triage", Triage, "Assess", {"incident": {}, "evidence": []})
    assert model.bound is None
