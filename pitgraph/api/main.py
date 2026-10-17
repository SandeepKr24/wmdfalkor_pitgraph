"""PitGraph HTTP API and web UI.

  POST /ask              ask a question as a user (runs the agent flow)
  GET  /trace/{a_id}     the evidence behind an answer (T5), for the "Why?" panel
  GET  /memory?user=     what a user's memory graph holds, plus shared procedures
  GET  /                 the web UI (pitgraph/web)

Run: uvicorn pitgraph.api.main:app --port 8000

Note: there is no login. user_id is chosen by the client, which is fine for a demo
but means anyone who knows a user_id can read that user's memory.
"""

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pitgraph import llm
from pitgraph.agents import flow
from pitgraph.api import views
from pitgraph.graph import queries as q
from pitgraph.graph.db import f1_graph, user_memory_graph

log = logging.getLogger("pitgraph.api")

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
ID_PATTERN = r"^[a-zA-Z0-9_]{1,64}$"  # same rule as graph names (db.USER_ID_PATTERN)
# Each question costs several LLM calls and starts its own MCP server process
# (~90 MB), so cap how many run at once. Lower it on small hosts (Render free: 2).
MAX_CONCURRENT_ASKS = int(os.getenv("MAX_CONCURRENT_ASKS", "4"))

app = FastAPI(title="PitGraph")
ask_slots = asyncio.Semaphore(MAX_CONCURRENT_ASKS)
users_asking: set[str] = set()  # one question at a time per user


class AskRequest(BaseModel):
    user_id: str = Field(pattern=ID_PATTERN)
    question: str = Field(min_length=3, max_length=500)
    session_id: str | None = Field(default=None, pattern=ID_PATTERN)


@app.post("/ask")
async def ask(req: AskRequest) -> dict:
    if req.user_id in users_asking:
        raise HTTPException(429, "This user already has a question running.")
    if ask_slots.locked():
        raise HTTPException(429, "Too many questions running. Try again in a minute.")

    users_asking.add(req.user_id)
    try:
        async with ask_slots:
            state = await flow.ask(req.question.strip(), req.user_id, req.session_id)
    except llm.LLMError as error:
        raise HTTPException(502, f"The model couldn't answer: {error}") from error
    except Exception as error:
        log.exception("ask failed for user %s", req.user_id)
        raise HTTPException(500, "Something went wrong answering that question.") from error
    finally:
        users_asking.discard(req.user_id)
    return answer_payload(state)


def answer_payload(state: dict) -> dict:
    """The parts of the final flow state the UI shows."""
    analysis, plan, retrieval = state["analysis"], state["plan"], state["retrieval"]
    return {
        "a_id": state["a_id"],
        "session_id": state["session_id"],
        "question": plan.question,
        "intent": plan.intent,
        "circuits": plan.circuits,
        "verdict": analysis.verdict,
        "reasoning": analysis.reasoning_summary,
        "confidence": analysis.confidence,
        "used_similar_circuits": analysis.used_similar_circuits,
        "brief": analysis.brief.model_dump() if analysis.brief and state.get("act_id") else None,
        "recovery": {"used": retrieval["recovery"], "reason": retrieval["reason"]},
        "procedure_reused": state.get("procedure_reused", False),
        "tool_sequence": plan.tool_sequence,
        "tool_calls": [
            {"tool": c.tool, "params": c.params, "ok": c.ok, "error": c.error,
             "rows": len(c.rows) if isinstance(c.rows, list) else int(bool(c.rows))}
            for c in state["tool_calls"]
        ],
        "reviews": [{"verdict": r["verdict"], "note": r["note"]} for r in state["reviews"]],
        "recalled_insights": state.get("insights", []),
        "insights_written": state.get("insights_written", []),
    }


@app.get("/trace/{a_id}")
def trace(a_id: str, user: str = Query(pattern=ID_PATTERN)) -> dict:
    graph = user_memory_graph(user)
    result = q.trace(graph, f1_graph(), a_id) if views.graph_exists(graph.name) else None
    if result is None:
        # Also what another user gets for this a_id: memories are per user.
        raise HTTPException(404, "No such answer for this user.")
    return result


@app.get("/memory")
def memory(user: str = Query(pattern=ID_PATTERN)) -> dict:
    return {**views.user_memory(user), "procedures": views.procedures()}


# Last, so the API routes above take precedence over static files.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
