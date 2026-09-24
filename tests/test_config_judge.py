import pytest

from incident_platform.config import Settings

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}


def test_judge_is_off_by_default():
    assert Settings(**BASE).judge_enabled is False


def test_judge_model_falls_back_to_llm_model():
    cfg = Settings(**BASE, llm_model="shared-alias")
    assert cfg.judge_model == "shared-alias"


def test_judge_model_uses_its_own_alias_when_set():
    cfg = Settings(**BASE, llm_model_judge="incident-judge")
    assert cfg.judge_model == "incident-judge"


def test_judge_requires_tracing():
    # Its writes are silently dropped without a trace to attach them to, so this is not a
    # preference: without tracing the judge produces nothing and reports no error.
    with pytest.raises(ValueError, match="langfuse_enabled"):
        Settings(**BASE, judge_enabled=True, mock_mode=False, litellm_api_key="gw")


def test_judge_requires_the_gateway():
    with pytest.raises(ValueError, match="litellm_api_key"):
        Settings(**BASE, judge_enabled=True, langfuse_enabled=True)
