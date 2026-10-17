"""Memory tests: insights (new / confirm / contradict / supersede), the guards on the
model's insight decisions, procedural memory, session history and user isolation.

No LLM calls: the model's decisions are built by hand and passed to insights.apply.
Runs against the live FalkorDB instance on throwaway graphs, deleted afterwards.

Run: pytest tests/test_memory.py
"""

import uuid

import pytest

from pitgraph.agents import insights, memory
from pitgraph.agents.insights import InsightDecision, InsightUpdate
from pitgraph.graph import queries as q
from pitgraph.graph.db import get_client, get_graph

VALID_CIRCUITS = {"monza", "spa", "marina_bay"}


def make_user() -> str:
    return f"test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def user():
    """A fresh user memory graph with two analyses to hang insights on."""
    user_id = make_user()
    graph = memory.memory_graph(user_id)
    graph.query("CREATE (:Analysis {a_id: 'a1', confidence: 0.8}), (:Analysis {a_id: 'a2', confidence: 0.6})")
    yield user_id, graph
    graph.delete()


@pytest.fixture
def global_graph():
    """Stands in for mem_global, so tests never touch the real procedures."""
    graph = get_graph(f"mem_test_global_{uuid.uuid4().hex[:8]}")
    yield graph
    if graph.name in get_client().list_graphs():  # a test that writes nothing creates no graph
        graph.delete()


def decision(statement="Monza: M-H one-stop won 3 of 3", relation="new", target=None,
             circuits=("monza",), confidence=0.7) -> InsightDecision:
    return InsightDecision(statement=statement, circuits=list(circuits), confidence=confidence,
                           relation=relation, existing_i_id=target)


def write(graph, a_id, *decisions, stored=(), max_confidence=0.9):
    update = InsightUpdate(insights=list(decisions))
    return insights.apply(graph, a_id, update, list(stored), VALID_CIRCUITS, max_confidence)


# ---------- insights ----------

def test_new_insight_is_recalled_for_its_circuit(user):
    _, graph = user
    write(graph, "a1", decision())

    recalled = q.recall_insights(graph, ["monza"])

    assert [r["statement"] for r in recalled] == ["Monza: M-H one-stop won 3 of 3"]
    assert recalled[0]["times_confirmed"] == 1
    assert q.recall_insights(graph, ["spa"]) == []


def test_confirming_counts_and_averages_confidence(user):
    _, graph = user
    [first] = write(graph, "a1", decision(confidence=0.8))
    stored = q.recall_insights(graph, ["monza"])

    [second] = write(graph, "a2", decision(relation="confirms", target=first["i_id"], confidence=0.6), stored=stored)

    [row] = q.recall_insights(graph, ["monza"])
    assert second["i_id"] == first["i_id"] and second["relation"] == "confirms"
    assert row["times_confirmed"] == 2
    assert row["confidence"] == pytest.approx(0.7)
    derived = graph.ro_query("MATCH (:Insight)-[:DERIVED_FROM]->(a:Analysis) RETURN a.a_id ORDER BY a.a_id").result_set
    assert derived == [["a1"], ["a2"]]


def test_superseded_insight_is_no_longer_recalled(user):
    _, graph = user
    [old] = write(graph, "a1", decision(statement="Monza is a two-stop"))
    stored = q.recall_insights(graph, ["monza"])

    write(graph, "a2", decision(statement="Monza is a one-stop", relation="supersedes", target=old["i_id"]), stored=stored)

    assert [r["statement"] for r in q.recall_insights(graph, ["monza"])] == ["Monza is a one-stop"]


def test_contradiction_keeps_both_and_shows_the_conflict(user):
    _, graph = user
    write(graph, "a1", decision(statement="Monza: one-stop is best"))
    stored = q.recall_insights(graph, ["monza"])

    write(graph, "a2", decision(statement="Monza: two-stop wins under SC", relation="contradicts",
                                target=stored[0]["i_id"]), stored=stored)

    recalled = {r["statement"]: r["conflicts"] for r in q.recall_insights(graph, ["monza"])}
    assert recalled == {
        "Monza: one-stop is best": ["Monza: two-stop wins under SC"],
        "Monza: two-stop wins under SC": ["Monza: one-stop is best"],
    }


