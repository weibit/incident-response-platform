import hmac
from contextlib import asynccontextmanager
from dataclasses import asdict

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from incident_platform.config import settings
from incident_platform.knowledge.embeddings import provider
from incident_platform.knowledge.retrieve import search

cfg = settings()
pool = AsyncConnectionPool(
    cfg.database_url,
    open=False,
    min_size=1,
    max_size=8,
    kwargs={"row_factory": dict_row},
    timeout=3,
)
embedder = provider(cfg)


@asynccontextmanager
async def lifespan(_server):
    await pool.open(wait=True)
    try:
        yield None
    finally:
        await pool.close()


mcp = MCPServer("incident-runbooks", lifespan=lifespan)


@mcp.tool()
async def search_runbooks(service: str, query: str, limit: int = 5) -> list[dict]:
    """Search operational runbooks scoped to a service. Read-only."""
    found = await search(
        pool, kind="runbook", service=service, query=query, limit=limit, embedder=embedder
    )
    return [asdict(snippet) for snippet in found]


@mcp.tool()
async def search_similar_incidents(service: str, query: str, limit: int = 5) -> list[dict]:
    """Search resolved prior incidents scoped to a service. Read-only."""
    found = await search(
        pool, kind="incident", service=service, query=query, limit=limit, embedder=embedder
    )
    return [asdict(snippet) for snippet in found]


def bearer_auth(app, token: str):
    """Minimal ASGI guard. The server publishes no host port; this is defence in depth."""
    expected = f"Bearer {token}"

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(supplied, expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)

    return guarded


def build_app():
    token = cfg.knowledge_mcp_token.get_secret_value()
    if not token:
        raise RuntimeError("knowledge_mcp_token must be set to run the knowledge server")
    # DNS-rebinding protection is on by default and validates the Host header. The
    # worker reaches this service by its Compose hostname, so that must be allowed.
    security = TransportSecuritySettings(allowed_hosts=cfg.knowledge_allowed_hosts)
    return bearer_auth(
        mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security),
        token,
    )


if __name__ == "__main__":
    # No lifespan="off": the connection pool opens in the MCPServer lifespan.
    uvicorn.run(build_app(), host="0.0.0.0", port=8080)
