"""Development-only OpenAI-compatible stub upstream. Never enable in a deployment.

LiteLLM's `mock_response` is documented only for string content, but the analyst uses
`with_structured_output`, which needs a tool call. This returns one, synthesised from the
JSON schema the caller sent, and records which upstream model name it was reached under so
per-role routing can be asserted without a provider account.

Nothing in the application imports this module. It runs only as its own Compose service.
"""

import json
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

# Module-level so a test and the verification script can read it back. This is a
# single-process development stub; it is not safe for concurrent use and does not need to be.
RECORDED: list[str] = []


class ChatRequest(BaseModel):
    model: str
    messages: list[dict] = []
    tools: list[dict] | None = None


def _value(schema: dict):
    """Smallest value satisfying one property schema. Enums take their first member."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "boolean":
        return True
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        # Clamped into any declared bound, so a bounded field like RCA.confidence
        # validates. Without this the parse would fail for a reason unrelated to routing.
        low, high = schema.get("minimum", 0.0), schema.get("maximum", 1.0)
        return max(low, min(high, 0.9))
    if kind == "array":
        return []
    if kind == "object":
        return _arguments(schema)
    return "stub"


def _arguments(schema: dict) -> dict:
    """Synthesise only the required properties. Optional ones are left out deliberately."""
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {name: _value(properties.get(name) or {}) for name in required}


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest) -> dict:
    if not request.tools:
        raise HTTPException(status_code=400, detail="stub upstream expects a tool-calling request")
    RECORDED.append(request.model)
    function = request.tools[0]["function"]
    arguments = _arguments(function.get("parameters") or {})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": function["name"],
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@app.get("/_calls")
async def calls() -> list[str]:
    return RECORDED


@app.delete("/_calls")
async def reset() -> dict:
    RECORDED.clear()
    return {"cleared": True}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
