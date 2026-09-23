# Event-driven AI Incident Response

A Codex-ready Python monorepo with a FastAPI event gateway, Kafka transport, a separate
LangGraph worker, PostgreSQL application/checkpoint state, LangChain tools/model integration,
and opt-in self-hosted Langfuse tracing. **Local mode runs without LLM credentials and only simulates
evidence and execution.** Live evidence, Jira/Slack notification, and production execution adapters
are intentionally interfaces/stubs. This is an operational starter, not an autonomous production operator.

## Architecture

The core event flow:

![Core event flow from webhook through gateway, outbox, Kafka, inbox, and workflow checkpoints](docs/diagrams/event-flow.svg)

[Editable Mermaid source](docs/diagrams/event-flow.mmd)

The full architecture, including integrations:

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
  Graph -->|Callbacks + metadata| Langfuse["Self-hosted Langfuse"]
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
approvals, audit and simulated execution ledger. Redis is unnecessary for the incident platform's own state; the `redis` and `langfuse-redis`
services in Compose belong to the gateway and the tracing profile, not to this workflow.

`incident_id` is the Kafka key, graph `thread_id`, database identifier, log correlation value and
Langfuse session/metadata identifier. Each delivery has its own stable `event_id`, and the pair
derives that delivery's trace id, so a trace is addressable without storing a mapping. Source-scoped
external IDs produce deterministic UUIDs. A trusted adapter can provide an explicit UUID `incident_id`
to correlate Jira and Slack into the same incident. The starter sets `correlation_id = incident_id`.

## Layout

```text
src/incident_platform/
  gateway/           HTTP trust boundary, signed callbacks, status API
  worker/            Kafka intake, durable jobs, graph resume/recovery
  workflow/          typed graph and deterministic authorization/policy
  integrations/      read tools, LLM analyst, notification/executor interfaces, evaluation hooks
  knowledge/         chunking, indexing, hybrid retrieval, read-only MCP server
  knowledge/loaders/ per-format document loading into the shared chunk shape
  devtools/          development-only stub upstream and gateway verifier
  models.py          Pydantic contracts and typed IncidentState
  storage.py         PostgreSQL outbox/inbox, dedup, approvals, audit, per-incident lock
  schema.sql         additive application schema
  transport.py       idempotent Kafka producer + transactional outbox relay
  telemetry.py       structured logs, trace ids, credential masking, Langfuse callbacks
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
The investigator selects one read-only tool per round. Retrieval is one of them, `knowledge`,
taking the corpus `kind` to search; it is never mandatory up-front retrieval. The default mock
path uses topology, metrics and deployments.
What corpora exist is declared in `corpus.yaml`, not in code: a source names a path, a kind, a
loader and a service, so adding a document type needs no code change and no prompt change.
Markdown with frontmatter keeps its existing parser; anything else goes through a text splitter
and, for PDFs, `pypdf`. Every loader produces chunk ids with the same recipe, so evidence ids
stay stable across a re-index.
When `KNOWLEDGE_ENABLED=true`, those two tool names are served by a separate read-only MCP
server (`runbooks` in Compose) that performs hybrid search over `docs/runbooks/` and resolved
incidents, stored in PostgreSQL with pgvector. Lexical search needs no credentials; dense
search activates only when `EMBEDDING_MODEL` and `EMBEDDING_DIM` are set. Retrieved text is
untrusted reference material: it is delimited and labeled, never treated as instructions, and
its `trusted` flag is set by the adapter rather than inferred from content. An empty result is
returned as untrusted so it cannot satisfy the RCA corroboration rules.

Model calls and MCP tool calls both leave the incident cluster through a LiteLLM gateway.
The application names roles, not providers: `LLM_MODEL_TRIAGE`, `LLM_MODEL_RCA` and
`LLM_MODEL_PLAN` hold gateway aliases that LiteLLM maps to real models, so changing which
model serves a role is a gateway config change rather than an application redeploy. Each
falls back to `LLM_MODEL` when unset. Retrieval is gateway-only: the worker has no direct
route to the `runbooks` service, and `allowed_tools` in the gateway config pins the exposed
tool set independently of what the server registers.

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

Langfuse is self-hosted in the optional `observability` Compose profile, off by default:
`docker compose --profile observability up`. That profile adds its own PostgreSQL, ClickHouse,
Redis and MinIO, so the ordinary `up` is unchanged. The UI is published on
`http://localhost:3100`, and project keys are provisioned headlessly, so the stack comes up
already usable. Set `LANGFUSE_ENABLED=true` to start exporting.

`langfuse.langchain.CallbackHandler` traces the graph and nested chains/tools. Each delivered
event gets its own trace, whose id is derived from `incident_id` and `event_id` rather than
stored, so it is recomputable after a restart or replay. An incident's traces — its initial
event and any approval resume — are grouped by `langfuse_session_id=incident_id`. The worker
forwards that same id to the gateway in request metadata, so LiteLLM reports each generation
into the same trace and the graph tree and model usage appear together.

Traces carry evidence, ticket text and runbook snippets **verbatim**; only credentials are
masked. That is defensible only because the collector sits inside the deployment boundary, so
never point `LANGFUSE_BASE_URL` at an external host.
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
- A document's `service` scopes every retrieval of it, and a generic source must declare one
  even if that declaration is `service: null` for a global document. An omitted service is
  rejected rather than defaulted, because an unscoped document is eligible for every
  incident and would be cited as precedent for services it never described.
- The development stub upstream returns schema-shaped placeholder values, not reasoning. It
  proves routing and wiring, never model quality. Replace the aliases in
  `deploy/litellm/config.yaml` with real providers before drawing any conclusion about
  analyst behavior.
- Retrieval evidence is procedure and precedent, not observation. A valid RCA must cite at
  least one observational tool (logs, metrics, kubernetes, deployments, config_diffs,
  topology); runbooks and similar incidents alone cannot establish a root cause.
- Lexical ranking uses PostgreSQL `ts_rank_cd`, which weights term frequency and proximity
  but applies no IDF and no query-coverage weighting. A chunk repeating one query term can
  outrank a chunk covering more distinct terms. Measured on the seed corpus, the correct
  document still ranks first, and enabling dense retrieval fuses a second opinion over the
  same candidates. Replace the ranker before relying on lexical-only ordering at scale.
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
