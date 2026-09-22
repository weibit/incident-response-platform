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
