"""Reads and writes for the memory graphs.

Every step of the agent flow leaves a node behind in mem_<user_id>, so any answer
can be traced:
  Session, Message      what was said (episodic)
  Question, Task        the question and the agent-to-agent handoff chain
  QueryRun              every tool call, including failed ones
  Analysis, Evidence    the answer and the f1 nodes it relied on
  Action                a saved strategy brief
  Insight, EntityRef    what the agents learned, per circuit (semantic)
Procedures (procedural memory) live in mem_global, shared by all users.
"""

import json
import uuid
from datetime import UTC, datetime

from falkordb import Graph
from redis.exceptions import ResponseError

from pitgraph.graph.db import user_memory_graph
from pitgraph.graph.schema import USER_MEMORY_INDEXES, ensure_indexes

_indexed_users: set[str] = set()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def memory_graph(user_id: str) -> Graph:
    """The user's memory graph, with indexes created the first time we touch it."""
    graph = user_memory_graph(user_id)
    if user_id not in _indexed_users:
        ensure_indexes(graph, USER_MEMORY_INDEXES)
        _indexed_users.add(user_id)
    return graph


# ---------- session and conversation ----------

def start_session(graph: Graph, user_id: str, session_id: str) -> None:
    graph.query("""
        MERGE (u:User {user_id: $user_id})
        MERGE (s:Session {session_id: $session_id})
        ON CREATE SET s.started_at = $ts
        MERGE (s)-[:BY]->(u)
    """, {"user_id": user_id, "session_id": session_id, "ts": now()})


def add_message(graph: Graph, session_id: str, role: str, text: str) -> str:
    """Append a message to the session, chained to the previous one with NEXT."""
    m_id = new_id("m")
    graph.query("""
        MATCH (s:Session {session_id: $session_id})
        OPTIONAL MATCH (prev:Message)-[:IN_SESSION]->(s)
        WHERE NOT (prev)-[:NEXT]->()
        CREATE (m:Message {m_id: $m_id, role: $role, text: $text, ts: $ts})-[:IN_SESSION]->(s)
        FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:NEXT]->(m))
    """, {"session_id": session_id, "m_id": m_id, "role": role, "text": text, "ts": now()})
    return m_id


def add_question(graph: Graph, m_id: str, text: str) -> str:
    q_id = new_id("q")
    graph.query("""
        MATCH (m:Message {m_id: $m_id})
        CREATE (:Question {q_id: $q_id, text: $text, intent: null})-[:FROM_MESSAGE]->(m)
    """, {"m_id": m_id, "q_id": q_id, "text": text})
    return q_id


def set_intent(graph: Graph, q_id: str, intent: str) -> None:
    graph.query("MATCH (q:Question {q_id: $q_id}) SET q.intent = $intent", {"q_id": q_id, "intent": intent})


# ---------- agent handoffs ----------

def add_task(
    graph: Graph, q_id: str, from_agent: str, to_agent: str, summary: str, previous_t_id: str | None
) -> str:
    """Record one handoff. Each Task links to the one before it, forming the chain
    Planner -> Retriever -> Analyst -> Critic (-> Analyst on a revise) -> MemoryWriter."""
    t_id = new_id("t")
    graph.query("""
        MATCH (q:Question {q_id: $q_id})
        CREATE (t:Task {t_id: $t_id, from_agent: $from_agent, to_agent: $to_agent,
                        status: 'done', payload_summary: $summary, created_at: $ts})-[:FOR_QUESTION]->(q)
        WITH t
        OPTIONAL MATCH (prev:Task {t_id: $previous})
        FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:HANDED_OFF_TO]->(t))
    """, {
        "q_id": q_id, "t_id": t_id, "from_agent": from_agent, "to_agent": to_agent,
        "summary": summary[:500], "previous": previous_t_id or "", "ts": now(),
    })
    return t_id


def add_query_run(
    graph: Graph, tool: str, params: dict, row_count: int, ok: bool, error: str | None
) -> str:
    """Log a tool call. Failed calls are kept: they show how the agent recovered."""
    qr_id = new_id("qr")
    graph.query("""
        CREATE (:QueryRun {qr_id: $qr_id, tool: $tool, params: $params,
                           row_count: $row_count, ok: $ok, error: $error, ts: $ts})
    """, {
        "qr_id": qr_id, "tool": tool, "params": json.dumps(params, sort_keys=True),
        "row_count": row_count, "ok": ok, "error": error, "ts": now(),
    })
    return qr_id


# ---------- the answer ----------

