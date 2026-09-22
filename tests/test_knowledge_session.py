import pytest

from incident_platform.knowledge import _Session

SNIPPET = {
    "chunk_id": "c1",
    "doc_id": "d1",
    "title": "t",
    "heading": "h",
    "body": "b",
    "source": "s",
}


class FakeResult:
    def __init__(self, structured_content, is_error=False, content="boom"):
        self.structured_content, self.is_error, self.content = structured_content, is_error, content


class FakeSession:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


async def test_unwraps_the_result_key_into_a_list():
    # mcp 2.2.0 returns {"result": [...]} for a list-returning tool, not the bare list.
    session = _Session(FakeSession(FakeResult({"result": [SNIPPET]})))
    assert await session.call_tool("search_runbooks", {"service": "checkout"}) == [SNIPPET]


async def test_empty_corpus_is_an_empty_list():
    session = _Session(FakeSession(FakeResult({"result": []})))
    assert await session.call_tool("search_similar_incidents", {}) == []


async def test_a_failed_tool_call_raises_instead_of_looking_empty():
    session = _Session(FakeSession(FakeResult(None, is_error=True)))
    with pytest.raises(RuntimeError):
        await session.call_tool("search_runbooks", {})
