import os
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from incident_platform.knowledge.chunker import chunk_markdown
from incident_platform.knowledge.index import ingest_documents
from incident_platform.storage import Store

pytestmark = pytest.mark.integration

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def pool():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL is not set")
    store = Store(DSN)
    await store.open()
    await store.setup()
    await store.close()
    pool = AsyncConnectionPool(
        DSN, open=False, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, timeout=3
    )
    await pool.open(wait=True)
    yield pool
    await pool.close()


async def test_setup_creates_knowledge_tables(pool):
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM information_schema.tables "
                "WHERE table_name IN ('knowledge_document','knowledge_chunk')"
            )
        ).fetchone()
    assert row["n"] == 2


def _corpus():
    return [
        chunk_markdown(path.read_text(), source=str(path))
        for path in sorted(Path("docs/runbooks").glob("*.md"))
    ]


async def test_ingest_is_idempotent(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    async with pool.connection() as conn:
        first = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_chunk c "
                "JOIN knowledge_document d USING(doc_id) WHERE d.kind='runbook'"
            )
        ).fetchone()
    await ingest_documents(pool, _corpus(), kind="runbook")
    async with pool.connection() as conn:
        second = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_chunk c "
                "JOIN knowledge_document d USING(doc_id) WHERE d.kind='runbook'"
            )
        ).fetchone()
    assert first["n"] == second["n"] == 10


async def test_ingest_removes_chunks_that_no_longer_exist(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    shrunk = chunk_markdown(
        "---\nid: checkout-rollback\ntitle: T\nservice: checkout\n---\n\nOnly one chunk.\n",
        source="docs/runbooks/checkout-rollback.md",
    )
    await ingest_documents(pool, [shrunk], kind="runbook")
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_chunk WHERE doc_id='checkout-rollback'"
            )
        ).fetchone()
    assert row["n"] == 1
