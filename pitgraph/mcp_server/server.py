"""MCP server exposing the graph tools T1-T8 (plus T1b and list_circuits).

The agents discover and call these tools over MCP; the LLM chooses which tool
to use and with what arguments, but never writes Cypher.

Run standalone (stdio transport): `python -m pitgraph.mcp_server.server`

Security note: T4/T5 read a user's private memory graph. Their `user_id`
argument is filled in by the agent runtime from the session, never by the LLM,
so a prompt can't ask for another user's memory.
"""

from functools import lru_cache

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pitgraph.graph import queries as q
from pitgraph.graph.db import f1_graph, user_memory_graph

server = MCPServer("pitgraph")

# Bounds for numeric arguments, so a bad tool call can't ask for huge results.
MAX_TOP_N = 20
MAX_MIN_USES = 50
MAX_PATH_LEN = 6


# ---------- validation ----------
# ToolError messages reach the model, so it can correct a bad call (e.g. a wrong
# circuit ID). Any other exception is treated as a crash and its details hidden.

@lru_cache(maxsize=1)
def known_circuits() -> dict[str, str]:
    """circuit_id -> name, read once from `f1` (it's read-only at runtime)."""
    rows = q.rows(f1_graph(), "MATCH (c:Circuit) RETURN c.circuit_id AS id, c.name AS name")
    return {r["id"]: r["name"] for r in rows}


def check_circuit(circuit: str) -> str:
    if circuit not in known_circuits():
        raise ToolError(f"Unknown circuit {circuit!r}. Call list_circuits for valid IDs.")
    return circuit


def check_range(name: str, value: int, low: int, high: int) -> int:
    if not low <= value <= high:
        raise ToolError(f"{name} must be between {low} and {high}, got {value}")
    return value


def memory_for(user_id: str):
    try:
        return user_memory_graph(user_id)
    except ValueError as error:
        raise ToolError(str(error)) from error


# ---------- tools ----------

@server.tool()
def list_circuits() -> list[dict]:
    """List every circuit in the data (2023-2025) as {circuit_id, name}.
    Use it to turn a place name like "Singapore" into an ID like "marina_bay"."""
    return [{"circuit_id": cid, "name": name} for cid, name in sorted(known_circuits().items())]


@server.tool()
def winning_strategies(circuit: str, top_n: int = 3) -> list[dict]:
    """T1: tyre strategy (compound sequence and stop count) of the top-N finishers
    in every race at a circuit. Shows what worked."""
    return q.winning_strategies(
        f1_graph(), check_circuit(circuit), check_range("top_n", top_n, 1, MAX_TOP_N)
    )


@server.tool()
def strategy_outcomes(circuit: str, min_uses: int = 2) -> list[dict]:
    """T1b: every strategy used at a circuit, ranked by average positions gained
    (grid minus finish, classified finishers only). Negative gain = what to avoid.
    Cross-check with neutralisation_impact: a safety car can rescue a bad strategy."""
    return q.strategy_outcomes(
        f1_graph(), check_circuit(circuit), check_range("min_uses", min_uses, 1, MAX_MIN_USES)
    )


@server.tool()
def similar_circuits(circuit: str) -> list[dict]:
    """T2: the 3 most similar circuits, with a similarity score and the shared
    traits (basis). Use when the circuit itself has too little data."""
    return q.similar_circuits(f1_graph(), check_circuit(circuit))


@server.tool()
def neutralisation_impact(circuit: str) -> list[dict]:
    """T3: each safety car (SC), virtual safety car (VSC) and red flag at a circuit,
    with how many drivers pitted during it."""
    return q.neutralisation_impact(f1_graph(), check_circuit(circuit))


@server.tool()
def recall_insights(user_id: str, ref_ids: list[str]) -> list[dict]:
    """T4: what this user's past sessions learned about these entities
    (e.g. ["monza"]), skipping outdated insights and listing conflicts.
    user_id is supplied by the agent runtime."""
    return q.recall_insights(memory_for(user_id), ref_ids)


@server.tool()
def trace(user_id: str, a_id: str) -> dict | None:
    """T5: the evidence behind a past answer: the analysis, the race data it used
    (with neighbouring nodes), the queries it ran and the Critic's review.
    user_id is supplied by the agent runtime."""
    return q.trace(memory_for(user_id), f1_graph(), a_id)


@server.tool()
def strategy_clusters(circuit: str | None = None) -> list[dict]:
    """T6: groups of circuits that win with the same strategy (community detection).
    Pass a circuit to get only its group. An independent second similarity signal."""
    return q.strategy_clusters(f1_graph(), check_circuit(circuit) if circuit else None)


@server.tool()
def key_races(circuit: str) -> list[dict]:
    """T7: past races ranked by weight as evidence (PageRank): races at the circuit
    first, then at similar circuits, most representative first."""
    return q.key_races(f1_graph(), check_circuit(circuit))


@server.tool()
def strategy_path(from_circuit: str, to_circuit: str, max_len: int = 4) -> list[dict]:
    """T8: the shortest chain linking two circuits through shared dominant
    strategies or similarity, e.g. monza -> M-H -> marina_bay."""
    return q.strategy_path(
        f1_graph(),
        check_circuit(from_circuit),
        check_circuit(to_circuit),
        check_range("max_len", max_len, 1, MAX_PATH_LEN),
    )


if __name__ == "__main__":
    server.run()  # stdio transport
