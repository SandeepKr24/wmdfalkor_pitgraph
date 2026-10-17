"""Connects the agents to the PitGraph MCP server and calls its tools.

The server runs as a subprocess over stdio, exactly as any MCP client would use it.
This module also:
  - converts MCP tool definitions into Claude tool definitions (race tools only:
    memory tools are never offered to the model)
  - fills in user_id for memory tools from the session
  - logs every call as a QueryRun in the user's memory graph
"""

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment

from pitgraph.agents import memory
from pitgraph.graph.db import REQUIRED_ENV_VARS

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Tools that read a user's memory: user_id comes from the session, not the model.
USER_SCOPED_TOOLS = {"recall_insights", "trace"}


@asynccontextmanager
async def connect():
    """Start the MCP server subprocess and yield a connected client."""
    # The MCP SDK gives the subprocess only a minimal safe environment, so pass
    # the FalkorDB credentials on explicitly (in Docker there is no .env to fall back on).
    env = get_default_environment()
    env.update({name: os.environ[name] for name in REQUIRED_ENV_VARS if name in os.environ})
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pitgraph.mcp_server.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    async with Client(params) as client:
        yield client


@dataclass
class ToolCall:
    tool: str
    params: dict
    ok: bool
    rows: list | dict | None
    error: str | None
    qr_id: str


@dataclass
class ToolBox:
    """The MCP tools available to one agent run, bound to one user."""

    client: Client
    user_id: str
    calls: list[ToolCall] = field(default_factory=list)

    async def claude_tools(self) -> list[dict]:
        """The race tools as Claude tool definitions. Memory tools are left out:
        only the agent code calls them, with the session's user_id."""
        listed = await self.client.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed.tools
            if t.name not in USER_SCOPED_TOOLS
        ]

    async def call(self, tool: str, params: dict) -> ToolCall:
        """Call a tool over MCP and log it as a QueryRun (successful or not)."""
        args = dict(params)
        if tool in USER_SCOPED_TOOLS:
            args["user_id"] = self.user_id  # always the session's user

        result = await self.client.call_tool(tool, args)
        if result.is_error:
            error = result.content[0].text if result.content else "tool error"
            rows, ok = None, False
        else:
            structured = result.structured_content or {}
            rows, ok, error = structured.get("result", structured), True, None

        row_count = len(rows) if isinstance(rows, list) else (1 if rows else 0)
        qr_id = memory.add_query_run(
            memory.memory_graph(self.user_id), tool, params, row_count, ok, error
        )
        call = ToolCall(tool, params, ok, rows, error, qr_id)
        self.calls.append(call)
        return call
