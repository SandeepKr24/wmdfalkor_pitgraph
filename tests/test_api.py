"""API tests for the parts that don't need the LLM: input validation, the trace and
memory endpoints, per-user isolation, and the web UI being served.
(POST /ask with a valid question runs the full agent flow; that is checked live.)

Run: pytest tests/test_api.py
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from pitgraph.agents import memory
from pitgraph.api.main import app
from pitgraph.graph.db import get_client

client = TestClient(app)


@pytest.fixture
def user():
    """A user with one answered question, one insight and one strategy brief."""
    user_id = f"test_{uuid.uuid4().hex[:8]}"
    graph = memory.memory_graph(user_id)
    memory.start_session(graph, user_id, "s_test")
    m_id = memory.add_message(graph, "s_test", "user", "Best strategy at Monza?")
    q_id = memory.add_question(graph, m_id, "Best strategy at Monza?")
    t_id = memory.add_task(graph, q_id, "Analyst", "Critic", "one-stop", None)
    a_id = memory.add_analysis(graph, q_id, {"verdict": "One-stop M-H", "reasoning_summary": "3 of 3 wins",
                                             "confidence": 0.8}, t_id, [], [])
    memory.new_insight(graph, a_id, "Monza: M-H one-stop won 3 of 3", 0.8, ["monza"])
    memory.add_action(graph, a_id, "strategy_brief", {"circuits": ["monza"], "primary_strategy": "M-H"})
    yield user_id, a_id
    graph.delete()


def test_ask_rejects_bad_input_before_running_agents():
    for body in (
        {"user_id": "bad id!", "question": "Best strategy at Monza?"},
        {"user_id": "ok_user", "question": "?"},
        {"user_id": "ok_user", "question": "x" * 501},
        {"user_id": "ok_user", "question": "Best strategy?", "session_id": "s; DROP"},
    ):
        assert client.post("/ask", json=body).status_code == 422


def test_trace_returns_the_answer_for_its_owner(user):
    user_id, a_id = user

    response = client.get(f"/trace/{a_id}", params={"user": user_id})

    assert response.status_code == 200
    assert response.json()["analysis"]["verdict"] == "One-stop M-H"
    assert response.json()["question"]["text"] == "Best strategy at Monza?"


def test_another_user_cannot_trace_the_answer(user):
    _, a_id = user

    assert client.get(f"/trace/{a_id}", params={"user": "someone_else"}).status_code == 404


def test_memory_shows_insights_answers_and_briefs(user):
    user_id, a_id = user

    body = client.get("/memory", params={"user": user_id}).json()

    assert [i["statement"] for i in body["insights"]] == ["Monza: M-H one-stop won 3 of 3"]
    assert body["insights"][0]["circuits"] == ["monza"]
    assert [a["a_id"] for a in body["answers"]] == [a_id]
    assert body["briefs"][0]["content"]["primary_strategy"] == "M-H"
    assert body["node_counts"]["Insight"] == 1
    assert isinstance(body["procedures"], list)


def test_memory_for_a_new_user_is_empty_and_creates_nothing():
    user_id = f"test_{uuid.uuid4().hex[:8]}"

    body = client.get("/memory", params={"user": user_id}).json()

    assert body["insights"] == [] and body["answers"] == [] and body["node_counts"] == {}
    assert f"mem_{user_id}" not in get_client().list_graphs()


def test_memory_rejects_an_invalid_user_id():
    assert client.get("/memory", params={"user": "x) MATCH (n"}).status_code == 422


def test_web_ui_is_served():
    page = client.get("/")

    assert page.status_code == 200 and "PitGraph" in page.text
    assert client.get("/app.js").status_code == 200
