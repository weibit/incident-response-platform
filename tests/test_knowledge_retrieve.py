import os

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

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
