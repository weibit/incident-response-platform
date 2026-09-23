"""Prove both gateway paths end to end. Development-only; run inside the worker container.

    docker compose exec -T worker python -m incident_platform.devtools.verify_gateway

Exits non-zero on the first failed assertion.
"""

import asyncio
import json
import sys
import urllib.request

from incident_platform.config import Settings
from incident_platform.integrations.knowledge_backend import KnowledgeEvidenceBackend
from incident_platform.integrations.llm import Analyst
from incident_platform.integrations.tools import MockEvidenceBackend
from incident_platform.knowledge import mcp_client

STUB = "http://stub-model:8100/_calls"
STATE = {
    "incident": {"service": "checkout", "severity": "high", "summary": "Checkout timing out"},
    "evidence": [],
    "rounds": 0,
}


def _recorded() -> list[str]:
    with urllib.request.urlopen(STUB) as response:
        return json.load(response)


def _reset() -> None:
    urllib.request.urlopen(urllib.request.Request(STUB, method="DELETE")).read()


def _live_settings() -> Settings:
    """A non-mock configuration built in-process.

    mock_mode stays true for the running worker. This object exists only to construct an
    analyst that talks to the gateway, so the deployment's fail-closed coupling between
    simulated evidence and simulated execution is never disturbed.
    """
    base = Settings()
    return Settings(
        _env_file=None,
        mock_mode=False,
        jira_webhook_token="verify-jira",
        jira_approval_token="verify-approve",
        status_token="verify-status",
        slack_signing_secret="verify-slack",
        siem_signing_secret="verify-siem",
        litellm_base_url=base.litellm_base_url,
        litellm_api_key=base.litellm_api_key.get_secret_value(),
        llm_model_triage=base.triage_model,
        llm_model_rca=base.rca_model,
        llm_model_plan=base.plan_model,
    )


async def verify_roles() -> None:
    _reset()
    analyst = Analyst(_live_settings())
    await analyst.triage(STATE)
    await analyst.investigate(STATE)
    await analyst.plan(STATE)
    recorded = _recorded()
    assert len(recorded) == 3, f"expected three upstream calls, got {recorded}"
    assert len(set(recorded)) == 3, f"roles did not route to distinct models: {recorded}"
    print(f"roles routed to distinct upstreams: {recorded}")


async def verify_retrieval() -> None:
    cfg = Settings()
    assert cfg.knowledge_enabled, "set KNOWLEDGE_ENABLED=true before running this"
    async with mcp_client(cfg) as client:
        backend = KnowledgeEvidenceBackend(MockEvidenceBackend(), client, cfg)
        evidence = await backend.query("knowledge", "checkout", "rollback error rate", "runbook")
        documents = evidence.observation.count("BEGIN DOCUMENT")
        assert documents == 5, f"expected five documents through the gateway, got {documents}"
        assert evidence.trusted is True
        delegated = await backend.query("metrics", "checkout", "error rate")
        assert delegated.tool == "metrics", "a non-retrieval tool must not reach the gateway"
    print(f"retrieval through the gateway returned {documents} trusted documents")


async def main() -> None:
    await verify_roles()
    await verify_retrieval()
    print("gateway verification passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        raise SystemExit(1) from failure
