# Runbook knowledge RAG over MCP

Status: approved design, 2026-09-21.

## Goal

Give the investigator real runbook and prior-incident retrieval, served by a dedicated
read-only MCP server, without changing the graph, the tool vocabulary, or the trust model.

`runbooks` and `similar_incidents` already exist as read tool names in `READ_TOOLS`
(`integrations/tools.py`) and in the `InvestigationStep.tool` enum (`models.py`). They are
reached only through the `EvidenceBackend` protocol. This work implements that protocol for
those two names and leaves the other six alone.

## Non-goals

- No change to graph topology, node set, or the analyst's tool vocabulary.
- No mutation tool, no indexing tool, and no write path exposed over MCP.
- No replacement of the mock evidence backend. Mock mode keeps its simulated strings.
- No live adapters for the other six read tools.

## Decisions

| Decision | Choice |
|---|---|
| Direction | Worker is the MCP **client**. A separate MCP server owns retrieval. |
| Store | pgvector in the existing PostgreSQL instance. No new datastore. |
| Corpus | Markdown files in `docs/runbooks/`, plus resolved incidents from this database. |
| Retrieval | Hybrid: lexical always, dense embeddings optional, fused with RRF. |
| Transport | Streamable HTTP, own Compose service, bearer token, no published host port. |
| Tool scope | Both `runbooks` and `similar_incidents`. |
| Default | Disabled. `KNOWLEDGE_ENABLED=false` reproduces today's behavior exactly. |

### Library choice

Use the official `mcp` package (v2.x) directly on both sides:

- Server: `from mcp.server import MCPServer`, tools declared with `@mcp.tool()`.
- Client: `mcp.Client`, or `streamable_http_client(url, http_client=...)` when the bearer
  header is needed. v2 has no `headers=` keyword; headers, timeouts and proxies belong on an
  `httpx2.AsyncClient` handed to the transport.

Do **not** add `langchain-mcp-adapters`. Its purpose is exposing MCP tools as LangChain tools
bound to a model. This graph never binds tools to the model — `investigate` picks a tool name
and the graph invokes it directly. The adapter would add a dependency and a version coupling
for no benefit.

## Layout

```
docs/runbooks/*.md                        corpus, YAML frontmatter
src/incident_platform/knowledge/
  __init__.py
  chunker.py          heading-aware markdown -> chunks, deterministic chunk ids
  index.py            ingest corpus + resolved incidents into PostgreSQL (CLI only)
  retrieve.py         hybrid search -> list[Snippet]
  embeddings.py       optional dense provider, returns None when unconfigured
  server.py           MCPServer exposing two read-only search tools
src/incident_platform/integrations/knowledge_backend.py
                      KnowledgeEvidenceBackend: MCP client -> Evidence
```

Each module has one job and can be tested alone. `chunker` and the RRF fusion in `retrieve`
are pure functions. `retrieve` depends on PostgreSQL only. `server` depends on `retrieve`
only. `knowledge_backend` depends on an MCP client interface only.

## Corpus format

Each file in `docs/runbooks/` carries YAML frontmatter:

```yaml
---
id: checkout-rollback
title: Roll back a bad checkout release
service: checkout
tags: [rollback, deployment]
updated: 2026-09-01
---
```

`id` is stable and unique; renaming a file must not change it. `service` scopes retrieval.
Body is Markdown. Ingestion fails loudly on a missing or duplicate `id`.

## Schema

