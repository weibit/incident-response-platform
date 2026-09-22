import asyncio
import sys
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from incident_platform.config import settings
from incident_platform.knowledge.chunker import Document, chunk_markdown

CORPUS = Path("docs/runbooks")


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
                        "heading=EXCLUDED.heading, body=EXCLUDED.body, ordinal=EXCLUDED.ordinal",
                        (chunk.chunk_id, doc.doc_id, chunk.heading, chunk.body, chunk.ordinal),
                    )
                total += len(doc.chunks)
    return total


def load_corpus(directory: Path = CORPUS) -> list[Document]:
    paths = sorted(directory.glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"No runbooks found under {directory}")
    documents = [chunk_markdown(path.read_text(), source=str(path)) for path in paths]
    seen: dict[str, str] = {}
    for doc in documents:
        if doc.doc_id in seen:
            raise ValueError(
                f"Duplicate runbook id {doc.doc_id!r} in {doc.source} and {seen[doc.doc_id]}"
            )
        seen[doc.doc_id] = doc.source
    return documents


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
        count = await ingest_documents(pool, load_corpus(), kind="runbook")
        print(f"indexed {count} runbook chunks", file=sys.stderr)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
