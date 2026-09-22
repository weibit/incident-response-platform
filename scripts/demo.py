"""Real HTTP demo against Compose; no external providers or production actions."""

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid

from incident_platform.config import settings


def request(base, path, token, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        base + path,
        body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def poll(base, incident_id, token, wanted):
    for _ in range(120):
        try:
            state = request(base, f"/incidents/{incident_id}", token)
            if state.get("status") in wanted:
                return state
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        time.sleep(0.5)
    raise TimeoutError(f"Incident did not reach {wanted}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    cfg = settings()
    identifier = str(uuid.uuid4())
    payload = {
        "source_event_id": identifier,
        "external_incident_id": f"DEMO-{identifier}",
        "summary": "Checkout errors after deployment",
        "service": "checkout",
        "severity": "high",
    }
    accepted = request(
        args.base_url, "/webhooks/jira", cfg.jira_webhook_token.get_secret_value(), payload
    )
    duplicate = request(
        args.base_url, "/webhooks/jira", cfg.jira_webhook_token.get_secret_value(), payload
    )
    assert duplicate["duplicate"] and duplicate["event_id"] == accepted["event_id"]
    incident_id = accepted["incident_id"]
    state = poll(
        args.base_url, incident_id, cfg.status_token.get_secret_value(), {"awaiting_approval"}
    )
    print(
        json.dumps(
            {"incident_id": incident_id, "status": state["status"], "plan": state["plan"]}, indent=2
        )
    )
    request(
        args.base_url,
        f"/callbacks/jira/approvals/{incident_id}",
        cfg.jira_approval_token.get_secret_value(),
        {"plan_id": state["plan"]["plan_id"], "approved": not args.reject},
    )
    final = poll(
        args.base_url, incident_id, cfg.status_token.get_secret_value(), {"resolved", "denied"}
    )
    print(json.dumps({"status": final["status"], "execution": final.get("execution")}, indent=2))
    assert final["status"] == ("denied" if args.reject else "resolved")


if __name__ == "__main__":
    main()