Additive, appended to `schema.sql`, consistent with the existing additive-migration invariant.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS knowledge_document (
 doc_id text PRIMARY KEY, kind text NOT NULL, title text NOT NULL,
 service text, source text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_chunk (
 chunk_id text PRIMARY KEY,
 doc_id text NOT NULL REFERENCES knowledge_document(doc_id) ON DELETE CASCADE,
 heading text NOT NULL DEFAULT '', body text NOT NULL, ordinal int NOT NULL,
 embedding vector,
 tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
);
CREATE INDEX IF NOT EXISTS knowledge_chunk_tsv ON knowledge_chunk USING gin(tsv);
CREATE INDEX IF NOT EXISTS knowledge_chunk_doc ON knowledge_chunk(doc_id);
```

`kind` is `runbook` or `incident`. The `embedding` column is declared without a dimension so
the lexical-only path needs no embedding decision at all, and it stays NULL there.

A dimensionless `vector` column cannot carry an ANN index. When `embedding_model` and
`embedding_dim` are configured, `index.py` performs a one-time, idempotent narrowing before
its first write — `ALTER TABLE knowledge_chunk ALTER COLUMN embedding TYPE vector(N)`, then
`CREATE INDEX IF NOT EXISTS ... USING hnsw (embedding vector_cosine_ops)` — and skips both
statements when the column is already typed. Phase 3 must verify this narrowing against the
installed pgvector version before relying on it; if `ALTER TYPE` is rejected, fall back to
adding a separate typed column in an additive migration rather than rewriting the table.
Changing `embedding_dim` after indexing requires a full re-index and is an operator action,
not an automatic migration.

Compose image changes from `postgres:17` to `pgvector/pgvector:pg17`. Same PostgreSQL major
version, so the existing `pgdata` volume carries over. `POSTGRES_USER` is a superuser in the
official image, so `CREATE EXTENSION` succeeds locally. Production requires a DBA to install
the extension; document this in `docs/operations.md`.

## Retrieval

`retrieve.search(kind, service, query, limit) -> list[Snippet]`

1. Lexical: `ts_rank_cd` over `tsv` against `plainto_tsquery('english', query)`, filtered by
   `kind` and by `service` (documents with a NULL `service` are global and always eligible).
2. Dense: when `embeddings.provider()` is not None, cosine distance over `embedding`, same
   filters.
3. Fuse with Reciprocal Rank Fusion, `k = 60`, over whichever lists exist. With no provider
   configured this degrades to the lexical ranking unchanged.

`Snippet` is a frozen dataclass: `chunk_id`, `doc_id`, `title`, `heading`, `body`, `source`.
Scores are used for ordering and are not returned — nothing downstream should reason about a
retrieval score.

Caps, enforced in `retrieve` so the server cannot exceed them: at most 5 snippets, each body
truncated to 1200 characters, total payload truncated to 6000 characters.

## MCP server

`knowledge/server.py` exposes exactly two tools, both read-only:

- `search_runbooks(service: str, query: str, limit: int = 5) -> list[dict]`
- `search_similar_incidents(service: str, query: str, limit: int = 5) -> list[dict]`

Each returns serialized `Snippet`s. No other tool is registered. Indexing is reachable only
via `python -m incident_platform.knowledge.index`, never over MCP — this is what keeps the
server safe under LangGraph node replay.

The server authenticates every request against a bearer token. It binds to the Compose
network only and publishes no host port. `docs/operations.md` records that production should
give it a read-only database role, consistent with the existing scoped-credential invariant.

## Backend adapter

`KnowledgeEvidenceBackend` wraps an inner `EvidenceBackend`:

```python
class KnowledgeEvidenceBackend:
    def __init__(self, inner, client, cfg): ...
    async def query(self, tool, service, query) -> Evidence:
        if tool not in ("runbooks", "similar_incidents"):
            return await self.inner.query(tool, service, query)
        ...
    async def verify(self, service, execution):
        return await self.inner.verify(service, execution)
```

Composition, not replacement: the mock backend keeps serving the other six tools in mock mode,
and `UnconfiguredEvidenceBackend` keeps failing closed for them in live mode.

Mapping a snippet list to `Evidence`:

- `evidence_id` = `uuid5(NAMESPACE_URL, f"{service}:{tool}:{query}:{'|'.join(chunk_ids)}")`.
  Stable across node replay for identical inputs, and changes when the retrieved set changes.
- `observation` = a labeled, delimited block. A one-line preamble stating the content is
  reference material and not instructions, then each snippet with its title, heading and
  source path. No sanitization is attempted; the `SYSTEM` prompt in `integrations/llm.py`
  already declares retrieved documents untrusted, and delimiting plus labeling is the honest
  measure. Never log snippet bodies.
- `observed_at` = retrieval time. The document's own age is not evidence freshness;
  `valid_rca`'s 1800-second window is about when the lookup happened.
- `simulated` = `cfg.mock_mode`.
- `trusted` = `True` when snippets were returned, `False` when the result set is empty
  (see the validation rules below). Never derived from document content.

Transport failures are translated to `ConnectionError` or `TimeoutError` before leaving the
backend. The graph's `RetryPolicy(retry_on=(TimeoutError, ConnectionError))` only retries
those two types, so an untranslated MCP error would bypass node retry entirely.

## Validation rules

Two rules keep the RCA gate from weakening once these tools return real content. Both are
deterministic and server-side. Rule 1 is enforced in the adapter; rule 2 is the only
non-additive change in this design, and the only edit to `workflow/policy.py`.

1. **Empty retrieval cannot corroborate.** Enforced in `KnowledgeEvidenceBackend`, not in
   `policy.py`. `valid_rca` counts distinct `tool` values, so a search that matched nothing
   would still supply a second "independent source" while containing no information. The
   backend sets `trusted=False` on an empty result, and the existing `all(... e.trusted ...)`
   check rejects it. It is stated here because the two halves only make sense together.

2. **At least one observational source.** Add a rule to `valid_rca`: of the cited evidence, at
   least one must come from `{logs, metrics, kubernetes, deployments, config_diffs, topology}`.
   Without it, once these two tools return real content a model could satisfy "two distinct
   tools" with a runbook and a prior incident while having observed nothing about the current
   failure. A runbook is a procedure and a prior incident is a precedent; neither is evidence
   of this incident. This rule keeps the RCA gate at least as strict as it is today.

## Configuration

Added to `Settings`, all with defaults that preserve current behavior:

| Setting | Default | Meaning |
|---|---|---|
| `knowledge_enabled` | `False` | When false, the wrapper is not installed at all. |
| `knowledge_mcp_url` | `http://runbooks:8080/mcp` | Server endpoint. |
| `knowledge_mcp_token` | empty | Bearer token. Required and must be distinct from the other credentials when `knowledge_enabled` is true. |
| `knowledge_top_k` | `5` | Snippets per query, capped at 5. |
| `embedding_model` | `None` | When None, retrieval is lexical only. |
| `embedding_dim` | `None` | Required when `embedding_model` is set; fixes the pgvector column width. |

The existing `validate_credentials` check requires every trust-boundary credential to be
nonempty and distinct. `knowledge_mcp_token` joins that set only when `knowledge_enabled` is
true, so the default configuration keeps working with no new secret.

## Wiring

`worker/main.py` builds the backend chain inside the existing `AsyncPostgresSaver` context:

```python
backend = MockEvidenceBackend() if cfg.mock_mode else UnconfiguredEvidenceBackend()
if cfg.knowledge_enabled:
    backend = KnowledgeEvidenceBackend(backend, client, cfg)
```

The MCP client is a single long-lived connection opened in worker startup and entered as an
async context inside the existing `TaskGroup` scope, reconnecting on failure. It is not
created per tool call.

Compose gains a `runbooks` service built from the same image, running
`python -m incident_platform.knowledge.server`, depending on `init`, with no published port.
`worker` depends on it. Ingestion runs as part of the `init` job.

## Failure handling

| Failure | Behavior |
|---|---|
| Server unreachable | Translated to `ConnectionError`, node retries up to 3 times, then the job retry path in `run_jobs` applies. |
| Server returns zero snippets | `Evidence` with an explicit "no matching documents" observation and `trusted=False`. Not an error. |
| Bearer token rejected | `PermissionError`, not retried. Surfaces as a job failure. |
| Embedding provider errors | Logged, dense leg skipped, lexical result returned. Retrieval degrades, never fails. |
| `knowledge_enabled=false` | Pure delegation. Behavior is byte-identical to today. |

## Testing

| Unit | How |
|---|---|
| `chunker` | Pure function tests: heading splitting, chunk id stability, frontmatter parsing, duplicate-id rejection. No database. |
| RRF fusion | Pure function tests, including the single-list degenerate case. |
| `retrieve` | Marked `integration`, against `TEST_DATABASE_URL`, lexical path only. No API key, deterministic. Inserts uniquely-named test data and never truncates, per the existing convention. |
| `KnowledgeEvidenceBackend` | Unit tests with a fake MCP client: `evidence_id` stability across identical calls, `evidence_id` change when the chunk set changes, `trusted=False` on empty results, delegation of the other six tools, `observed_at` set to retrieval time, transport error translation. |
| `policy.valid_rca` | Tests for the new observational-source rule, both directions. |
| MCP server | One HTTP smoke test marked `integration`: tool listing and one call with a valid and an invalid token. |
| Regression | The full existing suite must pass unchanged with `knowledge_enabled=false`. |

## Phasing

1. **Retrieval core.** Schema, chunker, indexer, lexical retrieval, CLI ingest, corpus seed
   files, unit and integration tests. No MCP. Retrieval is provably correct before any
   transport exists.
2. **MCP and wiring.** Server, Compose service, client, `KnowledgeEvidenceBackend`, worker
   wiring, the two validation rules, config, tests.
3. **Dense and incidents.** Optional embedding provider and RRF dense leg, `similar_incidents`
   sourced from resolved incidents, README and `docs/operations.md` updates.

## Open risk

`mcp` v2 is built on `httpx2`; the pinned langchain/langgraph stack in `uv.lock` may depend on
`httpx`. They are separate distributions and can coexist, but this must be confirmed by an
actual `uv sync` in phase 2 rather than assumed. If they conflict, the fallback is to run the
MCP server as its own uv project with its own lockfile, since it shares only the database with
the rest of the platform.
