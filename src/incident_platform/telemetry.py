"""Configure structured JSON logs and optional Langfuse tracing.

graph_config() sets incident_id as the LangGraph thread_id, derives this delivered event's
trace id, and adds correlation metadata, tags and optional callbacks. One trace covers one
delivered event; traces for an incident are grouped by langfuse_session_id. The thread ID
selects checkpoint history; this module does not persist workflow state.
"""

import logging
import re

import structlog
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from pydantic import SecretStr

log = structlog.get_logger()


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
    test. Secrets are derived from the Settings model's SecretStr fields rather than listed, so a
    credential added later cannot silently reach the trace store. Empty ones are dropped
    because "" matches everywhere.
    """
    secrets = sorted(
        (
            value.get_secret_value()
            for name in type(cfg).model_fields
            if isinstance(value := getattr(cfg, name), SecretStr) and value.get_secret_value()
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


def init_tracing(cfg):
    """Construct a Langfuse client with masking configured.

    The SDK shares the masked configuration with clients obtained later via get_client().
    Returns None when tracing is disabled. The SDK disables itself rather than raising
    when credentials are absent, so a mistyped key degrades export instead of refusing
    startup: core correctness must not depend on trace delivery.
    """
    if not cfg.langfuse_enabled:
        return None
    try:
        client = Langfuse(mask=build_mask(cfg))
    except Exception as exc:
        # The SDK parses LANGFUSE_SAMPLE_RATE and LANGFUSE_TIMEOUT before it decides to
        # disable itself, so a typo in either raises out of the constructor. Worker startup
        # must never depend on tracing.
        log.warning("langfuse.init_failed", error_type=type(exc).__name__)
        return None
    try:
        if not client.auth_check():
            log.warning("langfuse.unauthenticated")
    except Exception as exc:
        log.warning("langfuse.auth_check_failed", error_type=type(exc).__name__)
    return client


def graph_config(incident_id: str, correlation_id: str, event_id: str, enabled: bool) -> dict:
    trace_id = trace_id_for(incident_id, event_id)
    callbacks = []
    if enabled:
        try:
            callbacks.append(
                CallbackHandler(trace_context={"trace_id": trace_id}, update_trace=True)
            )
        except Exception as exc:
            log.warning("langfuse.handler_unavailable", error_type=type(exc).__name__)
    return {
        "configurable": {"thread_id": incident_id},
        "callbacks": callbacks,
        "metadata": {
            "incident_id": incident_id,
            "correlation_id": correlation_id,
            "event_id": event_id,
            "trace_id": trace_id,
            "langfuse_session_id": incident_id,
        },
        "tags": ["incident-response"],
        "recursion_limit": 100,
    }
