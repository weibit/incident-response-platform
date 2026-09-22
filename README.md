# Event-driven AI Incident Response

A Codex-ready Python monorepo with a FastAPI event gateway, Kafka transport, a separate
LangGraph worker, PostgreSQL application/checkpoint state, LangChain tools/model integration,
and opt-in external Langfuse tracing. **Local mode runs without LLM credentials and only simulates
evidence and execution.** Live evidence, Jira/Slack notification, and production execution adapters
are intentionally interfaces/stubs. This is an operational starter, not an autonomous production operator.

## Architecture

```mermaid
flowchart LR
  Sources["Jira Automation / Slack adapter / SIEM"] -->|Signed or authenticated HTTP| Gateway["FastAPI Event Gateway"]
  Gateway -->|Validate + deduplicate + commit| Outbox[(PostgreSQL outbox)]
  Outbox -->|Gateway/worker producer relay| Kafka["Kafka: incident.raw / incident.approval"]
  Kafka --> Consumer["Worker: Kafka consumer"]
  Consumer -->|Persist then commit offset| Inbox[(PostgreSQL inbox)]
  Inbox --> Graph["LangGraph runtime
thread_id = incident_id"]
  Graph <--> Checkpoints[(PostgreSQL checkpoints)]
  Graph --> Tools["LangChain read-only tools / on-demand RAG"]
  Graph -->|Callbacks + metadata| Langfuse["External Langfuse"]
  Graph -->|Durable domain events| Outbox
  Outbox --> Domain["Kafka domain topics"]
```

```mermaid
flowchart TD
  N[Normalize: deterministic] --> E[Enrich: topology]
  E --> T[Triage: rules + model]
  T -->|Investigate| R[RCA loop: hypothesis + selected read tool]
  T -->|No automated investigation| M[Manual review]
  R -->|Next tool, within budget| R
  R --> V[Validate evidence IDs, provenance, freshness, confidence]
  R -->|Budget exhausted| M
  V -->|Valid| P[Mitigation planner]
  V -->|Insufficient: retry within budget| R
  V -->|Budget exhausted| M
  P --> Policy[Deterministic policy / risk]
  Policy -->|Low risk| X[Executor: recheck authorization]
  Policy -->|Medium / high risk| H[interrupt: exact-plan approval]
  Policy -->|Deny| D[Denied]
  Callback[Authenticated Slack / Jira callback] -->|Kafka approval + Command resume| H
  H -->|Approved| X
  H -->|Rejected| D
  X --> Verify[Independent verification adapter]
  Verify -->|Healthy| Done[Resolve]
  Verify -->|Unhealthy: retry within budget| R
  Verify -->|Budget exhausted| M
```

**Kafka vs LangGraph:** Kafka distributes durable events between services and independent subscribers.
It does not sit between graph nodes. LangGraph owns the incident's workflow, checkpoints, retries,
conditional branches and interrupt/resume. PostgreSQL owns ingress deduplication, outbox, inbox,
approvals, audit and simulated execution ledger. Redis is unnecessary for this starter.

`incident_id` is the Kafka key, graph `thread_id`, database identifier, log correlation value and
Langfuse session/metadata identifier. Each delivery has its own stable `event_id`. Source-scoped
external IDs produce deterministic UUIDs. A trusted adapter can provide an explicit UUID `incident_id`
to correlate Jira and Slack into the same incident. The starter sets `correlation_id = incident_id`.

## Layout

```text
src/incident_platform/
  gateway/           HTTP trust boundary, signed callbacks, status API
  worker/            Kafka intake, durable jobs, graph resume/recovery
  workflow/          typed graph and deterministic authorization/policy
  integrations/      read tools, LLM analyst, notification/executor interfaces, evaluation hooks
  models.py          Pydantic contracts and typed IncidentState
  storage.py         PostgreSQL outbox/inbox, dedup, approvals, audit, per-incident lock
  schema.sql         additive application schema
  transport.py       idempotent Kafka producer + transactional outbox relay
  telemetry.py       structured logs and Langfuse callbacks
  bootstrap.py       one-time application/checkpoint/topic setup
scripts/             curl fixtures, signing helper, end-to-end HTTP demo
 tests/              gateway, graph, policy, PostgreSQL recovery tests
 evals/              dataset-ready adversarial/scenario cases
 docs/operations.md  delivery guarantees, failure handling, integration boundaries
AGENTS.md            Codex implementation and verification guidance
```

