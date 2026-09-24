"""FastAPI entry point for incident webhooks, approval callbacks, and status reads.

Authenticate and validate requests, then use Store to persist alerts or approval
decisions before acknowledgment. The lifespan manages the database pool and outbox
relay. Investigation runs in separate workers; acceptance does not mean completion.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from incident_platform.config import settings
from incident_platform.gateway.auth import bearer, signed
from incident_platform.models import Approval, Envelope, IncidentInput
from incident_platform.observability import configure_logging
from incident_platform.storage import Conflict, Store
from incident_platform.transport import relay_forever


def create_app(config=None, store=None, relay=True):
    @asynccontextmanager
    async def lifespan(app):
        configure_logging()
        cfg = config or settings()
        db = store or Store(cfg.database_url)
        app.state.config, app.state.store = cfg, db
        await db.open()
        task = asyncio.create_task(relay_forever(db, cfg)) if relay else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await db.close()

    app = FastAPI(title="Incident Event Gateway", lifespan=lifespan)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def validation_handler(request, exc):
        return JSONResponse(status_code=422, content={"detail": "Invalid payload schema"})

    async def body_and_auth(request: Request, source: str, approval=False):
        cfg = request.app.state.config
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > cfg.max_body_bytes:
                raise HTTPException(413, "Body too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        if source == "jira":
            token = cfg.jira_approval_token if approval else cfg.jira_webhook_token
            bearer(request.headers.get("authorization"), token.get_secret_value())
        elif source in {"slack", "siem"}:
            slack = source == "slack"
            signed(
                body,
                request.headers.get("x-slack-request-timestamp" if slack else "x-timestamp"),
                request.headers.get("x-slack-signature" if slack else "x-signature"),
                (cfg.slack_signing_secret if slack else cfg.siem_signing_secret).get_secret_value(),
                slack=slack,
            )
        else:
            raise HTTPException(404, "Unknown source")
        try:
            if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
                data = json.loads(parse_qs(body.decode())["payload"][0])
            else:
                data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            return data
        except (ValueError, KeyError, IndexError, UnicodeError):
            raise HTTPException(422, "Invalid JSON or Slack form payload") from None

    @app.get("/health/live")
    async def health():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request):
        async with request.app.state.store.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return {"status": "ready", "acceptance": "postgres-outbox"}

    @app.post("/webhooks/{source}", status_code=202)
    async def webhook(source: str, request: Request):
        data = await body_and_auth(request, source)
        if source == "slack" and data.get("type") == "url_verification":
            if not isinstance(data.get("challenge"), str):
                raise HTTPException(422, "Missing challenge")
            return {"challenge": data["challenge"]}
        # Jira Automation and Slack adapters deliver the small documented contract.
        # Slack Events API wraps that contract under event.incident.
        payload = data
        if source == "slack" and data.get("type") == "event_callback":
            if not isinstance(data.get("event"), dict):
                raise HTTPException(422, "Expected Slack event object")
            payload = data["event"].get("incident", {})
            if not isinstance(payload, dict):
                raise HTTPException(422, "Expected incident object")
            payload = {**payload, "source_event_id": data.get("event_id", "")}
        incident = IncidentInput.model_validate(payload)
        incident_id = incident.incident_id or uuid5(
            NAMESPACE_URL, f"{source}:{incident.external_incident_id}"
        )
        event = Envelope(
            incident_id=incident_id,
            correlation_id=incident_id,
            event_type="incident.created",
            source=source,
            payload=incident.model_dump(mode="json"),
        )
        accepted, duplicate = await request.app.state.store.accept(
            source, incident.source_event_id, event
        )
        return {
            "status": "accepted",
            "durability": "postgres-outbox",
            "duplicate": duplicate,
            "incident_id": accepted["incident_id"],
            "event_id": accepted["event_id"],
            "correlation_id": accepted["correlation_id"],
        }

    @app.post("/callbacks/{source}/approvals/{incident_id}", status_code=202)
    async def approve(source: str, incident_id: UUID, request: Request):
        if source not in {"jira", "slack"}:
            raise HTTPException(404, "Unknown approval source")
        data = await body_and_auth(request, source, approval=True)
        cfg = request.app.state.config
        if source == "slack":
            if not isinstance(data.get("user"), dict):
                raise HTTPException(422, "Expected Slack user object")
            actor = data.get("user", {}).get("id")
            if actor not in cfg.slack_approvers:
                raise HTTPException(403, "Actor is not an approver")
            try:
                decision = Approval.model_validate_json(data["actions"][0]["value"])
            except (KeyError, IndexError, TypeError):
                raise HTTPException(422, "Expected Slack block action") from None
        else:
            # Dedicated credential represents trusted Jira Automation, never payload-supplied actor.
            actor = "jira-automation"
            decision = Approval.model_validate(data)
        await request.app.state.store.decide(
            str(incident_id), decision.plan_id, decision.approved, actor
        )
        return {"status": "accepted"}

    @app.get("/incidents/{incident_id}")
    async def status(incident_id: UUID, request: Request):
        bearer(
            request.headers.get("authorization"),
            request.app.state.config.status_token.get_secret_value(),
        )
        state = await request.app.state.store.get_state(str(incident_id))
        if state is None:
            raise HTTPException(404, "Not processed yet")
        return state

    return app


app = create_app()
