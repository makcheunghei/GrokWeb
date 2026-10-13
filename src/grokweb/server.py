"""Web and X search over stdio MCP."""

import json
from collections.abc import Awaitable

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from grokweb.client import GrokClient
from grokweb.errors import GrokError


async def tool_result(operation: Awaitable[dict]) -> CallToolResult:
    try:
        result = await operation
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result,
        )
    except Exception as exc:
        safe = exc if isinstance(exc, GrokError) else GrokError("internal_error")
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=str(safe))],
        )


def create_server(client: GrokClient | None = None) -> FastMCP:
    client = client or GrokClient()
    server = FastMCP(
        "GrokWeb",
        log_level="ERROR",
        instructions=(
            "Search the public web or public X posts using the user's local Chrome Grok session. "
            "Use query for date/account requirements. model is an optional website mode ID; "
            "omit it to use the current website default. Searches consume the "
            "user's Grok allowance. Do not automatically retry tool failures."
        ),
    )
    search_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )

    @server.tool(annotations=search_annotations)
    async def grok_web_search(query: str, model: str | None = None) -> CallToolResult:
        """Search the current public web with Grok; return answer, citations and sources."""
        return await tool_result(client.search(query, "web", model))

    @server.tool(annotations=search_annotations)
    async def grok_x_search(query: str, model: str | None = None) -> CallToolResult:
        """Search public X posts with Grok's native X search; return answer and post sources."""
        return await tool_result(client.search(query, "x", model))

    return server
