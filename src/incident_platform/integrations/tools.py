from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from langchain_core.tools import StructuredTool

from incident_platform.models import Evidence

READ_TOOLS = (
    "logs",
    "metrics",
    "kubernetes",
    "deployments",
    "config_diffs",
    "topology",
    "runbooks",
    "similar_incidents",
)


class EvidenceBackend(Protocol):
    async def query(self, tool: str, service: str, query: str) -> Evidence: ...
    async def verify(self, service: str, execution: dict) -> bool: ...


class NotificationBackend(Protocol):
    async def jira_comment(self, incident_id: str, text: str, idempotency_key: str) -> None: ...
    async def slack_message(self, incident_id: str, text: str, idempotency_key: str) -> None: ...


class MutationBackend(Protocol):
    # Implementations must persist/reconcile remote operation IDs and honor idempotency keys.
    async def execute(self, action: str, target: str, idempotency_key: str) -> dict: ...
    async def reconcile(self, idempotency_key: str) -> dict: ...


class MockEvidenceBackend:
    """Explicitly simulated observations; never connect to a real environment."""

    async def query(self, tool, service, query):
        observations = {
            "metrics": "Simulated: checkout error rate 12%, latency p95 1800ms after release v2.",
            "logs": "Simulated: upstream timeout errors began after release v2.",
            "deployments": "Simulated: release v2 deployed 8 minutes before the alert; v1 healthy.",
            "config_diffs": "Simulated: release v2 changed upstream timeout from 5s to 100ms.",
            "kubernetes": "Simulated: pods ready; no OOMKilled events.",
            "topology": "Simulated: checkout -> payments; owner commerce-oncall.",
            "runbooks": "Simulated runbook: rollback v2 after approval; verify error rate <1%.",
            "similar_incidents": "Simulated prior timeout regression resolved by rollback.",
        }
        return Evidence(
            evidence_id=str(uuid5(NAMESPACE_URL, f"{service}:{tool}:{query}")),
            tool=tool,
            observation=observations[tool],
            trusted=True,
            simulated=True,
        )

    async def verify(self, service, execution):
        return execution.get("simulated") is True and execution.get("success") is True


class UnconfiguredEvidenceBackend:
    async def query(self, tool, service, query):
        raise NotImplementedError(f"Configure read-only {tool} adapter before live mode")

    async def verify(self, service, execution):
        raise NotImplementedError("Configure independent post-action SLO verification")


def read_tools(backend: EvidenceBackend, service: str):
    def make(name):
        async def query(query: str) -> dict:
            evidence = await backend.query(name, service, query)
            return evidence.model_dump(mode="json")

        return StructuredTool.from_function(
            coroutine=query,
            name=name,
            description=f"Read-only {name} evidence for the scoped service, queried on demand.",
        )

    return {name: make(name) for name in READ_TOOLS}
