"""Integration tests for graph tools T1-T8.

These run against the live FalkorDB instance, so `f1` must be loaded first
(load_graph + derive). T4/T5 use a throwaway memory graph that is deleted after.

Run: pytest tests/test_queries.py
"""

import uuid

import pytest

from pitgraph.graph import queries as q
from pitgraph.graph.db import f1_graph, get_graph


@pytest.fixture(scope="module")
def f1():
    return f1_graph()


@pytest.fixture
def memory():
    """A temporary memory graph with one analysis, two insights and evidence."""
    graph = get_graph(f"mem_test_{uuid.uuid4().hex[:8]}")
    graph.query("""
        CREATE (a:Analysis {a_id: 'a1', verdict: 'one-stop M-H', confidence: 0.8}),
               (qn:Question {q_id: 'q1', text: 'Best strategy at Monza?'}),
               (t0:Task {t_id: 't0', from_agent: 'Planner', to_agent: 'Retriever'}),
               (q:QueryRun {qr_id: 'qr1', tool: 'winning_strategies', ok: true}),
               (t:Task {t_id: 't1', from_agent: 'Analyst', to_agent: 'Critic'}),
               (ev:Evidence {ref_label: 'Race', ref_id: '2024_16', weight: 1.0}),
               (bad:Evidence {ref_label: 'NotALabel', ref_id: 'x', weight: 0.1}),
               (x:EntityRef {ref_label: 'Circuit', ref_id: 'monza'}),
               (old:Insight {i_id: 'i1', statement: 'Monza is a two-stop', confidence: 0.4, times_confirmed: 1}),
               (new:Insight {i_id: 'i2', statement: 'Monza is a one-stop', confidence: 0.9, times_confirmed: 3}),
               (other:Insight {i_id: 'i3', statement: 'Monza two-stops win under SC', confidence: 0.5, times_confirmed: 1}),
               (a)-[:RAN]->(q), (a)-[:USED]->(ev), (a)-[:USED]->(bad),
               (a)-[:REVIEWED {agent: 'Critic', verdict: 'approve', note: 'ok'}]->(t),
               (a)-[:ANSWERS]->(qn), (t0)-[:FOR_QUESTION]->(qn), (t)-[:FOR_QUESTION]->(qn),
               (t0)-[:HANDED_OFF_TO]->(t), (a)-[:RECALLED]->(old), (new)-[:DERIVED_FROM]->(a),
               (old)-[:ABOUT]->(x), (new)-[:ABOUT]->(x), (other)-[:ABOUT]->(x),
               (new)-[:SUPERSEDES]->(old), (new)-[:CONTRADICTS]->(other)
    """)
    yield graph
    graph.delete()


# ---------- T1 / T1b ----------

def test_winning_strategies_returns_top_finishers_with_tyre_sequence(f1):
    result = q.winning_strategies(f1, "monza", top_n=3)

    assert len(result) == 9  # 3 races x top 3
    assert all(r["finish_pos"] <= 3 for r in result)
    assert all(len(r["strategy"]) == r["stops"] + 1 for r in result)


def test_winning_strategies_is_empty_for_unknown_circuit(f1):
    assert q.winning_strategies(f1, "nowhere") == []


def test_strategy_outcomes_ranks_by_positions_gained(f1):
    result = q.strategy_outcomes(f1, "monza")

    gains = [r["avg_positions_gained"] for r in result]
    assert gains == sorted(gains, reverse=True)
    assert all(r["uses"] >= 2 for r in result)
    assert gains[-1] < 0  # at least one strategy to avoid


# ---------- T2 / T3 ----------

def test_similar_circuits_returns_three_explained_matches(f1):
    result = q.similar_circuits(f1, "monza")

    assert len(result) == 3
    assert all(r["basis"] and r["races"] for r in result)
    assert [r["score"] for r in result] == sorted((r["score"] for r in result), reverse=True)


def test_neutralisation_impact_counts_stops_under_safety_car(f1):
    result = q.neutralisation_impact(f1, "marina_bay")

    sc = [r for r in result if r["race_id"] == "2023_15" and r["type"] == "SC"]
    assert sc and sc[0]["stops_under_neutralisation"] > 0


# ---------- T4 / T5 (memory graph) ----------

def test_recall_insights_skips_superseded_and_shows_conflicts(memory):
    result = q.recall_insights(memory, ["monza"])

    statements = {r["statement"] for r in result}
    assert "Monza is a two-stop" not in statements  # superseded
    current = next(r for r in result if r["i_id"] == "i2")
    assert current["conflicts"] == ["Monza two-stops win under SC"]


def test_trace_resolves_evidence_into_f1_and_skips_unknown_labels(memory, f1):
    result = q.trace(memory, f1, "a1")

    assert result["analysis"]["verdict"] == "one-stop M-H"
    assert [e["node"]["race_id"] for e in result["evidence"]] == ["2024_16"]
    assert any(n["type"] == "HELD_AT" for n in result["evidence"][0]["neighbours"])
    assert result["queries"][0]["tool"] == "winning_strategies"
    assert result["reviews"][0]["verdict"] == "approve"


def test_trace_shows_question_handoff_chain_and_memory_used(memory, f1):
    result = q.trace(memory, f1, "a1")

    assert result["question"]["text"] == "Best strategy at Monza?"
    assert [(h["from_agent"], h["to_agent"]) for h in result["handoffs"]] == [
        ("Planner", "Retriever"), ("Analyst", "Critic"),
    ]
    assert [i["i_id"] for i in result["recalled_insights"]] == ["i1"]
    assert [i["i_id"] for i in result["learned_insights"]] == ["i2"]


def test_trace_returns_none_for_missing_analysis(memory, f1):
    assert q.trace(memory, f1, "missing") is None


# ---------- T6 / T7 / T8 (algorithms) ----------

def test_strategy_clusters_cover_every_circuit_once(f1):
    clusters = q.strategy_clusters(f1)

    circuits = [c for cluster in clusters for c in cluster["circuits"]]
    assert len(circuits) == len(set(circuits)) == 24
    assert all(cluster["strategies"] for cluster in clusters)


def test_strategy_clusters_filters_to_the_given_circuit(f1):
    result = q.strategy_clusters(f1, "monza")

    assert len(result) == 1 and "monza" in result[0]["circuits"]


def test_key_races_puts_own_circuit_first_then_ranked_neighbours(f1):
    result = q.key_races(f1, "monza")

    own = [r for r in result if r["same_circuit"]]
    others = [r for r in result if not r["same_circuit"]]
    assert result[: len(own)] == own and len(own) == 3
    ranks = [r["circuit_rank"] for r in others]
    assert ranks == sorted(ranks, reverse=True)
    assert all(0 < r["circuit_rank"] <= 1 for r in result)


def test_strategy_path_links_two_circuits(f1):
    result = q.strategy_path(f1, "monza", "marina_bay")

    path = result[0]["path_nodes"]
    assert path[0] == "monza" and path[-1] == "marina_bay"
    assert len(result[0]["path_links"]) == len(path) - 1


def test_strategy_path_is_empty_when_too_far(f1):
    assert q.strategy_path(f1, "monza", "bahrain", max_len=1) == []
