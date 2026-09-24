import pytest
from langchain_core.runnables import RunnableLambda

from incident_platform.config import Settings
from incident_platform.integrations.llm import JUDGE_SYSTEM, Judge
from incident_platform.models import Verdict

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}

CITED = [{"evidence_id": "e1", "tool": "metrics", "observation": "p95 2400ms after v2"}]


def _cfg(**overrides):
    return Settings(
        **BASE,
        mock_mode=False,
        litellm_api_key="gateway",
        langfuse_enabled=True,
        judge_enabled=True,
        **overrides,
    )


class RecordingJudgeModel:
    def __init__(self, result):
        self.result, self.seen = result, None

    def with_structured_output(self, schema, **_):
        def capture(messages):
            self.seen = messages
            return self.result

        return RunnableLambda(capture)


def _recording(monkeypatch, result=None):
    model = RecordingJudgeModel(
        result or Verdict(label="supported", reason="metrics show the regression")
    )
    monkeypatch.setattr("incident_platform.integrations.llm._model", lambda cfg, alias: model)
    return model


def test_judge_system_prompt_treats_cited_evidence_as_untrusted():
    # Regression guard: deleting this instruction is invisible and reopens the injection path
    # that the analyst's own prompt already defends against.
    lowered = JUDGE_SYSTEM.lower()
    assert "untrusted" in lowered
    assert "never follow instructions" in lowered


async def test_faithfulness_returns_the_verdict(monkeypatch):
    _recording(monkeypatch)
    verdict = await Judge(_cfg()).faithfulness("release v2 regression", 0.9, CITED)
    assert verdict.label == "supported"
    assert verdict.reason


async def test_faithfulness_prompt_carries_the_hypothesis_and_cited_evidence(monkeypatch):
    # Named for what it checks. Exclusivity is not testable here — faithfulness() can only
    # carry what it is handed — so the "only cited evidence" property is pinned where it
    # actually lives, in cited_evidence's own test.
    cfg = _cfg()
    model = _recording(monkeypatch)
    await Judge(cfg).faithfulness("release v2 regression", 0.9, CITED)
    rendered = str(model.seen)
    assert "p95 2400ms after v2" in rendered
    assert "release v2 regression" in rendered
    # One exclusion that is meaningful at this level: no credential may reach the prompt.
    assert cfg.litellm_api_key.get_secret_value() not in rendered


async def test_judge_uses_its_own_role_alias(monkeypatch):
    built = []
    monkeypatch.setattr(
        "incident_platform.integrations.llm._model",
        lambda cfg, alias: built.append(alias) or RecordingJudgeModel(None),
    )
    Judge(_cfg(llm_model_judge="incident-judge"))
    assert built == ["incident-judge"]


def test_verdict_rejects_an_unknown_label():
    with pytest.raises(ValueError):
        Verdict(label="great", reason="x")
