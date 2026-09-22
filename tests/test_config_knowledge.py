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


def test_disabled_by_default_and_needs_no_new_secret():
    cfg = Settings(**BASE)
    assert cfg.knowledge_enabled is False
    assert cfg.embedding_model is None


def test_enabling_without_a_token_is_rejected():
    with pytest.raises(ValueError, match="knowledge"):
        Settings(**BASE, knowledge_enabled=True, knowledge_mcp_token="")


def test_enabling_with_a_duplicate_token_is_rejected():
    with pytest.raises(ValueError, match="distinct"):
        Settings(**BASE, knowledge_enabled=True, knowledge_mcp_token="status")


def test_enabling_with_a_distinct_token_is_accepted():
    cfg = Settings(**BASE, knowledge_enabled=True, knowledge_mcp_token="knowledge")
    assert cfg.knowledge_mcp_token.get_secret_value() == "knowledge"


def test_embedding_model_requires_a_dimension():
    with pytest.raises(ValueError, match="embedding_dim"):
        Settings(**BASE, embedding_model="voyage-3")


def test_allowed_hosts_defaults_to_the_compose_hostname():
    # The MCP SDK enables DNS-rebinding protection by default and rejects any Host
    # header not on this list. The worker reaches the server by its Compose name.
    cfg = Settings(**BASE)
    assert "runbooks:8080" in cfg.knowledge_allowed_hosts
