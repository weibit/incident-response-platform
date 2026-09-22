import os
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from incident_platform.knowledge.chunker import chunk_markdown
from incident_platform.knowledge.index import ingest_documents, prune_missing
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


async def test_prune_removes_documents_the_corpus_no_longer_has(pool):
    # A renamed frontmatter id or a deleted file must not keep being retrieved.
    withdrawn = chunk_markdown(
        "---\nid: zz-withdrawn\ntitle: Gone\nservice: checkout\n---\n\nStale guidance.\n",
        source="docs/runbooks/zz-withdrawn.md",
    )
    await ingest_documents(pool, [*_corpus(), withdrawn], kind="runbook")
    async with pool.connection() as conn:
        before = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_document WHERE doc_id='zz-withdrawn'"
            )
        ).fetchone()
    assert before["n"] == 1

    pruned = await prune_missing(pool, _corpus(), kind="runbook")
    assert pruned == 1
    async with pool.connection() as conn:
        after = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_document WHERE doc_id='zz-withdrawn'"
            )
        ).fetchone()
        orphans = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_chunk WHERE doc_id='zz-withdrawn'"
            )
        ).fetchone()
    assert after["n"] == 0
    assert orphans["n"] == 0  # chunks follow via ON DELETE CASCADE


async def test_prune_refuses_an_empty_corpus(pool):
    # A glob that momentarily returns nothing must not wipe the index.
    with pytest.raises(ValueError, match="empty corpus"):
        await prune_missing(pool, [], kind="runbook")


async def test_prune_leaves_other_kinds_alone(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    other = chunk_markdown(
        "---\nid: zz-incident\ntitle: Prior\nservice: checkout\n---\n\nPrior incident.\n",
        source="incident:zz-incident",
    )
    await ingest_documents(pool, [other], kind="incident")
    await prune_missing(pool, _corpus(), kind="runbook")
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM knowledge_document WHERE doc_id='zz-incident'"
            )
        ).fetchone()
    assert row["n"] == 1
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM knowledge_document WHERE doc_id='zz-incident'")
