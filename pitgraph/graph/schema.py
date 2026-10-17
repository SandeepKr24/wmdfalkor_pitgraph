"""Range indexes for every graph.

Each index covers the ID property we MATCH or MERGE on, so lookups don't
scan every node with that label. Safe to re-run: existing indexes are skipped.

Run `python -m pitgraph.graph.schema` to index `f1` and `mem_global`.
User memory graphs are indexed when first created (see agents/memory.py).
"""

from falkordb import Graph
from redis.exceptions import ResponseError

from pitgraph.graph.db import f1_graph, global_memory_graph

# (label, property) pairs per graph.
F1_INDEXES = [
    ("Season", "year"),
    ("Race", "race_id"),
    ("Circuit", "circuit_id"),
    ("Driver", "driver_id"),
    ("Team", "team_id"),
    ("Entry", "entry_id"),
    ("Stint", "stint_id"),
    ("PitStop", "pit_id"),
    ("Neutralisation", "n_id"),
    ("Compound", "name"),
    ("Strategy", "pattern"),
]

USER_MEMORY_INDEXES = [
    ("User", "user_id"),
    ("Session", "session_id"),
    ("Message", "m_id"),
    ("Question", "q_id"),
    ("Task", "t_id"),
    ("Analysis", "a_id"),
    ("QueryRun", "qr_id"),
    ("EntityRef", "ref_id"),
    ("Insight", "i_id"),
    ("Action", "act_id"),
]

GLOBAL_MEMORY_INDEXES = [
    ("Procedure", "intent"),
]


def existing_indexes(graph: Graph) -> set[tuple[str, str]]:
    """Return the (label, property) pairs already indexed in this graph."""
    try:
        rows = graph.list_indices().result_set
    except ResponseError:
        return set()  # the graph doesn't exist yet, so it has no indexes
    # Each row starts with: label, [indexed properties], ...
    return {(row[0], prop) for row in rows for prop in row[1]}


def ensure_indexes(graph: Graph, indexes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Create any missing indexes. Returns the ones that were created."""
    present = existing_indexes(graph)
    created = []
    for label, prop in indexes:
        if (label, prop) not in present:
            graph.create_node_range_index(label, prop)
            created.append((label, prop))
    return created


def main() -> None:
    for graph, indexes in (
        (f1_graph(), F1_INDEXES),
        (global_memory_graph(), GLOBAL_MEMORY_INDEXES),
    ):
        created = ensure_indexes(graph, indexes)
        print(f"{graph.name}: created {len(created)} index(es), {len(indexes) - len(created)} already present")


if __name__ == "__main__":
    main()
