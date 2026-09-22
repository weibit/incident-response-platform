import json

from fastapi.testclient import TestClient

from incident_platform.devtools.stub_model import RECORDED, app

TRIAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "Triage",
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {"enum": ["critical", "high", "medium", "low"]},
                "investigate": {"type": "boolean"},
                "reason": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["severity", "investigate", "reason"],
        },
    },
}


def _body(model):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [TRIAGE_TOOL],
    }


def test_returns_a_tool_call_matching_the_requested_schema():
    client = TestClient(app)
    payload = client.post("/v1/chat/completions", json=_body("stub-triage")).json()
    message = payload["choices"][0]["message"]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    call = message["tool_calls"][0]
    assert call["function"]["name"] == "Triage"
    arguments = json.loads(call["function"]["arguments"])
    assert arguments["severity"] == "critical"
    assert arguments["investigate"] is True
    assert isinstance(arguments["reason"], str)
    # Only required properties are synthesised; an optional one must not appear.
    assert "note" not in arguments


def test_records_the_upstream_model_name_it_was_called_under():
    client = TestClient(app)
    client.delete("/_calls")
    client.post("/v1/chat/completions", json=_body("stub-rca"))
    assert client.get("/_calls").json() == ["stub-rca"]


def test_a_number_stays_inside_a_bounded_schema():
    # RCA.confidence declares 0 <= x <= 1. A stub that returned 1.5 would fail
    # validation for a reason unrelated to the gateway being tested.
    client = TestClient(app)
    tool = {
        "type": "function",
        "function": {
            "name": "RCA",
            "parameters": {
                "type": "object",
                "properties": {"confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["confidence"],
            },
        },
    }
    payload = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "tools": [tool]},
    ).json()
    arguments = json.loads(
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    )
    assert 0 <= arguments["confidence"] <= 1


def test_rejects_a_request_carrying_no_tools():
    # with_structured_output always sends tools. A request without them means the
    # analyst wiring changed, and the stub must fail loudly rather than return prose
    # that would parse as an empty result.
    client = TestClient(app)
    response = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert response.status_code == 400


def test_health_endpoint_reports_ok():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_recorded_is_importable_for_assertions():
    assert isinstance(RECORDED, list)