def add_analysis(
    graph: Graph,
    q_id: str,
    analysis: dict,
    analyst_t_id: str,
    qr_ids: list[str],
    evidence: list[dict],
) -> str:
    """Write the answer, link it to its question, the analyst's task, the queries it
    ran, and the f1 nodes it used as evidence (stubs resolved later by trace)."""
    a_id = new_id("a")
    graph.query("""
        MATCH (q:Question {q_id: $q_id}), (t:Task {t_id: $t_id})
        CREATE (a:Analysis {a_id: $a_id, verdict: $verdict, reasoning_summary: $reasoning,
                            confidence: $confidence, created_at: $ts})
        CREATE (a)-[:ANSWERS]->(q)
        CREATE (a)-[:PRODUCED_BY {agent: 'Analyst'}]->(t)
        WITH a
        UNWIND $qr_ids AS qr_id
        MATCH (qr:QueryRun {qr_id: qr_id})
        CREATE (a)-[:RAN]->(qr)
    """, {
        "q_id": q_id, "t_id": analyst_t_id, "a_id": a_id, "qr_ids": qr_ids or [""],
        "verdict": analysis["verdict"], "reasoning": analysis["reasoning_summary"],
        "confidence": analysis["confidence"], "ts": now(),
    })
    if evidence:
        graph.query("""
            MATCH (a:Analysis {a_id: $a_id})
            UNWIND $evidence AS ev
            CREATE (a)-[:USED]->(:Evidence {ref_label: ev.ref_label, ref_id: ev.ref_id,
                                            weight: ev.weight, why: ev.why})
        """, {"a_id": a_id, "evidence": evidence})
    return a_id


def add_review(graph: Graph, a_id: str, critic_t_id: str, verdict: str, note: str) -> None:
    graph.query("""
        MATCH (a:Analysis {a_id: $a_id}), (t:Task {t_id: $t_id})
        CREATE (a)-[:REVIEWED {agent: 'Critic', verdict: $verdict, note: $note}]->(t)
    """, {"a_id": a_id, "t_id": critic_t_id, "verdict": verdict, "note": note})


def add_action(graph: Graph, a_id: str, action_type: str, content: dict) -> str:
    """An action taken because of the answer, e.g. a saved strategy brief."""
    act_id = new_id("act")
    graph.query("""
        MATCH (a:Analysis {a_id: $a_id})
        CREATE (:Action {act_id: $act_id, type: $type, content: $content, created_at: $ts})-[:BASED_ON]->(a)
    """, {"a_id": a_id, "act_id": act_id, "type": action_type,
          "content": json.dumps(content), "ts": now()})
    return act_id


def link_recalled(graph: Graph, a_id: str, i_ids: list[str]) -> None:
    """Mark the past insights the Planner recalled for this answer, so the trace
    shows which memories were reused."""
    graph.query("""
        MATCH (a:Analysis {a_id: $a_id})
        UNWIND $i_ids AS i_id
        MATCH (i:Insight {i_id: i_id})
        MERGE (a)-[:RECALLED]->(i)
    """, {"a_id": a_id, "i_ids": i_ids})


# ---------- episodic recall ----------

def recent_messages(graph: Graph, session_id: str, limit: int) -> list[dict]:
    """The session's last `limit` messages, oldest first (for follow-up questions).
    Walks the NEXT chain back from the newest message, so order never depends on
    timestamps."""
    result = graph.ro_query("""
        MATCH (last:Message)-[:IN_SESSION]->(:Session {session_id: $session_id})
        WHERE NOT (last)-[:NEXT]->()
        MATCH chain = (m:Message)-[:NEXT*0..]->(last)
        RETURN m.role AS role, m.text AS text, length(chain) AS steps_back
        ORDER BY steps_back LIMIT $limit
    """, {"session_id": session_id, "limit": limit}).result_set
    return [{"role": role, "text": text} for role, text, _steps in reversed(result)]


# ---------- semantic memory: insights ----------
# An insight is a short, reusable fact about one or more circuits, derived from an
# approved answer. Later answers can confirm, contradict or supersede it.

def new_insight(graph: Graph, a_id: str, statement: str, confidence: float, circuits: list[str]) -> str:
    i_id = new_id("i")
    graph.query("""
        MATCH (a:Analysis {a_id: $a_id})
        CREATE (i:Insight {i_id: $i_id, statement: $statement, confidence: $confidence,
                           times_confirmed: 1, created_at: $ts, updated_at: $ts})-[:DERIVED_FROM]->(a)
        WITH i
        UNWIND $circuits AS circuit
        MERGE (x:EntityRef {ref_id: circuit})
        ON CREATE SET x.ref_label = 'Circuit'
        CREATE (i)-[:ABOUT]->(x)
    """, {"a_id": a_id, "i_id": i_id, "statement": statement,
          "confidence": confidence, "circuits": circuits, "ts": now()})
    return i_id


