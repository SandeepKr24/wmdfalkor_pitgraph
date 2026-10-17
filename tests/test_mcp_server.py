"""MCP server tests: tools are listed, return full results, and reject bad input
with a message the model can act on. Uses an in-process client (no subprocess).

Run: pytest tests/test_mcp_server.py
"""

import asyncio

from mcp import Client

from pitgraph.mcp_server.server import server

EXPECTED_TOOLS = {
    "list_circuits", "winning_strategies", "strategy_outcomes", "similar_circuits",
    "neutralisation_impact", "recall_insights", "trace", "strategy_clusters",
    "key_races", "strategy_path",
}


def call(tool: str, args: dict):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(run())


def test_lists_every_graph_tool():
    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    names = {t.name for t in asyncio.run(run()).tools}
    assert names == EXPECTED_TOOLS


def test_tool_returns_all_rows_as_structured_content():
    result = call("key_races", {"circuit": "monza"})

    assert not result.is_error
    assert len(result.structured_content["result"]) == 12


def test_unknown_circuit_tells_the_model_how_to_recover():
    result = call("winning_strategies", {"circuit": "atlantis"})

    assert result.is_error
    assert "list_circuits" in result.content[0].text


def test_out_of_range_argument_is_rejected():
    result = call("strategy_path", {"from_circuit": "monza", "to_circuit": "spa", "max_len": 99})

    assert result.is_error
    assert "max_len" in result.content[0].text


def test_invalid_user_id_cannot_reach_another_graph():
    result = call("recall_insights", {"user_id": "demo}) MATCH (n", "ref_ids": ["monza"]})

    assert result.is_error
    assert "Invalid user_id" in result.content[0].text
