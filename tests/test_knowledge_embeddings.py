from incident_platform.config import Settings
from incident_platform.knowledge.embeddings import provider

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}


def test_returns_none_when_unconfigured():
    assert provider(Settings(**BASE)) is None


def test_returns_a_provider_when_configured(monkeypatch):
    calls = {}

    def fake_init(model):
        calls["model"] = model
        return object()

    monkeypatch.setattr("incident_platform.knowledge.embeddings._init", fake_init)
    cfg = Settings(**BASE, embedding_model="voyage-3", embedding_dim=1024)
    assert provider(cfg) is not None
    assert calls["model"] == "voyage-3"
