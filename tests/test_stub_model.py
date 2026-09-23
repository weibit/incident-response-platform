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


def test_json_schema_mode_returns_parsable_content():
    # langchain-openai defaults with_structured_output to method="json_schema", which
    # sends response_format and expects JSON content rather than a tool call.
    client = TestClient(app)
    payload = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub-plan",
            "messages": [],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "Triage", "schema": TRIAGE_TOOL["function"]["parameters"]},
            },
        },
    ).json()
    message = payload["choices"][0]["message"]
    assert message.get("tool_calls") is None
    arguments = json.loads(message["content"])
    assert arguments["severity"] == "critical"
    assert arguments["investigate"] is True


def test_json_schema_mode_is_also_recorded():
    client = TestClient(app)
    client.delete("/_calls")
    client.post(
        "/v1/chat/completions",
        json={
            "model": "stub-rca",
            "messages": [],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "Triage", "schema": TRIAGE_TOOL["function"]["parameters"]},
            },
        },
    )
    assert client.get("/_calls").json() == ["stub-rca"]


def test_an_optional_field_becomes_null_not_a_made_up_value():
    # Strict json_schema mode marks every property required and expresses optionality as a
    # null branch. Inventing a value there fails validation against a Literal or a nested
    # model, which is exactly how the investigate role broke before this.
    client = TestClient(app)
    schema = {
        "type": "object",
        "properties": {
            "hypothesis": {"type": "string"},
            "tool": {"anyOf": [{"enum": ["logs", "metrics"]}, {"type": "null"}]},
            "conclusion": {"anyOf": [{"type": "object", "properties": {}}, {"type": "null"}]},
        },
        "required": ["hypothesis", "tool", "conclusion"],
    }
    payload = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub-rca",
            "messages": [],
            "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        },
    ).json()
    arguments = json.loads(payload["choices"][0]["message"]["content"])
    assert arguments["hypothesis"] == "stub"
    assert arguments["tool"] is None
    assert arguments["conclusion"] is None


def test_an_enum_behind_a_ref_is_resolved():
    # Pydantic hoists nested enums and models into $defs and references them. Without
    # resolution the stub returns "stub" for an enum field, which fails validation - how
    # the plan role broke before this.
    client = TestClient(app)
    schema = {
        "type": "object",
        "$defs": {"Action": {"enum": ["observe", "rollback_deployment"]}},
        "properties": {"action": {"$ref": "#/$defs/Action"}},
        "required": ["action"],
    }
    payload = client.post(
        "/v1/chat/completions",
        json={
            "model": "stub-plan",
            "messages": [],
            "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        },
    ).json()
    assert json.loads(payload["choices"][0]["message"]["content"])["action"] == "observe"
