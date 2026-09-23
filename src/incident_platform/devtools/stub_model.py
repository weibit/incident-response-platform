"""Development-only OpenAI-compatible stub upstream. Never enable in a deployment.

Synthesises a reply from whatever schema the caller sent, and records which upstream model
name it was reached under. That recording is the point: it is what proves each analyst role
routed to a different model through the gateway, which LiteLLM's own `mock_response` cannot
show.

langchain-openai 1.6.4 defaults `with_structured_output` to `method="json_schema"`, so the
analyst sends `response_format` and expects JSON content. The `tools` branch is kept because
`method="function_calling"` is a supported setting and would otherwise fail confusingly.

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
    response_format: dict | None = None


def _resolve(schema: dict, root: dict) -> dict:
    """Follow a local $ref. Pydantic hoists nested models and enums into $defs."""
    ref = schema.get("$ref")
    if not ref or not ref.startswith("#/"):
        return schema
    target = root
    for part in ref[2:].split("/"):
        target = target.get(part, {})
    return target or schema


def _value(schema: dict, root: dict):
    """Smallest value satisfying one property schema. Enums take their first member."""
    schema = _resolve(schema, root)
    branches = schema.get("anyOf")
    if branches:
        # Strict json_schema mode marks every property required and expresses optionality
        # as a null branch. Choosing null is what "not provided" means, and it keeps the
        # stub from inventing a value for a field the caller left open - which would fail
        # validation against a Literal or a nested model.
        if any(branch.get("type") == "null" for branch in branches):
            return None
        return _value(branches[0], root)
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
        return _arguments(schema, root)
    return "stub"


def _arguments(schema: dict, root: dict | None = None) -> dict:
    """Synthesise only the required properties. Optional ones are left out deliberately."""
    root = schema if root is None else root
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {name: _value(properties.get(name) or {}, root) for name in required}


def _completion(model: str, content: str) -> dict:
    """A minimal chat completion carrying JSON content, which json_schema mode parses."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _schema_from_response_format(response_format: dict | None) -> dict | None:
    """The JSON schema of a json_schema response_format, or None for any other shape."""
    if not response_format or response_format.get("type") != "json_schema":
        return None
    return (response_format.get("json_schema") or {}).get("schema") or {}


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest) -> dict:
    schema = _schema_from_response_format(request.response_format)
    if schema is None and not request.tools:
        raise HTTPException(
            status_code=400,
            detail="stub upstream expects response_format=json_schema or a tool-calling request",
        )
    RECORDED.append(request.model)
    if schema is not None:
        return _completion(request.model, content=json.dumps(_arguments(schema)))
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
