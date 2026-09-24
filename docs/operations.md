# Delivery, recovery and production integration

## Durable boundaries

1. Gateway authenticates exact request bytes, enforces a size cap, validates the contract, derives IDs,
   and commits ingress uniqueness + raw outbox event in one PostgreSQL transaction. Only then returns 202.
   Database failure means no success acknowledgment. Rate limiting belongs at an authenticated edge proxy
   (per-source quotas, burst caps); this starter does not provide a distributed rate limiter.
2. Producer relay holds a short outbox row lock through Kafka `send_and_wait`, with idempotent producer
   and `acks=all`. It marks sent only after broker acknowledgment. Crashes may cause redelivery.
3. Kafka consumer validates envelope/key/type, inserts inbox row keyed by event ID, then commits offsets.
   Invalid records are durably represented in DLQ outbox before offset commit; raw bodies are not logged.
4. A job runner takes a PostgreSQL session advisory lock for the incident, reloads pending status,
   and invokes the graph with `thread_id=incident_id`, synchronous checkpoint durability and a wall timeout.
   An interrupted graph is a successful job: its Kafka record is not kept uncommitted until approval.
5. Every graph invocation uses the existing checkpoint. Retries never reset the graph to Normalize.
   Domain event IDs are deterministic by incident/stage/investigation round; outbox uniqueness suppresses
   node replay duplicates. Consumers must still deduplicate event IDs because Kafka can redeliver.
6. Approval decision is compare-and-set under a row lock and binds the exact stored plan, incident,
   authenticated actor and expiration. Its Kafka command is transactional with the decision. The worker
   reloads the decision from PostgreSQL, validates the checkpoint's plan and invokes `Command(resume=...)`.
7. Execute rechecks policy and approval. Mock execution inserts an idempotent result ledger; a real
   adapter must use the plan ID as a remote idempotency key and reconcile uncertain outcomes before retry.
   An execution ledger alone cannot guarantee exactly once against an external system.

## Topics

| Topic | Publisher | Purpose |
|---|---|---|
| incident.raw | Gateway outbox | Accepted raw contract; envelope event_type is incident.created |
| incident.created | Worker outbox | Deterministic normalization completed |
| incident.triaged | Worker outbox | Structured severity/investigation decision |
| incident.rca_completed | Worker outbox | Validated RCA and evidence references |
| incident.mitigation_proposed | Worker outbox | Immutable plan for review/notification subscribers |
| incident.approval | Gateway outbox | Authenticated durable approval/rejection command |
| incident.mitigated | Worker outbox | Execution result, explicitly simulated in local mode |
| incident.resolved | Worker outbox | Verification succeeded |
| incident.manual_review | Worker outbox | Investigation budget/validation/verification requires intervention |
| incident.dlq | Consumer/job runner | Poison message location/hash or exhausted job reference |

Cross-topic order is not guaranteed. Per-incident locking plus checkpoint/plan validation protects
approval processing. The durable inbox is the recovery source after Kafka offsets have advanced.
A unique deterministic incident ID collision across unrelated source tenants must be prevented by adding
tenant identity to the ID derivation before multi-tenant deployment.

## Failure and operator procedures

- **Context budget exceeded:** the graph finishes as `manual_review` with a fixed
  `context_unavailable:<stage>` reason rather than repeatedly submitting the same oversized
  prompt. Inspect original evidence in the checkpoint/status snapshot and the `context.built`
  estimates. Check `CONTEXT_*` settings against the smallest real model/fallback window and
  the `incident-summary` gateway route. Summarization cannot change policy or approval state.
  Summaries are lossy and request-local; cited observations remain verbatim for planning.
  Replanning/reopening still requires an explicit operator procedure; changing a budget does
  not automatically restart an incident already sent to manual review.

- **Kafka down:** accepted webhooks accumulate in outbox; relay reconnects. Monitor oldest unsent age,
  outbox count, disk space, and publish errors. On sustained outage, shed load before exhausting storage.
- **Database down:** gateway cannot acknowledge events; upstream must retry with the same source event ID.
  Consumer does not commit unpersisted records. Worker process restart recovers pending inbox jobs.
- **Tool/model timeout:** read nodes retry connection/timeouts up to three attempts; whole jobs get bounded
  exponential retry scheduling (2..60 seconds) up to MAX_ATTEMPTS, then status `dead` plus an atomic DLQ event.
  Model-provider adapters also set request timeouts. Add provider-specific transient exceptions deliberately.
- **Worker crash:** inbox remains pending; advisory lock is released with the database session; next worker
  resumes checkpoint. A crash after graph completion but before inbox acknowledgment reads completed state
  and marks the inbox done without repeating execution.
- **DLQ replay:** fix the cause, inspect the inbox event and checkpoint, then reset only that row to
  `status='pending', attempts=0, next_attempt_at=now()` via an audited operator procedure. Do not replay a
  mutated production action without remote reconciliation. Transport-malformed messages require corrected
  re-ingestion with a new source event ID; the DLQ location points to retained Kafka input.
- **Approval expired:** leave the old plan unexecuted. Operator review and explicit new plan are required;
  never extend the old record silently. Automatic expiration/replanning is a future extension.
- **Verification failed:** gather new observations in another bounded investigation round; manual review
  when the shared investigation budget is exhausted. Do not mark resolved on a model assertion.
- **Followup event:** retained in the audit table with incident.followup_recorded. There is no implicit
  state reset or reopen. This avoids silently invalidating an already reviewed action.

## Production least privilege

