"""The agent flow, as a LangGraph state graph:

  Planner -> Retriever -> Analyst -> Critic --approve--> MemoryWriter
                             ^          |
                             +-revise---+   (at most once)

Every handoff is written to the user's memory graph as a Task node linked to the
previous one, so the whole chain can be shown later.

Memory, per the three types:
  episodic    the session's recent messages go to the Planner (follow-up questions)
  semantic    the Planner recalls the user's insights (T4); the MemoryWriter adds
              0-2 new ones after a real approval (confirm / contradict / supersede)
  procedural  the Planner sees the best tool sequence per intent (mem_global); the
              Critic's final verdict counts as that sequence's success or failure
"""

import logging
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from pitgraph import llm
from pitgraph.agents import analyst, critic, insights, memory, planner, retriever
from pitgraph.agents.mcp_tools import ToolBox, connect
from pitgraph.graph.db import global_memory_graph

HISTORY_MESSAGES = 6  # how many earlier messages of the session the Planner sees

log = logging.getLogger("pitgraph.flow")


class FlowState(TypedDict, total=False):
    question: str
    session_id: str
    q_id: str
    last_t_id: str
    history: list[dict]
    procedures: dict[str, dict]
    plan: planner.Plan
    procedure_reused: bool
    insights: list[dict]
    retrieval: dict
    evidence_pack: list[dict]
    analysis: analyst.Analysis
    analyst_t_id: str
    reviews: list[dict]
    a_id: str
    act_id: str | None
    insights_written: list[dict]


