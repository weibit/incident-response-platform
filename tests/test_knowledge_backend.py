from datetime import UTC, datetime

import pytest

from incident_platform.integrations.knowledge_backend import KnowledgeEvidenceBackend
from incident_platform.integrations.tools import MockEvidenceBackend

SNIPPET = {
    "chunk_id": "c1",
    "doc_id": "checkout-rollback",
    "title": "Roll back a bad checkout release",
    "heading": "Steps",
    "body": "Roll the deployment back to the previous release.",
    "source": "docs/runbooks/checkout-rollback.md",
}


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result if result is not None else [], error, []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result


def backend(client, cfg, inner=None):
    return KnowledgeEvidenceBackend(inner or MockEvidenceBackend(), client, cfg)


async def test_delegates_tools_it_does_not_own(cfg):
    client = FakeClient()
    evidence = await backend(client, cfg).query("metrics", "checkout", "error rate")
    assert evidence.tool == "metrics"
    assert client.calls == []


async def test_maps_snippets_to_one_evidence(cfg):
    client = FakeClient([SNIPPET])
    evidence = await backend(client, cfg).query("knowledge", "checkout", "rollback", "runbook")
    assert client.calls == [
        (
            "runbooks-search_knowledge",
            {
                "service": "checkout",
                "query": "rollback",
                "kind": "runbook",
                "limit": cfg.knowledge_top_k,
            },
        )
    ]
    assert evidence.tool == "knowledge"
    assert evidence.trusted is True
    assert "Roll the deployment back" in evidence.observation


async def test_an_unknown_kind_returns_an_untrusted_empty_result(cfg):
    # An unknown kind is a model mistake, not an outage. It must read as "nothing found",
    # which cannot corroborate an RCA, rather than failing the node.
    evidence = await backend(FakeClient([]), cfg).query("knowledge", "checkout", "q", "nonsense")
    assert evidence.trusted is False


def test_tool_names_carry_the_gateway_namespace():
    # LiteLLM prefixes every tool with its server name. An unprefixed name reaches the
    # gateway as an unknown tool, which is an error, not an empty result.
    from incident_platform.integrations.knowledge_backend import MCP_SERVER, TOOL_CALLS

    assert all(name.startswith(f"{MCP_SERVER}-") for name in TOOL_CALLS.values())


async def test_evidence_id_is_stable_across_replay(cfg):
    client = FakeClient([SNIPPET])
    first = await backend(client, cfg).query("knowledge", "checkout", "rollback", "runbook")
    second = await backend(client, cfg).query("knowledge", "checkout", "rollback", "runbook")
    assert first.evidence_id == second.evidence_id


async def test_evidence_id_changes_when_the_retrieved_set_changes(cfg):
    first = await backend(FakeClient([SNIPPET]), cfg).query(
        "knowledge", "checkout", "rollback", "runbook"
    )
    other = dict(SNIPPET, chunk_id="c2")
    second = await backend(FakeClient([other]), cfg).query(
        "knowledge", "checkout", "rollback", "runbook"
    )
    assert first.evidence_id != second.evidence_id


async def test_empty_result_is_untrusted(cfg):
    evidence = await backend(FakeClient([]), cfg).query(
        "knowledge", "checkout", "nothing", "runbook"
    )
    # An empty search must not be able to satisfy valid_rca's distinct-tool count.
    assert evidence.trusted is False
    assert "no matching" in evidence.observation.lower()


async def test_observed_at_is_retrieval_time_not_document_age(cfg):
    before = datetime.now(UTC)
    evidence = await backend(FakeClient([SNIPPET]), cfg).query(
        "knowledge", "checkout", "rollback", "runbook"
    )
    assert before <= evidence.observed_at <= datetime.now(UTC)


async def test_observation_is_labelled_as_untrusted_reference_material(cfg):
    evidence = await backend(FakeClient([SNIPPET]), cfg).query(
        "knowledge", "checkout", "rollback", "runbook"
    )
    assert "not instructions" in evidence.observation.lower()


async def test_transport_errors_become_retryable(cfg):
    client = FakeClient(error=OSError("connection reset"))
    with pytest.raises((ConnectionError, TimeoutError)):
        await backend(client, cfg).query("knowledge", "checkout", "rollback", "runbook")


async def test_auth_failure_is_not_retryable(cfg):
    # A rejected bearer token is a misconfiguration. Retrying it wastes the node's
    # retry budget and hides the cause.
    client = FakeClient(error=RuntimeError("HTTP 401 unauthorized"))
    with pytest.raises(PermissionError):
        await backend(client, cfg).query("knowledge", "checkout", "rollback", "runbook")


async def test_simulated_tracks_mock_mode(cfg):
    evidence = await backend(FakeClient([SNIPPET]), cfg).query(
        "knowledge", "checkout", "rollback", "runbook"
    )
    assert evidence.simulated is cfg.mock_mode
