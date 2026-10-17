"""Graph tools T1-T8: the only queries the agents can run.

The LLM picks a tool and its arguments; it never writes Cypher itself.
Each tool returns a list of plain dicts (one per row), keyed by column name.

Race-layer tools read the `f1` graph. T4 and T5 read a user's memory graph.
"""

from falkordb import Graph

# ---------- helpers ----------

# Statuses for drivers who were classified at the finish (not retired, DSQ or DNS).
CLASSIFIED_STATUSES = ["Finished", "Lapped"]

# Labels the trace view may resolve in `f1`, and the ID property each one uses.
# Labels can't be query parameters in Cypher, so we only allow these.
TRACEABLE_LABELS = {
    "Race": "race_id",
    "Entry": "entry_id",
    "Stint": "stint_id",
    "Circuit": "circuit_id",
}


def rows(graph: Graph, query: str, params: dict | None = None) -> list[dict]:
    """Run a read-only query and return each row as {column: value}."""
    result = graph.ro_query(query, params or {})
    columns = [name for _type, name in result.header]
    return [dict(zip(columns, row, strict=True)) for row in result.result_set]


def as_props(node) -> dict:
    """A FalkorDB node as a plain dict: its label plus its properties."""
    return {"label": node.labels[0], **node.properties}


# ---------- T1: winning strategies ----------

WINNING_STRATEGIES = """
MATCH (c:Circuit {circuit_id: $circuit})<-[:HELD_AT]-(r:Race)<-[:IN_RACE]-(e:Entry)
WHERE e.finish_pos <= $top_n
MATCH (e)-[:HAD_STINT]->(s:Stint)-[:ON_COMPOUND]->(k:Compound)
WITH r, e, s, k ORDER BY s.stint_no
RETURN r.race_id AS race_id, e.entry_id AS entry_id, e.finish_pos AS finish_pos,
       collect(k.name) AS strategy, e.stops AS stops
ORDER BY race_id, finish_pos
"""


def winning_strategies(graph: Graph, circuit: str, top_n: int = 3) -> list[dict]:
    """T1: tyre sequence of the top-N finishers in every race at a circuit (what worked)."""
    return rows(graph, WINNING_STRATEGIES, {"circuit": circuit, "top_n": top_n})


# ---------- T1b: strategy outcomes (what to avoid) ----------

# Positions gained (grid - finish) compares each driver with their own starting
# spot, so a slow car isn't blamed for being slow. Only classified finishers
# count, so retirements don't look like strategy failures. Pit-lane starts
# (grid 0) are left out because they have no grid slot to compare against.
STRATEGY_OUTCOMES = """
MATCH (:Circuit {circuit_id: $circuit})<-[:HELD_AT]-(r:Race)<-[:IN_RACE]-(e:Entry)-[:USED_STRATEGY]->(st:Strategy)
WHERE e.status IN $classified AND e.grid > 0
WITH st, count(e) AS uses, avg(e.grid - e.finish_pos) AS gain, avg(e.finish_pos) AS finish,
     collect(DISTINCT r.race_id) AS races
WHERE uses >= $min_uses
RETURN st.pattern AS strategy, st.stops AS stops, uses,
       round(gain * 10) / 10 AS avg_positions_gained,
       round(finish * 10) / 10 AS avg_finish, races
ORDER BY avg_positions_gained DESC
"""


def strategy_outcomes(graph: Graph, circuit: str, min_uses: int = 2) -> list[dict]:
    """T1b: every strategy used at a circuit, ranked by average positions gained.

    Top rows are what worked; bottom rows (negative gain) are what to avoid.
    """
    return rows(graph, STRATEGY_OUTCOMES, {
        "circuit": circuit,
        "min_uses": min_uses,
        "classified": CLASSIFIED_STATUSES,
    })


# ---------- T2: similar circuits ----------

SIMILAR_CIRCUITS = """
MATCH (:Circuit {circuit_id: $circuit})-[sim:SIMILAR_TO]->(c2:Circuit)<-[:HELD_AT]-(r:Race)
RETURN c2.circuit_id AS circuit_id, sim.score AS score, sim.basis AS basis,
       collect(r.race_id) AS races
ORDER BY score DESC
"""


def similar_circuits(graph: Graph, circuit: str) -> list[dict]:
    """T2: the 3 most similar circuits and why. Used for recovery when T1 is thin."""
    return rows(graph, SIMILAR_CIRCUITS, {"circuit": circuit})


# ---------- T3: neutralisation impact ----------

