"""Configure structured JSON logs and optional Langfuse tracing.

graph_config() sets incident_id as the LangGraph thread_id and adds correlation
metadata, tags, and optional callbacks. The thread ID selects checkpoint history;
this module does not persist workflow state.
"""

import logging

import structlog


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
