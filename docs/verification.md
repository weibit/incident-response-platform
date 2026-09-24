# Verification record

Verified on 2026-09-19 with Python 3.12, uv, Docker Compose, LangGraph 1.2.11, LangChain 1.4.2,
Langfuse 3.15.0, PostgreSQL 17, and Apache Kafka 4.1.2.

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run pytest -q`: 23 passed, 4 PostgreSQL tests skipped when no test database was supplied.
- `TEST_DATABASE_URL=postgresql://incident:incident@localhost:5432/incident uv run pytest -m integration -q`:
  4 passed. These covered concurrent ingress deduplication, checkpoint restart/resume, advisory locks,
  and retaining an outbox row after a Kafka send failure.
- `docker compose config --quiet`: passed.
- `docker compose up --build -d`: passed. Gateway, worker, PostgreSQL and Kafka became healthy; the
  one-shot init service completed successfully.
- `uv run python scripts/demo.py`: passed. A duplicate webhook was deduplicated, an exact rollback plan
  paused for approval, and the simulated execution resolved the incident.
- `uv run python scripts/demo.py --reject`: passed. The same plan was rejected and no execution was recorded.
- `uv build`: passed.

The Compose check used local-only mock evidence and execution. No external LLM, Langfuse, Jira, Slack,
SIEM, Kubernetes or production mutation adapter was contacted. The live integrations intentionally fail
closed until their scoped adapters are supplied.

## Self-hosted Langfuse tracing (2026-09-23)

Verified with Langfuse server `3.225.10` (image `langfuse/langfuse:3`), langfuse SDK 3.15.0,
LiteLLM proxy v1.90.2, ClickHouse 24.8, and the `observability` Compose profile brought up with
`docker compose --profile observability up -d`. The app image was rebuilt first; the containers
had been running code predating this feature, and a stale image verifies nothing.

- `uv run pytest`: 133 passed, 23 skipped. `uv run ruff check .` and `ruff format --check .`: passed.
- `docker compose config --services`: still exactly the original ten services. The profile adds six
  (`clickhouse`, `langfuse-minio`, `langfuse-postgres`, `langfuse-redis`, `langfuse-web`,
  `langfuse-worker`), so the ordinary `up` is unchanged.
- `langfuse-web` reached healthy in about 15 seconds. Headless provisioning worked:
  `curl -u pk-lf-dev-public:sk-lf-dev-secret .../api/public/projects` returned the `incident-platform`
  project under the `incident-response` organisation, with no UI click-through.
- `scripts/demo.py` with `LANGFUSE_ENABLED=true`: the incident paused for approval and resolved,
  unchanged from the untraced run.
- **One trace per delivered event, not per incident.** The demo incident produced two traces
  (initial event and approval resume) with 20 and 7 observations, both grouped under
  `sessionId = incident_id`.
- **Observations form the graph tree**, named for the nodes: `triage`, `investigate`,
  `validate_rca`, `approval`, `execute`, `verify`, under a `LangGraph` root. No flat list.
- **Trace-level input and output are populated**, confirming `update_trace=True` took effect.
- **Trace ids are derived, not stored.** Recomputing `trace_id_for(incident_id, event_id)` locally
  reproduced both stored ids exactly (`6cef4a27…f6bb` and `f36376c6…a99c`). This is what lets
  evaluation attach scores later without a mapping table.
- **Gateway propagation works.** A live `Analyst.triage` call through LiteLLM, with worker-side
  tracing switched off so the gateway callback was the only possible trace author, produced a trace
  whose id was exactly the derived one, carrying a `GENERATION` observation for `openai/stub-triage`
  with token usage. The key the proxy honours is `existing_trace_id`, read from the request body.
  **Cost is 0 and cannot be otherwise here**: the stub upstream is not a priced provider model, so
  LiteLLM has no pricing to apply. Cost becomes real only against a real provider.
