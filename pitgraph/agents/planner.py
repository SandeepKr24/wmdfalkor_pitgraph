"""Planner: reads the question, finds the circuits it's about, picks an intent and
the tools to run, and loads what this user's memory already knows (T4).

It is given two kinds of memory up front:
  - the session's recent messages (episodic), so "what about Spa?" makes sense
  - the best tool sequence per intent from past answers by all users (procedural)
"""

from typing import Literal

from pydantic import BaseModel, Field

from pitgraph import llm
from pitgraph.agents.mcp_tools import ToolBox

Intent = Literal["strategy_choice", "sc_impact", "circuit_comparison", "memory_recall", "general"]

MAX_MESSAGE_CHARS = 400  # per past message in the prompt

RACE_TOOLS = [
    "winning_strategies", "strategy_outcomes", "similar_circuits", "neutralisation_impact",
    "strategy_clusters", "key_races", "strategy_path",
]


class Plan(BaseModel):
    question: str = Field(description="The question rewritten to stand on its own, using the conversation so far (unchanged if it already does)")
    intent: Intent = Field(description="What kind of question this is")
    circuits: list[str] = Field(description="circuit_id values the question is about (from the list given)")
    tool_sequence: list[str] = Field(description="Tools to call, in order, from the allowed list")
    reasoning: str = Field(description="One sentence: why these tools")


SYSTEM = f"""You plan how to answer Formula 1 race-strategy questions using a graph of 2023-2025 races.

Pick the intent:
- strategy_choice: which tyre strategy / how many stops at a circuit
- sc_impact: how safety cars, VSCs or red flags change strategy
- circuit_comparison: how circuits relate or compare
- memory_recall: the user asks what we already know, learned or discussed before
  (e.g. "What do you know about Monza?", "Remind me what we found for Spa")
- general: anything else

Choose circuits only from the circuit list you are given (use circuit_id, not the name).
Choose tools only from: {", ".join(RACE_TOOLS)}.
Typical plans:
- strategy_choice: winning_strategies, strategy_outcomes, neutralisation_impact, key_races
- sc_impact: neutralisation_impact, winning_strategies, strategy_outcomes
- circuit_comparison: similar_circuits, strategy_clusters, strategy_path
- memory_recall: (no tools needed if recalled insights cover it) else winning_strategies
Keep plans short: 2-5 tools.
If proven plans from past answers are listed, use the one for your intent unless the
question clearly needs different tools."""


def procedures_text(procedures: dict[str, dict]) -> str:
    """The best past plan per intent, as lines for the prompt."""
    lines = []
    for intent, proc in procedures.items():
        uses = proc["successes"] + proc["failures"]
        line = (f"- {intent}: {', '.join(proc['tool_sequence'])} "
                f"(approved in {proc['successes']} of {uses} answers)")
        if proc["fallbacks"]:
            line += f"; when data was thin it added {proc['fallbacks'][0].replace(',', ', ')}"
        lines.append(line)
    return "\n".join(lines)


def history_text(history: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {m['text'][:MAX_MESSAGE_CHARS]}" for m in history)


async def plan(
    question: str,
    toolbox: ToolBox,
    circuits: list[dict],
    history: list[dict],
    procedures: dict[str, dict],
) -> tuple[Plan, list[dict]]:
    """Return the plan and any insights recalled from the user's memory."""
    circuit_list = "\n".join(f"- {c['circuit_id']}: {c['name']}" for c in circuits)
    prompt = f"Circuits:\n{circuit_list}\n\n"
    if procedures:
        prompt += f"Proven plans from past answers:\n{procedures_text(procedures)}\n\n"
    if history:
        prompt += f"Conversation so far:\n{history_text(history)}\n\n"
    result = await llm.structured(SYSTEM, prompt + f"Question: {question}", Plan, effort="low")

    # Guard the model's choices: keep only real circuits and allowed tools.
    valid_circuits = {c["circuit_id"] for c in circuits}
    result.circuits = [c for c in result.circuits if c in valid_circuits]
    result.tool_sequence = [t for t in result.tool_sequence if t in RACE_TOOLS]

    insights = []
    if result.circuits:
        recalled = await toolbox.call("recall_insights", {"ref_ids": result.circuits})
        insights = recalled.rows if recalled.ok and isinstance(recalled.rows, list) else []
    return result, insights