def test_apply_guards_the_models_choices(user):
    _, graph = user
    [existing] = write(graph, "a1", decision(statement="Spa: M-H works", circuits=["spa"]))
    stored = q.recall_insights(graph, ["spa"])

    written = write(
        graph, "a2",
        decision(circuits=["atlantis"]),                                           # unknown circuit: dropped
        decision(statement="Monza: X", relation="confirms", target="i_missing"),   # not recalled: becomes new
        decision(statement="Spa: Y", circuits=["spa"], relation="confirms", target=existing["i_id"]),  # 3rd: over cap
        stored=stored, max_confidence=0.5,
    )

    assert [(w["statement"], w["relation"], w["target"]) for w in written] == [("Monza: X", "new", None)]
    monza = q.recall_insights(graph, ["monza"])
    assert monza[0]["confidence"] == 0.5  # capped at the answer's confidence


def test_two_decisions_about_the_same_stored_insight_write_once(user):
    _, graph = user
    [existing] = write(graph, "a1", decision())
    stored = q.recall_insights(graph, ["monza"])

    written = write(graph, "a2",
                    decision(relation="confirms", target=existing["i_id"]),
                    decision(statement="Monza: refined", relation="supersedes", target=existing["i_id"]),
                    stored=stored)

    assert [w["relation"] for w in written] == ["confirms"]
    assert q.recall_insights(graph, ["monza"])[0]["times_confirmed"] == 2


def test_link_recalled_marks_reused_insights(user):
    _, graph = user
    [ins] = write(graph, "a1", decision())

    memory.link_recalled(graph, "a2", [ins["i_id"]])

    linked = graph.ro_query("MATCH (:Analysis {a_id: 'a2'})-[:RECALLED]->(i:Insight) RETURN i.i_id").result_set
    assert linked == [[ins["i_id"]]]


def test_memories_are_isolated_per_user(user):
    _, graph = user
    write(graph, "a1", decision())
    other = memory.memory_graph(make_user())
    other.query("CREATE (:Analysis {a_id: 'a1'})")
    try:
        assert q.recall_insights(other, ["monza"]) == []
    finally:
        other.delete()


# ---------- episodic: session history ----------

def test_recent_messages_are_per_session_and_oldest_first(user):
    user_id, graph = user
    memory.start_session(graph, user_id, "s_one")
    memory.start_session(graph, user_id, "s_two")
    for role, text in [("user", "q1"), ("assistant", "a1"), ("user", "q2")]:
        memory.add_message(graph, "s_one", role, text)  # same second: order comes from NEXT
    memory.add_message(graph, "s_two", "user", "other session")

    history = memory.recent_messages(graph, "s_one", limit=2)

    assert [m["text"] for m in history] == ["a1", "q2"]


# ---------- procedural memory ----------

def test_best_procedure_ranks_by_smoothed_success_rate(global_graph):
    reliable = ["winning_strategies", "strategy_outcomes"]
    shaky = ["key_races"]
    for _ in range(3):
        memory.record_procedure(global_graph, "strategy_choice", reliable, success=True, fallback=None)
    memory.record_procedure(global_graph, "strategy_choice", shaky, success=True, fallback=None)
    memory.record_procedure(global_graph, "strategy_choice", shaky, success=False, fallback=None)

    best = memory.best_procedures(global_graph)

    assert best["strategy_choice"]["tool_sequence"] == reliable  # 4/5 beats 2/4
    assert (best["strategy_choice"]["successes"], best["strategy_choice"]["failures"]) == (3, 0)


def test_fallback_is_linked_but_never_ranked_as_a_plan(global_graph):
    memory.record_procedure(global_graph, "sc_impact", ["neutralisation_impact"], success=True,
                            fallback=["similar_circuits", "strategy_clusters"])

    best = memory.best_procedures(global_graph)

    assert list(best) == ["sc_impact"]
    assert best["sc_impact"]["tool_sequence"] == ["neutralisation_impact"]
    assert best["sc_impact"]["fallbacks"] == ["similar_circuits,strategy_clusters"]


def test_best_procedures_is_empty_before_any_answers(global_graph):
    assert memory.best_procedures(global_graph) == {}