- **Masking holds against the live configuration.** Inside the worker, `build_mask(settings())`
  reduced the configured `JIRA_WEBHOOK_TOKEN` to `[redacted]` while leaving an evidence line
  byte-identical. That token appears zero times in the stored traces.
- **An unreachable collector cannot break an incident.** With `langfuse-web` and `langfuse-worker`
  stopped and tracing still enabled, `scripts/demo.py` resolved normally: zero `incident.retry`
  entries in the worker log. The gateway likewise stayed healthy and served a live model call with
  the collector absent.

Three defects in this feature were found only by running it, not by reading it: `ENCRYPTION_KEY`
unquoted in YAML resolved to the integer `0` and crash-looped the web service; the container binds
only its eth0 address, so a loopback healthcheck can never pass; and MinIO must pre-create the
`langfuse` bucket or every event upload fails.

Traces in this check contained mock evidence only. No external LLM, Langfuse Cloud, Jira, Slack,
SIEM or Kubernetes endpoint was contacted.

## LLM-as-judge evaluation (2026-09-24)

Verified against Langfuse server `3.225.10`, langfuse SDK 3.15.0, LiteLLM proxy v1.90.2, with
`docker compose --profile observability up`, `LANGFUSE_ENABLED=true` and `JUDGE_ENABLED=true`.
The app image was rebuilt first and the gateway recreated: without the rebuild the judge module
is absent from the image, and without recreating `litellm` the `incident-judge` alias is not
registered, so the judge's model call would fail.

- `uv run pytest`: 193 passed, 23 skipped. `ruff check` and `ruff format --check`: passed.
- `docker compose config --services`: still exactly the original ten. `judge` appears only under
  the `observability` profile, which totals 17 services.
- The judge joined consumer group `incident-judge-v1` and subscribed to `incident.resolved` and
  `incident.manual_review`.
- **Five scores on the incident's first trace**, with the types the design specifies:
  `investigation_rounds` NUMERIC 3, `outcome_status` CATEGORICAL with `resolved` in `stringValue`,
  `outcome_verified` BOOLEAN 1, `rca_citations_valid` BOOLEAN 1, `rca_faithfulness` NUMERIC 1 with
  comment `supported: stub`. The reason is `stub` because the stub upstream returns canned output;
  a real provider supplies a substantive one.
- **Trace identity is derived, confirmed independently.** Recomputing
  `trace_id_for(incident_id, state["input_event_id"])` locally produced `4c3f0e75…af58`, which
  resolves to a real trace carrying 20 observations and `sessionId` equal to the incident id.
- **Idempotency holds under genuine redelivery.** The `incident-judge-v1` consumer group was reset
  to earliest and the judge restarted, replaying every terminal event. The trace still carried
  **five** scores, not ten: the derived `score_id` upserts rather than appending.
- **Audit rows are append-only, unlike scores.** The same replay took `judge.verdict` rows from 13
  to 26. That is correct — the audit log records each judging attempt — but it means audit volume
  grows with replays while score volume does not.
- No `judge.skipped`, `judge.unparseable`, `judge.export_failed` or `judge.untraceable` in any run.

**The silent-discard behaviour was confirmed at scale, and it is the strongest argument for the
audit row being the durable floor.** On first start the judge consumed twelve historical terminal
events from earlier runs and wrote a `judge.verdict` row for every one, attempting roughly sixty
score writes. Only five were stored. The other incidents had been processed while tracing was
off, so the trace ids the judge derived name traces that do not exist, and the server discarded
those writes without an error anywhere — no exception, no warning, nothing in the judge's log.
Only the PostgreSQL audit rows record that those incidents were judged at all.

Operational note carried over from the tracing work: the Langfuse ingestion worker does not
recover its Redis connections after the host sleeps. It logged socket timeouts and ingested
nothing until restarted. Check `docker compose logs langfuse-worker` for socket timeouts before
concluding that a score write failed.
