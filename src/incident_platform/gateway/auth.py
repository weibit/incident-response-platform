"""Validate bearer credentials and signed HTTP requests.

HMAC verification uses the original request bytes and a five-minute timestamp window,
with Slack's v0 signature format supported. Routes enforce actor restrictions, while
workflow policy separately controls permission to execute a mitigation plan.
"""

import hashlib
import hmac
import time

from fastapi import HTTPException


def bearer(actual: str | None, expected: str):
    if not expected or not hmac.compare_digest(actual or "", f"Bearer {expected}"):
        raise HTTPException(401, "Invalid credentials")


def signed(
    body: bytes, timestamp: str | None, signature: str | None, secret: str, *, slack: bool = False
):
    try:
        valid_time = abs(time.time() - int(timestamp or "0")) <= 300
    except ValueError:
        valid_time = False
    prefix = b"v0:" if slack else b""
    message = prefix + (timestamp or "").encode() + b":" + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    expected = f"v0={expected}" if slack else expected
    if not secret or not valid_time or not hmac.compare_digest(signature or "", expected):
        raise HTTPException(401, "Invalid signature or timestamp")
