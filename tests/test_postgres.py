"""Run with TEST_DATABASE_URL pointing to a disposable database; all rows use unique IDs."""

import asyncio
import os
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from incident_platform.integrations.llm import Analyst
from incident_platform.integrations.tools import MockEvidenceBackend
from incident_platform.models import Envelope
from incident_platform.storage import Conflict, Store
from incident_platform.transport import publish_batch
from incident_platform.worker.main import process_event
from incident_platform.workflow.graph import build_graph

pytestmark = pytest.mark.integration


@pytest.fixture
async def db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    store = Store(url)
    await store.open()
    await store.setup()
    try:
        yield store
    finally:
        await store.close()


async def test_concurrent_ingress_and_durable_outbox(db, incident):
    event = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        source="jira",
        event_type="incident.created",
        payload=incident["raw"],
    )
    results = await asyncio.gather(
        *[db.accept("jira", incident["raw"]["source_event_id"], event) for _ in range(10)]
    )
    assert sum(not duplicate for _, duplicate in results) == 1
    with pytest.raises(Conflict):
        await db.accept(
            "jira",
            incident["raw"]["source_event_id"],
            event.model_copy(update={"payload": {**event.payload, "summary": "conflict"}}),
        )
    await db.receive(event)
    await db.receive(event)
    row = await db.job(str(event.event_id))
    assert row["status"] == "pending"
    await db.fail(row, TimeoutError(), 1)
    assert (await db.job(str(event.event_id)))["status"] == "dead"
    async with db.pool.connection() as conn:
        count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM outbox WHERE incident_id=%s", (incident["incident_id"],)
            )
        ).fetchone()
        assert count["n"] == 2  # raw + DLQ committed atomically with failure status


async def test_restart_checkpoint_and_approval(db, cfg, incident):
    cfg.database_url = os.environ["TEST_DATABASE_URL"]
    event = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        source="jira",
        event_type="incident.created",
        payload=incident["raw"],
    )
    async with AsyncPostgresSaver.from_conn_string(cfg.database_url) as saver:
        await saver.setup()
        graph = build_graph(cfg, db, Analyst(cfg), MockEvidenceBackend(), saver)
        await process_event(db, graph, cfg, event)
    state = await db.get_state(incident["incident_id"])
    assert state["status"] == "awaiting_approval"
    plan_id = state["plan"]["plan_id"]
    await db.decide(incident["incident_id"], plan_id, True, "jira-automation")
    with pytest.raises(Conflict):
        await db.decide(incident["incident_id"], plan_id, False, "jira-automation")
    approval = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        source="gateway",
        event_type="incident.approval",
        payload={"plan_id": plan_id, "approved": True, "actor": "jira-automation"},
    )
    async with AsyncPostgresSaver.from_conn_string(cfg.database_url) as saver:
        graph = build_graph(cfg, db, Analyst(cfg), MockEvidenceBackend(), saver)
        await process_event(db, graph, cfg, approval)
        await process_event(db, graph, cfg, approval)
    assert (await db.get_state(incident["incident_id"]))["status"] == "resolved"
    async with db.pool.connection() as conn:
        count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM executions WHERE incident_id=%s",
                (incident["incident_id"],),
            )
        ).fetchone()
        assert count["n"] == 1


async def test_advisory_lock_exclusion(db):
    identifier = str(uuid4())
    async with db.incident_lock(identifier) as first:
        assert first
        async with db.incident_lock(identifier) as second:
            assert not second
    async with db.incident_lock(identifier) as third:
        assert third


async def test_publish_failure_keeps_outbox(db, incident):
    class BrokenProducer:
        async def send_and_wait(self, *args, **kwargs):
            raise ConnectionError("offline")

    event = Envelope(
        incident_id=incident["incident_id"],
        correlation_id=incident["correlation_id"],
        source="jira",
        event_type="incident.created",
        payload=incident["raw"],
    )
    await db.accept("jira", incident["raw"]["source_event_id"], event)
    with pytest.raises(ConnectionError):
        await publish_batch(db, BrokenProducer())
    async with db.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT sent_at FROM outbox WHERE event_id=%s", (str(event.event_id),)
            )
        ).fetchone()
        assert row["sent_at"] is None