def confirm_insight(graph: Graph, i_id: str, a_id: str, confidence: float) -> None:
    """Another answer reached the same conclusion: count it, and fold its confidence
    into a running average."""
    graph.query("""
        MATCH (i:Insight {i_id: $i_id}), (a:Analysis {a_id: $a_id})
        SET i.confidence = (i.confidence * i.times_confirmed + $confidence) / (i.times_confirmed + 1),
            i.times_confirmed = i.times_confirmed + 1,
            i.updated_at = $ts
        MERGE (i)-[:DERIVED_FROM]->(a)
    """, {"i_id": i_id, "a_id": a_id, "confidence": confidence, "ts": now()})


# Relationship types can't be query parameters, so each one has its own query.
INSIGHT_LINKS = {
    # Both stay current; recall shows the conflict.
    "contradicts": "MATCH (new:Insight {i_id: $new}), (old:Insight {i_id: $old}) MERGE (new)-[:CONTRADICTS]->(old)",
    # The old one is retired: recall (T4) skips any insight that has been superseded.
    "supersedes": "MATCH (new:Insight {i_id: $new}), (old:Insight {i_id: $old}) MERGE (new)-[:SUPERSEDES]->(old)",
}


def link_insight(graph: Graph, relation: str, new_i_id: str, old_i_id: str) -> None:
    graph.query(INSIGHT_LINKS[relation], {"new": new_i_id, "old": old_i_id})


# ---------- procedural memory (mem_global) ----------
# A Procedure is a tool sequence the Planner used for an intent. It counts as a
# success when the Critic approves the answer (after at most one revision), and a
# failure when the Critic still had an open issue at the end. Shared by all users:
# no personal data, only tool names.

def record_procedure(
    graph: Graph, intent: str, tool_sequence: list[str], success: bool, fallback: list[str] | None
) -> None:
    """Count one outcome for (intent, tool_sequence). If the recovery path ran, link
    the tools it added as this procedure's fallback."""
    params = {
        "intent": intent, "seq": ",".join(tool_sequence), "ts": now(),
        "success": int(success), "fail": int(not success), "fallback": ",".join(fallback or []),
    }
    graph.query("""
        MERGE (p:Procedure {intent: $intent, tool_sequence: $seq})
        ON CREATE SET p.success_count = 0, p.fail_count = 0
        SET p.success_count = p.success_count + $success,
            p.fail_count = p.fail_count + $fail,
            p.updated_at = $ts
    """, params)
    if fallback:
        # Fallback nodes keep zero counts, so they never rank as a plan of their own.
        graph.query("""
            MATCH (p:Procedure {intent: $intent, tool_sequence: $seq})
            MERGE (f:Procedure {intent: $intent, tool_sequence: $fallback})
            ON CREATE SET f.success_count = 0, f.fail_count = 0
            MERGE (p)-[:FALLBACK_TO]->(f)
        """, params)


# Ranked by (successes + 1) / (uses + 2): a plan that worked 3 of 3 times beats one
# that worked once, and one failure doesn't rule a plan out for good.
BEST_PROCEDURES = """
MATCH (p:Procedure)
WHERE p.success_count + p.fail_count > 0
OPTIONAL MATCH (p)-[:FALLBACK_TO]->(f:Procedure)
WITH p, collect(f.tool_sequence) AS fallbacks,
     (p.success_count + 1.0) / (p.success_count + p.fail_count + 2) AS score
RETURN p.intent AS intent, p.tool_sequence AS tool_sequence, p.success_count AS successes,
       p.fail_count AS failures, fallbacks, score
ORDER BY intent, score DESC, successes DESC
"""


def best_procedures(graph: Graph) -> dict[str, dict]:
    """intent -> the best-scoring procedure for it (empty before any answers)."""
    try:
        result = graph.ro_query(BEST_PROCEDURES)
    except ResponseError:
        return {}  # mem_global doesn't exist yet
    columns = [name for _type, name in result.header]
    best: dict[str, dict] = {}
    for row in result.result_set:
        proc = dict(zip(columns, row, strict=True))
        proc["tool_sequence"] = proc["tool_sequence"].split(",") if proc["tool_sequence"] else []
        best.setdefault(proc["intent"], proc)  # rows are sorted best-first per intent
    return best
