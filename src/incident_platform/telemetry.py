"""Configure structured JSON logs and optional Langfuse tracing.

graph_config() sets incident_id as the LangGraph thread_id and adds correlation
metadata, tags, and optional callbacks. The thread ID selects checkpoint history;
this module does not persist workflow state.
"""

import logging
import re

import structlog
from langfuse import Langfuse


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def trace_id_for(incident_id: str, event_id: str) -> str:
    """Derive one delivered event's Langfuse trace id.

    A pure function of values every component already carries, so no mapping is stored.
    A retry of the same event therefore reuses its trace rather than creating a near
    duplicate, which is what keeps later score attachment idempotent.
    """
    return Langfuse.create_trace_id(seed=f"{incident_id}:{event_id}")


REDACTED = "[redacted]"
BEARER = re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE)


def build_mask(cfg):
    """Redact known credentials from everything sent to the trace store.

    Exact-value redaction only. Evidence, ticket and runbook bodies are traced verbatim;
    heuristics would false-positive on log and stack-trace text and cannot be pinned by a
    test. Empty credentials are dropped because "" matches everywhere.
    """
    secrets = sorted(
        (
            value
            for value in (
                cfg.jira_webhook_token.get_secret_value(),
                cfg.jira_approval_token.get_secret_value(),
                cfg.status_token.get_secret_value(),
                cfg.slack_signing_secret.get_secret_value(),
                cfg.siem_signing_secret.get_secret_value(),
                cfg.litellm_api_key.get_secret_value(),
                cfg.knowledge_mcp_token.get_secret_value(),
            )
            if value
        ),
        key=len,
        reverse=True,
    )

    def redact(text):
        # Longest first, so a secret that contains another is not partially rewritten.
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        return BEARER.sub(rf"\1{REDACTED}", text)

    def walk(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [walk(item) for item in value]
        return value

    def mask(*, data, **_):
        return walk(data)

    return mask


def graph_config(incident_id: str, correlation_id: str, enabled: bool) -> dict:
    callbacks = []
    if enabled:
        from langfuse.langchain import CallbackHandler

        callbacks.append(CallbackHandler())
    return {
        "configurable": {"thread_id": incident_id},
        "callbacks": callbacks,
        "metadata": {
            "incident_id": incident_id,
            "correlation_id": correlation_id,
            "langfuse_session_id": incident_id,
        },
        "tags": ["incident-response"],
        "recursion_limit": 100,
    }
