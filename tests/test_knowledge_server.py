import os

import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("TEST_KNOWLEDGE_MCP_URL")
TOKEN = os.environ.get("TEST_KNOWLEDGE_MCP_TOKEN", "knowledge")


@pytest.fixture
def url():
    if not URL:
        pytest.skip("TEST_KNOWLEDGE_MCP_URL is not set")
    return URL


async def test_rejects_a_missing_token(url):
    import httpx2

    async with httpx2.AsyncClient() as http:
        response = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401


async def test_lists_exactly_the_two_read_tools(url):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                tools = await session.list_tools()
    names = sorted(tool.name for tool in tools.tools)
    assert names == ["search_runbooks", "search_similar_incidents"]
