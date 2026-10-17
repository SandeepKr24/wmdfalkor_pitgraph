# QUERIES.md: every Cypher query and algorithm call in PitGraph

Everything the agents run is listed here, copied from the code, with what each query does and why it's written that way. The LLM never writes Cypher. It picks one of these tools and its arguments over MCP, and every value it passes goes in as a query parameter (`$circuit`) and never pasted into the query text.

Graphs: `f1` (race data), `mem_<user_id>` (one per user), `mem_global` (shared procedures). See [README](README.md) for the data model.

Contents
1. [Agent tools: race layer (T1–T3)](#1-agent-tools-race-layer)
2. [Algorithm tools (T6–T8)](#2-algorithm-tools)
3. [Memory tools (T4, T5)](#3-memory-tools)
4. [Memory writes](#4-memory-writes)
5. [Procedural memory](#5-procedural-memory-mem_global)
6. [Derived data (built once after ingestion)](#6-derived-data)
7. [Ingestion](#7-ingestion)
8. [Memory panel reads](#8-memory-panel-reads)

---

## 1. Agent tools: race layer

Source: `pitgraph/graph/queries.py`, exposed by `pitgraph/mcp_server/server.py`.

### `list_circuits`
```cypher
MATCH (c:Circuit) RETURN c.circuit_id AS id, c.name AS name
```
Lists every circuit, so the Planner can turn "Singapore" into `marina_bay`. It's read once and cached, since `f1` is read-only at runtime, and the same list validates the `circuit` argument of every other tool.

### T1 `winning_strategies(circuit, top_n)`: strategy as a path
```cypher
MATCH (c:Circuit {circuit_id: $circuit})<-[:HELD_AT]-(r:Race)<-[:IN_RACE]-(e:Entry)
WHERE e.finish_pos <= $top_n
MATCH (e)-[:HAD_STINT]->(s:Stint)-[:ON_COMPOUND]->(k:Compound)
WITH r, e, s, k ORDER BY s.stint_no
RETURN r.race_id AS race_id, e.entry_id AS entry_id, e.finish_pos AS finish_pos,
       collect(k.name) AS strategy, e.stops AS stops
ORDER BY race_id, finish_pos
```
For each race at the circuit, finds the top-N finishers and follows each one's stints to their compounds. Sorting by `stint_no` before `collect` puts the tyres in race order, e.g. `["MEDIUM", "HARD"]`. This shows what worked.

### T1b `strategy_outcomes(circuit, min_uses)`: what worked and what to avoid
```cypher
MATCH (:Circuit {circuit_id: $circuit})<-[:HELD_AT]-(r:Race)<-[:IN_RACE]-(e:Entry)-[:USED_STRATEGY]->(st:Strategy)
WHERE e.status IN $classified AND e.grid > 0
WITH st, count(e) AS uses, avg(e.grid - e.finish_pos) AS gain, avg(e.finish_pos) AS finish,
     collect(DISTINCT r.race_id) AS races
WHERE uses >= $min_uses
RETURN st.pattern AS strategy, st.stops AS stops, uses,
       round(gain * 10) / 10 AS avg_positions_gained,
       round(finish * 10) / 10 AS avg_finish, races
ORDER BY avg_positions_gained DESC
```
Ranks every strategy used at the circuit by average positions gained (grid − finish). Comparing each driver with their own starting spot means a slow car isn't blamed for being slow. Only classified finishers count (`$classified` = `Finished`, `Lapped`), so retirements don't look like strategy failures. Pit-lane starts (`grid 0`) are left out. Negative gain means a strategy to avoid. `min_uses` drops one-off strategies.

### T2 `similar_circuits(circuit)`: the recovery path
```cypher
MATCH (:Circuit {circuit_id: $circuit})-[sim:SIMILAR_TO]->(c2:Circuit)<-[:HELD_AT]-(r:Race)
RETURN c2.circuit_id AS circuit_id, sim.score AS score, sim.basis AS basis,
       collect(r.race_id) AS races
ORDER BY score DESC
```
The circuit's 3 most similar circuits, the shared traits behind each match (`basis`, e.g. `"street,low_sc,one_stop"`), and their races. The Retriever adds this tool (and T6) when the circuit's own data is thin: fewer than 3 rows from T1 or T1b, or from T3 for safety-car questions.

### T3 `neutralisation_impact(circuit)`
```cypher
MATCH (r:Race)-[:HELD_AT]->(:Circuit {circuit_id: $circuit})
MATCH (r)-[:HAD_NEUTRALISATION]->(n:Neutralisation)
OPTIONAL MATCH (r)<-[:IN_RACE]-(:Entry)-[:PITTED]->(p:PitStop)
WHERE p.lap >= n.lap_start AND p.lap <= n.lap_end
RETURN r.race_id AS race_id, n.type AS type, n.lap_start AS lap_start, n.lap_end AS lap_end,
       count(p) AS stops_under_neutralisation
ORDER BY race_id, lap_start
```
Lists every safety car, virtual safety car and red flag at the circuit, with how many drivers pitted during it (a "cheap" stop). `OPTIONAL MATCH` keeps neutralisations where nobody pitted (count 0), so an empty result really means the circuit had none.

---

## 2. Algorithm tools

FalkorDB's algorithms only run on edges stored in the graph, which is why `FAVOURS` and `SIMILAR_TO` are real edges (section 6).

### T6 `strategy_clusters(circuit?)`: label propagation (community detection)
```cypher
CALL algo.labelPropagation({nodeLabels: ['Circuit', 'Strategy'], relationshipTypes: ['FAVOURS']})
YIELD node, communityId
WITH node, communityId WHERE node:Circuit
MATCH (node)-[:FAVOURS]->(st:Strategy)
RETURN communityId AS cluster, collect(DISTINCT node.circuit_id) AS circuits,
       collect(DISTINCT st.pattern) AS strategies
ORDER BY size(circuits) DESC
```
Runs community detection over `Circuit -FAVOURS-> Strategy`, so circuits that win with the same dominant strategy end up in the same community. That's a second similarity signal, independent of the hand-picked features behind `SIMILAR_TO`. Each cluster's strategies are read back through its circuits' `FAVOURS` edges. If a circuit is passed, Python keeps only the cluster that contains it.

### T7 `key_races(circuit)`: PageRank
```cypher
CALL algo.pageRank('Circuit', 'SIMILAR_TO') YIELD node, score
RETURN node.circuit_id AS circuit_id, score
```
```cypher
MATCH (target:Circuit {circuit_id: $circuit})
MATCH (c:Circuit) WHERE c = target OR (target)-[:SIMILAR_TO]->(c)
MATCH (r:Race)-[:HELD_AT]->(c)
RETURN r.race_id AS race_id, c.circuit_id AS circuit_id, c = target AS same_circuit
```
PageRank over `SIMILAR_TO` scores a circuit high when many others resemble it, which makes its races the most representative evidence. The second query collects races at the circuit and at its similar circuits. Python then ranks them: same circuit first, then by circuit PageRank, rescaled so 1.0 = the most central circuit (raw scores are spread over the whole graph and too small to read).

### T8 `strategy_path(from_circuit, to_circuit, max_len)`: shortest path
```cypher
MATCH (a:Circuit {circuit_id: $from_circuit}), (b:Circuit {circuit_id: $to_circuit})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['FAVOURS', 'SIMILAR_TO'],
                   relDirection: 'both', maxLen: $max_len})
YIELD path
RETURN [n IN nodes(path) | coalesce(n.circuit_id, n.pattern)] AS path_nodes,
       [r IN relationships(path) | type(r)] AS path_links
```
Finds the shortest chain linking two circuits through shared dominant strategies or similarity, e.g. `monza -FAVOURS- M-H -FAVOURS- marina_bay`, ignoring edge direction. `algo.SPpaths` is used because Cypher `shortestPath()` in FalkorDB is directed-only. `coalesce` labels each node by its circuit ID or strategy pattern.

---

## 3. Memory tools

These read the user's own `mem_<user_id>` graph. Their `user_id` comes from the session in the agent runtime and is never offered to the model.

### T4 `recall_insights(ref_ids)`
```cypher
MATCH (i:Insight)-[:ABOUT]->(x:EntityRef)
WHERE x.ref_id IN $ref_ids AND NOT (i)<-[:SUPERSEDES]-()
OPTIONAL MATCH (i)-[:CONTRADICTS]-(other:Insight)
RETURN i.i_id AS i_id, i.statement AS statement, i.confidence AS confidence,
       i.times_confirmed AS times_confirmed, collect(other.statement) AS conflicts
ORDER BY times_confirmed DESC
```
Returns what this user's past sessions learned about these circuits. The `NOT (i)<-[:SUPERSEDES]-()` pattern skips insights a newer one has replaced. Contradictions (in either direction) come back with the insight, so the Analyst sees the conflict. The most-confirmed insights come first.

### T5 `trace(a_id)`: the "Why?" panel
Cypher can't cross graphs, so the trace reads the memory graph first, then looks up each evidence node in `f1`.

```cypher
// 1. the answer, its evidence stubs, tool calls and Critic reviews
MATCH (a:Analysis {a_id: $a_id})
OPTIONAL MATCH (a)-[:USED]->(ev:Evidence)
OPTIONAL MATCH (a)-[:RAN]->(q:QueryRun)
OPTIONAL MATCH (a)-[rv:REVIEWED]->(:Task)
RETURN a, collect(DISTINCT ev) AS evidence, collect(DISTINCT q) AS queries,
       collect(DISTINCT rv) AS reviews
```
Each `OPTIONAL MATCH` + `collect(DISTINCT …)` gathers one kind of related node without the others multiplying the rows.

```cypher
// 2. the question, the insights this answer recalled, and the ones it produced
MATCH (a:Analysis {a_id: $a_id})
OPTIONAL MATCH (a)-[:ANSWERS]->(qn:Question)
OPTIONAL MATCH (a)-[:RECALLED]->(recalled:Insight)
OPTIONAL MATCH (learned:Insight)-[:DERIVED_FROM]->(a)
RETURN qn, collect(DISTINCT recalled) AS recalled, collect(DISTINCT learned) AS learned
```
Shows memory reuse: which past insights fed this answer, and which ones it wrote or confirmed.

```cypher
// 3. the agent handoff chain, as a variable-length path
MATCH (:Analysis {a_id: $a_id})-[:ANSWERS]->(:Question)<-[:FOR_QUESTION]-(first:Task)
WHERE NOT ()-[:HANDED_OFF_TO]->(first)
MATCH chain = (first)-[:HANDED_OFF_TO*0..]->(last:Task)
WHERE NOT (last)-[:HANDED_OFF_TO]->()
RETURN [t IN nodes(chain) | {from_agent: t.from_agent, to_agent: t.to_agent,
                             summary: t.payload_summary}] AS steps
```
Starts at the Task nothing hands off to and follows `HANDED_OFF_TO` to the end. A variable-length path handles a chain with a Critic "revise" round (two extra steps) without a special case.

```cypher
// 4. on f1, once per evidence stub: the node and its 1-hop neighbourhood (shown for a Race stub)
MATCH (n:Race {race_id: $ref_id})
OPTIONAL MATCH (n)-[rel]-(m)
RETURN n, collect({type: type(rel), node: m}) AS neighbours
```
Labels can't be query parameters in Cypher, so the code holds this as a template, and the label and ID property (`Race` / `race_id` above) are filled in from a fixed allow-list (`Race`/`race_id`, `Entry`/`entry_id`, `Stint`/`stint_id`, `Circuit`/`circuit_id`), never from user input. Stubs with any other label are skipped. The neighbourhood is what the UI draws as the evidence subgraph.

---

## 4. Memory writes

Source: `pitgraph/agents/memory.py`. All of them write to the user's `mem_<user_id>` graph.

**Session and message chain**
```cypher
MERGE (u:User {user_id: $user_id})
MERGE (s:Session {session_id: $session_id})
ON CREATE SET s.started_at = $ts
MERGE (s)-[:BY]->(u)
```
Creates the user and session the first time, and does nothing on later calls (`MERGE`).

```cypher
MATCH (s:Session {session_id: $session_id})
OPTIONAL MATCH (prev:Message)-[:IN_SESSION]->(s)
WHERE NOT (prev)-[:NEXT]->()
CREATE (m:Message {m_id: $m_id, role: $role, text: $text, ts: $ts})-[:IN_SESSION]->(s)
FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:NEXT]->(m))
```
Appends a message and links it from the current last message (the one with no outgoing `NEXT`). The `FOREACH … CASE` is the Cypher idiom for "create this edge only if `prev` exists", so a session's first message has no link.

**Recent messages (episodic recall for follow-ups)**
```cypher
MATCH (last:Message)-[:IN_SESSION]->(:Session {session_id: $session_id})
WHERE NOT (last)-[:NEXT]->()
MATCH chain = (m:Message)-[:NEXT*0..]->(last)
RETURN m.role AS role, m.text AS text, length(chain) AS steps_back
ORDER BY steps_back LIMIT $limit
```
Walks back from the newest message along the chain. Ordering by path length instead of timestamps means two messages written in the same second can't swap places.

**Question and agent handoff**
```cypher
MATCH (m:Message {m_id: $m_id})
CREATE (:Question {q_id: $q_id, text: $text, intent: null})-[:FROM_MESSAGE]->(m)
```
```cypher
MATCH (q:Question {q_id: $q_id})
CREATE (t:Task {t_id: $t_id, from_agent: $from_agent, to_agent: $to_agent,
                status: 'done', payload_summary: $summary, created_at: $ts})-[:FOR_QUESTION]->(q)
WITH t
OPTIONAL MATCH (prev:Task {t_id: $previous})
FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:HANDED_OFF_TO]->(t))
```
Each agent handoff becomes a Task for the question, linked from the previous handoff. That builds the chain Planner → Retriever → Analyst → Critic (→ Analyst on revise) → MemoryWriter that T5 walks. The Planner then sets the question's intent: `MATCH (q:Question {q_id: $q_id}) SET q.intent = $intent`.

**Tool calls**
```cypher
CREATE (:QueryRun {qr_id: $qr_id, tool: $tool, params: $params,
                   row_count: $row_count, ok: $ok, error: $error, ts: $ts})
```
Every tool call is logged, failed ones included, as the record of how the agent recovered.

**The answer and its evidence**
```cypher
MATCH (q:Question {q_id: $q_id}), (t:Task {t_id: $t_id})
CREATE (a:Analysis {a_id: $a_id, verdict: $verdict, reasoning_summary: $reasoning,
                    confidence: $confidence, created_at: $ts})
CREATE (a)-[:ANSWERS]->(q)
CREATE (a)-[:PRODUCED_BY {agent: 'Analyst'}]->(t)
WITH a
UNWIND $qr_ids AS qr_id
MATCH (qr:QueryRun {qr_id: qr_id})
CREATE (a)-[:RAN]->(qr)
```
```cypher
MATCH (a:Analysis {a_id: $a_id})
UNWIND $evidence AS ev
CREATE (a)-[:USED]->(:Evidence {ref_label: ev.ref_label, ref_id: ev.ref_id,
                                weight: ev.weight, why: ev.why})
```
Stores the answer, links it to its question, the Analyst's task and every tool call it relied on, then adds one `Evidence` stub per cited `f1` node. Before this, the code drops any cited ID that doesn't appear in the retrieved rows, so the answer can't cite evidence it never saw. `UNWIND` writes all links in one round trip.

**Review, recalled insights, action**
```cypher
MATCH (a:Analysis {a_id: $a_id}), (t:Task {t_id: $t_id})
CREATE (a)-[:REVIEWED {agent: 'Critic', verdict: $verdict, note: $note}]->(t)
```
```cypher
MATCH (a:Analysis {a_id: $a_id})
UNWIND $i_ids AS i_id
MATCH (i:Insight {i_id: i_id})
MERGE (a)-[:RECALLED]->(i)
```
```cypher
MATCH (a:Analysis {a_id: $a_id})
CREATE (:Action {act_id: $act_id, type: $type, content: $content, created_at: $ts})-[:BASED_ON]->(a)
```
Records each Critic verdict, marks which past insights this answer reused (shown in the trace), and saves a strategy brief as an `Action` based on the answer.

**Insights: new, confirm, contradict, supersede**
```cypher
MATCH (a:Analysis {a_id: $a_id})
CREATE (i:Insight {i_id: $i_id, statement: $statement, confidence: $confidence,
                   times_confirmed: 1, created_at: $ts, updated_at: $ts})-[:DERIVED_FROM]->(a)
WITH i
UNWIND $circuits AS circuit
MERGE (x:EntityRef {ref_id: circuit})
ON CREATE SET x.ref_label = 'Circuit'
CREATE (i)-[:ABOUT]->(x)
```
A new insight, linked to the answer it came from and to one `EntityRef` per circuit. `MERGE` reuses the user's existing circuit ref, so all of a user's Monza insights hang off one node, which is what T4 matches on.

```cypher
MATCH (i:Insight {i_id: $i_id}), (a:Analysis {a_id: $a_id})
SET i.confidence = (i.confidence * i.times_confirmed + $confidence) / (i.times_confirmed + 1),
    i.times_confirmed = i.times_confirmed + 1,
    i.updated_at = $ts
MERGE (i)-[:DERIVED_FROM]->(a)
```
Confirms an insight: counts one more confirmation, folds the new confidence into a running average, and links the confirming answer.

```cypher
MATCH (new:Insight {i_id: $new}), (old:Insight {i_id: $old}) MERGE (new)-[:CONTRADICTS]->(old)
MATCH (new:Insight {i_id: $new}), (old:Insight {i_id: $old}) MERGE (new)-[:SUPERSEDES]->(old)
```
These are two separate queries, because relationship types can't be parameters. `CONTRADICTS` keeps both insights current, and recall shows the conflict. `SUPERSEDES` retires the old insight from recall (T4 skips it), but it stays in the graph as history.

---

## 5. Procedural memory (`mem_global`)

```cypher
MERGE (p:Procedure {intent: $intent, tool_sequence: $seq})
ON CREATE SET p.success_count = 0, p.fail_count = 0
SET p.success_count = p.success_count + $success,
    p.fail_count = p.fail_count + $fail,
    p.updated_at = $ts
```
After each answer, counts one outcome for the plan (intent + comma-separated tool names). It's a success if the Critic approved, and a failure if it was a forced approval with an issue still open. It's one atomic query, so concurrent users can't lose an update. It stores no user data, which is why `mem_global` can be shared.

```cypher
MATCH (p:Procedure {intent: $intent, tool_sequence: $seq})
MERGE (f:Procedure {intent: $intent, tool_sequence: $fallback})
ON CREATE SET f.success_count = 0, f.fail_count = 0
MERGE (p)-[:FALLBACK_TO]->(f)
```
When the recovery path ran, links the recovery tools as this plan's fallback. Fallback nodes keep zero counts, so they never rank as a plan of their own.

```cypher
MATCH (p:Procedure)
WHERE p.success_count + p.fail_count > 0
OPTIONAL MATCH (p)-[:FALLBACK_TO]->(f:Procedure)
WITH p, collect(f.tool_sequence) AS fallbacks,
     (p.success_count + 1.0) / (p.success_count + p.fail_count + 2) AS score
RETURN p.intent AS intent, p.tool_sequence AS tool_sequence, p.success_count AS successes,
       p.fail_count AS failures, fallbacks, score
ORDER BY intent, score DESC, successes DESC
```
The best plan per intent, shown to the Planner before it plans. The score `(successes + 1) / (uses + 2)` is a smoothed success rate, so a plan that worked 3 times out of 3 beats one that worked once, and a single failure doesn't rule a plan out. Python keeps the top row per intent.

---

## 6. Derived data

Source: `pitgraph/ingest/derive.py`. These run once after ingestion (part of `python -m pitgraph.rebuild`) and are safe to re-run.

**Pit loss per circuit**
```cypher
MATCH (c:Circuit)<-[:HELD_AT]-(:Race)<-[:IN_RACE]-(:Entry)-[:PITTED]->(p:PitStop)
WHERE p.duration_s >= $low AND p.duration_s <= $high
RETURN c.circuit_id, collect(p.duration_s)
```
Collects pit-lane times between 10 and 60 s per circuit (outside that range are garage stops, red-flag stops or bad data). Python takes the median and writes it back with `UNWIND $rows … MATCH (c:Circuit {circuit_id: row.circuit_id}) SET c.pit_loss_s = row.pit_loss_s`.

**`Strategy` nodes and `USED_STRATEGY`**
```cypher
MATCH (e:Entry)-[:HAD_STINT]->(s:Stint)
OPTIONAL MATCH (s)-[:ON_COMPOUND]->(k:Compound)
WITH e, s, k ORDER BY s.stint_no
RETURN e.entry_id, count(s), collect(k.name)
```
```cypher
UNWIND $rows AS row
MERGE (st:Strategy {pattern: row.pattern})
SET st.stops = row.stops
WITH st, row
MATCH (e:Entry {entry_id: row.entry_id})
MERGE (e)-[:USED_STRATEGY]->(st)
```
Reads each entry's compounds in stint order and turns them into a pattern (`SOFT, MEDIUM, HARD` → `"S-M-H"`). Entries with an unknown compound are skipped: `collect` drops nulls, so the list comes back shorter than the stint count. Each pattern becomes one shared `Strategy` node.

**`FAVOURS`: each circuit's dominant strategy (input to T6 and T8)**
```cypher
MATCH (c:Circuit)<-[:HELD_AT]-(r:Race)<-[:IN_RACE]-(e:Entry)-[:USED_STRATEGY]->(st:Strategy)
WHERE e.finish_pos <= $top AND NOT r.wet
WITH c, st, count(e) AS n
WITH c, collect({st: st, n: n}) AS uses, max(n) AS best
UNWIND uses AS u
WITH c, u WHERE u.n = best
WITH c, u.st AS st, u.n AS n
MERGE (c)-[f:FAVOURS]->(st)
SET f.count = n
```
Counts each strategy's top-10 finishes per circuit in dry races, then keeps only the most-used one (ties keep both). The `collect` / `max` / `UNWIND` steps compare each strategy's count with the circuit's best. Only the dominant strategy is linked: linking every strategy would connect almost every circuit through `M-H`, and community detection would return one giant cluster. Wet races are left out because one-off rain-tyre sequences say nothing about the circuit.

**`SIMILAR_TO`: feature similarity (input to T2, T7, T8)**
```cypher
MATCH (c:Circuit)<-[:HELD_AT]-(r:Race)
OPTIONAL MATCH (r)-[:HAD_NEUTRALISATION]->(n:Neutralisation)
WITH c, r, count(n) AS neutralisations
OPTIONAL MATCH (r)<-[:IN_RACE]-(e:Entry) WHERE e.stops IS NOT NULL
WITH c, r, neutralisations, avg(e.stops) AS race_stops
RETURN c.circuit_id, c.type, c.length_km, c.pit_loss_s, avg(neutralisations), avg(race_stops)
```
```cypher
UNWIND $rows AS row
MATCH (a:Circuit {circuit_id: row.a}), (b:Circuit {circuit_id: row.b})
MERGE (a)-[s:SIMILAR_TO]->(b)
SET s.score = row.score, s.basis = row.basis
```
Builds one feature row per circuit: street or permanent, lap length, pit loss, average stops, and neutralisations per race. Neutralisations are counted per race first and then averaged, so a circuit with more races in the data doesn't look more chaotic. In Python, each feature is min-max scaled to 0–1 so all five count equally. The score is 1 − the mean absolute difference. Each circuit gets edges to its 3 closest circuits, with a `basis` naming the traits they share (e.g. `"street,low_sc,one_stop"`).

Before each step, the old derived edges are deleted (`MATCH ()-[r:FAVOURS]->() DELETE r`, and the same for the others), so a re-run replaces them instead of adding duplicates.

---

## 7. Ingestion

Source: `pitgraph/ingest/load_graph.py`. Every node type is written in batches with the same pattern, here for stints:
```cypher
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
```
`UNWIND` sends a whole batch of rows in one round trip to the Cloud instance instead of one per node. `MERGE` on the ID makes re-runs safe. Seasons, circuits, races, drivers, teams, entries, pit stops and neutralisations follow the same shape.

```cypher
MATCH (e:Entry)-[:HAD_STINT]->(a:Stint), (e)-[:HAD_STINT]->(b:Stint)
WHERE b.stint_no = a.stint_no + 1
MERGE (a)-[:NEXT]->(b)
```
Chains each entry's stints in order (stint 1 → stint 2 → …), which makes a strategy a path in the graph.

```cypher
MATCH (e:Entry)-[:HAD_STINT]->(s:Stint)
WITH e, count(s) AS stints
SET e.stops = stints - 1
```
Sets stops = stints − 1, so an entry's stop count always matches its strategy pattern.

Indexes (`pitgraph/graph/schema.py`) are range indexes on every ID property that the queries above look up by: `race_id`, `circuit_id`, `entry_id`, `stint_id` and so on in `f1`; `session_id`, `m_id`, `a_id`, `i_id`, `ref_id` and so on in each memory graph; `intent` in `mem_global`.

---

## 8. Memory panel reads

Source: `pitgraph/api/views.py`. These are only for the UI's Memory panel. The agents never call them.

```cypher
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
```
Every insight, current or not. The UI strikes through superseded ones, so the user can see how a memory changed over time.

```cypher
MATCH (a:Analysis)-[:ANSWERS]->(qn:Question)-[:FROM_MESSAGE]->(:Message)-[:IN_SESSION]->(s:Session)
RETURN a.a_id AS a_id, qn.text AS question, qn.intent AS intent, a.verdict AS verdict,
       a.confidence AS confidence, a.created_at AS created_at, s.session_id AS session_id
ORDER BY created_at DESC LIMIT $limit
```
Past answers across all sessions, newest first. Each can be opened in the Why? panel.

```cypher
MATCH (act:Action)-[:BASED_ON]->(a:Analysis)
RETURN act.act_id AS act_id, act.type AS type, act.content AS content,
       act.created_at AS created_at, a.a_id AS a_id
ORDER BY created_at DESC LIMIT $limit
```
Saved strategy briefs and the answer each one is based on.

```cypher
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC
```
Node counts per label: how much the user's memory graph holds.

```cypher
MATCH (p:Procedure)
WHERE p.success_count + p.fail_count > 0
OPTIONAL MATCH (p)-[:FALLBACK_TO]->(f:Procedure)
RETURN p.intent AS intent, p.tool_sequence AS tool_sequence, p.success_count AS successes,
       p.fail_count AS failures, collect(f.tool_sequence) AS fallbacks
ORDER BY intent, successes DESC
```
Shared procedural memory: each plan's record per intent, and its fallback tools.
