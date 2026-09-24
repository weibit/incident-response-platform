"""Connect durable incoming events to the incident workflow.

Run three concurrent loops: Kafka intake into the inbox, inbox job processing, and
outbox publication. Offsets advance after durable receipt. The job loop runs one graph
invocation at a time per process, using incident locks and checkpoints for recovery.
Approval interrupts release the worker; additional processes provide job concurrency.
"""

import asyncio
import hashlib
from contextlib import AsyncExitStack
from uuid import NAMESPACE_URL, uuid5

import structlog
from aiokafka import AIOKafkaConsumer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from incident_platform.config import settings
from incident_platform.integrations.knowledge_backend import KnowledgeEvidenceBackend
from incident_platform.integrations.llm import Analyst
from incident_platform.integrations.tools import MockEvidenceBackend, UnconfiguredEvidenceBackend
from incident_platform.knowledge import mcp_client
from incident_platform.models import Envelope
from incident_platform.observability import configure_logging, graph_config, init_tracing
from incident_platform.storage import Store
from incident_platform.transport import relay_forever
from incident_platform.workflow.graph import build_graph

log = structlog.get_logger()


async def ingest(store, cfg):
    while True:
        consumer = AIOKafkaConsumer(
            "incident.raw",
            "incident.approval",
            bootstrap_servers=cfg.kafka_bootstrap_servers,
            group_id="incident-worker-v1",
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_poll_records=1,
        )
        try:
            await consumer.start()
            async for message in consumer:
                try:
                    event = Envelope.model_validate_json(message.value)
                    if message.key != str(event.incident_id).encode():
                        raise ValueError("Kafka key differs from incident ID")
                    expected = {
                        "incident.raw": "incident.created",
                        "incident.approval": "incident.approval",
                    }
                    if event.event_type != expected[message.topic]:
                        raise ValueError("Event type does not match input topic")
                except ValueError as exc:
                    # Do not include potentially sensitive raw payloads in logs or the DLQ.
                    key = f"{message.topic}:{message.partition}:{message.offset}"
                    identifier = uuid5(NAMESPACE_URL, key)
                    dlq = Envelope(
                        event_id=identifier,
                        incident_id=identifier,
                        correlation_id=identifier,
                        event_type="incident.dlq",
                        source="worker",
                        payload={
                            "location": key,
                            "error_type": type(exc).__name__,
                            "sha256": hashlib.sha256(message.value or b"").hexdigest(),
                        },
                    )
                    async with store.pool.connection() as conn:
                        await store.enqueue(conn, dlq, "incident.dlq")
                else:
                    await store.receive(event)
                # Commit only after durable inbox/DLQ insertion, never after graph execution.
                await consumer.commit()
        except Exception as exc:
            log.warning("consumer.retry", error_type=type(exc).__name__)
            await asyncio.sleep(2)
        finally:
            await consumer.stop()


async def process_event(store, graph, cfg, event: Envelope):
    incident_id, correlation_id = str(event.incident_id), str(event.correlation_id)
    config = graph_config(incident_id, correlation_id, str(event.event_id), cfg.langfuse_enabled)
    snapshot = await graph.aget_state(config)
    interrupted = any(task.interrupts for task in snapshot.tasks)
    if event.event_type == "incident.approval":
        if not snapshot.values:
            raise RuntimeError("Incident checkpoint not yet available")
        expected_plan = snapshot.values.get("plan", {}).get("plan_id")
        if expected_plan != event.payload.get("plan_id"):
            await store.audit(incident_id, "approval.stale", {"event_id": str(event.event_id)})
            return
        row = await store.approved(incident_id, expected_plan)
        if not row or row["decision"] != event.payload:
            await store.audit(incident_id, "approval.invalid", {"event_id": str(event.event_id)})
            return
        if interrupted:
            arg = Command(resume=row["decision"])
        elif snapshot.next:
            arg = None  # Resume failed execution after the approval node already completed.
        else:
            arg = None
    elif snapshot.values:
        if snapshot.values.get("input_event_id") != str(event.event_id):
            # Never overwrite a suspended plan with a later webhook. Retain updates for operators.
            await store.audit(
                incident_id, "incident.followup_recorded", event.model_dump(mode="json")
            )
            return
        arg = None  # Recover from last successful checkpoint on transport/job redelivery.
    else:
        arg = {
            "incident_id": incident_id,
            "correlation_id": correlation_id,
            "raw": event.payload,
            "input_event_id": str(event.event_id),
        }
    if not snapshot.values or (snapshot.next and (not interrupted or isinstance(arg, Command))):
        await graph.ainvoke(arg, config, durability="sync")
    snapshot = await graph.aget_state(config)
    state = dict(snapshot.values)
    if any(task.interrupts for task in snapshot.tasks):
        state["status"] = "awaiting_approval"
    await store.save_state(state)
    log.info(
        "incident.processed",
        incident_id=incident_id,
        correlation_id=correlation_id,
        event_id=str(event.event_id),
        status=state.get("status"),
    )


async def run_jobs(store, graph, cfg):
    while True:
        rows = await store.pending()
        for candidate in rows:
            async with store.incident_lock(candidate["incident_id"]) as locked:
                if not locked:
                    continue
                row = await store.job(candidate["event_id"])
                if row["status"] != "pending":
                    continue
                try:
                    async with asyncio.timeout(cfg.job_timeout_seconds):
                        await process_event(
                            store, graph, cfg, Envelope.model_validate(row["event"])
                        )
                    await store.complete(row["event_id"])
                except Exception as exc:
                    log.warning(
                        "incident.retry",
                        incident_id=row["incident_id"],
                        event_id=row["event_id"],
                        error_type=type(exc).__name__,
                    )
                    await store.fail(row, exc, cfg.max_attempts)
        await asyncio.sleep(0.5)


async def main():
    configure_logging()
    cfg = settings()
    init_tracing(cfg)
    store = Store(cfg.database_url)
    await store.open()
    try:
        async with AsyncPostgresSaver.from_conn_string(cfg.database_url) as saver:
            backend = MockEvidenceBackend() if cfg.mock_mode else UnconfiguredEvidenceBackend()
            async with AsyncExitStack() as stack:
                if cfg.knowledge_enabled:
                    client = await stack.enter_async_context(mcp_client(cfg))
                    backend = KnowledgeEvidenceBackend(backend, client, cfg)
                graph = build_graph(cfg, store, Analyst(cfg), backend, saver)
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(ingest(store, cfg))
                    tasks.create_task(run_jobs(store, graph, cfg))
                    tasks.create_task(relay_forever(store, cfg))
    finally:
        await store.close()
        if cfg.langfuse_enabled:
            from langfuse import get_client

            get_client().flush()


if __name__ == "__main__":
    asyncio.run(main())
