"""Derived data, computed from the ingested race layer.

1. Circuit.pit_loss_s   median pit-lane time at the circuit
2. Strategy nodes       compound sequence per entry, e.g. "M-H", plus USED_STRATEGY
3. FAVOURS edges        Circuit -> its dominant dry-race strategy among top-10 finishers (for T6)
4. SIMILAR_TO edges     top 3 most similar circuits by features, with a readable basis

Run after load_graph: `python -m pitgraph.ingest.derive`. Safe to re-run.
"""

from statistics import mean, median

from falkordb import Graph

from pitgraph.graph.db import f1_graph
from pitgraph.ingest.load_graph import write_batches

# Pit-lane times outside this range are garage stops, red-flag stops or bad data.
PIT_TIME_RANGE_S = (10, 60)
POINTS_POSITIONS = 10
SIMILAR_NEIGHBOURS = 3


# ---------- 1. Pit loss ----------

def derive_pit_loss(graph: Graph) -> None:
    rows = graph.ro_query("""
        MATCH (c:Circuit)<-[:HELD_AT]-(:Race)<-[:IN_RACE]-(:Entry)-[:PITTED]->(p:PitStop)
        WHERE p.duration_s >= $low AND p.duration_s <= $high
        RETURN c.circuit_id, collect(p.duration_s)
    """, {"low": PIT_TIME_RANGE_S[0], "high": PIT_TIME_RANGE_S[1]}).result_set
    updates = [{"circuit_id": cid, "pit_loss_s": round(median(times), 1)} for cid, times in rows]
    write_batches(graph, """
        UNWIND $rows AS row
        MATCH (c:Circuit {circuit_id: row.circuit_id})
        SET c.pit_loss_s = row.pit_loss_s
    """, updates)
    print(f"pit_loss_s set on {len(updates)} circuits")


# ---------- 2. Strategy ----------

def derive_strategies(graph: Graph) -> None:
    rows = graph.ro_query("""
        MATCH (e:Entry)-[:HAD_STINT]->(s:Stint)
        OPTIONAL MATCH (s)-[:ON_COMPOUND]->(k:Compound)
        WITH e, s, k ORDER BY s.stint_no
        RETURN e.entry_id, count(s), collect(k.name)
    """).result_set

    links = []
    for entry_id, stint_count, compounds in rows:
        # collect() drops nulls, so an unknown compound makes the list shorter
        # than the stint count. Skip those entries: their strategy is incomplete.
        if len(compounds) != stint_count:
            continue
        pattern = "-".join(c[0] for c in compounds)  # SOFT, MEDIUM, HARD -> "S-M-H"
        links.append({"entry_id": entry_id, "pattern": pattern, "stops": stint_count - 1})

    graph.query("MATCH (:Entry)-[r:USED_STRATEGY]->() DELETE r")
    write_batches(graph, """
        UNWIND $rows AS row
        MERGE (st:Strategy {pattern: row.pattern})
        SET st.stops = row.stops
        WITH st, row
        MATCH (e:Entry {entry_id: row.entry_id})
        MERGE (e)-[:USED_STRATEGY]->(st)
    """, links)
    print(f"strategies linked for {len(links)} entries ({len({l['pattern'] for l in links})} patterns)")


# ---------- 3. FAVOURS ----------

def derive_favours(graph: Graph) -> None:
    """Each circuit's dominant strategy: the one most used by top-10 finishers in dry races.

    Only the dominant strategy gets an edge (ties keep both). Linking every strategy
    ever used would connect almost every circuit through M-H, and community
    detection (T6) would then return one giant cluster. Wet races are left out
    because their one-off rain-tyre sequences say nothing about the circuit.
    """
    graph.query("MATCH (:Circuit)-[r:FAVOURS]->() DELETE r")
    graph.query("""
        MATCH (c:Circuit)<-[:HELD_AT]-(r:Race)<-[:IN_RACE]-(e:Entry)-[:USED_STRATEGY]->(st:Strategy)
        WHERE e.finish_pos <= $top AND NOT r.wet
        WITH c, st, count(e) AS n
        WITH c, collect({st: st, n: n}) AS uses, max(n) AS best
        UNWIND uses AS u
        WITH c, u WHERE u.n = best
        WITH c, u.st AS st, u.n AS n
        MERGE (c)-[f:FAVOURS]->(st)
        SET f.count = n
    """, {"top": POINTS_POSITIONS})
    count = graph.ro_query("MATCH ()-[f:FAVOURS]->() RETURN count(f)").result_set[0][0]
    print(f"{count} FAVOURS edges")