## Quick start: all services in Docker

Prerequisites: Docker Compose v2, a running Docker daemon; Python 3.12 and
[uv](https://docs.astral.sh/uv/) for host-side tests and demo.

```sh
cp .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs -f gateway worker
```

The init job creates application tables, LangGraph checkpoint tables and Kafka topics.
The gateway is available at `http://localhost:8000`; interactive schema docs are at `/docs`.
The readiness probe tests PostgreSQL because HTTP acceptance depends on outbox durability,
not immediate Kafka availability. Exposed ports bind only to loopback.

```sh
uv sync --frozen
uv run python scripts/demo.py
uv run python scripts/demo.py --reject
```

The demo posts an event twice, checks deduplication, waits for the mock rollback plan, sends a
Jira approval/rejection callback, then checks `resolved` or `denied`. No real action is performed.

Stop with `docker compose down`; named volumes preserve data. To develop processes on the host:

```sh
docker compose up -d postgres kafka
uv sync --frozen
uv run python -m incident_platform.bootstrap
uv run uvicorn incident_platform.gateway.app:app --reload
# In another terminal, from this directory:
uv run python -m incident_platform.worker.main
```

Do not run host processes and full Compose apps simultaneously on the same ports.

## Webhook examples

The input contract is deliberately small: source event ID, external incident ID, summary, service,
severity and optional canonical incident UUID. Configure **Jira Automation** to map native issue fields
to this JSON and set a dedicated bearer credential. This route does not claim to parse arbitrary
native Jira webhook variants. Slack Events API envelopes must contain `event.incident`, populated
by your incident adapter; arbitrary channel messages are not automatically treated as incidents.
Slack's URL verification and signed `block_actions` callback form bodies are supported.

Jira (development credential from `.env.example`):

```sh
curl -sS http://localhost:8000/webhooks/jira \
  -H 'Authorization: Bearer dev-jira-webhook' \
  -H 'Content-Type: application/json' \
  --data-binary @scripts/jira.json
```

Slack, signed over the **exact bytes** sent; local signing is for development only:

```sh
export SLACK_SIGNING_SECRET=dev-slack-signing
TS=$(date +%s)
SIG=$(uv run python scripts/sign_payload.py slack "$TS" scripts/slack.json)
curl -sS http://localhost:8000/webhooks/slack \
  -H 'Content-Type: application/json' \
  -H "X-Slack-Request-Timestamp: $TS" \
  -H "X-Slack-Signature: $SIG" \
  --data-binary @scripts/slack.json
```

SIEM uses the same direct contract as Jira, with `X-Timestamp` and `X-Signature` containing
hex HMAC-SHA256 of `timestamp + ':' + raw_body`; use `scripts/sign_payload.py siem` with
`SIEM_SIGNING_SECRET`. Slack uses its documented `v0` scheme. Both reject timestamps outside five minutes.

Read the state and submit an exact-plan approval, substituting values returned by the API:

```sh
INCIDENT_ID='<incident_id from accepted response>'
curl -sS "http://localhost:8000/incidents/$INCIDENT_ID" \
  -H 'Authorization: Bearer dev-status'
PLAN_ID='<plan.plan_id from state>'
curl -sS "http://localhost:8000/callbacks/jira/approvals/$INCIDENT_ID" \
  -H 'Authorization: Bearer dev-jira-approval' \
  -H 'Content-Type: application/json' \
  --data "{\"plan_id\":\"$PLAN_ID\",\"approved\":true}"
```

Jira approval has a separate credential and is attributed to `jira-automation`; configure Jira
workflow permissions to restrict who can trigger that automation. The gateway does not accept
an arbitrary actor supplied by the caller. For Slack, send a signed block action to
`/callbacks/slack/approvals/{incident_id}`; `user.id` must be in `SLACK_APPROVERS`, and the first
`actions[].value` is JSON `{"plan_id":"...","approved":true}`. Notification delivery/UI creation
is an adapter integration point, not enabled in the starter.

A 202 means the event is **durably accepted in PostgreSQL**, not that Kafka publication or
investigation has completed. The gateway's producer relay publishes asynchronously. A repeated
source/event ID returns the original IDs; reusing it with different content returns 409.

## LLMs, tools and Langfuse

Mock mode uses deterministic analyst responses and labeled evidence. The real analyst uses
`init_chat_model` and `with_structured_output`; read tools use LangChain `StructuredTool`.
The investigator selects one read-only tool per round. Runbooks and similar incidents are optional
RAG tools, never mandatory up-front retrieval. The default mock path uses topology, metrics and deployments.

To integrate live systems:

1. Implement `EvidenceBackend` with scoped logs, metrics, K8s, deployments/config changes,
   topology/CMDB and retrieval clients; select that backend in worker startup. Set `trusted=True`
   only for validated responses from authorized adapters, never from model output.
2. Install a provider package, e.g. `uv sync --extra anthropic`, configure its API key and a model
   available to your account in `LLM_MODEL`, then set `MOCK_MODE=false`.
3. Implement notification delivery and a separate scoped `MutationBackend` with remote operation
   idempotency/reconciliation. Wire it inside `authorized_execution` only after the checks.
   Live mode currently fails closed at unconfigured evidence/execution adapters.
4. Replace simulated verification with fresh service SLO measurements over an observation window.

For external Langfuse, set `LANGFUSE_ENABLED=true`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
and `LANGFUSE_BASE_URL`. `langfuse.langchain.CallbackHandler` traces the graph and nested chains/tools,
with `langfuse_session_id=incident_id` and correlation metadata. Langfuse v3 credentials are read by
its SDK. No extra local analytics/clickhouse/object-storage stack is needed. External traces may
contain input/evidence: deploy redaction, access controls and retention before enabling with live data.
`integrations/evaluation.py` supplies explicit prompt, score and dataset hooks; `evals/cases.jsonl`
is ready to seed evaluation datasets. Upload/evaluation is opt-in, not automatically invoked.

## Tests and verification

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
# Optional real PostgreSQL tests, using a disposable database:
TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident_test uv run pytest -m integration
```

Integration tests never truncate tables, but insert persistent test data with unique IDs. They test
concurrent ingress deduplication, outbox failure retention, DLQ creation, advisory locking and
closing/reopening PostgreSQL checkpoint connections around approval/resume. CI provisions a separate
PostgreSQL test database. The HTTP demo validates Kafka/Compose end to end when services are running.
See `docs/verification.md` for what was actually run while creating this repository.

## Deliberate boundaries

- Read adapters, notification delivery and real mutation are stubs; local evidence/actions are simulated.
- One workflow lifecycle per incident ID. Subsequent source events are durably journaled as followups,
  without overwriting a paused plan or automatically reopening a resolved incident. Implement explicit
  update/reopen policy before using continuous incident updates.
- Rules classify observe as low risk, restart as medium and rollback as high (both need approval).
  Unknown/shell actions or out-of-scope targets are denied. This small policy is an extension point.
- RCA checks enforce citation existence, independent source types, trusted provenance, freshness and
  confidence threshold; they do not prove causality. Use domain-specific validators/evaluations.
- Single-node plaintext Kafka and one PostgreSQL role are for local use. Production needs TLS/SASL,
  scoped roles, edge rate limiting, secret management, retention, monitoring and capacity tuning.
- A worker runs one job at a time; scale worker processes for concurrency. PostgreSQL advisory locks
  serialize a given incident across workers and input topics. There is no periodic approval-expiry
  scheduler; expired callbacks return 409 and require operator review/replanning.

## API references checked during implementation

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupt/resume](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangChain models and structured outputs](https://docs.langchain.com/oss/python/langchain/models)
- [Langfuse LangChain/LangGraph integration](https://langfuse.com/integrations/frameworks/langchain)
- [Slack request verification](https://docs.slack.dev/authentication/verifying-requests-from-slack/)
- [Apache Kafka Docker setup](https://kafka.apache.org/41/getting-started/docker/)

`uv.lock` records the exact installed dependency set; use `uv sync --frozen` to reproduce it.
