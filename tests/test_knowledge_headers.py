from incident_platform.config import Settings
from incident_platform.knowledge import gateway_headers

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}


def _cfg():
    return Settings(
        **BASE,
        knowledge_enabled=True,
        knowledge_mcp_token="runbooks-token",
        litellm_api_key="gateway-key",
    )


def test_authenticates_the_caller_to_the_gateway():
    assert gateway_headers(_cfg())["x-litellm-api-key"] == "Bearer gateway-key"


def test_forwards_the_servers_own_bearer_token():
    # The gateway fronts the runbooks server's auth boundary; it does not replace it.
    # Without this header the server rejects the proxied call with 401.
    assert gateway_headers(_cfg())["x-mcp-runbooks-authorization"] == "Bearer runbooks-token"


def test_sends_no_plain_authorization_header():
    # A plain Authorization header is what the direct path used. Leaving it in place would
    # authenticate against the wrong boundary and mask a misconfigured gateway key.
    assert "Authorization" not in gateway_headers(_cfg())


def test_header_name_tracks_the_server_constant():
    from incident_platform.integrations.knowledge_backend import MCP_SERVER

    assert f"x-mcp-{MCP_SERVER}-authorization" in gateway_headers(_cfg())