def build_graph(toolbox: ToolBox, circuits: list[dict]):
    graph_db = memory.memory_graph(toolbox.user_id)
    valid_circuits = {c["circuit_id"] for c in circuits}

    def handoff(state: FlowState, from_agent: str, to_agent: str, summary: str) -> str:
        return memory.add_task(graph_db, state["q_id"], from_agent, to_agent, summary, state.get("last_t_id"))

    async def plan_node(state: FlowState) -> FlowState:
        procedures = state.get("procedures", {})
        plan, recalled = await planner.plan(
            state["question"], toolbox, circuits, state.get("history", []), procedures
        )
        memory.set_intent(graph_db, state["q_id"], plan.intent)
        best = procedures.get(plan.intent)
        reused = bool(best) and best["tool_sequence"] == plan.tool_sequence
        t_id = handoff(state, "Planner", "Retriever", plan.model_dump_json())
        return {"plan": plan, "insights": recalled, "procedure_reused": reused, "last_t_id": t_id}

    async def retrieve_node(state: FlowState) -> FlowState:
        # From here on, agents work on the Planner's standalone version of the question.
        question = state["plan"].question
        retrieval = await retriever.retrieve(question, state["plan"], toolbox)
        pack = retriever.evidence_pack(toolbox)
        if state.get("insights"):
            pack.append({"tool": "recall_insights", "params": {}, "ok": True, "rows": state["insights"], "error": None})
        summary = retrieval["note"] + (f" | RECOVERY: {retrieval['reason']}" if retrieval["recovery"] else "")
        t_id = handoff(state, "Retriever", "Analyst", summary)
        return {"retrieval": retrieval, "evidence_pack": pack, "last_t_id": t_id}

    async def analyse_node(state: FlowState) -> FlowState:
        reviews = state.get("reviews", [])
        note = reviews[-1]["note"] if reviews and reviews[-1]["verdict"] == "revise" else None
        result = await analyst.analyse(state["plan"].question, state["evidence_pack"], state["retrieval"], note)
        t_id = handoff(state, "Analyst", "Critic", result.verdict)
        return {"analysis": result, "analyst_t_id": t_id, "last_t_id": t_id}

    async def critic_node(state: FlowState) -> FlowState:
        reviews = state.get("reviews", [])
        revisions = sum(r["verdict"] == "revise" for r in reviews)
        result = await critic.review(state["plan"].question, state["analysis"], state["evidence_pack"], revisions)
        to_agent = "Analyst" if result.verdict == "revise" else "MemoryWriter"
        t_id = handoff(state, "Critic", to_agent, f"{result.verdict}: {result.note}")
        reviews = reviews + [{"t_id": t_id, "verdict": result.verdict, "note": result.note}]
        return {"reviews": reviews, "last_t_id": t_id}

    def after_critic(state: FlowState) -> str:
        return "analyst" if state["reviews"][-1]["verdict"] == "revise" else "memory_writer"

    async def memory_node(state: FlowState) -> FlowState:
        result, plan, reviews = state["analysis"], state["plan"], state["reviews"]
        evidence = analyst.grounded_evidence(result, state["evidence_pack"])
        qr_ids = [c.qr_id for c in toolbox.calls]
        a_id = memory.add_analysis(graph_db, state["q_id"], result.model_dump(), state["analyst_t_id"], qr_ids, evidence)
        for rv in reviews:
            memory.add_review(graph_db, a_id, rv["t_id"], rv["verdict"], rv["note"])
        memory.link_recalled(graph_db, a_id, [i["i_id"] for i in state.get("insights", [])])

        # A forced approval means the Critic still had an open issue after the revision.
        approved = not reviews[-1]["note"].startswith(critic.FORCED_APPROVAL)

        # Procedural: did this plan lead to an answer the Critic approved?
        if plan.tool_sequence:
            memory.record_procedure(
                global_memory_graph(), plan.intent, plan.tool_sequence, success=approved,
                fallback=list(retriever.RECOVERY_TOOLS) if state["retrieval"]["recovery"] else None,
            )

        # Semantic: only learn from approved answers. A memory_recall answer only
        # repeats what memory already holds.
        written = []
        if approved and plan.intent != "memory_recall" and plan.circuits:
            stored = state.get("insights", [])
            try:
                update = await insights.decide(plan.question, result, plan.circuits, stored)
                written = insights.apply(graph_db, a_id, update, stored, valid_circuits, result.confidence)
            except llm.LLMError:
                # The answer is already saved; losing one memory update shouldn't lose it.
                log.warning("insight step failed for %s; answer kept without new insights", a_id)

        act_id = None
        if plan.intent == "strategy_choice" and result.brief:
            act_id = memory.add_action(graph_db, a_id, "strategy_brief", {
                "circuits": plan.circuits, **result.brief.model_dump(),
            })

        memory.add_message(graph_db, state["session_id"], "assistant", f"{result.verdict}\n\n{result.reasoning_summary}")
        return {"a_id": a_id, "act_id": act_id, "insights_written": written}

    flow = StateGraph(FlowState)
    flow.add_node("planner", plan_node)
    flow.add_node("retriever", retrieve_node)
    flow.add_node("analyst", analyse_node)
    flow.add_node("critic", critic_node)
    flow.add_node("memory_writer", memory_node)
    for start, end in [(START, "planner"), ("planner", "retriever"), ("retriever", "analyst"),
                       ("analyst", "critic"), ("memory_writer", END)]:
        flow.add_edge(start, end)
    flow.add_conditional_edges("critic", after_critic)  # returns the next node's name
    return flow.compile()


async def ask(question: str, user_id: str, session_id: str | None = None) -> FlowState:
    """Answer one question for one user. Returns the final flow state."""
    session_id = session_id or f"s_{uuid.uuid4().hex[:10]}"
    graph_db = memory.memory_graph(user_id)
    memory.start_session(graph_db, user_id, session_id)
    history = memory.recent_messages(graph_db, session_id, HISTORY_MESSAGES)  # before this question
    m_id = memory.add_message(graph_db, session_id, "user", question)
    q_id = memory.add_question(graph_db, m_id, question)

    async with connect() as client:
        toolbox = ToolBox(client, user_id)
        circuits_call = await toolbox.call("list_circuits", {})
        app = build_graph(toolbox, circuits_call.rows or [])
        state = await app.ainvoke({
            "question": question, "session_id": session_id, "q_id": q_id,
            "history": history, "procedures": memory.best_procedures(global_memory_graph()),
        })
    return {**state, "tool_calls": toolbox.calls}
