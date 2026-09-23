# Self-hosted Langfuse Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trace every incident's graph and model behaviour into a self-hosted Langfuse, with a trace id any component can recompute from data it already holds.

**Architecture:** The trace id is a pure function of `(incident_id, event_id)` via `Langfuse.create_trace_id(seed=...)`, so nothing is stored and nothing is plumbed through the graph. The worker binds the LangChain callback handler to that id, and forwards the same id to LiteLLM as request metadata so gateway-reported cost lands in the same trace. Langfuse runs in an opt-in Compose profile, which is what makes tracing unredacted evidence defensible.

**Tech Stack:** Python 3.12, langfuse 3.15.0 SDK, langchain 1.4.2 / langchain-openai 1.6.4, LangGraph 1.2.11, LiteLLM proxy v1.90.2 (container), Docker Compose profiles, pytest, structlog.

**Spec:** `docs/superpowers/specs/2026-09-23-langfuse-observability-design.md`

---

## Facts already verified against the pinned versions

Do not re-litigate these; they were checked against this repo's `.venv` before the plan was written.

- `Langfuse.create_trace_id(seed=...)` is a **staticmethod**, needs no client, returns a 32-char hex string, and is deterministic for a given seed.
- `Langfuse(...)` accepts `mask` as a **constructor-only** parameter. It does **not** raise when credentials are absent — it logs `Authentication error` and disables itself.
- `Langfuse.auth_check() -> bool` exists.
- If a mask function raises, the SDK substitutes `"<fully masked due to failed mask function>"` and logs. Masks fail closed.
- `langfuse.langchain.CallbackHandler.__init__` accepts `trace_context: Optional[TraceContext]` and `update_trace: bool = False`. `TraceContext` is `{"trace_id": str, "parent_span_id": NotRequired[str]}`.
- The handler applies `langfuse_session_id` / `langfuse_user_id` / `langfuse_tags` from config metadata at the root run **independently** of the `update_trace` flag.
- `ChatOpenAI.bind(extra_body=...)` returns a `_ChatModelBinding` that **does** expose `with_structured_output`, and the bound value reaches the request payload as `extra_body` (`payload = {**self._default_params, **kwargs}`). Correct order: `model.bind(...).with_structured_output(schema)`.

## Before every commit

Ruff enforces `E`, `F`, `I`, `B`, `UP` at line length 100. Run `uv run ruff format .` and
`uv run ruff check . --fix` before each commit step, and keep imports at the top of the file
in sorted order — `E402` and isort are both enabled, so a mid-file import fails the check.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/incident_platform/telemetry.py` | Logging setup, trace id derivation, mask construction, client init, graph config. Grows 42 → ~110 lines; purpose unchanged. | 1–4 |
| `tests/test_telemetry.py` | New. Covers all of the above with no Langfuse server. | 1–4 |
| `src/incident_platform/integrations/llm.py` | Adds per-call trace metadata binding. Public signatures unchanged. | 5 |
| `tests/test_llm_roles.py` | Adds binding cases. | 5 |
| `src/incident_platform/worker/main.py` | Calls `init_tracing`; passes `event_id` to `graph_config`. | 6 |
| `tests/test_workflow.py` | Adds one case asserting the delivered event id reaches `graph_config`. | 6 |
| `compose.yaml`, `.env.example` | `observability` profile and its variables. | 7 |
| `deploy/litellm/config.yaml` | Gateway Langfuse callback. | 8 |
| `AGENTS.md`, `README.md`, `docs/operations.md` | Correct statements this work makes false. | 9 |
| `docs/verification.md` | Record of the manual end-to-end run. | 10 |

`src/incident_platform/integrations/evaluation.py` is **not** edited. It begins working as a side effect of Task 3.

---

### Task 1: Derive the trace id

**Files:**
- Modify: `src/incident_platform/telemetry.py`
- Test: `tests/test_telemetry.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_telemetry.py`:

```python
from incident_platform.telemetry import trace_id_for


def test_trace_id_is_stable_for_the_same_event():
    # Recomputable after restarts, replays and approval resumes, so nothing is stored.
    assert trace_id_for("inc-1", "evt-1") == trace_id_for("inc-1", "evt-1")


def test_trace_id_differs_per_event():
    assert trace_id_for("inc-1", "evt-1") != trace_id_for("inc-1", "evt-2")


