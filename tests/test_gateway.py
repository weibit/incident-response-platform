import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from incident_platform.gateway.app import create_app


def signature(body, timestamp, secret="slack", slack=True):
    base = ("v0:" if slack else "") + timestamp + ":"
    value = hmac.new(secret.encode(), base.encode() + body, hashlib.sha256).hexdigest()
    return ("v0=" if slack else "") + value


def test_jira_accept_dedup_and_conflict(cfg, store, incident):
    with TestClient(create_app(cfg, store, relay=False)) as client:
        headers = {"Authorization": "Bearer jira"}
        first = client.post("/webhooks/jira", json=incident["raw"], headers=headers)
        second = client.post("/webhooks/jira", json=incident["raw"], headers=headers)
        assert first.status_code == 202
        assert first.json()["event_id"] == second.json()["event_id"]
        assert second.json()["duplicate"] is True
        changed = {**incident["raw"], "summary": "Changed"}
        assert client.post("/webhooks/jira", json=changed, headers=headers).status_code == 409
        assert client.post("/webhooks/jira", json=incident["raw"]).status_code == 401
        assert client.post("/webhooks/jira", json={}, headers=headers).status_code == 422


def test_slack_signature_replay_window_and_challenge(cfg, store, incident):
    body = json.dumps(incident["raw"]).encode()
    timestamp = str(int(time.time()))
    with TestClient(create_app(cfg, store, relay=False)) as client:
        headers = {
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature(body, timestamp),
        }
        assert client.post("/webhooks/slack", content=body, headers=headers).status_code == 202
        assert (
            client.post("/webhooks/slack", content=body + b" ", headers=headers).status_code == 401
        )
        old = str(int(time.time()) - 600)
        headers = {"X-Slack-Request-Timestamp": old, "X-Slack-Signature": signature(body, old)}
        assert client.post("/webhooks/slack", content=body, headers=headers).status_code == 401
        challenge = b'{"type":"url_verification","challenge":"abc"}'
        headers = {
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature(challenge, timestamp),
        }
        assert client.post("/webhooks/slack", content=challenge, headers=headers).json() == {
            "challenge": "abc"
        }


def test_body_limit_status_and_siem(cfg, store, incident):
    with TestClient(create_app(cfg, store, relay=False)) as client:
        assert (
            client.post("/webhooks/jira", content=b"x" * (cfg.max_body_bytes + 1)).status_code
            == 413
        )
        assert client.get(f"/incidents/{incident['incident_id']}").status_code == 401
        body = json.dumps(incident["raw"]).encode()
        timestamp = str(int(time.time()))
        headers = {
            "X-Timestamp": timestamp,
            "X-Signature": signature(body, timestamp, "siem", False),
        }
        assert client.post("/webhooks/siem", content=body, headers=headers).status_code == 202


def test_slack_unauthorized_approver(cfg, store, incident):
    body = json.dumps({"user": {"id": "U_ATTACKER"}, "actions": []}).encode()
    timestamp = str(int(time.time()))
    with TestClient(create_app(cfg, store, relay=False)) as client:
        response = client.post(
            f"/callbacks/slack/approvals/{incident['incident_id']}",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": signature(body, timestamp),
            },
        )
        assert response.status_code == 403


def test_jira_ingress_credential_cannot_approve(cfg, store, incident):
    with TestClient(create_app(cfg, store, relay=False)) as client:
        response = client.post(
            f"/callbacks/jira/approvals/{incident['incident_id']}",
            json={"plan_id": "x", "approved": True},
            headers={"Authorization": "Bearer jira"},
        )
        assert response.status_code == 401


def test_signed_slack_form_approval_is_idempotent(cfg, store, incident):
    from datetime import UTC, datetime, timedelta
    from urllib.parse import urlencode

    plan_id = "immutable-plan"
    store.approvals[plan_id] = {
        "incident_id": incident["incident_id"],
        "plan": {"plan_id": plan_id},
        "decision": None,
        "expires": datetime.now(UTC) + timedelta(minutes=1),
    }
    data = {
        "user": {"id": "U_DEMO_APPROVER"},
        "actions": [{"value": json.dumps({"plan_id": plan_id, "approved": True})}],
    }
    body = urlencode({"payload": json.dumps(data)}).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature(body, timestamp),
    }
    with TestClient(create_app(cfg, store, relay=False)) as client:
        url = f"/callbacks/slack/approvals/{incident['incident_id']}"
        assert client.post(url, content=body, headers=headers).status_code == 202
        assert client.post(url, content=body, headers=headers).status_code == 202
        assert store.approvals[plan_id]["decision"]["actor"] == "U_DEMO_APPROVER"


def test_malformed_signed_slack_event_and_strict_approval(cfg, store, incident):
    body = b'{"type":"event_callback","event":null}'
    timestamp = str(int(time.time()))
    with TestClient(create_app(cfg, store, relay=False)) as client:
        assert (
            client.post(
                "/webhooks/slack",
                content=body,
                headers={
                    "X-Slack-Request-Timestamp": timestamp,
                    "X-Slack-Signature": signature(body, timestamp),
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/callbacks/jira/approvals/{incident['incident_id']}",
                headers={"Authorization": "Bearer approve"},
                json={"plan_id": "x", "approved": "true"},
            ).status_code
            == 422
        )
