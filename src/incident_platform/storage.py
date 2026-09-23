"""PostgreSQL access layer shared by the gateway, worker, and workflow nodes.

Store manages application persistence:
- Ingress deduplication and outbox events awaiting publication to Kafka.
- Inbox jobs, completion tracking, retry scheduling, and dead-letter events.
- Per-incident locks that prevent workers from advancing the same incident together.
- Incident status snapshots, approval decisions, audit records, and simulated execution.

Related writes share a database transaction where needed, such as accepting an alert
and queuing its event, or recording an approval and queuing its notification event.
Kafka publishing lives in transport.py; job processing lives in worker/main.py.
LangGraph checkpoints are managed separately by AsyncPostgresSaver. The incident
snapshots stored here support status reads, not workflow recovery.
"""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from uuid import NAMESPACE_URL, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from incident_platform.models import Envelope


class Conflict(Exception):
    pass


class Store:
    def __init__(self, dsn: str):
        self.pool = AsyncConnectionPool(
            dsn, open=False, min_size=1, max_size=12, kwargs={"row_factory": dict_row}, timeout=3
        )

    async def open(self):
        await self.pool.open(wait=True)

    async def close(self):
        await self.pool.close()

    async def setup(self):
        async with self.pool.connection() as conn:
            await conn.execute(files("incident_platform").joinpath("schema.sql").read_text())

    async def enqueue(self, conn, event: Envelope, topic: str):
        await conn.execute(
            "INSERT INTO outbox(event_id,topic,incident_id,event) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                str(event.event_id),
                topic,
                str(event.incident_id),
                Jsonb(event.model_dump(mode="json")),
            ),
        )

    async def accept(self, source: str, source_event_id: str, event: Envelope):
        fingerprint = hashlib.sha256(json.dumps(event.payload, sort_keys=True).encode()).hexdigest()
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO ingress VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING event",
                    (source, source_event_id, fingerprint, Jsonb(event.model_dump(mode="json"))),
                )
            ).fetchone()
            if not row:
                previous = await (
                    await conn.execute(
                        "SELECT * FROM ingress WHERE source=%s AND source_event_id=%s",
                        (source, source_event_id),
                    )
                ).fetchone()
                if previous["fingerprint"] != fingerprint:
                    raise Conflict("Source event ID reused with a different payload")
                return previous["event"], True
            await self.enqueue(conn, event, "incident.raw")
            return row["event"], False

    async def emit(self, state: dict, kind: str, payload: dict):
        # A node can replay after a crash; deterministic event IDs suppress duplicate outbox writes.
        key = f"{state['incident_id']}:{kind}:{state.get('rounds', 0)}"
        event = Envelope(
            event_id=uuid5(NAMESPACE_URL, key),
            incident_id=state["incident_id"],
            correlation_id=state["correlation_id"],
            event_type=kind,
            source="worker",
            payload=payload,
        )
        async with self.pool.connection() as conn:
            await self.enqueue(conn, event, kind)

    async def receive(self, event: Envelope):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO inbox(event_id,incident_id,event) VALUES(%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (str(event.event_id), str(event.incident_id), Jsonb(event.model_dump(mode="json"))),
            )

    async def pending(self):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM inbox WHERE status='pending' AND next_attempt_at<=now() "
                    "ORDER BY created_at LIMIT 20"
                )
            ).fetchall()

    @asynccontextmanager
    async def incident_lock(self, incident_id: str):
        # Session lock spans graph/checkpoint operations; separate from short SQL transactions.
        async with self.pool.connection() as conn:
            await conn.set_autocommit(True)
            row = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (incident_id,)
                )
            ).fetchone()
            try:
                yield row["locked"]
            finally:
                if row["locked"]:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (incident_id,)
                    )
                await conn.set_autocommit(False)

    async def job(self, event_id):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute("SELECT * FROM inbox WHERE event_id=%s", (event_id,))
            ).fetchone()

    async def complete(self, event_id):
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE inbox SET status='done' WHERE event_id=%s", (event_id,))

    async def fail(self, row, error: Exception, max_attempts: int):
        attempts = row["attempts"] + 1
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE inbox SET attempts=%s,error=%s,status=%s,next_attempt_at=%s "
                "WHERE event_id=%s",
                (
                    attempts,
                    type(error).__name__,
                    "dead" if attempts >= max_attempts else "pending",
                    datetime.now(UTC) + timedelta(seconds=min(2**attempts, 60)),
                    row["event_id"],
                ),
            )
            if attempts >= max_attempts:
                original = Envelope.model_validate(row["event"])
                dlq = original.model_copy(
                    update={
                        "event_id": uuid5(NAMESPACE_URL, f"dlq:{row['event_id']}"),
                        "event_type": "incident.dlq",
                        "payload": {
                            "original_event_id": row["event_id"],
                            "error_type": type(error).__name__,
                            "attempts": attempts,
                        },
                    }
                )
                await self.enqueue(conn, dlq, "incident.dlq")

    async def save_state(self, state: dict):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO incidents(incident_id,correlation_id,state) VALUES(%s,%s,%s) "
                "ON CONFLICT(incident_id) DO UPDATE SET state=excluded.state,updated_at=now()",
                (state["incident_id"], state["correlation_id"], Jsonb(state)),
            )

    async def get_state(self, incident_id: str):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT state FROM incidents WHERE incident_id=%s", (incident_id,)
                )
            ).fetchone()
            return row["state"] if row else None

    async def audit(self, incident_id: str, kind: str, detail: dict):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit(incident_id,kind,detail) VALUES(%s,%s,%s)",
                (incident_id, kind, Jsonb(detail)),
            )

    async def request_approval(self, state, ttl: int):
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO approvals(plan_id,incident_id,plan,expires_at) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    state["plan"]["plan_id"],
                    state["incident_id"],
                    Jsonb(state["plan"]),
                    datetime.now(UTC) + timedelta(seconds=ttl),
                ),
            )

    async def decide(self, incident_id: str, plan_id: str, approved: bool, actor: str):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT *,expires_at>now() AS fresh FROM approvals WHERE plan_id=%s "
                    "AND incident_id=%s FOR UPDATE",
                    (plan_id, incident_id),
                )
            ).fetchone()
            decision = {"plan_id": plan_id, "approved": approved, "actor": actor}
            if not row or not row["fresh"]:
                raise Conflict("Approval missing or expired")
            if row["decision"]:
                if row["decision"] != decision:
                    raise Conflict("Plan already has a decision")
                return
            await conn.execute(
                "UPDATE approvals SET decision=%s WHERE plan_id=%s", (Jsonb(decision), plan_id)
            )
            event = Envelope(
                event_id=uuid5(NAMESPACE_URL, f"approval:{plan_id}"),
                incident_id=incident_id,
                correlation_id=incident_id,
                event_type="incident.approval",
                source="gateway",
                payload=decision,
            )
            await self.enqueue(conn, event, "incident.approval")
            await conn.execute(
                "INSERT INTO audit(incident_id,kind,detail) VALUES(%s,%s,%s)",
                (incident_id, "approval.decided", Jsonb(decision)),
            )

    async def approved(self, incident_id: str, plan_id: str):
        async with self.pool.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM approvals WHERE incident_id=%s AND plan_id=%s "
                    "AND expires_at>now()",
                    (incident_id, plan_id),
                )
            ).fetchone()

    async def simulate_execution(self, state):
        result = {"simulated": True, "success": True, "plan_id": state["plan"]["plan_id"]}
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO executions(plan_id,incident_id,result) VALUES(%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (state["plan"]["plan_id"], state["incident_id"], Jsonb(result)),
            )
            row = await (
                await conn.execute(
                    "SELECT result FROM executions WHERE plan_id=%s", (state["plan"]["plan_id"],)
                )
            ).fetchone()
            return row["result"]
