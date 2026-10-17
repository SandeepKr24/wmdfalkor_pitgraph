"""Read-only queries behind the UI's Memory panel.

The agents never call these; they only show a user what their memory graph holds,
plus the shared procedural memory in mem_global.
"""

import json

from falkordb import Graph

from pitgraph.graph.db import get_client, global_memory_graph, user_memory_graph
from pitgraph.graph.queries import rows

# Every insight, current or not: superseded ones are shown struck through, so the
# user can see how a memory changed over time.
INSIGHTS = """
MATCH (i:Insight)
OPTIONAL MATCH (i)-[:ABOUT]->(x:EntityRef)
OPTIONAL MATCH (newer:Insight)-[:SUPERSEDES]->(i)
OPTIONAL MATCH (i)-[:CONTRADICTS]-(other:Insight)
RETURN i.i_id AS i_id, i.statement AS statement, i.confidence AS confidence,
       i.times_confirmed AS times_confirmed, i.updated_at AS updated_at,
       collect(DISTINCT x.ref_id) AS circuits,
       collect(DISTINCT newer.i_id) AS superseded_by,
       collect(DISTINCT other.i_id) AS conflicts_with
ORDER BY updated_at DESC
"""

# Past answers, newest first, with the session they belong to.
RECENT_ANSWERS = """
MATCH (a:Analysis)-[:ANSWERS]->(qn:Question)-[:FROM_MESSAGE]->(:Message)-[:IN_SESSION]->(s:Session)
RETURN a.a_id AS a_id, qn.text AS question, qn.intent AS intent, a.verdict AS verdict,
       a.confidence AS confidence, a.created_at AS created_at, s.session_id AS session_id
ORDER BY created_at DESC LIMIT $limit
"""

BRIEFS = """
MATCH (act:Action)-[:BASED_ON]->(a:Analysis)
RETURN act.act_id AS act_id, act.type AS type, act.content AS content,
       act.created_at AS created_at, a.a_id AS a_id
ORDER BY created_at DESC LIMIT $limit
"""

NODE_COUNTS = "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC"

PROCEDURES = """
MATCH (p:Procedure)
WHERE p.success_count + p.fail_count > 0
OPTIONAL MATCH (p)-[:FALLBACK_TO]->(f:Procedure)
RETURN p.intent AS intent, p.tool_sequence AS tool_sequence, p.success_count AS successes,
       p.fail_count AS failures, collect(f.tool_sequence) AS fallbacks
ORDER BY intent, successes DESC
"""

RECENT_LIMIT = 20


def graph_exists(name: str) -> bool:
    """Reading a graph that doesn't exist is an error in FalkorDB, so check first."""
    return name in get_client().list_graphs()


def user_memory(user_id: str) -> dict:
    """Everything the Memory panel shows for one user (empty if they're new)."""
    graph = user_memory_graph(user_id)  # validates user_id
    if not graph_exists(graph.name):
        return {"user_id": user_id, "node_counts": {}, "insights": [], "answers": [], "briefs": []}
    return {
        "user_id": user_id,
        "node_counts": {r["label"]: r["count"] for r in rows(graph, NODE_COUNTS)},
        "insights": rows(graph, INSIGHTS),
        "answers": rows(graph, RECENT_ANSWERS, {"limit": RECENT_LIMIT}),
        "briefs": [briefs_row(r) for r in rows(graph, BRIEFS, {"limit": RECENT_LIMIT})],
    }


def briefs_row(row: dict) -> dict:
    return {**row, "content": json.loads(row["content"])}  # stored as a JSON string


def procedures(graph: Graph | None = None) -> list[dict]:
    """Shared procedural memory: tool sequences per intent and how they fared."""
    graph = graph or global_memory_graph()
    if not graph_exists(graph.name):
        return []
    return rows(graph, PROCEDURES)
