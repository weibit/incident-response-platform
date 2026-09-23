"""Kafka topic definitions and PostgreSQL outbox publication.

Lock an unsent outbox row, publish its envelope with incident_id as the Kafka key,
and record sent_at after acknowledgment. Row locks coordinate concurrent relays.
A crash between publication and database commit can cause duplicates, so consumers
must deduplicate event IDs. Inbox processing belongs to worker/main.py.
"""

import asyncio
import json

import structlog
from aiokafka import AIOKafkaProducer

log = structlog.get_logger()
TOPICS = [
    "incident.raw",
    "incident.created",
    "incident.triaged",
    "incident.rca_completed",
    "incident.mitigation_proposed",
    "incident.mitigated",
    "incident.resolved",
    "incident.approval",
    "incident.dlq",
    "incident.manual_review",
]


async def publish_batch(store, producer):
    # Lock one row at a time. A crash after send and before marking sent yields a duplicate,
    # handled by inbox event_id uniqueness. Never claim distributed exactly-once semantics.
    async with store.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY created_at "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        ).fetchone()
        if not row:
            return False
        await asyncio.wait_for(
            producer.send_and_wait(
                row["topic"], json.dumps(row["event"]).encode(), key=row["incident_id"].encode()
            ),
            10,
        )
        await conn.execute("UPDATE outbox SET sent_at=now() WHERE event_id=%s", (row["event_id"],))
        log.info(
            "event.published",
            event_id=row["event_id"],
            incident_id=row["incident_id"],
            correlation_id=row["event"]["correlation_id"],
            topic=row["topic"],
        )
        return True


async def relay_forever(store, cfg):
    while True:
        producer = AIOKafkaProducer(
            bootstrap_servers=cfg.kafka_bootstrap_servers,
            enable_idempotence=True,
            acks="all",
            request_timeout_ms=10000,
        )
        try:
            await producer.start()
            while True:
                if not await publish_batch(store, producer):
                    await asyncio.sleep(0.2)
        except Exception as exc:
            log.warning("outbox.retry", error_type=type(exc).__name__)
            await asyncio.sleep(2)
        finally:
            await producer.stop()
