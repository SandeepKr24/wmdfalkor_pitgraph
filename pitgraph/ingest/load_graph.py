"""Write ingested rows into the `f1` graph.

All writes use UNWIND with batches of rows: one round trip to the Cloud
instance per batch instead of one per node. MERGE makes re-runs safe.

Run `python -m pitgraph.ingest.load_graph` to (re)build the race layer.
"""

from falkordb import Graph

from pitgraph.graph.db import f1_graph
from pitgraph.graph.schema import F1_INDEXES, ensure_indexes
from pitgraph.ingest import jolpica, openf1

BATCH_SIZE = 500

# Nodes: MERGE on the ID, then set the remaining properties.
UPSERT_SEASONS = """
UNWIND $rows AS row
MERGE (s:Season {year: row.year})
"""

UPSERT_CIRCUITS = """
UNWIND $rows AS row
MERGE (c:Circuit {circuit_id: row.circuit_id})
SET c.name = row.name, c.country = row.country, c.type = row.type, c.length_km = row.length_km
"""

UPSERT_RACES = """
UNWIND $rows AS row
MERGE (r:Race {race_id: row.race_id})
SET r.name = row.name, r.round = row.round, r.date = row.date, r.total_laps = row.total_laps
WITH r, row
MATCH (s:Season {year: row.year}), (c:Circuit {circuit_id: row.circuit_id})
MERGE (r)-[:IN_SEASON]->(s)
MERGE (r)-[:HELD_AT]->(c)
"""

UPSERT_DRIVERS = """
UNWIND $rows AS row
MERGE (d:Driver {driver_id: row.driver_id})
SET d.code = row.code, d.name = row.name, d.number = row.number
"""

UPSERT_TEAMS = """
UNWIND $rows AS row
MERGE (t:Team {team_id: row.team_id})
SET t.name = row.name
"""

UPSERT_ENTRIES = """
UNWIND $rows AS row
MERGE (e:Entry {entry_id: row.entry_id})
SET e.grid = row.grid, e.finish_pos = row.finish_pos, e.points = row.points, e.status = row.status
WITH e, row
MATCH (d:Driver {driver_id: row.driver_id}), (r:Race {race_id: row.race_id}), (t:Team {team_id: row.team_id})
MERGE (d)-[:DROVE]->(e)
MERGE (e)-[:IN_RACE]->(r)
MERGE (e)-[:FOR_TEAM]->(t)
"""


UPSERT_COMPOUNDS = """
UNWIND $rows AS row
MERGE (:Compound {name: row.name})
"""

UPSERT_STINTS = """
UNWIND $rows AS row
MERGE (s:Stint {stint_id: row.stint_id})
SET s.stint_no = row.stint_no, s.lap_start = row.lap_start, s.lap_end = row.lap_end,
    s.laps = row.laps, s.tyre_age_start = row.tyre_age_start
WITH s, row
MATCH (e:Entry {entry_id: row.entry_id})
MERGE (e)-[:HAD_STINT]->(s)
WITH s, row
MATCH (k:Compound {name: row.compound})
MERGE (s)-[:ON_COMPOUND]->(k)
"""

# Stint chain per entry: stint 1 -> stint 2 -> ... (a strategy as a path).
LINK_STINTS = """
MATCH (e:Entry)-[:HAD_STINT]->(a:Stint), (e)-[:HAD_STINT]->(b:Stint)
WHERE b.stint_no = a.stint_no + 1
MERGE (a)-[:NEXT]->(b)
"""

# Tyre changes per entry (stints - 1), so stops always match the strategy pattern.
SET_ENTRY_STOPS = """
MATCH (e:Entry)-[:HAD_STINT]->(s:Stint)
WITH e, count(s) AS stints
SET e.stops = stints - 1
"""

UPSERT_PIT_STOPS = """
UNWIND $rows AS row
MERGE (p:PitStop {pit_id: row.pit_id})
SET p.lap = row.lap, p.duration_s = row.duration_s
WITH p, row
MATCH (e:Entry {entry_id: row.entry_id})
MERGE (e)-[:PITTED]->(p)
"""

UPSERT_NEUTRALISATIONS = """
UNWIND $rows AS row
MERGE (n:Neutralisation {n_id: row.n_id})
SET n.type = row.type, n.lap_start = row.lap_start, n.lap_end = row.lap_end
WITH n, row
MATCH (r:Race {race_id: row.race_id})
MERGE (r)-[:HAD_NEUTRALISATION]->(n)
"""

SET_RACE_CONDITIONS = """
UNWIND $rows AS row
MATCH (r:Race {race_id: row.race_id})
SET r.wet = row.wet, r.air_temp_avg = row.air_temp_avg, r.track_temp_avg = row.track_temp_avg
"""


def write_batches(graph: Graph, query: str, rows: list[dict]) -> None:
    for start in range(0, len(rows), BATCH_SIZE):
        graph.query(query, {"rows": rows[start:start + BATCH_SIZE]})


def load_season(graph: Graph, year: int) -> None:
    """Jolpica (races, results) first, then OpenF1 (stints, pits, SC, weather) on top."""
    rows = jolpica.load_season(year)
    # Order matters: later queries MATCH nodes written by earlier ones.
    write_batches(graph, UPSERT_SEASONS, [{"year": year}])
    write_batches(graph, UPSERT_CIRCUITS, rows["circuits"])
    write_batches(graph, UPSERT_RACES, rows["races"])
    write_batches(graph, UPSERT_DRIVERS, rows["drivers"])
    write_batches(graph, UPSERT_TEAMS, rows["teams"])
    write_batches(graph, UPSERT_ENTRIES, rows["entries"])
    print(f"{year}: {len(rows['races'])} races, {len(rows['entries'])} entries (Jolpica)")

    live = openf1.load_season(year, rows["races"], rows["entries"], rows["drivers"])
    write_batches(graph, UPSERT_STINTS, live["stints"])
    write_batches(graph, UPSERT_PIT_STOPS, live["pit_stops"])
    write_batches(graph, UPSERT_NEUTRALISATIONS, live["neutralisations"])
    write_batches(graph, SET_RACE_CONDITIONS, live["conditions"])
    print(
        f"{year}: {len(live['stints'])} stints, {len(live['pit_stops'])} pit stops, "
        f"{len(live['neutralisations'])} neutralisations (OpenF1)"
    )


def main() -> None:
    graph = f1_graph()
    ensure_indexes(graph, F1_INDEXES)
    write_batches(graph, UPSERT_COMPOUNDS, [{"name": c} for c in openf1.COMPOUNDS])
    # Neutralisations are fully re-parsed each run; clear them so none go stale.
    graph.query("MATCH (n:Neutralisation) DETACH DELETE n")
    for year in jolpica.SEASONS:
        load_season(graph, year)
    graph.query(LINK_STINTS)
    graph.query(SET_ENTRY_STOPS)


if __name__ == "__main__":
    main()
