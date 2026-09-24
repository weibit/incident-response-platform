"""Structured JSON logging, independent of tracing.

Separate from the Langfuse modules beside it because it has nothing to do with them: the
gateway configures logging without ever enabling tracing.
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
