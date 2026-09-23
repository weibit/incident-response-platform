import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from incident_platform.knowledge.chunker import chunk_markdown
from incident_platform.knowledge.index import (
    embed_corpus,
    ingest_documents,
    ingest_resolved_incidents,
    prune_missing,
)
from incident_platform.knowledge.retrieve import MAX_SNIPPETS, dense_ids, search
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


async def test_finds_a_runbook_by_topic(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    snippets = await search(pool, kind="runbook", service="checkout", query="rollback release")
    assert snippets
    assert any(s.doc_id == "checkout-rollback" for s in snippets)
    assert all(s.body for s in snippets)


async def test_scopes_to_the_service_but_keeps_global_documents(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    snippets = await search(pool, kind="runbook", service="payments", query="escalate approval")
    doc_ids = {s.doc_id for s in snippets}
    assert "oncall-escalation" in doc_ids  # global: no service in frontmatter
    assert "checkout-rollback" not in doc_ids  # scoped to checkout


async def test_returns_empty_for_a_nonmatching_query(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    assert await search(pool, kind="runbook", service="checkout", query="zqxjkv") == []


async def test_respects_the_snippet_cap(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    snippets = await search(
        pool,
        kind="runbook",
        service="checkout",
        query="release rollback timeout approval",
        limit=99,
    )
    # This query matches more chunks than the cap, so the assertion is exact rather
    # than vacuous. An empty result would satisfy "<= MAX_SNIPPETS" and prove nothing.
    assert len(snippets) == MAX_SNIPPETS


async def test_a_multi_word_query_ors_its_terms(pool):
    # ANDing the terms would return nothing here: no single chunk contains all of
    # them. An investigator's query is a description, not a conjunction.
    await ingest_documents(pool, _corpus(), kind="runbook")
    snippets = await search(
        pool, kind="runbook", service="checkout", query="checkout error rate spike after release"
    )
    assert snippets
    assert snippets[0].doc_id == "checkout-rollback"


async def test_a_query_with_no_usable_terms_returns_empty(pool):
    # Empty, punctuation-only and stopword-only queries must degrade to no results
    # rather than raising a tsquery syntax error.
    await ingest_documents(pool, _corpus(), kind="runbook")
    for query in ("", "   ", "!!!", "the a of"):
        assert await search(pool, kind="runbook", service="checkout", query=query) == []


async def test_dense_leg_is_skipped_without_a_provider(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")
    assert await dense_ids(pool, None, kind="runbook", service="checkout", query="rollback") == []


async def test_dense_leg_returns_ids_with_a_stub_provider(pool):
    await ingest_documents(pool, _corpus(), kind="runbook")

    class StubEmbedder:
        dim = 3

        async def embed(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

        async def embed_query(self, text):
            return [0.1, 0.2, 0.3]

    embedder = StubEmbedder()
    await embed_corpus(pool, embedder)
    ids = await dense_ids(pool, embedder, kind="runbook", service="checkout", query="rollback")
    assert ids


async def test_indexes_only_resolved_incidents(pool):
    resolved, active = str(uuid4()), str(uuid4())
    async with pool.connection() as conn:
        for identifier, status in ((resolved, "resolved"), (active, "investigating")):
            await conn.execute(
                "INSERT INTO incidents(incident_id, correlation_id, state) VALUES(%s,%s,%s) "
                "ON CONFLICT (incident_id) DO UPDATE SET state=EXCLUDED.state",
                (
                    identifier,
                    identifier,
                    json.dumps(
                        {
                            "status": status,
                            "incident": {"service": "checkout", "summary": "Checkout timing out"},
                            "rca": {"hypothesis": "Release v2 timeout regression"},
                        }
                    ),
                ),
            )
    count = await ingest_resolved_incidents(pool)
    assert count >= 1
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT doc_id FROM knowledge_document WHERE kind='incident' AND doc_id = ANY(%s)",
                ([resolved, active],),
            )
        ).fetchall()
    found = {row["doc_id"] for row in rows}
    assert resolved in found
    assert active not in found


async def test_similar_incident_search_finds_the_rca(pool):
    identifier = str(uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO incidents(incident_id, correlation_id, state) VALUES(%s,%s,%s) "
            "ON CONFLICT (incident_id) DO UPDATE SET state=EXCLUDED.state",
            (
                identifier,
                identifier,
                json.dumps(
                    {
                        "status": "resolved",
                        "incident": {"service": "checkout", "summary": "Checkout timing out"},
                        "rca": {"hypothesis": "Release v2 timeout regression"},
                    }
                ),
            ),
        )
    await ingest_resolved_incidents(pool)
    snippets = await search(pool, kind="incident", service="checkout", query="timeout regression")
    assert any("regression" in s.body for s in snippets)


async def test_reindexing_the_existing_corpus_keeps_every_chunk_id(pool):
    # The proof that generalising ingestion did not disturb replay stability. Evidence ids
    # hash the chunk ids they cite, so a churned id silently invalidates a checkpointed
    # citation.
    from incident_platform.knowledge.index import by_kind

    await ingest_documents(pool, by_kind()["runbook"], kind="runbook")
    async with pool.connection() as conn:
        before = await (
            await conn.execute(
                "SELECT chunk_id FROM knowledge_chunk c JOIN knowledge_document d "
                "USING(doc_id) WHERE d.kind='runbook' ORDER BY chunk_id"
            )
        ).fetchall()
    await ingest_documents(pool, by_kind()["runbook"], kind="runbook")
    async with pool.connection() as conn:
        after = await (
            await conn.execute(
                "SELECT chunk_id FROM knowledge_chunk c JOIN knowledge_document d "
                "USING(doc_id) WHERE d.kind='runbook' ORDER BY chunk_id"
            )
        ).fetchall()
    assert [r["chunk_id"] for r in before] == [r["chunk_id"] for r in after]
    assert len(before) == 10
