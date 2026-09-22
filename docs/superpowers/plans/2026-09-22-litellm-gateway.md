# LiteLLM Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route both model calls and MCP tool calls through one LiteLLM proxy, so the application names roles rather than providers and the platform tier owns model selection and tool entitlements.

**Architecture:** Three new Compose services form an AI platform tier: `litellm` (proxy, both `/v1/chat/completions` and `/mcp`), `litellm-postgres` (its own instance, so the incident schema keeps sole ownership of its server) and `redis`. A fourth, `stub-model`, is a development-only OpenAI-compatible upstream returning real `tool_calls` so the analyst path can be exercised with no credentials. The `runbooks` MCP server is unchanged but is now reached only by LiteLLM.

**Tech Stack:** LiteLLM proxy (Docker), PostgreSQL 17, Redis, FastAPI (already a dependency), `langchain-openai` (new), `mcp` 2.2.0 client.

**Design:** `docs/superpowers/specs/2026-09-22-litellm-gateway-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/incident_platform/devtools/__init__.py` | New package marker. Development-only tooling that ships in the image but is never imported by the app. |
| `src/incident_platform/devtools/stub_model.py` | OpenAI-compatible stub upstream. Returns a `tool_calls` response synthesised from the requested JSON schema; records the upstream model name it was called under. |
| `src/incident_platform/devtools/verify_gateway.py` | Runs inside the worker container. Proves per-role routing and MCP-through-gateway retrieval. |
| `src/incident_platform/config.py` | Adds `litellm_base_url`, `litellm_api_key` and three role aliases with fallback properties. |
| `src/incident_platform/integrations/llm.py` | Builds one model per role against the gateway instead of one shared model. |
| `src/incident_platform/integrations/knowledge_backend.py` | `TOOL_CALLS` moves to LiteLLM's `<server>-<tool>` namespacing. |
| `src/incident_platform/knowledge/__init__.py` | `mcp_client` sends gateway headers; header construction extracted so it is testable without a network. |
| `deploy/litellm/config.yaml` | Model aliases, `mcp_servers`, `allowed_tools`. Mounted into the proxy. |
| `compose.yaml` | The four new services and the worker's changed dependency. |
| `pyproject.toml` | Adds `langchain-openai`. |

Tests: `tests/test_stub_model.py`, `tests/test_config_gateway.py`, `tests/test_llm_roles.py`, `tests/test_knowledge_headers.py`, plus edits to `tests/test_knowledge_backend.py`.

---

## Task 1: Settle the LiteLLM surface

Nothing is written in this task. It exists because the previous plan in this repository baked a documentation guess (`structuredContent`) in as fact, and the resulting bug returned an empty corpus silently. Do not repeat that.

**Files:** none modified.

- [ ] **Step 1: Pin and pull the image**

```bash
docker pull ghcr.io/berriai/litellm:v1.90.2
docker run --rm ghcr.io/berriai/litellm:v1.90.2 --help 2>&1 | head -40
```

Expected: the help text lists a `--config` flag and a `--port` flag. Record the exact version you pulled; use that same tag in Task 8. If `v1.90.2` no longer exists, pick the newest stable tag, record it, and continue.

- [ ] **Step 2: Confirm the config keys against the image, not the docs**

```bash
docker run --rm ghcr.io/berriai/litellm:v1.90.2 \
  python -c "
import litellm, inspect, importlib.metadata as md
print('litellm', md.version('litellm'))
from litellm.proxy.proxy_server import ProxyConfig
print([m for m in dir(ProxyConfig) if not m.startswith('_')])
"
```

Record which of these the installed version accepts in `config.yaml`: `model_list[].model_name`, `model_list[].litellm_params.{model,api_base,api_key}`, `mcp_servers[].{server_name,url,transport,available_on_public_internet,allowed_tools}`, `general_settings.{master_key,store_model_in_db}`.

**If `allowed_tools` is not supported on `mcp_servers` in this version, stop and report.** The design leans on it as the second enforcement layer for the read-only guarantee. Do not silently drop it.

- [ ] **Step 3: Record the facts in the plan file**