Use a migrations role to create schemas/checkpoint tables, a gateway role for ingress/outbox and
approval/status access, a worker role for inbox/checkpoints/audit, read-only evidence credentials,
and a separate executor identity for an allowlisted namespace/service/action. Supply Kafka TLS/SASL
configuration and ACLs: gateway writes raw/approval; worker reads those and writes domain topics;
external subscribers cannot publish approval commands. The starter's broker settings are local-only.

`JIRA_APPROVAL_TOKEN` must be distinct from ingress/status tokens and restricted to an approved Jira
workflow automation. Slack signatures prove the sending application, and actor allowlisting restricts
who can approve; map user IDs to roles/service scopes for production. Pin deployment targets/revisions,
check current resource versions to prevent stale-plan execution, and define escalation/on-call ownership.

Implement adapters with deadlines, result size limits, secret/PII redaction, provenance, resource scoping,
and safe query languages. Never expose arbitrary shell/SQL execution as a model tool. RAG documents,
ticket descriptions and logs are untrusted inputs; prompt instructions help but cannot replace runtime
policy. Notification consumers need their own event-ID deduplication and should redact sensitive evidence.

Observe inbox backlog/age, DLQ count, policy denials, approval age/expiry, checkpoint failures,
verification outcomes, tool latency, model token/cost, and end-to-end incident duration. Langfuse is
optional; core correctness must not depend on trace delivery. Add infrastructure OTel export for
HTTP/Kafka/SQL when deploying; the starter emits structured correlated logs and LLM/graph callbacks.
Langfuse is self-hosted in the `observability` Compose profile. ClickHouse grows with trace
volume; retention and pruning there are an operator responsibility, as for the incident database.
Traces contain evidence verbatim, so the collector belongs inside the same trust boundary as the
worker.

Retention/pruning and database backups are operator responsibilities; migrations currently create tables
additively, not a full versioned migration framework.

## Runbook knowledge retrieval

The `runbooks` service exposes exactly two read-only MCP tools, `search_runbooks` and
`search_similar_incidents`. Indexing is not an MCP tool; it runs only through
`python -m incident_platform.knowledge.index`, which the `init` job invokes. This matters
because LangGraph replays nodes after failures and interrupts - a write reachable from a
tool call would be replayed with it.

The service publishes no host port and authenticates every request with a bearer token.
In production, give it a database role with SELECT on `knowledge_document` and
`knowledge_chunk` only; the indexer runs separately and needs write access. The pgvector
extension must be installed by a DBA before first start - the local Compose image ships it,
but a managed PostgreSQL instance generally will not have it enabled by default.

Retrieved documents are untrusted input. They are delimited and labeled in the evidence
observation, and snippet bodies are never logged. Retrieval is capped at five snippets,
1200 characters each, 6000 characters total.

Changing `EMBEDDING_DIM` after indexing requires clearing the embedding column and
re-running the indexer. It is an operator action, not an automatic migration.

`worker` waits for `runbooks` to become healthy, and `runbooks` refuses to start without
`KNOWLEDGE_MCP_TOKEN`. An `.env` predating this feature therefore fails the whole stack,
not just retrieval. Copy the `KNOWLEDGE_*` block from `.env.example` into an existing
`.env` before the first `docker compose up` after upgrading.

## Gateway

LiteLLM serves both `/v1/chat/completions` and `/mcp/` for this platform. It owns model
selection, tool entitlements and usage accounting; the incident cluster holds only aliases
and a gateway key. It runs against its own PostgreSQL instance rather than the incident
database, so neither side's migrations touch the other, and Redis carries shared rate limits.

Two credentials are in play, but not the way a pass-through design would suggest. The worker
authenticates to the gateway with `x-litellm-api-key`. The gateway authenticates itself to
the runbooks server with its own `authentication_token`, configured per MCP server. It has to
own that credential rather than relay the caller's: it connects to runbooks at its own
startup to list tools, when no caller request exists, and a relayed header cannot serve that
call. A leaked gateway key therefore does grant retrieval access, which a pure pass-through
design would not - weigh that before exposing the gateway more widely.

The MCP endpoint needs its trailing slash. `/mcp` answers with a 307 to `/mcp/`, and the MCP
SDK does not follow redirects on POST, so omitting it fails the initialize handshake with an
opaque "Server returned an error response".

Tool names are namespaced `<server_name>-<tool_name>`. Renaming the server in
`deploy/litellm/config.yaml` without changing `MCP_SERVER` in `knowledge_backend.py` makes
every retrieval call fail as an unknown tool.

The worker depends on the gateway being healthy, and the gateway depends on its database.
A gateway outage therefore stops the worker from starting, where previously only retrieval
was affected. That is the cost of the gateway-only decision, and it is deliberate: there is
no direct fallback path to misconfigure or forget to secure.

## Corpus

`corpus.yaml` declares every indexed source: a glob, a `kind`, a `loader` and a `service`.
Adding a document type is a manifest entry, not a code change, and the available kinds reach
the analyst through the knowledge tool's generated description.

`service` is the scoping boundary. `service: null` means global and eligible for every
incident; an omitted `service` on a non-Markdown source is an error rather than a default,
because the failure mode is silent and serious: an unscoped document is retrieved for every
service and can be cited as precedent for one it never described. Markdown may omit it
because frontmatter carries it per document.

A source matching no files fails ingestion. That is deliberate: a glob that quietly matches
nothing looks identical to a healthy run, and the next prune would remove that kind entirely.

`corpus.yaml` and every declared corpus directory must be copied into the image. The init job
reads the manifest at startup, so a missing file there is not a degraded corpus - it is a
stack that will not start.

Re-indexing must not change chunk ids. They hash document id, heading and content, and
evidence ids hash the chunk ids they cite, so a churned id silently invalidates a
checkpointed citation. Any loader change should be checked against
`test_reindexing_the_existing_corpus_keeps_every_chunk_id` before it ships.
