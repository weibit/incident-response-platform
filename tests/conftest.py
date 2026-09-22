import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from incident_platform.config import Settings
from incident_platform.storage import Conflict


@pytest.fixture
def cfg():
    return Settings(
        _env_file=None,
        jira_webhook_token="jira",
        jira_approval_token="approve",
        status_token="status",
        slack_signing_secret="slack",
        siem_signing_secret="siem",
        max_investigation_rounds=6,
    )


@pytest.fixture
def incident():
    identifier = str(uuid4())
    return {
        "incident_id": identifier,
        "correlation_id": identifier,
        "raw": {
            "source_event_id": str(uuid4()),
            "external_incident_id": "INC-1",
            "summary": "Checkout is timing out",
            "service": "checkout",
            "severity": "high",
        },
    }


class MemoryStore:
    def __init__(self):
        self.events, self.approvals, self.executions, self.states, self.ingress = [], {}, {}, {}, {}
        self.audits = []

    async def open(self):
        pass

    async def close(self):
        pass

    async def emit(self, state, kind, payload):
        self.events.append((kind, copy.deepcopy(payload)))

    async def audit(self, incident_id, kind, detail):
        self.audits.append((kind, detail))

    async def request_approval(self, state, ttl):
        self.approvals.setdefault(
            state["plan"]["plan_id"],
            {
                "plan": copy.deepcopy(state["plan"]),
                "incident_id": state["incident_id"],
                "decision": None,
                "expires": datetime.now(UTC) + timedelta(seconds=ttl),
            },
        )

    async def decide(self, incident_id, plan_id, approved, actor):
        row = await self.approved(incident_id, plan_id)
        if not row:
            raise Conflict("Missing or expired approval")
        decision = {"plan_id": plan_id, "approved": approved, "actor": actor}
        if row["decision"] and row["decision"] != decision:
            raise Conflict("Already decided")
        row["decision"] = decision

    async def approved(self, incident_id, plan_id):
        row = self.approvals.get(plan_id)
        if row and row["incident_id"] == incident_id and row["expires"] > datetime.now(UTC):
            return row
        return None

    async def simulate_execution(self, state):
        key = state["plan"]["plan_id"]
        return self.executions.setdefault(key, {"simulated": True, "success": True, "plan_id": key})

    async def save_state(self, state):
        self.states[state["incident_id"]] = copy.deepcopy(state)

    async def get_state(self, incident_id):
        return self.states.get(incident_id)

    async def accept(self, source, source_event_id, event):
        key = (source, source_event_id)
        digest = hashlib.sha256(json.dumps(event.payload, sort_keys=True).encode()).hexdigest()
        if key in self.ingress:
            prior, fingerprint = self.ingress[key]
            if fingerprint != digest:
                raise Conflict("Conflicting payload")
            return prior, True
        self.ingress[key] = (event.model_dump(mode="json"), digest)
        return self.ingress[key][0], False


@pytest.fixture
def store():
    return MemoryStore()