Append a table to this task listing what you confirmed: image tag, config keys accepted, health endpoint path, default port. Later tasks read that table rather than re-deriving it.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-09-22-litellm-gateway.md
git commit -m "docs(plan): record the verified LiteLLM surface"
```

---

## Task 2: Stub upstream service

**Files:**
- Create: `src/incident_platform/devtools/__init__.py` (empty)
- Create: `src/incident_platform/devtools/stub_model.py`
- Test: `tests/test_stub_model.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stub_model.py`:

```python
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
    arguments = json.loads(payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stub_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'incident_platform.devtools'`.

- [ ] **Step 3: Write the implementation**

Create `src/incident_platform/devtools/__init__.py` as an empty file.

Create `src/incident_platform/devtools/stub_model.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_stub_model.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Lint**

```bash
uv run ruff check .
uv run ruff format --check .
```

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/devtools tests/test_stub_model.py
git commit -m "feat(devtools): add an OpenAI-compatible stub upstream"
```

---

## Task 3: Add the langchain-openai dependency

`init_chat_model("openai:<alias>")` needs `langchain-openai`, which is not installed. The gateway speaks the OpenAI protocol regardless of which provider serves a request, so this is a core dependency rather than an optional extra.

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Confirm it is genuinely absent**

```bash
uv run python -c "import langchain_openai" 2>&1 | tail -1
```

Expected: `ModuleNotFoundError: No module named 'langchain_openai'`.

- [ ] **Step 2: Add the dependency**

In `pyproject.toml`, change the `dependencies` list so the `langchain` line reads:

```toml
  "pydantic-settings>=2.8,<3", "langgraph>=1,<2", "langchain>=1,<2",
  "langchain-openai>=1,<2",
```

Leave the `anthropic` and `embeddings` extras alone. They remain valid for a direct-to-provider deployment.

- [ ] **Step 3: Resolve**

```bash
uv sync
uv run python -c "import langchain_openai, importlib.metadata as md; print(md.version('langchain-openai'))"
```

Expected: a version is printed. If resolution fails on an `openai` or `httpx` conflict with the existing `langfuse` and `mcp` pins, stop and report rather than forcing a resolution.

- [ ] **Step 4: Confirm nothing broke**

```bash
uv run pytest -q
```

Expected: the suite passes with the same counts as before this task.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add langchain-openai for the gateway protocol"
```

---

## Task 4: Gateway configuration settings

**Files:**
- Modify: `src/incident_platform/config.py`
- Test: `tests/test_config_gateway.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_gateway.py`:

```python
import pytest
from pydantic import ValidationError

from incident_platform.config import Settings

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}


def test_role_models_fall_back_to_llm_model():
    cfg = Settings(**BASE, llm_model="shared-alias")
    assert cfg.triage_model == "shared-alias"
    assert cfg.rca_model == "shared-alias"
    assert cfg.plan_model == "shared-alias"


def test_role_models_override_independently():
    cfg = Settings(**BASE, llm_model="shared", llm_model_rca="incident-rca")
    assert cfg.rca_model == "incident-rca"
    # The roles that were not overridden must keep the shared default.
    assert cfg.triage_model == "shared"
    assert cfg.plan_model == "shared"


def test_gateway_key_is_required_once_retrieval_is_enabled():
    # Retrieval is gateway-only, so an enabled feature with no gateway credential
    # would fail at the first tool call rather than at startup.
    with pytest.raises(ValidationError, match="litellm_api_key"):
        Settings(**BASE, knowledge_enabled=True, knowledge_mcp_token="knowledge")


def test_gateway_key_is_required_once_mock_mode_is_off():
    with pytest.raises(ValidationError, match="litellm_api_key"):
        Settings(**BASE, mock_mode=False)


def test_gateway_key_is_not_required_by_the_default_configuration():
    cfg = Settings(**BASE)
    assert cfg.knowledge_enabled is False
    assert cfg.mock_mode is True
    assert cfg.litellm_api_key.get_secret_value() == ""


def test_gateway_key_joins_the_distinct_credential_check():
    with pytest.raises(ValidationError, match="distinct"):
        Settings(**BASE, mock_mode=False, litellm_api_key="jira")


def test_knowledge_url_defaults_to_the_gateway():
    assert Settings(**BASE).knowledge_mcp_url == "http://litellm:4000/mcp"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_gateway.py -v`
Expected: FAIL. `test_role_models_fall_back_to_llm_model` errors with `AttributeError: 'Settings' object has no attribute 'triage_model'`.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/config.py`, change the `knowledge_mcp_url` default and add the new fields immediately after `llm_model`:

```python
    llm_model: str = "anthropic:claude-sonnet-4-5"
    # Gateway aliases, not provider model names. LiteLLM maps these to real models, so
    # changing which model serves a role is a gateway config change, not a redeploy.
    llm_model_triage: str | None = None
    llm_model_rca: str | None = None
    llm_model_plan: str | None = None
    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: SecretStr = SecretStr("")
```

and change:

```python
    knowledge_mcp_url: str = "http://litellm:4000/mcp"
```

Add the three resolving properties after the field declarations and before `validate_credentials`:

```python
    @property
    def triage_model(self) -> str:
        return self.llm_model_triage or self.llm_model

    @property
    def rca_model(self) -> str:
        return self.llm_model_rca or self.llm_model

    @property
    def plan_model(self) -> str:
        return self.llm_model_plan or self.llm_model
```

In `validate_credentials`, add the gateway requirement directly after the existing
`knowledge_enabled` block:

```python
        if self.knowledge_enabled or not self.mock_mode:
            # Both paths reach the gateway: retrieval is gateway-only, and a live analyst
            # calls models through it. Fail at startup rather than at the first call.
            if not self.litellm_api_key.get_secret_value():
                raise ValueError("litellm_api_key is required when the gateway is in use")
            secrets.append(self.litellm_api_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config_gateway.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Check the existing suite**

```bash
uv run pytest -q
```

Expected: `tests/test_config_knowledge.py` may assert the old `knowledge_mcp_url` default of `http://runbooks:8080/mcp`. If it fails on that, update that single assertion to the gateway URL — the change is intentional and this plan's Task 9 removes the direct path entirely. If it fails for any other reason, stop and report.

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/config.py tests/test_config_gateway.py tests/test_config_knowledge.py
git commit -m "feat(config): add gateway settings and per-role model aliases"
```

---

## Task 5: Per-role models in the analyst

**Files:**
- Modify: `src/incident_platform/integrations/llm.py`
- Test: `tests/test_llm_roles.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_roles.py`:

```python
from incident_platform.config import Settings
from incident_platform.integrations.llm import Analyst

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}

STATE = {"incident": {"severity": "high", "service": "checkout"}, "evidence": []}


def _live(**overrides):
    return Settings(**BASE, mock_mode=False, litellm_api_key="gateway", **overrides)


def test_builds_one_model_per_role(monkeypatch):
    built = []

    def fake(cfg, alias):
        built.append(alias)
        return object()

    monkeypatch.setattr("incident_platform.integrations.llm._model", fake)
    analyst = Analyst(
        _live(
            llm_model_triage="incident-triage",
            llm_model_rca="incident-rca",
            llm_model_plan="incident-plan",
        )
    )
    assert built == ["incident-triage", "incident-rca", "incident-plan"]
    assert set(analyst.models) == {"triage", "rca", "plan"}


def test_roles_share_one_alias_when_only_llm_model_is_set(monkeypatch):
    built = []
    monkeypatch.setattr(
        "incident_platform.integrations.llm._model",
        lambda cfg, alias: built.append(alias) or object(),
    )
    Analyst(_live(llm_model="shared-alias"))
    assert built == ["shared-alias", "shared-alias", "shared-alias"]


def test_mock_mode_builds_no_models(cfg):
    # Mock mode must not reach the gateway at all, so no client is constructed.
    assert Analyst(cfg).models == {}


async def test_mock_mode_still_returns_canned_triage(cfg):
    result = await Analyst(cfg).triage(STATE)
    assert result.severity == "high"
    assert result.investigate is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_roles.py -v`
Expected: FAIL. `test_builds_one_model_per_role` errors with `AttributeError: <module 'incident_platform.integrations.llm'> has no attribute '_model'`.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/integrations/llm.py`, add `_model` immediately after the `SYSTEM`
constant:

```python
def _model(cfg, alias: str):
    """Build one gateway-backed chat model.

    Isolated so tests can substitute it without a gateway, the same pattern the embedding
    provider uses. The alias is a gateway name, not a provider model, so the application
    never names a provider.
    """
    return init_chat_model(
        f"openai:{alias}",
        base_url=cfg.litellm_base_url,
        api_key=cfg.litellm_api_key.get_secret_value(),
        timeout=30,
        max_retries=2,
    )
```

Replace the `__init__` and `structured` methods:

```python
class Analyst:
    def __init__(self, cfg):
        self.cfg = cfg
        self.models = (
            {}
            if cfg.mock_mode
            else {
                "triage": _model(cfg, cfg.triage_model),
                "rca": _model(cfg, cfg.rca_model),
                "plan": _model(cfg, cfg.plan_model),
            }
        )

    async def structured(self, role, schema, task, state):
        prompt = ChatPromptTemplate.from_messages(
            [("system", SYSTEM), ("human", "Task: {task}\nIncident data: {data}")]
        )
        chain = prompt | self.models[role].with_structured_output(schema)
        return await chain.ainvoke({"task": task, "data": json.dumps(state)})
```

Then pass the role at each of the three call sites. In `triage`:

```python
        return await self.structured(
            "triage", Triage, "Assess severity and need for investigation", state
        )
```

In `investigate`:

```python
        return await self.structured(
            "rca",
            InvestigationStep,
            "Choose one read tool or conclude RCA. Tools: logs, metrics, kubernetes, deployments, "
            "config_diffs, topology, runbooks, similar_incidents. Retrieval is optional. "
            "Conclude only with corroborating evidence.",
            state,
        )
```

In `plan`:

```python
        return await self.structured(
            "plan",
            PlanDraft,
            "Propose observe, restart_workload, or rollback_deployment. "
            "Include rollback and verification. "
            "No shell commands. Use exact scoped service as target.",
            state,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_roles.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Confirm the graph tests are untouched**

```bash
uv run pytest -q
```

Expected: all pass. The graph runs in mock mode, where no model is constructed and each
method returns its canned result, so nothing in `tests/test_workflow.py` should move. If
something does, stop and report — it would mean the mock path reaches a model.

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/integrations/llm.py tests/test_llm_roles.py
git commit -m "feat(llm): select a model per role through the gateway"
```

---

## Task 6: MCP tool namespacing

LiteLLM namespaces tools across servers as `<server_name>-<tool_name>`. The adapter calls
tools by exact name, so those names must change.

**Files:**
- Modify: `src/incident_platform/integrations/knowledge_backend.py`
- Test: `tests/test_knowledge_backend.py` (modify existing assertions)

- [ ] **Step 1: Update the failing assertions**

In `tests/test_knowledge_backend.py`, change `test_maps_snippets_to_one_evidence` so the
expected call name carries the gateway prefix:

```python
async def test_maps_snippets_to_one_evidence(cfg):
    client = FakeClient([SNIPPET])
    evidence = await backend(client, cfg).query("runbooks", "checkout", "rollback")
    assert client.calls == [
        (
            "runbooks-search_runbooks",
            {"service": "checkout", "query": "rollback", "limit": cfg.knowledge_top_k},
        )
    ]
    assert evidence.tool == "runbooks"
    assert evidence.trusted is True
    assert "Roll the deployment back" in evidence.observation
    assert "docs/runbooks/checkout-rollback.md" in evidence.observation
```

and `test_similar_incidents_uses_its_own_tool`:

```python
async def test_similar_incidents_uses_its_own_tool(cfg):
    client = FakeClient([SNIPPET])
    await backend(client, cfg).query("similar_incidents", "checkout", "timeouts")
    assert client.calls[0][0] == "runbooks-search_similar_incidents"
```

Add one test pinning the naming rule itself:

```python
def test_tool_names_carry_the_gateway_namespace():
    # LiteLLM prefixes every tool with its server name. An unprefixed name reaches the
    # gateway as an unknown tool, which is an error, not an empty result.
    from incident_platform.integrations.knowledge_backend import MCP_SERVER, TOOL_CALLS

    assert all(name.startswith(f"{MCP_SERVER}-") for name in TOOL_CALLS.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_backend.py -v`
Expected: FAIL. Two tests fail on the call name (`'search_runbooks' != 'runbooks-search_runbooks'`) and the new test fails on `ImportError: cannot import name 'MCP_SERVER'`.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/integrations/knowledge_backend.py`, replace the `TOOL_CALLS`
constant:

```python
# The MCP server's name in the gateway config. LiteLLM namespaces tools across servers as
# <server_name>-<tool_name>, so this prefix is part of the call, not decoration.
MCP_SERVER = "runbooks"
TOOL_CALLS = {
    "runbooks": f"{MCP_SERVER}-search_runbooks",
    "similar_incidents": f"{MCP_SERVER}-search_similar_incidents",
}
```

Nothing else in the file changes. The keys are still the analyst's tool vocabulary; only the
values, which are what the gateway is asked for, gain the prefix.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_backend.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add src/incident_platform/integrations/knowledge_backend.py tests/test_knowledge_backend.py
git commit -m "feat(knowledge): namespace tool calls for the gateway"
```

---

## Task 7: Gateway headers on the MCP client

**Files:**
- Modify: `src/incident_platform/knowledge/__init__.py`
- Test: `tests/test_knowledge_headers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_knowledge_headers.py`:

```python
from incident_platform.config import Settings
from incident_platform.knowledge import gateway_headers

BASE = {
    "_env_file": None,
    "jira_webhook_token": "jira",
    "jira_approval_token": "approve",
    "status_token": "status",
    "slack_signing_secret": "slack",
    "siem_signing_secret": "siem",
}


def _cfg():
    return Settings(
        **BASE,
        knowledge_enabled=True,
        knowledge_mcp_token="runbooks-token",
        litellm_api_key="gateway-key",
    )


def test_authenticates_the_caller_to_the_gateway():
    assert gateway_headers(_cfg())["x-litellm-api-key"] == "Bearer gateway-key"


def test_forwards_the_servers_own_bearer_token():
    # The gateway fronts the runbooks server's auth boundary; it does not replace it.
    # Without this header the server rejects the proxied call with 401.
    assert gateway_headers(_cfg())["x-mcp-runbooks-authorization"] == "Bearer runbooks-token"


def test_sends_no_plain_authorization_header():
    # A plain Authorization header is what the direct path used. Leaving it in place would
    # authenticate against the wrong boundary and mask a misconfigured gateway key.
    assert "Authorization" not in gateway_headers(_cfg())


def test_header_name_tracks_the_server_constant():
    from incident_platform.integrations.knowledge_backend import MCP_SERVER

    assert f"x-mcp-{MCP_SERVER}-authorization" in gateway_headers(_cfg())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_knowledge_headers.py -v`
Expected: FAIL with `ImportError: cannot import name 'gateway_headers' from 'incident_platform.knowledge'`.

- [ ] **Step 3: Write the implementation**

In `src/incident_platform/knowledge/__init__.py`, add the header builder above `mcp_client`
and use it:

```python
from contextlib import asynccontextmanager

from incident_platform.integrations.knowledge_backend import MCP_SERVER


def gateway_headers(cfg) -> dict[str, str]:
    """Headers for one MCP call through LiteLLM.

    The gateway authenticates the caller with x-litellm-api-key and forwards the runbooks
    server's own bearer token under x-mcp-<server>-authorization, so that server still
    authenticates the call itself. Two boundaries, two credentials.
    """
    return {
        "x-litellm-api-key": f"Bearer {cfg.litellm_api_key.get_secret_value()}",
        f"x-mcp-{MCP_SERVER}-authorization": (
            f"Bearer {cfg.knowledge_mcp_token.get_secret_value()}"
        ),
    }


@asynccontextmanager
async def mcp_client(cfg):
    """One long-lived MCP client for the worker's lifetime, not one per tool call."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers=gateway_headers(cfg), timeout=20.0) as http:
        async with streamable_http_client(cfg.knowledge_mcp_url, http_client=http) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                yield _Session(session)
```

Leave `_Session` exactly as it is. Its `structured_content` handling and its `is_error` raise
are unchanged by this work.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_knowledge_headers.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Confirm no import cycle**

```bash
uv run python -c "import incident_platform.knowledge.server; print('import ok')"
uv run pytest -q
```

Expected: `import ok`, then the full suite passes. `knowledge/__init__.py` now imports from
`integrations.knowledge_backend`, which imports only `models`. If you see a circular import,
stop and report rather than moving the constant.

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/knowledge/__init__.py tests/test_knowledge_headers.py
git commit -m "feat(knowledge): send gateway headers on the MCP client"
```

---

## Task 8: LiteLLM configuration file

**Files:**
- Create: `deploy/litellm/config.yaml`

- [ ] **Step 1: Write the config**

Create `deploy/litellm/config.yaml`. Use the key names you confirmed in Task 1, not these,
if the installed version disagrees:

```yaml
# Aliases, not provider models. The application asks for incident-triage; this file decides
# what serves it. Changing a model here needs no application redeploy.
#
# All three point at the development stub so the platform can be exercised with no provider
# account. Replace each api_base/model pair with a real provider to go live.
model_list:
  - model_name: incident-triage
    litellm_params:
      model: openai/stub-triage
      api_base: http://stub-model:8100/v1
      api_key: stub
  - model_name: incident-rca
    litellm_params:
      model: openai/stub-rca
      api_base: http://stub-model:8100/v1
      api_key: stub
  - model_name: incident-plan
    litellm_params:
      model: openai/stub-plan
      api_base: http://stub-model:8100/v1
      api_key: stub

mcp_servers:
  - server_name: runbooks
    url: http://runbooks:8080/mcp
    transport: http
    available_on_public_internet: false
    # Second enforcement layer for the read-only guarantee. The server registers only these
    # two tools; this makes that independent of the server's own registration, and LiteLLM
    # applies it at both tools/list and tools/call.
    allowed_tools:
      - search_runbooks
      - search_similar_incidents

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: true
```

Each alias maps to a **different** upstream model name (`stub-triage`, `stub-rca`,
`stub-plan`). That is what makes per-role routing observable in Task 10 — the stub records
the name it was called under.

- [ ] **Step 2: Validate the YAML parses**

```bash
uv run python -c "
import yaml, pathlib
config = yaml.safe_load(pathlib.Path('deploy/litellm/config.yaml').read_text())
assert [m['model_name'] for m in config['model_list']] == [
    'incident-triage', 'incident-rca', 'incident-plan']
assert config['mcp_servers'][0]['allowed_tools'] == [
    'search_runbooks', 'search_similar_incidents']
print('config valid')
"
```

Expected: `config valid`. If `yaml` is not importable, run it with `uv run --with pyyaml`.

- [ ] **Step 3: Commit**

```bash
git add deploy/litellm/config.yaml
git commit -m "feat(gateway): declare model aliases and the runbooks MCP server"
```

---

## Task 9: Compose services

**Files:**
- Modify: `compose.yaml`
- Modify: `.env.example`

- [ ] **Step 1: Add the AI platform tier**

In `compose.yaml`, add these services after `runbooks` and before `worker`. Use the image tag
recorded in Task 1:

```yaml
  stub-model:
    <<: *app
    command:
      ["uvicorn", "incident_platform.devtools.stub_model:app",
       "--host", "0.0.0.0", "--port", "8100"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8100/health')"]
      interval: 10s
      timeout: 5s
      retries: 6
  litellm-postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: litellm
      POSTGRES_PASSWORD: litellm
      POSTGRES_DB: litellm
    volumes: ["litellmdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U litellm -d litellm"]
      interval: 3s
      timeout: 3s
      retries: 30
  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 3s
      timeout: 3s
      retries: 30
  litellm:
    image: ghcr.io/berriai/litellm:v1.90.2
    command: ["--config", "/app/proxy_server_config.yaml", "--port", "4000"]
    volumes: ["./deploy/litellm/config.yaml:/app/proxy_server_config.yaml:ro"]
    environment:
      LITELLM_MASTER_KEY: ${LITELLM_API_KEY:-dev-litellm}
      LITELLM_SALT_KEY: ${LITELLM_SALT_KEY:-dev-litellm-salt}
      DATABASE_URL: postgresql://litellm:litellm@litellm-postgres:5432/litellm
      REDIS_HOST: redis
      REDIS_PORT: "6379"
    depends_on:
      litellm-postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
      runbooks: {condition: service_healthy}
      stub-model: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/readiness')"]
      interval: 10s
      timeout: 5s
      retries: 12
    restart: unless-stopped
```

Note `litellm` does not use the `*app` anchor: it is not built from this repository's image.
It therefore needs its own `restart` key.

- [ ] **Step 2: Point the worker at the gateway**

Replace the `worker` service's `depends_on` block:

```yaml
  worker:
    <<: *app
    command: ["python", "-m", "incident_platform.worker.main"]
    depends_on:
      init: {condition: service_completed_successfully}
      litellm: {condition: service_healthy}
```

The `runbooks` dependency is removed deliberately: the worker no longer talks to it, and
`litellm` already waits for it to be healthy.

- [ ] **Step 3: Add the volume**

In the `volumes:` block at the bottom of the file, add `litellmdata:` alongside `pgdata:`
and `kafkadata:`.

- [ ] **Step 4: Document the settings**

Replace the knowledge block at the end of `.env.example` with:

```sh
# Runbook knowledge retrieval, served through the gateway. Off by default.
KNOWLEDGE_ENABLED=false
KNOWLEDGE_MCP_URL=http://litellm:4000/mcp
KNOWLEDGE_MCP_TOKEN=dev-knowledge
KNOWLEDGE_TOP_K=5
# Gateway. Required whenever KNOWLEDGE_ENABLED is true or MOCK_MODE is false.
LITELLM_BASE_URL=http://litellm:4000
LITELLM_API_KEY=dev-litellm
LITELLM_SALT_KEY=dev-litellm-salt
# Role aliases resolved by the gateway. Each falls back to LLM_MODEL when unset.
LLM_MODEL_TRIAGE=incident-triage
LLM_MODEL_RCA=incident-rca
LLM_MODEL_PLAN=incident-plan
# Optional dense retrieval. Lexical search works with no key at all.
# EMBEDDING_MODEL=voyage-3
# EMBEDDING_DIM=1024
# VOYAGE_API_KEY=
```

- [ ] **Step 5: Validate and start**

```bash
docker compose config --quiet && echo "compose valid"
docker compose down
docker compose up --build -d
docker compose ps
```

Expected: `compose valid`, then every service reaches healthy, including `litellm`.

If `litellm` is unhealthy, read its logs before changing anything:

```bash
docker compose logs litellm | tail -40
```

A missing `LITELLM_SALT_KEY` or an unsupported config key are the likely causes. If the
config key names differ from Task 1's recorded table, stop and report.

- [ ] **Step 6: Commit**

```bash
git add compose.yaml .env.example
git commit -m "feat(gateway): run LiteLLM with its own Postgres and Redis"
```

---

## Task 10: End-to-end verification

**Files:**
- Create: `src/incident_platform/devtools/verify_gateway.py`

This runs inside the worker container so that neither `litellm` nor `stub-model` needs a
published host port, matching the posture `runbooks` already has.

- [ ] **Step 1: Write the verifier**

Create `src/incident_platform/devtools/verify_gateway.py`:

```python
"""Prove both gateway paths end to end. Development-only; run inside the worker container.

    docker compose exec -T worker python -m incident_platform.devtools.verify_gateway

Exits non-zero on the first failed assertion.
"""

import asyncio
import json
import sys
import urllib.request

from incident_platform.config import Settings
from incident_platform.integrations.knowledge_backend import KnowledgeEvidenceBackend
from incident_platform.integrations.llm import Analyst
from incident_platform.integrations.tools import MockEvidenceBackend
from incident_platform.knowledge import mcp_client

STUB = "http://stub-model:8100/_calls"
STATE = {
    "incident": {"service": "checkout", "severity": "high", "summary": "Checkout timing out"},
    "evidence": [],
    "rounds": 0,
}


def _recorded() -> list[str]:
    with urllib.request.urlopen(STUB) as response:
        return json.load(response)


def _reset() -> None:
    request = urllib.request.Request(STUB, method="DELETE")
    urllib.request.urlopen(request).read()


def _live_settings() -> Settings:
    """A non-mock configuration built in-process.

    mock_mode stays true for the running worker. This object exists only to construct an
    analyst that talks to the gateway, so the deployment's fail-closed coupling between
    simulated evidence and simulated execution is never disturbed.
    """
    base = Settings()
    return Settings(
        _env_file=None,
        mock_mode=False,
        jira_webhook_token="verify-jira",
        jira_approval_token="verify-approve",
        status_token="verify-status",
        slack_signing_secret="verify-slack",
        siem_signing_secret="verify-siem",
        litellm_base_url=base.litellm_base_url,
        litellm_api_key=base.litellm_api_key.get_secret_value(),
        llm_model_triage=base.triage_model,
        llm_model_rca=base.rca_model,
        llm_model_plan=base.plan_model,
    )


async def verify_roles() -> None:
    _reset()
    analyst = Analyst(_live_settings())
    await analyst.triage(STATE)
    await analyst.investigate(STATE)
    await analyst.plan(STATE)
    recorded = _recorded()
    assert len(recorded) == 3, f"expected three upstream calls, got {recorded}"
    assert len(set(recorded)) == 3, f"roles did not route to distinct models: {recorded}"
    print(f"roles routed to distinct upstreams: {recorded}")


async def verify_retrieval() -> None:
    cfg = Settings()
    assert cfg.knowledge_enabled, "set KNOWLEDGE_ENABLED=true before running this"
    async with mcp_client(cfg) as client:
        backend = KnowledgeEvidenceBackend(MockEvidenceBackend(), client, cfg)
        evidence = await backend.query("runbooks", "checkout", "rollback error rate")
        documents = evidence.observation.count("BEGIN DOCUMENT")
        assert documents == 5, f"expected five documents through the gateway, got {documents}"
        assert evidence.trusted is True
        delegated = await backend.query("metrics", "checkout", "error rate")
        assert delegated.tool == "metrics", "a non-retrieval tool must not reach the gateway"
    print(f"retrieval through the gateway returned {documents} trusted documents")


async def main() -> None:
    await verify_roles()
    await verify_retrieval()
    print("gateway verification passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        raise SystemExit(1) from failure
```

- [ ] **Step 2: Enable retrieval and restart the worker**

```bash
sed -i '' 's/^KNOWLEDGE_ENABLED=false/KNOWLEDGE_ENABLED=true/' .env
grep -E 'KNOWLEDGE|LITELLM|LLM_MODEL' .env
docker compose up --build -d --force-recreate worker
```

If your `.env` predates this feature it will lack the `LITELLM_*` and `LLM_MODEL_*` lines.
Copy them from `.env.example` before restarting; the worker fails to start without
`LITELLM_API_KEY` once retrieval is enabled.

- [ ] **Step 3: Run the verifier**

```bash
docker compose exec -T worker python -m incident_platform.devtools.verify_gateway
```

Expected, all three lines:

```
roles routed to distinct upstreams: ['stub-triage', 'stub-rca', 'stub-plan']
retrieval through the gateway returned 5 trusted documents
gateway verification passed
```

If the role assertion fails with three identical names, the aliases in
`deploy/litellm/config.yaml` are pointing at the same upstream model. If retrieval fails with
an unknown-tool error, the `runbooks-` prefix from Task 6 does not match the `server_name` in
the gateway config.

- [ ] **Step 4: Settle the rejected-key question**

The design lists this as unverified: the adapter raises `PermissionError` only when the
error message contains `401` or `unauthorized`. Whether a LiteLLM rejection looks like that
is unknown. Find out deliberately rather than discovering it during an incident.

```bash
docker compose exec -T -e LITELLM_API_KEY=wrong-key worker python -c "
import asyncio
from incident_platform.config import Settings
from incident_platform.knowledge import mcp_client

async def main():
    try:
        async with mcp_client(Settings()) as client:
            await client.call_tool('runbooks-search_runbooks', {'service':'checkout','query':'x'})
        print('UNEXPECTED: a wrong key was accepted')
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}')

asyncio.run(main())
"
```

Record the exception type and message.

- If it is `PermissionError`, the existing check already recognises it. Note that and move on.
- If it is any other type, widen the check in
  `src/incident_platform/integrations/knowledge_backend.py` so a rejected gateway key is not
  retried. Add the matching substring to the existing condition:

```python
            if "401" in str(exc) or "unauthorized" in str(exc).lower():
```

  and add a test to `tests/test_knowledge_backend.py` pinning the real message:

```python
async def test_a_rejected_gateway_key_is_not_retryable(cfg):
    # The literal message LiteLLM returns, recorded in Task 10 Step 4. Retrying a bad
    # gateway key burns the node's retry budget and hides the misconfiguration.
    client = FakeClient(error=RuntimeError("<paste the recorded message here>"))
    with pytest.raises(PermissionError):
        await backend(client, cfg).query("runbooks", "checkout", "rollback")
```

**If the wrong key is accepted, stop and report.** That would mean the gateway is not
authenticating callers at all, which is a security finding, not a configuration detail.

- [ ] **Step 5: Confirm the demo is unchanged**

```bash
uv run python scripts/demo.py | tail -8
```

Expected: `"status": "resolved"`. The worker still runs in mock mode, so this proves the
gateway wiring is inert until something asks for it.

- [ ] **Step 6: Commit**

```bash
git add src/incident_platform/devtools/verify_gateway.py \
       src/incident_platform/integrations/knowledge_backend.py tests/test_knowledge_backend.py
git commit -m "feat(devtools): verify both gateway paths end to end"
```

---

## Task 11: Regression gate

**Files:** none modified.

- [ ] **Step 1: Run the full verification sequence**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test uv run pytest -m integration -q
```

Expected: all four pass. Unit tests should be 67 plus the tests added by Tasks 2, 4, 5, 6 and
7. Integration counts are unchanged; this work adds no integration tests.

- [ ] **Step 2: Confirm the default configuration still starts**

```bash
uv run python -c "
from incident_platform.config import Settings
cfg = Settings(_env_file=None, jira_webhook_token='a', jira_approval_token='b',
               status_token='c', slack_signing_secret='d', siem_signing_secret='e')
assert cfg.knowledge_enabled is False and cfg.mock_mode is True
assert cfg.litellm_api_key.get_secret_value() == ''
print('default configuration unchanged')
"
```

Expected: `default configuration unchanged`. A default deployment must still need no gateway
credential.

- [ ] **Step 3: Confirm the tree is clean**

```bash
git status --short
```

Expected: clean.

---

## Task 12: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update the README**

In the "LLMs, tools and Langfuse" section, after the sentence ending
"...cannot satisfy the RCA corroboration rules.", add:

```markdown
Model calls and MCP tool calls both leave the incident cluster through a LiteLLM gateway.
The application names roles, not providers: `LLM_MODEL_TRIAGE`, `LLM_MODEL_RCA` and
`LLM_MODEL_PLAN` hold gateway aliases that LiteLLM maps to real models, so changing which
model serves a role is a gateway config change rather than an application redeploy. Each
falls back to `LLM_MODEL` when unset. Retrieval is gateway-only: the worker has no direct
route to the `runbooks` service, and `allowed_tools` in the gateway config pins the exposed
tool set independently of what the server registers.
```

Add to the "Layout" block after the `knowledge/` line:

```text
  devtools/          development-only stub upstream and gateway verifier
```

Add to the "Deliberate boundaries" list:

```markdown
- The development stub upstream returns schema-shaped placeholder values, not reasoning. It
  proves routing and wiring, never model quality. Replace the aliases in
  `deploy/litellm/config.yaml` with real providers before drawing any conclusion about
  analyst behavior.
```

- [ ] **Step 2: Update operations documentation**

Append to `docs/operations.md`:

```markdown
## Gateway

LiteLLM serves both `/v1/chat/completions` and `/mcp` for this platform. It owns model
selection, tool entitlements and usage accounting; the incident cluster holds only aliases
and a gateway key. It runs against its own PostgreSQL instance rather than the incident
database, so neither side's migrations touch the other, and Redis carries shared rate limits.

Two credentials are in play on every retrieval call. `x-litellm-api-key` authenticates the
worker to the gateway. `x-mcp-runbooks-authorization` carries the runbooks server's own
bearer token, which the gateway forwards; that server still authenticates the call itself.
The gateway fronts the boundary rather than replacing it, so a leaked gateway key alone does
not grant access to the retrieval corpus.

Tool names are namespaced `<server_name>-<tool_name>`. Renaming the server in
`deploy/litellm/config.yaml` without changing `MCP_SERVER` in `knowledge_backend.py` makes
every retrieval call fail as an unknown tool.

The worker depends on the gateway being healthy, and the gateway depends on its database.
A gateway outage therefore stops the worker from starting, where previously only retrieval
was affected. That is the cost of the gateway-only decision, and it is deliberate: there is
no direct fallback path to misconfigure or forget to secure.
```

- [ ] **Step 3: Update AGENTS.md invariants**

Append to the invariants list in `AGENTS.md`:

```markdown
- The application names model roles, never providers. Provider selection belongs to the
  gateway config.
- The stub upstream is development-only. Never point a deployment's aliases at it, and never
  treat its output as model behavior.
```

- [ ] **Step 4: Final verification**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test uv run pytest -m integration -q
docker compose down && docker compose up --build -d && docker compose ps
docker compose exec -T worker python -m incident_platform.devtools.verify_gateway
uv run python scripts/demo.py | tail -8
```

Expected: lint clean, both suites pass, all services healthy, the verifier prints its three
lines, and the demo reaches `resolved`.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/operations.md AGENTS.md
git commit -m "docs(gateway): document the gateway boundaries and operations"
```

---

## Done

Report at the end:

- The LiteLLM image tag pinned in Task 1 and any config keys that differed from this plan.
- Whether `uv sync` in Task 3 resolved cleanly or hit an `openai`/`httpx` conflict.
- Whether `allowed_tools` was supported on `mcp_servers` in the installed version.
- What exception a rejected gateway key produced, and whether the adapter's 401 check
  needed widening.
- The upstream model names the verifier recorded, quoted, proving per-role routing.
- The final output of the four verification commands, quoted, not summarized.
