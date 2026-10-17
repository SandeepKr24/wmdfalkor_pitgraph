"""Rebuild the race graph from scratch in one command.

  python -m pitgraph.rebuild

Use it when the free FalkorDB Cloud instance was stopped or deleted (free tier:
stopped after 1 day unused, deleted after 7). For a new instance, put its
credentials in .env first.

Steps: connection check -> drop `f1` -> indexes (`f1`, `mem_global`) -> load
races (Jolpica + OpenF1) -> derived data -> verify against known-good counts.

Only `f1` is dropped: it is rebuilt entirely from data/raw/ (cached API
responses), so nothing is lost. User memory graphs (mem_*) are never touched.
If the cache is missing, the APIs are downloaded again (~12 min, rate-limited).
"""

import sys

from redis.exceptions import ResponseError

from pitgraph.graph import queries as q
from pitgraph.graph import schema
from pitgraph.graph.db import F1_GRAPH, f1_graph, get_client
from pitgraph.ingest import derive, load_graph
from pitgraph.ingest.http import CACHE_DIR

# What a correct rebuild of 2023-2025 contains (from the cached data).
EXPECTED_NODES = {
    "Season": 3, "Circuit": 24, "Race": 70, "Driver": 28, "Team": 12, "Entry": 1398,
    "Stint": 3948, "PitStop": 2339, "Neutralisation": 90, "Compound": 5, "Strategy": 167,
}
EXPECTED_EDGES = {"USED_STRATEGY": 1370, "FAVOURS": 27, "SIMILAR_TO": 72, "NEXT": 2578}
DONE_CHECK_CIRCUITS = ("monza", "marina_bay", "spa")


def step(title: str) -> None:
    print(f"\n== {title}")


def check_connection() -> None:
    try:
        print("Graphs on the instance:", get_client().list_graphs())
    except Exception as error:  # noqa: BLE001 - any failure here means we can't rebuild
        sys.exit(f"Cannot reach FalkorDB ({error}). Is the instance running, and is .env up to date?")


def drop_race_graph() -> None:
    if F1_GRAPH in get_client().list_graphs():
        f1_graph().delete()
        print(f"Dropped `{F1_GRAPH}`")
    else:
        print(f"No `{F1_GRAPH}` graph yet")


def counts(query: str) -> dict[str, int]:
    return {label: n for label, n in f1_graph().ro_query(query).result_set}


def verify() -> bool:
    """Compare against the known-good counts, then run the Day 1 check (T1-T3)."""
    nodes = counts("MATCH (n) RETURN labels(n)[0], count(*)")
    edges = counts("MATCH ()-[r]->() RETURN type(r), count(*)")
    problems = [
        f"{name}: expected {want}, got {got.get(name, 0)}"
        for expected, got in ((EXPECTED_NODES, nodes), (EXPECTED_EDGES, edges))
        for name, want in expected.items()
        if got.get(name, 0) != want
    ]
    for circuit in DONE_CHECK_CIRCUITS:
        if not q.winning_strategies(f1_graph(), circuit):
            problems.append(f"T1 returned no rows for {circuit}")
        if len(q.similar_circuits(f1_graph(), circuit)) != 3:
            problems.append(f"T2 did not return 3 similar circuits for {circuit}")
    if not q.neutralisation_impact(f1_graph(), "marina_bay"):
        problems.append("T3 returned no safety cars for Singapore (expected 2023)")

    for problem in problems:
        print("  MISMATCH", problem)
    return not problems


def main() -> None:
    step("1. Connection")
    check_connection()
    if not CACHE_DIR.exists():
        print(f"No cache at {CACHE_DIR}: data will be downloaded from the APIs (~12 min).")

    step("2. Drop the race graph")
    drop_race_graph()

    step("3. Indexes")
    schema.main()

    step("4. Load races (Jolpica + OpenF1)")
    load_graph.main()

    step("5. Derived data")
    derive.main()

    step("6. Verify")
    try:
        ok = verify()
    except ResponseError as error:
        ok = False
        print("  Query failed:", error)
    if not ok:
        sys.exit("Rebuild finished with mismatches (see above).")
    print("Rebuild OK: counts match and T1-T3 return data for Monza, Singapore and Spa.")


if __name__ == "__main__":
    main()