# Every neutralisation is listed, including ones where nobody pitted (count 0),
# so an empty result really means "no SC, VSC or red flag at this circuit".
NEUTRALISATION_IMPACT = """
MATCH (r:Race)-[:HELD_AT]->(:Circuit {circuit_id: $circuit})
MATCH (r)-[:HAD_NEUTRALISATION]->(n:Neutralisation)
OPTIONAL MATCH (r)<-[:IN_RACE]-(:Entry)-[:PITTED]->(p:PitStop)
WHERE p.lap >= n.lap_start AND p.lap <= n.lap_end
RETURN r.race_id AS race_id, n.type AS type, n.lap_start AS lap_start, n.lap_end AS lap_end,
       count(p) AS stops_under_neutralisation
ORDER BY race_id, lap_start
"""


def neutralisation_impact(graph: Graph, circuit: str) -> list[dict]:
    """T3: every SC / VSC / red flag at a circuit and how many drivers pitted during it."""
    return rows(graph, NEUTRALISATION_IMPACT, {"circuit": circuit})


# ---------- T4: recall insights (memory graph) ----------

RECALL_INSIGHTS = """
MATCH (i:Insight)-[:ABOUT]->(x:EntityRef)
WHERE x.ref_id IN $ref_ids AND NOT (i)<-[:SUPERSEDES]-()
OPTIONAL MATCH (i)-[:CONTRADICTS]-(other:Insight)
RETURN i.i_id AS i_id, i.statement AS statement, i.confidence AS confidence,
       i.times_confirmed AS times_confirmed, collect(other.statement) AS conflicts
ORDER BY times_confirmed DESC
"""


def recall_insights(memory: Graph, ref_ids: list[str]) -> list[dict]:
    """T4: current (not superseded) insights about these entities, with any conflicts."""
    return rows(memory, RECALL_INSIGHTS, {"ref_ids": ref_ids})


# ---------- T5: trace (memory graph, then f1) ----------

TRACE_ANALYSIS = """
MATCH (a:Analysis {a_id: $a_id})
OPTIONAL MATCH (a)-[:USED]->(ev:Evidence)
OPTIONAL MATCH (a)-[:RAN]->(q:QueryRun)
OPTIONAL MATCH (a)-[rv:REVIEWED]->(:Task)
RETURN a, collect(DISTINCT ev) AS evidence, collect(DISTINCT q) AS queries,
       collect(DISTINCT rv) AS reviews
"""

# The question, the past insights this answer recalled, and the ones it produced.
TRACE_MEMORY = """
MATCH (a:Analysis {a_id: $a_id})
OPTIONAL MATCH (a)-[:ANSWERS]->(qn:Question)
OPTIONAL MATCH (a)-[:RECALLED]->(recalled:Insight)
OPTIONAL MATCH (learned:Insight)-[:DERIVED_FROM]->(a)
RETURN qn, collect(DISTINCT recalled) AS recalled, collect(DISTINCT learned) AS learned
"""

# The agent handoff chain for the question: start at the Task nothing hands off to,
# follow HANDED_OFF_TO to the end. Variable-length path, so no fixed number of steps.
HANDOFF_CHAIN = """
MATCH (:Analysis {a_id: $a_id})-[:ANSWERS]->(:Question)<-[:FOR_QUESTION]-(first:Task)
WHERE NOT ()-[:HANDED_OFF_TO]->(first)
MATCH chain = (first)-[:HANDED_OFF_TO*0..]->(last:Task)
WHERE NOT (last)-[:HANDED_OFF_TO]->()
RETURN [t IN nodes(chain) | {from_agent: t.from_agent, to_agent: t.to_agent,
                             summary: t.payload_summary}] AS steps
"""

# {label} and {key} come from TRACEABLE_LABELS only, never from user input.
EVIDENCE_NEIGHBOURHOOD = """
MATCH (n:{label} {{{key}: $ref_id}})
OPTIONAL MATCH (n)-[rel]-(m)
RETURN n, collect({{type: type(rel), node: m}}) AS neighbours
"""


def trace(memory: Graph, race_graph: Graph, a_id: str) -> dict | None:
    """T5: an analysis with its evidence, queries and reviews, plus each evidence
    node's 1-hop neighbourhood in `f1` (Cypher can't cross graphs, so two steps).
    Also returns the question, the handoff chain, and the insights recalled/learned."""
    result = memory.ro_query(TRACE_ANALYSIS, {"a_id": a_id}).result_set
    if not result:
        return None
    analysis, evidence, queries, reviews = result[0]
    question, recalled, learned = memory.ro_query(TRACE_MEMORY, {"a_id": a_id}).result_set[0]
    chain = memory.ro_query(HANDOFF_CHAIN, {"a_id": a_id}).result_set

    return {
        "analysis": analysis.properties,
        "question": question.properties if question else None,
        "evidence": [r for ev in evidence if (r := resolve_evidence(race_graph, ev.properties))],
        "queries": [q.properties for q in queries],
        "reviews": [r.properties for r in reviews],
        "handoffs": chain[0][0] if chain else [],
        "recalled_insights": [i.properties for i in recalled],
        "learned_insights": [i.properties for i in learned],
    }


