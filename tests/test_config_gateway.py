import pytest
from pydantic import ValidationError

from incident_platform.config import Settings

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}


def test_role_models_fall_back_to_llm_model():
    cfg = Settings(**BASE, llm_model="shared-alias")
    assert cfg.triage_model == "shared-alias"
    assert cfg.rca_model == "shared-alias"
    assert cfg.plan_model == "shared-alias"


def test_role_models_override_independently():
    cfg = Settings(**BASE, llm_model="shared", llm_model_rca="incident-rca")
    assert cfg.rca_model == "incident-rca"
    # The roles that were not overridden must keep the shared default.
    assert cfg.triage_model == "shared"
    assert cfg.plan_model == "shared"


def test_gateway_key_is_required_once_retrieval_is_enabled():
    # Retrieval is gateway-only, so an enabled feature with no gateway credential
    # would fail at the first tool call rather than at startup.
    with pytest.raises(ValidationError, match="litellm_api_key"):
        Settings(**BASE, knowledge_enabled=True, knowledge_mcp_token="knowledge")


def test_gateway_key_is_required_once_mock_mode_is_off():
    with pytest.raises(ValidationError, match="litellm_api_key"):
        Settings(**BASE, mock_mode=False)


def test_gateway_key_is_not_required_by_the_default_configuration():
    cfg = Settings(**BASE)
    assert cfg.knowledge_enabled is False
    assert cfg.mock_mode is True
    assert cfg.litellm_api_key.get_secret_value() == ""


def test_gateway_key_joins_the_distinct_credential_check():
    with pytest.raises(ValidationError, match="distinct"):
        Settings(**BASE, mock_mode=False, litellm_api_key="jira")


def test_knowledge_url_defaults_to_the_gateway():
    # Trailing slash matters: /mcp 307-redirects and the MCP SDK will not follow it.
    assert Settings(**BASE).knowledge_mcp_url == "http://litellm:4000/mcp/"
