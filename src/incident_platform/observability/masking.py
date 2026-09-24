"""Redact credentials from everything sent to the trace store.

Its own module because it is the one security-relevant piece of the tracing path and can be
reasoned about on its own: a pure function of the Settings model, with no SDK involvement.
"""

import re

from pydantic import SecretStr

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

    # One alternation, longest first, so a secret that contains another is not partially
    # rewritten and the placeholder is never rescanned. Sequential str.replace calls reread
    # their own output, so a secret that is a substring of REDACTED — "red", "a", "ted" —
    # corrupted text the mask had already redacted. None is the empty case: an empty
    # alternation matches at every position.
    pattern = re.compile("|".join(re.escape(secret) for secret in secrets)) if secrets else None

    def redact(text):
        if pattern:
            text = pattern.sub(REDACTED, text)
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