def test_trace_id_differs_per_incident():
    assert trace_id_for("inc-1", "evt-1") != trace_id_for("inc-2", "evt-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: FAIL — `ImportError: cannot import name 'trace_id_for' from 'incident_platform.telemetry'`

- [ ] **Step 3: Write minimal implementation**

In `src/incident_platform/telemetry.py`, add the import at the top (after `import structlog`):

```python
from langfuse import Langfuse
```

and add the function after `configure_logging`:

```python
def trace_id_for(incident_id: str, event_id: str) -> str:
    """Derive one delivered event's Langfuse trace id.

    A pure function of values every component already carries, so no mapping is stored.
    A retry of the same event therefore reuses its trace rather than creating a near
    duplicate, which is what keeps later score attachment idempotent.
    """
    return Langfuse.create_trace_id(seed=f"{incident_id}:{event_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_telemetry.py src/incident_platform/telemetry.py
git commit -m "$(cat <<'MSG'
feat(telemetry): derive trace ids from incident and event id

Makes a trace addressable without storing a mapping, which is what the
evaluation hooks need to become reachable.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 2: Build the credential mask

The mask redacts **known secret values only**. It must not grow entropy scoring or PII regexes: those false-positive on log lines, stack traces and ticket text, and no test can pin their behaviour. Evidence bodies pass through verbatim — that is what self-hosting bought.

**Files:**
- Modify: `src/incident_platform/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Add `import json` and extend the existing telemetry import at the **top** of
`tests/test_telemetry.py` so it reads:

```python
import json

from incident_platform.telemetry import build_mask, trace_id_for
```

Ruff runs `E` and `I`, so an import appended mid-file fails `ruff check`. Then append the
tests themselves:

```python


def test_mask_redacts_every_configured_secret(cfg):
    mask = build_mask(cfg)
    data = "jira and approve and status and slack and siem"
    masked = mask(data=data)
    for secret in ("jira", "approve", "status", "slack", "siem"):
        assert secret not in masked


def test_mask_walks_nested_structures(cfg):
    mask = build_mask(cfg)
    masked = mask(data={"outer": [{"inner": "token is jira"}, ("tuple", "slack")]})
    assert "jira" not in json.dumps(masked)
    assert "slack" not in json.dumps(masked)


def test_mask_redacts_bearer_headers(cfg):
    masked = build_mask(cfg)(data="Authorization: Bearer sk-abc123xyz")
    assert "sk-abc123xyz" not in masked


def test_mask_leaves_evidence_bodies_byte_identical(cfg):
    # Regression guard: full-fidelity tracing is a deliberate decision, not an oversight.
    body = "ERROR checkout p95 latency 2400ms after release v2 at 14:02 UTC"
    assert build_mask(cfg)(data=body) == body


def test_mask_ignores_unset_secrets(cfg):
    # An empty credential must never be treated as a secret: "" matches at every
    # position, which would destroy the payload instead of redacting it.
    assert cfg.litellm_api_key.get_secret_value() == ""
    assert build_mask(cfg)(data="nothing secret here") == "nothing secret here"


def test_mask_output_is_json_serialisable(cfg):
    masked = build_mask(cfg)(data={"a": ("x", "y"), "b": [1, 2.5, None, True]})
    json.dumps(masked)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_mask'`

- [ ] **Step 3: Write minimal implementation**

In `src/incident_platform/telemetry.py`, add `import re` to the imports and this after `trace_id_for`:

```python
REDACTED = "[redacted]"
BEARER = re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE)


def build_mask(cfg):
    """Redact known credentials from everything sent to the trace store.

    Exact-value redaction only. Evidence, ticket and runbook bodies are traced verbatim;
    heuristics would false-positive on log and stack-trace text and cannot be pinned by a
    test. Empty credentials are dropped because "" matches everywhere.
    """
    secrets = sorted(
        (
            value
            for value in (
                cfg.jira_webhook_token.get_secret_value(),
                cfg.jira_approval_token.get_secret_value(),
                cfg.status_token.get_secret_value(),
                cfg.slack_signing_secret.get_secret_value(),
                cfg.siem_signing_secret.get_secret_value(),
                cfg.litellm_api_key.get_secret_value(),
                cfg.knowledge_mcp_token.get_secret_value(),
            )
            if value
        ),
        key=len,
        reverse=True,
    )

    def redact(text):
        # Longest first, so a secret that contains another is not partially rewritten.
        for secret in secrets:
            text = text.replace(secret, REDACTED)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_telemetry.py src/incident_platform/telemetry.py
git commit -m "$(cat <<'MSG'
feat(telemetry): redact configured credentials from traces

Exact-value redaction only. Evidence bodies stay verbatim by design, and a
test pins that so it is not later "improved" into heuristic redaction.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 3: Initialise the Langfuse client with the mask

`mask` is constructor-only, so a client created implicitly by `get_client()` can never redact. This task is what makes the bare `get_client()` calls in `integrations/evaluation.py` safe.

**Files:**
- Modify: `src/incident_platform/telemetry.py`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Add `import pytest` and extend the telemetry import at the **top** of the file so it reads:

```python
import json

import pytest

from incident_platform.telemetry import build_mask, init_tracing, trace_id_for
```

Then append:

```python
class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.checked = False

    def auth_check(self):
        self.checked = True
        return True


def test_init_tracing_returns_none_when_disabled(cfg, monkeypatch):
    monkeypatch.setattr(
        "incident_platform.telemetry.Langfuse",
        lambda **kwargs: pytest.fail("client must not be constructed when disabled"),
    )
    assert init_tracing(cfg) is None


def test_init_tracing_passes_a_mask(cfg, monkeypatch):
    built = []
    monkeypatch.setattr(
        "incident_platform.telemetry.Langfuse",
        lambda **kwargs: built.append(FakeClient(**kwargs)) or built[-1],
    )
    client = init_tracing(cfg.model_copy(update={"langfuse_enabled": True}))
    assert callable(client.kwargs["mask"])
    assert client.checked is True


def test_init_tracing_survives_a_failing_auth_check(cfg, monkeypatch):
    class Unreachable(FakeClient):
        def auth_check(self):
            raise ConnectionError("collector unreachable")

    monkeypatch.setattr("incident_platform.telemetry.Langfuse", Unreachable)
    # Tracing is never a reason the worker fails to start.
    assert init_tracing(cfg.model_copy(update={"langfuse_enabled": True})) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: FAIL — `ImportError: cannot import name 'init_tracing'`

- [ ] **Step 3: Write minimal implementation**

In `src/incident_platform/telemetry.py`, add a module logger after the imports:

```python
log = structlog.get_logger()
```

and add after `build_mask`:

```python
def init_tracing(cfg):
    """Construct the one Langfuse client, with masking configured.

    Returns None when tracing is disabled. The SDK disables itself rather than raising
    when credentials are absent, so a mistyped key degrades export instead of refusing
    startup: core correctness must not depend on trace delivery.
    """
    if not cfg.langfuse_enabled:
        return None
    client = Langfuse(mask=build_mask(cfg))
    try:
        if not client.auth_check():
            log.warning("langfuse.unauthenticated")
    except Exception as exc:
        log.warning("langfuse.auth_check_failed", error_type=type(exc).__name__)
    return client
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_telemetry.py src/incident_platform/telemetry.py
git commit -m "$(cat <<'MSG'
feat(telemetry): initialise the Langfuse client with masking

mask is constructor-only, so an implicitly created client can never redact.
Auth failures warn rather than refusing startup.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 4: Bind the graph config to the trace

**Files:**
- Modify: `src/incident_platform/telemetry.py:26-42`
- Test: `tests/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

Extend the telemetry import at the **top** of the file so it reads:

```python
from incident_platform.telemetry import build_mask, graph_config, init_tracing, trace_id_for
```

Then append:

```python
def test_graph_config_has_no_callbacks_when_disabled():
    config = graph_config("inc-1", "corr-1", "evt-1", False)
    assert config["callbacks"] == []
    assert config["configurable"]["thread_id"] == "inc-1"
    assert config["metadata"]["langfuse_session_id"] == "inc-1"
    assert config["metadata"]["trace_id"] == trace_id_for("inc-1", "evt-1")


def test_graph_config_binds_the_handler_to_the_derived_trace(monkeypatch):
    built = {}

    class FakeHandler:
        def __init__(self, *, trace_context, update_trace):
            built["trace_context"] = trace_context
            built["update_trace"] = update_trace

    monkeypatch.setattr("incident_platform.telemetry.CallbackHandler", FakeHandler)
    config = graph_config("inc-1", "corr-1", "evt-1", True)
    assert len(config["callbacks"]) == 1
    assert built["trace_context"] == {"trace_id": trace_id_for("inc-1", "evt-1")}
    # Promotes graph input/output to trace level, which trace-based dataset runs require.
    assert built["update_trace"] is True


def test_graph_config_degrades_when_the_handler_cannot_be_built(monkeypatch):
    def explode(**_):
        raise RuntimeError("langfuse unavailable")

    monkeypatch.setattr("incident_platform.telemetry.CallbackHandler", explode)
    # Tracing is never a reason an incident is not processed.
    config = graph_config("inc-1", "corr-1", "evt-1", True)
    assert config["callbacks"] == []
    assert config["metadata"]["incident_id"] == "inc-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: FAIL — `AttributeError: <module 'incident_platform.telemetry'> has no attribute 'CallbackHandler'`, and a `TypeError` about positional arguments on the disabled case.

- [ ] **Step 3: Write minimal implementation**

In `src/incident_platform/telemetry.py`, add to the imports:

```python
from langfuse.langchain import CallbackHandler
```

Hoisting this import to module level (it is currently inside the function) is deliberate: `langfuse` is a hard dependency in `pyproject.toml`, and a module-level name is what makes the handler substitutable in tests.

Replace the whole of `graph_config` with:

```python
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
```

Replace the module docstring's second paragraph (the one beginning "graph_config() sets
incident_id") with:

```
graph_config() sets incident_id as the LangGraph thread_id, derives this delivered event's
trace id, and adds correlation metadata, tags and optional callbacks. One trace covers one
delivered event; traces for an incident are grouped by langfuse_session_id. The thread ID
selects checkpoint history; this module does not persist workflow state.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: PASS, 15 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_telemetry.py src/incident_platform/telemetry.py
git commit -m "$(cat <<'MSG'
feat(telemetry): bind the graph handler to a derived trace

One trace per delivered event, grouped into one session per incident.
Handler construction degrades to no callbacks rather than failing incidents.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 5: Forward the trace id to the gateway

**Files:**
- Modify: `src/incident_platform/integrations/llm.py:51-56`
- Test: `tests/test_llm_roles.py`

- [ ] **Step 1: Write the failing test**

Add these to the imports at the **top** of `tests/test_llm_roles.py`, keeping them sorted:

```python
from langchain_core.runnables import RunnableLambda

from incident_platform.config import Settings
from incident_platform.integrations.llm import Analyst
from incident_platform.models import Triage
from incident_platform.telemetry import trace_id_for
```

Then append:

```python
TRACED_STATE = {
    "incident_id": "inc-1",
    "input_event_id": "evt-1",
    "incident": {"severity": "high", "service": "checkout"},
    "evidence": [],
}


class RecordingModel:
    def __init__(self, result):
        self.result, self.bound = result, None

    def bind(self, **kwargs):
        self.bound = kwargs
        return self

    def with_structured_output(self, schema):
        return RunnableLambda(lambda _: self.result)


def _recording(monkeypatch):
    model = RecordingModel(Triage(severity="high", investigate=True, reason="ok"))
    monkeypatch.setattr(
        "incident_platform.integrations.llm._model", lambda cfg, alias: model
    )
    return model


async def test_structured_binds_the_derived_trace_id(monkeypatch):
    model = _recording(monkeypatch)
    analyst = Analyst(_live(llm_model="shared-alias"))
    await analyst.structured("triage", Triage, "Assess", TRACED_STATE)
    assert model.bound == {
        "extra_body": {"metadata": {"existing_trace_id": trace_id_for("inc-1", "evt-1")}}
    }


async def test_structured_skips_binding_without_identifiers(monkeypatch):
    model = _recording(monkeypatch)
    analyst = Analyst(_live(llm_model="shared-alias"))
    # A gateway-side trace is never a reason a model call fails.
    await analyst.structured("triage", Triage, "Assess", {"incident": {}, "evidence": []})
    assert model.bound is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_roles.py -v`
Expected: FAIL — `assert None == {'extra_body': ...}`; `structured` does not bind yet.

- [ ] **Step 3: Write minimal implementation**

In `src/incident_platform/integrations/llm.py`, add to the imports:

```python
from incident_platform.telemetry import trace_id_for
```

Add this module-level helper just above `class Analyst`:

```python
def _trace_metadata(state):
    """Build this invocation's `extra_body` payload, or None when state lacks the ids.

    Both values are set on first invocation and persisted in checkpoints, so they
    survive resumes. Absence skips binding rather than raising: a gateway-side trace
    is never a reason a model call fails.
    """
    incident_id, event_id = state.get("incident_id"), state.get("input_event_id")
    if not incident_id or not event_id:
        return None
    return {"metadata": {"existing_trace_id": trace_id_for(incident_id, event_id)}}
```

Replace `Analyst.structured` with:

```python
    async def structured(self, role, schema, task, state):
        prompt = ChatPromptTemplate.from_messages(
            [("system", SYSTEM), ("human", "Task: {task}\nIncident data: {data}")]
        )
        model = self.models[role]
        extra_body = _trace_metadata(state)
        if extra_body:
            # Lands in the request body, so the gateway attaches its generation, with the
            # cost it actually billed, to the same trace the worker is writing.
            model = model.bind(extra_body=extra_body)
        chain = prompt | model.with_structured_output(schema)
        return await chain.ainvoke({"task": task, "data": json.dumps(state)})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_roles.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_llm_roles.py src/incident_platform/integrations/llm.py
git commit -m "$(cat <<'MSG'
feat(llm): forward the incident's trace id to the gateway

Derived in place from graph state, so no trace plumbing threads through the
graph and Analyst keeps its signatures.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 6: Wire the worker

**Files:**
- Modify: `src/incident_platform/worker/main.py:26`, `:88`, `:166-172`
- Test: `tests/test_workflow.py`

- [ ] **Step 1: Write the failing test**

Add `from incident_platform.worker import main as worker_main` to the imports at the **top**
of `tests/test_workflow.py`, keeping them sorted. Then append:

```python
async def test_trace_config_uses_the_delivered_event_id(cfg, store, incident, monkeypatch):
    seen = {}
    original = worker_main.graph_config

    def spy(incident_id, correlation_id, event_id, enabled):
        seen["event_id"] = event_id
        return original(incident_id, correlation_id, event_id, enabled)

    monkeypatch.setattr(worker_main, "graph_config", spy)
    graph = build_graph(cfg, store, Analyst(cfg), MockEvidenceBackend(), InMemorySaver())
    event = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        event_type="incident.created",
        source="jira",
        payload=incident["raw"],
    )
    await process_event(store, graph, cfg, event)
    assert seen["event_id"] == str(event.event_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workflow.py::test_trace_config_uses_the_delivered_event_id -v`
Expected: FAIL — `TypeError: spy() missing 1 required positional argument: 'enabled'` (the caller still passes three arguments).

- [ ] **Step 3: Write minimal implementation**

In `src/incident_platform/worker/main.py`, change the import on line 26 to:

```python
from incident_platform.telemetry import configure_logging, graph_config, init_tracing
```

Change line 88 to:

```python
    config = graph_config(incident_id, correlation_id, str(event.event_id), cfg.langfuse_enabled)
```

In `main()`, add the initialisation immediately after `cfg = settings()`:

```python
    init_tracing(cfg)
```

The existing `flush()` in the `finally` block stays exactly as it is.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: PASS, 17 passed (16 existing plus the new one)

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: all pass. Integration tests needing `TEST_DATABASE_URL` skip as usual.

- [ ] **Step 6: Commit**

```bash
git add tests/test_workflow.py src/incident_platform/worker/main.py
git commit -m "$(cat <<'MSG'
feat(worker): initialise tracing and trace per delivered event

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 7: Add the observability Compose profile

No unit test — this is infrastructure, verified by bringing it up.

**Files:**
- Modify: `compose.yaml`, `.env.example`

- [ ] **Step 1: Fetch the upstream reference before writing anything**

Run: `curl -sSL https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml -o /tmp/langfuse-upstream.yml && head -80 /tmp/langfuse-upstream.yml`

The block below is transcribed from Langfuse's published v3 self-hosting compose. **Diff it against the upstream file you just fetched and against the tag you pin.** Where they differ, upstream wins — correct the block and note the correction. Do not guess a variable name; if a value cannot be confirmed, stop and report.

- [ ] **Step 2: Add the services**

In `compose.yaml`, add these services before the `volumes:` key. Every one carries `profiles: ["observability"]`, so the default `docker compose up` is unchanged.

```yaml
  langfuse-postgres:
    image: postgres:17
    profiles: ["observability"]
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
      POSTGRES_DB: langfuse
    volumes: ["langfusedata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse -d langfuse"]
      interval: 3s
      timeout: 3s
      retries: 30
  langfuse-redis:
    image: redis:7-alpine
    profiles: ["observability"]
    command: ["redis-server", "--requirepass", "langfuse"]
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "langfuse", "ping"]
      interval: 3s
      timeout: 3s
      retries: 30
  clickhouse:
    image: clickhouse/clickhouse-server:24.8
    profiles: ["observability"]
    environment:
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: clickhouse
      CLICKHOUSE_DB: default
    volumes: ["clickhousedata:/var/lib/clickhouse"]
    ulimits:
      nofile: {soft: 262144, hard: 262144}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:8123/ping || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 30
  langfuse-minio:
    image: minio/minio
    profiles: ["observability"]
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: minio
      MINIO_ROOT_PASSWORD: miniosecret
    volumes: ["langfuseblobs:/data"]
    healthcheck:
      test: ["CMD-SHELL", "mc ready local || exit 1"]
      interval: 5s
      timeout: 5s
      retries: 30
  langfuse-worker:
    image: langfuse/langfuse-worker:3
    profiles: ["observability"]
    environment: &langfuse-env
      DATABASE_URL: postgresql://langfuse:langfuse@langfuse-postgres:5432/langfuse
      SALT: dev-langfuse-salt
      ENCRYPTION_KEY: 0000000000000000000000000000000000000000000000000000000000000000
      NEXTAUTH_SECRET: dev-langfuse-nextauth
      NEXTAUTH_URL: http://localhost:3000
      CLICKHOUSE_MIGRATION_URL: clickhouse://clickhouse:9000
      CLICKHOUSE_URL: http://clickhouse:8123
      CLICKHOUSE_USER: clickhouse
      CLICKHOUSE_PASSWORD: clickhouse
      CLICKHOUSE_CLUSTER_ENABLED: "false"
      REDIS_HOST: langfuse-redis
      REDIS_PORT: "6379"
      REDIS_AUTH: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_BUCKET: langfuse
      LANGFUSE_S3_EVENT_UPLOAD_REGION: auto
      LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT: http://langfuse-minio:9000
      LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID: minio
      LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY: miniosecret
      LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE: "true"
    depends_on:
      langfuse-postgres: {condition: service_healthy}
      clickhouse: {condition: service_healthy}
      langfuse-minio: {condition: service_healthy}
      langfuse-redis: {condition: service_healthy}
  langfuse-web:
    image: langfuse/langfuse:3
    profiles: ["observability"]
    ports: ["127.0.0.1:3000:3000"]
    environment:
      <<: *langfuse-env
      # Headless provisioning: the stack comes up already usable, with the same keys
      # the worker and gateway are configured with. No UI click-through to start up.
      LANGFUSE_INIT_ORG_ID: incident-response
      LANGFUSE_INIT_ORG_NAME: Incident Response
      LANGFUSE_INIT_PROJECT_ID: incident-platform
      LANGFUSE_INIT_PROJECT_NAME: Incident Platform
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:-pk-lf-dev-public}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_SECRET_KEY:-sk-lf-dev-secret}
      LANGFUSE_INIT_USER_EMAIL: dev@example.com
      LANGFUSE_INIT_USER_NAME: Developer
      LANGFUSE_INIT_USER_PASSWORD: dev-langfuse-password
    depends_on:
      langfuse-postgres: {condition: service_healthy}
      clickhouse: {condition: service_healthy}
      langfuse-minio: {condition: service_healthy}
      langfuse-redis: {condition: service_healthy}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:3000/api/public/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 30
```

Extend the `volumes:` block at the end of the file to:

```yaml
volumes:
  pgdata:
  kafkadata:
  litellmdata:
  langfusedata:
  clickhousedata:
  langfuseblobs:
```

- [ ] **Step 3: Point the defaults at the in-profile service**

In `.env.example`, replace lines 18-21 with:

```
# Tracing. Self-hosted only: traces carry unredacted evidence, so LANGFUSE_BASE_URL
# must never point at a host outside the deployment boundary.
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=pk-lf-dev-public
LANGFUSE_SECRET_KEY=sk-lf-dev-secret
LANGFUSE_BASE_URL=http://langfuse-web:3000
```

- [ ] **Step 4: Verify the default path is untouched, then the profile resolves**

Run: `docker compose config --services | sort`
Expected: exactly the nine existing services — `gateway`, `init`, `kafka`, `litellm`, `litellm-postgres`, `postgres`, `redis`, `runbooks`, `stub-model`, `worker`. No `langfuse-*` or `clickhouse`.

Run: `docker compose --profile observability config --services | sort`
Expected: the above plus `clickhouse`, `langfuse-minio`, `langfuse-postgres`, `langfuse-redis`, `langfuse-web`, `langfuse-worker`.

- [ ] **Step 5: Bring the profile up**

Run: `docker compose --profile observability up -d langfuse-web`
Run: `docker compose ps langfuse-web`
Expected: `healthy`. If it is not, read `docker compose logs langfuse-web` and correct against upstream before continuing. Do not proceed to Task 8 with an unhealthy stack.

- [ ] **Step 6: Commit**

```bash
git add compose.yaml .env.example
git commit -m "$(cat <<'MSG'
feat(compose): add a self-hosted Langfuse observability profile

Off by default, so the ordinary up is unchanged. Its own Postgres and Redis
rather than sharing LiteLLM's, matching the litellm-postgres precedent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 8: Verification gate — gateway trace propagation

This is the one link that cannot be confirmed from the repository. **Its outcome decides the design.** Do not force a resolution.

**Files:**
- Modify: `deploy/litellm/config.yaml`

- [ ] **Step 1: Confirm the metadata key the proxy honours**

Run: `docker compose exec litellm python -c "import litellm, inspect; from litellm.integrations import langfuse; print(inspect.getsourcefile(langfuse))"`
Then grep that file for `trace_id`:
Run: `docker compose exec litellm sh -c "grep -rn 'existing_trace_id\|trace_id' \$(python -c 'import litellm,os;print(os.path.dirname(litellm.__file__))')/integrations/langfuse/ | head -30"`

Record which key the integration reads from request metadata. Task 5 wrote `existing_trace_id` provisionally.

- [ ] **Step 2: Decide from the evidence**

- **If the key is `existing_trace_id`:** continue to Step 3 unchanged.
- **If it is a different name:** change the literal in `_trace_metadata` in `src/incident_platform/integrations/llm.py`, update the expected value in `tests/test_llm_roles.py`, and note the correction in the spec's "To verify at implementation time" section. Continue to Step 3.
- **If the proxy does not forward request `metadata` to its Langfuse logger at all:** stop. Take the fallback from the spec — the gateway logs to its own Langfuse project, correlated by `incident_id` and not nested — and report this to the user before writing any more config.

- [ ] **Step 3: Enable the gateway callback**

In `deploy/litellm/config.yaml`, extend `general_settings` and add a `litellm_settings` block:

```yaml
litellm_settings:
  # Second trace source. The worker writes the graph tree; the proxy attaches each
  # generation with the cost it actually billed to the same trace, via the trace id
  # the worker forwards in request metadata.
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
```

In `compose.yaml`, add to the `litellm` service's `environment` block:

```yaml
      LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:-pk-lf-dev-public}
      LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY:-sk-lf-dev-secret}
      LANGFUSE_HOST: ${LANGFUSE_BASE_URL:-http://langfuse-web:3000}
```

The gateway is not in the `observability` profile, so these are inert when the profile is down — the proxy simply cannot reach the collector and logs its failure.

- [ ] **Step 4: Confirm the gateway still starts without the profile**

Run: `docker compose up -d litellm && docker compose ps litellm`
Expected: `healthy`. A Langfuse callback pointed at an absent collector must not stop the proxy serving. If it does, move the callback behind the profile and report it.

- [ ] **Step 5: Commit**

```bash
git add deploy/litellm/config.yaml compose.yaml
git commit -m "$(cat <<'MSG'
feat(gateway): report generations into the incident's trace

Gives each trace the cost the proxy actually billed, rather than the cost the
client-side handler infers.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 9: Correct the documentation this work makes false

Three of these are corrections, not additions. Without them the repository contradicts itself.

**Files:**
- Modify: `AGENTS.md`, `README.md:5`, `README.md:30`, `README.md:232-236`, `docs/operations.md:87-90`

- [ ] **Step 1: Amend the AGENTS.md invariant**

Replace the line:

```
- Never disable signature checks, make model output authoritative policy, or trust ticket/log text
  as instructions. Do not log credentials or raw evidence.
```

with:

```
- Never disable signature checks, make model output authoritative policy, or trust ticket/log text
  as instructions. Never log credentials anywhere. Raw evidence may reach an in-boundary trace
  store; it must never reach an external one, so LANGFUSE_BASE_URL stays inside the deployment.
- Trace delivery must never affect incident correctness. Tracing failures degrade to no
  callbacks; they never fail an incident and never block worker startup.
```

- [ ] **Step 2: Correct the README's external-Langfuse claims**

At `README.md:5`, change "opt-in external Langfuse tracing" to "opt-in self-hosted Langfuse tracing".

At `README.md:30`, change the diagram node `Langfuse["External Langfuse"]` to `Langfuse["Self-hosted Langfuse"]`.

In the "LLMs, tools and Langfuse" section, replace the sentence *"No extra local analytics/clickhouse/object-storage stack is needed. External traces may ..."* with:

```
Langfuse runs in the optional `observability` Compose profile: `docker compose --profile
observability up`. That profile adds its own PostgreSQL, ClickHouse, Redis and MinIO, so the
default `up` is unchanged. Traces carry evidence verbatim, which is only defensible because
the collector is inside the deployment boundary; never point `LANGFUSE_BASE_URL` at an
external host.
```

Also replace *"`langfuse.langchain.CallbackHandler` traces the graph and nested chains/tools, with `langfuse_session_id=incident_id` and correlation metadata"* with:

```
`langfuse.langchain.CallbackHandler` traces the graph and nested chains/tools. Each delivered
event gets its own trace, whose id is derived from `incident_id` and `event_id` rather than
stored, so it is recomputable after a restart or replay. An incident's traces — its initial
event and any approval resume — are grouped by `langfuse_session_id=incident_id`.
```

- [ ] **Step 3: Update the operations note**

In `docs/operations.md`, in the paragraph beginning "Observe inbox backlog", keep "Langfuse is optional; core correctness must not depend on trace delivery" and append:

```
Langfuse is self-hosted in the `observability` Compose profile. ClickHouse grows with trace
volume; retention and pruning there are an operator responsibility, as for the incident
database. Traces contain evidence verbatim, so the collector belongs inside the same trust
boundary as the worker.
```

- [ ] **Step 4: Confirm no stale claim survives**

Run: `grep -rn "External Langfuse\|external Langfuse\|no extra local analytics\|clickhouse/object-storage" README.md docs/ AGENTS.md`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md docs/operations.md
git commit -m "$(cat <<'MSG'
docs: self-hosted tracing, and the invariant it changes

Full-fidelity traces contradict "do not log raw evidence" as written, so the
invariant now distinguishes in-boundary from external sinks.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

### Task 10: End-to-end verification

**Files:**
- Modify: `docs/verification.md`

- [ ] **Step 1: Bring up the full stack**

Run: `docker compose --profile observability up -d`
Run: `docker compose ps`
Expected: every service healthy or completed. `init` shows `exited (0)`.

- [ ] **Step 2: Enable tracing and restart the worker**

Run: `sed -i '' 's/^LANGFUSE_ENABLED=false/LANGFUSE_ENABLED=true/' .env` (macOS `sed`; on GNU `sed` drop the `''`)
Run: `docker compose --profile observability up -d --force-recreate worker litellm`

- [ ] **Step 3: Drive an incident through**

Run: `uv run python scripts/demo.py`
Expected: the demo completes and reports an approved, executed incident as it does today.

- [ ] **Step 4: Inspect the trace**

Open `http://localhost:3000`, sign in with `dev@example.com` / `dev-langfuse-password`, open the Incident Platform project, and confirm each of:

- one trace per delivered event, not one per incident;
- the graph's nodes appear as a nested tree, not a flat list;
- a generation from the gateway carries a non-zero cost (this is what Task 8 bought — if cost is absent, the propagation fallback is in effect and must be recorded);
- the approval resume and the original are two traces in **one** session named for the incident id;
- the trace's top-level input and output are populated, which is `update_trace=True` working.

- [ ] **Step 5: Confirm the mask on live data**

Run: `docker compose exec worker python -c "
from incident_platform.config import settings
from incident_platform.telemetry import build_mask
print(build_mask(settings())(data='token ' + settings().jira_webhook_token.get_secret_value()))"`
Expected: `token [redacted]`.

Then search the Langfuse UI for the literal value of `JIRA_WEBHOOK_TOKEN` from `.env`.
Expected: no results.

- [ ] **Step 6: Confirm an unreachable collector cannot break an incident**

This covers verification item 3 in the spec: whether the SDK's export path can raise into
caller code.

Run: `docker compose --profile observability stop langfuse-web langfuse-worker`
Run: `uv run python scripts/demo.py`
Expected: the incident is processed and resolved exactly as in Step 3. Worker logs may carry
Langfuse export warnings; they must not appear as `incident.retry` or a failed job.
Run: `docker compose --profile observability start langfuse-web langfuse-worker`

If an incident *does* fail, that is a finding, not a nuisance: report it, and wrap the export
path rather than proceeding.

- [ ] **Step 7: Record what was verified**

Append to `docs/verification.md`, filling each line from what you actually observed:

```markdown
## Self-hosted Langfuse tracing (2026-09-23)

Exercised with Langfuse server <tag>, langfuse SDK 3.15.0, LiteLLM v1.90.2, via
`docker compose --profile observability up` and `scripts/demo.py` in mock mode.

- One trace per delivered event: <observed>
- Graph nodes nested rather than flat: <observed>
- Gateway-reported cost present on the generation: <observed, or FALLBACK IN EFFECT>
- Initial event and approval resume grouped in one session: <observed>
- Trace-level input/output populated (`update_trace=True`): <observed>
- `JIRA_WEBHOOK_TOKEN` absent from trace search; mask returns `[redacted]`: <observed>
- Incidents still processed with the collector stopped: <observed>
```

- [ ] **Step 8: Reset the local environment**

Run: `sed -i '' 's/^LANGFUSE_ENABLED=true/LANGFUSE_ENABLED=false/' .env`
Run: `docker compose --profile observability down`

- [ ] **Step 9: Final check and commit**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: all pass.

```bash
git add docs/verification.md
git commit -m "$(cat <<'MSG'
docs(verification): record the self-hosted tracing run

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
)"
```

---

## Out of scope

Named so they are not quietly added:

- Scores, LLM-as-judge, datasets and dataset runs. A separate spec, dependent on this one for trace identity. `integrations/evaluation.py` is not edited by any task here.
- Operational metrics (inbox backlog, DLQ depth, policy denials, approval age, checkpoint failures) and OTel export.
- Prompt management via Langfuse; `evaluation.load_prompt` stays unused.
- Sampling and retention policy beyond the documentation note in Task 9.
- Any code enforcement that `LANGFUSE_BASE_URL` is in-boundary. Explicitly declined: it is a documented invariant, enforced by review.