def resolve_evidence(race_graph: Graph, evidence: dict) -> dict | None:
    """Look up one Evidence stub in `f1`, with its neighbours. None if unknown."""
    key = TRACEABLE_LABELS.get(evidence.get("ref_label"))
    if key is None:
        return None  # unknown label: skip rather than build an unsafe query
    query = EVIDENCE_NEIGHBOURHOOD.format(label=evidence["ref_label"], key=key)
    found = race_graph.ro_query(query, {"ref_id": evidence.get("ref_id")}).result_set
    if not found:
        return None
    node, neighbours = found[0]
    return {
        "evidence": evidence,
        "node": as_props(node),
        "neighbours": [
            {"type": nb["type"], "node": as_props(nb["node"])}
            for nb in neighbours if nb["node"] is not None
        ],
    }


# ---------- T6: strategy clusters (label propagation) ----------

# Label propagation over Circuit -FAVOURS-> Strategy (each circuit's dominant
# strategy). Circuits that win with the same strategy land in the same community.
# Strategy nodes end up in communities of their own, so each cluster's
# strategies are read back through its circuits' FAVOURS edges.
STRATEGY_CLUSTERS = """
CALL algo.labelPropagation({nodeLabels: ['Circuit', 'Strategy'], relationshipTypes: ['FAVOURS']})
YIELD node, communityId
WITH node, communityId WHERE node:Circuit
MATCH (node)-[:FAVOURS]->(st:Strategy)
RETURN communityId AS cluster, collect(DISTINCT node.circuit_id) AS circuits,
       collect(DISTINCT st.pattern) AS strategies
ORDER BY size(circuits) DESC
"""


def strategy_clusters(graph: Graph, circuit: str | None = None) -> list[dict]:
    """T6: groups of circuits that behave alike strategically.

    With a circuit, only the cluster containing it is returned.
    """
    clusters = rows(graph, STRATEGY_CLUSTERS)
    if circuit is None:
        return clusters
    return [c for c in clusters if circuit in c["circuits"]]


# ---------- T7: key races (PageRank) ----------

# PageRank over SIMILAR_TO: a circuit many others resemble scores high, so its
# races are the most representative evidence.
CIRCUIT_PAGERANK = """
CALL algo.pageRank('Circuit', 'SIMILAR_TO') YIELD node, score
RETURN node.circuit_id AS circuit_id, score
"""

# Races at the circuit itself and at its similar circuits.
CANDIDATE_RACES = """
MATCH (target:Circuit {circuit_id: $circuit})
MATCH (c:Circuit) WHERE c = target OR (target)-[:SIMILAR_TO]->(c)
MATCH (r:Race)-[:HELD_AT]->(c)
RETURN r.race_id AS race_id, c.circuit_id AS circuit_id, c = target AS same_circuit
"""


def key_races(graph: Graph, circuit: str) -> list[dict]:
    """T7: past races ranked by how much weight they carry as evidence.

    Races at the circuit itself come first, then races at similar circuits,
    ordered by circuit PageRank. Raw PageRank scores are tiny (they're spread
    over the whole graph), so they're rescaled: 1.0 = the most central circuit.
    """
    scores = {r["circuit_id"]: r["score"] for r in rows(graph, CIRCUIT_PAGERANK)}
    top = max(scores.values(), default=0) or 1
    races = rows(graph, CANDIDATE_RACES, {"circuit": circuit})
    for race in races:
        race["circuit_rank"] = round(scores.get(race["circuit_id"], 0) / top, 2)
    return sorted(races, key=lambda r: (not r["same_circuit"], -r["circuit_rank"], r["race_id"]))


# ---------- T8: strategy path (shortest path) ----------

# Shortest chain between two circuits over shared strategies (FAVOURS) and
# similarity (SIMILAR_TO), ignoring edge direction.
STRATEGY_PATH = """
MATCH (a:Circuit {circuit_id: $from_circuit}), (b:Circuit {circuit_id: $to_circuit})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['FAVOURS', 'SIMILAR_TO'],
                   relDirection: 'both', maxLen: $max_len})
YIELD path
RETURN [n IN nodes(path) | coalesce(n.circuit_id, n.pattern)] AS path_nodes,
       [r IN relationships(path) | type(r)] AS path_links
"""


def strategy_path(graph: Graph, from_circuit: str, to_circuit: str, max_len: int = 4) -> list[dict]:
    """T8: how two circuits are connected, e.g. monza -FAVOURS- M-H -FAVOURS- marina_bay."""
    return rows(graph, STRATEGY_PATH, {
        "from_circuit": from_circuit,
        "to_circuit": to_circuit,
        "max_len": max_len,
    })