# ---------- 4. SIMILAR_TO ----------

def circuit_features(graph: Graph) -> dict[str, dict]:
    """One feature row per circuit: type, length, pit loss, avg stops, neutralisations per race."""
    rows = graph.ro_query("""
        MATCH (c:Circuit)<-[:HELD_AT]-(r:Race)
        OPTIONAL MATCH (r)-[:HAD_NEUTRALISATION]->(n:Neutralisation)
        WITH c, r, count(n) AS neutralisations
        OPTIONAL MATCH (r)<-[:IN_RACE]-(e:Entry) WHERE e.stops IS NOT NULL
        WITH c, r, neutralisations, avg(e.stops) AS race_stops
        RETURN c.circuit_id, c.type, c.length_km, c.pit_loss_s, avg(neutralisations), avg(race_stops)
    """).result_set
    return {
        cid: {
            "street": 1.0 if ctype == "street" else 0.0,
            "length_km": length,
            "pit_loss_s": pit_loss,
            "avg_stops": avg_stops,
            "sc_per_race": sc_per_race,
        }
        for cid, ctype, length, pit_loss, sc_per_race, avg_stops in rows
        if pit_loss is not None and avg_stops is not None  # needs OpenF1 data to compare
    }


def scale_0_1(features: dict[str, dict]) -> dict[str, dict]:
    """Min-max scale every feature so each one counts equally in the distance."""
    names = next(iter(features.values())).keys()
    scaled = {cid: {} for cid in features}
    for name in names:
        values = [f[name] for f in features.values()]
        low, high = min(values), max(values)
        for cid, f in features.items():
            scaled[cid][name] = (f[name] - low) / (high - low) if high > low else 0.0
    return scaled


def shared_traits(a: dict, b: dict, medians: dict) -> str:
    """Human-readable reason for a match, e.g. "street,high_sc"."""
    traits = []
    if a["street"] == b["street"]:
        traits.append("street" if a["street"] else "permanent")
    checks = (
        ("sc_per_race", "high_sc", "low_sc"),
        ("pit_loss_s", "high_pit_loss", "low_pit_loss"),
        ("avg_stops", "multi_stop", "one_stop"),
        ("length_km", "long_lap", "short_lap"),
    )
    for name, high_tag, low_tag in checks:
        a_high, b_high = a[name] > medians[name], b[name] > medians[name]
        if a_high == b_high:
            traits.append(high_tag if a_high else low_tag)
    return ",".join(traits) or "closest_overall"


def derive_similar_circuits(graph: Graph) -> None:
    features = circuit_features(graph)
    scaled = scale_0_1(features)
    names = list(next(iter(scaled.values())).keys())
    medians = {n: median(f[n] for f in features.values()) for n in names}

    edges = []
    for cid, f in scaled.items():
        scores = []
        for other, g in scaled.items():
            if other == cid:
                continue
            distance = mean(abs(f[n] - g[n]) for n in names)
            scores.append((round(1 - distance, 3), other))
        for score, other in sorted(scores, reverse=True)[:SIMILAR_NEIGHBOURS]:
            edges.append({
                "a": cid,
                "b": other,
                "score": score,
                "basis": shared_traits(features[cid], features[other], medians),
            })

    graph.query("MATCH (:Circuit)-[r:SIMILAR_TO]->() DELETE r")
    write_batches(graph, """
        UNWIND $rows AS row
        MATCH (a:Circuit {circuit_id: row.a}), (b:Circuit {circuit_id: row.b})
        MERGE (a)-[s:SIMILAR_TO]->(b)
        SET s.score = row.score, s.basis = row.basis
    """, edges)
    print(f"{len(edges)} SIMILAR_TO edges across {len(features)} circuits")


def main() -> None:
    graph = f1_graph()
    derive_pit_loss(graph)
    derive_strategies(graph)
    derive_favours(graph)
    derive_similar_circuits(graph)


if __name__ == "__main__":
    main()
