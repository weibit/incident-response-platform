import pytest
from langchain.chat_models import init_chat_model
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_openai.chat_models.base import BaseChatOpenAI

from incident_platform.config import Settings
from incident_platform.integrations.llm import Analyst, _structured_model
from incident_platform.models import Triage
from incident_platform.observability import trace_id_for

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
        self.result = result

    def with_structured_output(self, schema, **_):
        # Swallows kwargs, so it can never verify extra_body forwarding. The trace tests
        # compose a real model for that; this double only covers the return path.
        return RunnableLambda(lambda _: self.result)


def _recording(monkeypatch):
    model = RecordingModel(Triage(severity="high", investigate=True, reason="ok"))
    monkeypatch.setattr("incident_platform.integrations.llm._model", lambda cfg, alias: model)
    return model


def _real_model():
    return init_chat_model("openai:incident-triage", base_url="http://gateway/v1", api_key="k")


def test_trace_id_reaches_the_request_kwargs():
    # A hand-written double cannot catch this. model.bind(...).with_structured_output(...)
    # silently drops the binding, so only composing a real model proves the id survives.
    step = _structured_model(_real_model(), Triage, TRACED_STATE).steps[0]
    assert step.kwargs["extra_body"] == {
        "metadata": {"existing_trace_id": trace_id_for("inc-1", "evt-1")}
    }
    assert "response_format" in step.kwargs  # structured output still intact


def test_no_extra_body_when_state_lacks_identifiers():
    # A gateway-side trace is never a reason a model call fails.
    step = _structured_model(_real_model(), Triage, {"incident": {}, "evidence": []}).steps[0]
    assert "extra_body" not in step.kwargs
    assert "response_format" in step.kwargs


async def test_structured_still_returns_the_parsed_result(monkeypatch):
    _recording(monkeypatch)
    analyst = Analyst(_live(llm_model="shared-alias"))
    result = await analyst.structured("triage", Triage, "Assess", TRACED_STATE)
    assert result.severity == "high"


class _Stop(Exception):
    """Ends the call once the payload is captured, so no request is attempted."""


async def test_trace_id_reaches_the_outgoing_request_body(monkeypatch):
    # kwargs on the runnable are one level short of the wire, and the wire is where the
    # original bug lived. This drives the real ainvoke path and reads the payload the HTTP
    # client would have sent.
    captured = {}
    original = BaseChatOpenAI._get_request_payload

    def capture(self, input_, *, stop=None, **kwargs):
        captured.update(original(self, input_, stop=stop, **kwargs))
        raise _Stop

    monkeypatch.setattr(BaseChatOpenAI, "_get_request_payload", capture)
    chain = ChatPromptTemplate.from_messages([("human", "{q}")]) | _structured_model(
        _real_model(), Triage, TRACED_STATE
    )
    with pytest.raises(_Stop):
        await chain.ainvoke({"q": "hi"})
    assert captured["extra_body"] == {
        "metadata": {"existing_trace_id": trace_id_for("inc-1", "evt-1")}
    }
    assert "response_format" in captured
