"""FalkorDB Cloud connection and graph selection.

One FalkorDB instance holds several graphs (multigraph):
  - f1               race data, read-only at runtime
  - mem_<user_id>    one isolated memory graph per user
  - mem_global       procedural memory shared by all users

Run `python -m pitgraph.graph.db` for a connection smoke test.
"""

import os
import re
from functools import lru_cache

from dotenv import load_dotenv
from falkordb import FalkorDB, Graph

load_dotenv()

F1_GRAPH = "f1"
GLOBAL_MEMORY_GRAPH = "mem_global"

REQUIRED_ENV_VARS = (
    "FALKORDB_HOST",
    "FALKORDB_PORT",
    "FALKORDB_USERNAME",
    "FALKORDB_PASSWORD",
)

# User IDs become part of a graph name, so only allow safe characters.
USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


@lru_cache(maxsize=1)
def get_client() -> FalkorDB:
    """Return a single shared FalkorDB client built from .env settings."""
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    return FalkorDB(
        host=os.environ["FALKORDB_HOST"],
        port=int(os.environ["FALKORDB_PORT"]),
        username=os.environ["FALKORDB_USERNAME"],
        password=os.environ["FALKORDB_PASSWORD"],
    )


def get_graph(name: str) -> Graph:
    """Select a graph by name. FalkorDB creates it on the first write."""
    return get_client().select_graph(name)


def f1_graph() -> Graph:
    return get_graph(F1_GRAPH)


def global_memory_graph() -> Graph:
    return get_graph(GLOBAL_MEMORY_GRAPH)


def user_memory_graph(user_id: str) -> Graph:
    """Each user gets their own graph, so memories never mix between users."""
    if not USER_ID_PATTERN.match(user_id):
        raise ValueError(f"Invalid user_id: {user_id!r}")
    return get_graph(f"mem_{user_id}")


def smoke_test() -> None:
    """Check the connection: list graphs, then write, read and delete a scratch graph."""
    client = get_client()
    print("Existing graphs:", client.list_graphs())

    scratch = get_graph("smoke_test")
    scratch.query("CREATE (:Ping {ok: true})")
    result = scratch.ro_query("MATCH (p:Ping) RETURN p.ok")
    print("Read back:", result.result_set)
    scratch.delete()
    print("Connection OK")


if __name__ == "__main__":
    smoke_test()
