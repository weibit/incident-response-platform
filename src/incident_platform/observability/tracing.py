"""Derive trace identity and attach Langfuse to the workflow.

graph_config() sets incident_id as the LangGraph thread_id, derives this delivered event's
trace id, and adds correlation metadata, tags and optional callbacks. One trace covers one
delivered event; traces for an incident are grouped by langfuse_session_id. The thread ID
selects checkpoint history; this module does not persist workflow state.
"""

import structlog
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from incident_platform.observability.masking import build_mask

log = structlog.get_logger()


def trace_id_for(incident_id: str, event_id: str) -> str:
    """Derive one delivered event's Langfuse trace id.

    A pure function of values every component already carries, so no mapping is stored.
    A retry of the same event therefore reuses its trace rather than creating a near
    duplicate, which is what keeps later score attachment idempotent.
    """
    return Langfuse.create_trace_id(seed=f"{incident_id}:{event_id}")


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
