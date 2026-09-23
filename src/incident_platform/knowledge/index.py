import asyncio
import hashlib
import sys
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from incident_platform.config import settings
from incident_platform.knowledge.chunker import Chunk, Document
from incident_platform.knowledge.corpus import load_manifest
from incident_platform.knowledge.loaders import load_source

MANIFEST = Path("corpus.yaml")


async def ingest_documents(pool, documents: list[Document], kind: str) -> int:
    """Upsert documents and replace their chunk sets. Idempotent.

    This is a CLI/bootstrap path only. It is never exposed as an MCP tool, which is
    what keeps the retrieval server safe under LangGraph node replay.
    """
    total = 0
    for doc in documents:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO knowledge_document(doc_id,kind,title,service,source) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT (doc_id) DO UPDATE SET "
                    "kind=EXCLUDED.kind, title=EXCLUDED.title, service=EXCLUDED.service, "
                    "source=EXCLUDED.source, updated_at=now()",
                    (doc.doc_id, kind, doc.title, doc.service, doc.source),
                )
                keep = [chunk.chunk_id for chunk in doc.chunks]
                await conn.execute(
                    "DELETE FROM knowledge_chunk WHERE doc_id=%s AND NOT (chunk_id = ANY(%s))",
                    (doc.doc_id, keep),
                )
                for chunk in doc.chunks:
                    await conn.execute(
                        "INSERT INTO knowledge_chunk(chunk_id,doc_id,heading,body,ordinal) "
                        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT (chunk_id) DO UPDATE SET "
                        # doc_id is set too: chunk_id is a global key, and without this a
                        # collision would leave a row owned by one document carrying
                        # another's text. Impossible while chunk ids hash doc_id (pinned
                        # by test_chunk_id_recipe_is_pinned), but the writer should not
                        # depend on an invariant enforced only in the chunker.
                        "doc_id=EXCLUDED.doc_id, heading=EXCLUDED.heading, "
                        "body=EXCLUDED.body, ordinal=EXCLUDED.ordinal",
                        (chunk.chunk_id, doc.doc_id, chunk.heading, chunk.body, chunk.ordinal),
                    )
                total += len(doc.chunks)
    return total


async def prune_missing(pool, documents: list[Document], kind: str) -> int:
    """Delete documents of this kind that the corpus no longer contains.

    ingest_documents can only shrink a document that is still present, so without
    this a renamed frontmatter id or a deleted file strands its rows forever and
    retrieval keeps serving withdrawn guidance. Chunks follow via ON DELETE CASCADE.
    """
    if not documents:
        # A corpus that momentarily reads as empty must not wipe the index.
        raise ValueError(f"Refusing to prune {kind} documents against an empty corpus")
    keep = [doc.doc_id for doc in documents]
    async with pool.connection() as conn:
        result = await conn.execute(
            "DELETE FROM knowledge_document WHERE kind=%s AND NOT (doc_id = ANY(%s))",
            (kind, keep),
        )
    return result.rowcount


def by_kind(manifest: Path = MANIFEST) -> dict[str, list[Document]]:
    """Documents grouped by the kind their source declared.

    The only way to read the corpus. A flat list across kinds invites a caller to ingest
    everything under one kind, which is how a document lands in the wrong corpus and is
    retrieved as the wrong sort of evidence.

    Duplicate ids are rejected across the whole corpus rather than per source, because
    documents share one table: two sources both producing `checkout` would overwrite each
    other's chunks row by row.
    """
    grouped: dict[str, list[Document]] = {}
    seen: dict[str, str] = {}
    for source in load_manifest(manifest):
        for document in load_source(source):
            if document.doc_id in seen:
                raise ValueError(
                    f"Duplicate document id {document.doc_id!r} in {document.source} "
                    f"and {seen[document.doc_id]}"
                )
            seen[document.doc_id] = document.source
            grouped.setdefault(source.kind, []).append(document)
    return grouped


async def ensure_vector_column(pool, dim: int) -> None:
    """One-time, idempotent narrowing so the column can carry an ANN index."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT atttypmod AS mod FROM pg_attribute "
                "WHERE attrelid='knowledge_chunk'::regclass AND attname='embedding'"
            )
        ).fetchone()
        if row and row["mod"] == -1:
            await conn.execute(
                f"ALTER TABLE knowledge_chunk ALTER COLUMN embedding TYPE vector({dim})"
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS knowledge_chunk_hnsw "
            "ON knowledge_chunk USING hnsw (embedding vector_cosine_ops)"
        )


async def embed_corpus(pool, embedder) -> int:
    """Fill missing embeddings. Safe to re-run; only NULL rows are computed."""
    if embedder is None:
        return 0
    await ensure_vector_column(pool, embedder.dim)
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT chunk_id, body FROM knowledge_chunk WHERE embedding IS NULL")
        ).fetchall()
    if not rows:
        return 0
    vectors = await embedder.embed([row["body"] for row in rows])
    if vectors is None:
        return 0
    async with pool.connection() as conn:
        async with conn.transaction():
            for row, vector in zip(rows, vectors, strict=True):
                await conn.execute(
                    "UPDATE knowledge_chunk SET embedding=%s WHERE chunk_id=%s",
                    (str(vector), row["chunk_id"]),
                )
    return len(rows)


RESOLVED_SQL = """
SELECT incident_id, state FROM incidents
WHERE state->>'status' = 'resolved' AND state->'rca'->>'hypothesis' IS NOT NULL
"""


async def ingest_resolved_incidents(pool) -> int:
    """Index resolved incidents as the `incident` corpus.

    Cold-starts empty, which is correct: with no history there is no precedent, and
    the backend reports an empty result as untrusted rather than inventing one.
    """
    async with pool.connection() as conn:
        rows = await (await conn.execute(RESOLVED_SQL)).fetchall()
    documents = []
    for row in rows:
        state = row["state"]
        incident = state.get("incident") or {}
        summary = incident.get("summary") or ""
        hypothesis = (state.get("rca") or {}).get("hypothesis") or ""
        body = f"Summary: {summary}\nRoot cause: {hypothesis}"
        doc_id = row["incident_id"]
        digest = hashlib.sha256(f"{doc_id}:0::{body}".encode()).hexdigest()
        documents.append(
            Document(
                doc_id=doc_id,
                title=summary or doc_id,
                service=incident.get("service"),
                source=f"incident:{doc_id}",
                chunks=[Chunk(chunk_id=digest[:32], heading="", body=body, ordinal=0)],
            )
        )
    if not documents:
        return 0
    return await ingest_documents(pool, documents, kind="incident")


async def main() -> None:
    cfg = settings()
    pool = AsyncConnectionPool(
        cfg.database_url,
        open=False,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": dict_row},
        timeout=3,
    )
    await pool.open(wait=True)
    try:
        from incident_platform.knowledge.embeddings import provider

        totals = []
        for kind, documents in by_kind().items():
            count = await ingest_documents(pool, documents, kind=kind)
            pruned = await prune_missing(pool, documents, kind=kind)
            totals.append(f"{kind}: {count} chunks, pruned {pruned}")
        incidents = await ingest_resolved_incidents(pool)
        # After the incident ingest, so chunks from both corpora are embedded in one run.
        embedded = await embed_corpus(pool, provider(cfg))
        print(
            f"indexed {'; '.join(totals)}; {incidents} incident chunks, embedded {embedded}",
            file=sys.stderr,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
