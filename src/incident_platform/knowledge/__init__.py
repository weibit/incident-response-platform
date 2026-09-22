from contextlib import asynccontextmanager


@asynccontextmanager
async def mcp_client(cfg):
    """One long-lived MCP client for the worker's lifetime, not one per tool call."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = cfg.knowledge_mcp_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=20.0) as http:
        async with streamable_http_client(cfg.knowledge_mcp_url, http_client=http) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                yield _Session(session)


class _Session:
    """Adapt the MCP session to the small call_tool contract the backend expects."""

    def __init__(self, session):
        self.session = session

    async def call_tool(self, name, arguments) -> list[dict]:
        result = await self.session.call_tool(name, arguments)
        if result.is_error:
            # An error carries structured_content=None. Returning [] for it would reach
            # the analyst as "no matching documents" - a corpus that looks empty rather
            # than a retrieval that broke. Raise so the node fails visibly instead.
            raise RuntimeError(f"knowledge tool {name} failed: {result.content}")
        # mcp 2.2.0 spells this structured_content and wraps a list return under
        # "result". Reading a camelCase structuredContent finds nothing and degrades
        # silently to an empty corpus, which is why this is asserted by a test.
        content = result.structured_content or {}
        rows = content.get("result", []) if isinstance(content, dict) else content
        return rows if isinstance(rows, list) else []
