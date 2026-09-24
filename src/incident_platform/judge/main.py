"""Score completed incidents from terminal domain events.

Runs as its own service with its own consumer group, so judging never touches the incident
path. Scores attach to the incident's first trace, because a score naming only a session is
silently dropped by the server. Verdicts are observational: nothing here changes an incident.
"""

import asyncio

import structlog
from aiokafka import AIOKafkaConsumer

from incident_platform.config import settings
from incident_platform.integrations.evaluation import record_score
from incident_platform.integrations.llm import Judge
from incident_platform.judge.scoring import (
    Score,
    cited_evidence,
    deterministic_scores,
    score_id_for,
    verdict_value,
)
from incident_platform.models import Envelope
from incident_platform.storage import Store
from incident_platform.telemetry import configure_logging, init_tracing, trace_id_for

log = structlog.get_logger()
TERMINAL_TOPICS = ("incident.resolved", "incident.manual_review")


async def judge_incident(store, judge, incident_id: str):
    state = await store.get_state(incident_id)
    if not state:
        # The outbox can outrun a state write; nothing to score, and nothing to retry.
        await store.audit(incident_id, "judge.state_missing", {})
        return
    scores = deterministic_scores(state)
    rca = state.get("rca") or {}
    verdict = None
    if rca:
        try:
            verdict = await judge.faithfulness(
                rca.get("hypothesis", ""), rca.get("confidence", 0.0), cited_evidence(state)
            )
            scores.append(
                Score(
                    "rca_faithfulness",
                    verdict_value(verdict.label),
                    "NUMERIC",
                    f"{verdict.label}: {verdict.reason}",
                )
            )
        except Exception as exc:
            # Losing four free scores because one model call failed would be the wrong trade.
            # A KeyError here means Verdict's labels and VERDICT_VALUES have drifted apart,
            # not that the gateway is down.
            log.warning("judge.unavailable", incident_id=incident_id, error_type=type(exc).__name__)
    event_id = state.get("input_event_id")
    if event_id:
        trace_id = trace_id_for(incident_id, event_id)
        for score in scores:
            record_score(
                trace_id=trace_id,
                name=score.name,
                value=score.value,
                comment=score.comment,
                score_id=score_id_for(incident_id, score.name),
                data_type=score.data_type,
            )
    else:
        log.warning("judge.untraceable", incident_id=incident_id)
    await store.audit(
        incident_id,
        "judge.verdict",
        {
            "scores": {score.name: score.value for score in scores},
            "faithfulness": verdict.label if verdict else None,
            "reason": verdict.reason if verdict else None,
            "rca_present": bool(rca),
        },
    )


async def consume(store, judge, cfg):
    while True:
        consumer = AIOKafkaConsumer(
            *TERMINAL_TOPICS,
            bootstrap_servers=cfg.kafka_bootstrap_servers,
            group_id="incident-judge-v1",
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_poll_records=1,
        )
        try:
            await consumer.start()
            async for message in consumer:
                event = Envelope.model_validate_json(message.value)
                await judge_incident(store, judge, str(event.incident_id))
                # Commit last: a redelivery overwrites the same scores by their derived ids.
                await consumer.commit()
        except Exception as exc:
            log.warning("judge.retry", error_type=type(exc).__name__)
            await asyncio.sleep(2)
        finally:
            await consumer.stop()


async def main():
    configure_logging()
    cfg = settings()
    init_tracing(cfg)
    store = Store(cfg.database_url)
    await store.open()
    try:
        await consume(store, Judge(cfg), cfg)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
