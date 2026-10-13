"""Exercise actual MCP sessions, without Chrome or Grok network access."""

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CancelledNotification, CancelledNotificationParams, ClientNotification

from grokweb.errors import GrokError
from grokweb.server import create_server


class FakeClient:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def search(self, query, kind, model):
        self.calls.append((query, kind, model))
        if query == "expired":
            raise GrokError("auth_required")
        if query == "unexpected":
            raise RuntimeError("SYNTHETIC-SECRET-MUST-NOT-LEAK")
        if query == "wait":
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        return {"answer": "Result", "citations": [], "sources": []}


async def test_mcp_tools_schema_and_structured_results():
    client = FakeClient()
    async with create_connected_server_and_client_session(create_server(client)) as session:
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == {
            "grok_web_search",
            "grok_x_search",
        }
        for tool in listed.tools:
            assert tool.annotations.readOnlyHint is True
            assert tool.inputSchema["required"] == ["query"]
            assert set(tool.inputSchema["properties"]) == {"query", "model"}
        result = await session.call_tool("grok_x_search", {"query": "NASA", "model": "auto"})
        assert not result.isError
        assert result.structuredContent["answer"] == "Result"
        assert json.loads(result.content[0].text) == result.structuredContent
        assert client.calls == [("NASA", "x", "auto")]


@pytest.mark.parametrize(
    "query,code", [("expired", "auth_required"), ("unexpected", "internal_error")]
)
async def test_tool_error_is_safe_and_session_survives(query, code, caplog):
    async with create_connected_server_and_client_session(create_server(FakeClient())) as session:
        result = await session.call_tool("grok_web_search", {"query": query})
        assert result.isError
        assert code in result.content[0].text
        assert "SYNTHETIC-SECRET" not in result.model_dump_json() + caplog.text
        assert not (await session.call_tool("grok_web_search", {"query": "success"})).isError


async def test_mcp_invalid_arguments_do_not_execute_search():
    client = FakeClient()
    async with create_connected_server_and_client_session(create_server(client)) as session:
        result = await session.call_tool("grok_web_search", {})
        assert result.isError
        assert not client.calls
        assert not (await session.call_tool("grok_web_search", {"query": "success"})).isError


async def test_mcp_cancellation_reaches_search_and_keeps_session_alive():
    client = FakeClient()
    async with create_connected_server_and_client_session(create_server(client)) as session:
        request_id = session._request_id
        pending = asyncio.create_task(session.call_tool("grok_x_search", {"query": "wait"}))
        try:
            await asyncio.wait_for(client.started.wait(), 2)
            await session.send_notification(
                ClientNotification(
                    root=CancelledNotification(
                        method="notifications/cancelled",
                        params=CancelledNotificationParams(
                            requestId=request_id, reason="User cancelled"
                        ),
                    )
                )
            )
            await asyncio.wait_for(client.cancelled.wait(), 2)
            assert not (await session.call_tool("grok_web_search", {"query": "success"})).isError
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_real_stdio_startup_and_errors_without_credentials(tmp_path):
    # A missing profile is deliberately selected, so this cannot access real cookies.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "grokweb", "--profile", "GrokWeb Synthetic Missing Profile", "serve"],
    )
    with (tmp_path / "stderr").open("w+") as stderr:
        async with stdio_client(params, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info.serverInfo.name == "GrokWeb"
                assert len((await session.list_tools()).tools) == 2
                error = await session.call_tool("grok_web_search", {"query": "test"})
                assert error.isError and "profile_missing" in error.content[0].text
                assert len((await session.list_tools()).tools) == 2
        stderr.seek(0)
        assert "Traceback" not in stderr.read()
