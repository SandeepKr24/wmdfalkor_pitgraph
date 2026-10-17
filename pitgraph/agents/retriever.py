"""Retriever: calls the graph tools over MCP to gather evidence for the plan.

The model chooses the calls (following the plan). Then a fixed rule checks the
result: if any of the circuit's own tools came back thin, it runs the recovery tools
(similar_circuits T2, strategy_clusters T6) so the Analyst has comparable evidence.
"""

import json

from pitgraph import llm
from pitgraph.agents.mcp_tools import ToolBox
from pitgraph.agents.planner import RACE_TOOLS, Plan

# Fewer rows than this from a circuit's own data counts as thin.
THIN_DATA_ROWS = 3
# Which tools hold the circuit's own data. Safety-car data only counts as thin for
# safety-car questions: "no SC at Monza" doesn't make a strategy question thin.
STRATEGY_TOOLS = {"winning_strategies", "strategy_outcomes"}
SC_TOOLS = STRATEGY_TOOLS | {"neutralisation_impact"}
RECOVERY_TOOLS = ("similar_circuits", "strategy_clusters")
MAX_RESULT_CHARS = 6000  # keep tool results short enough to reason over

SYSTEM = """You gather evidence from an F1 race graph by calling tools.
Follow the plan's tool sequence for each circuit in the plan. You may add a call if a
result clearly needs follow-up (e.g. similar_circuits when a circuit has little data).
Do not answer the question. When done, reply with one line listing what you fetched."""


async def retrieve(question: str, plan: Plan, toolbox: ToolBox) -> dict:
    """Run the plan's tools; returns {"note": ..., "recovery": bool, "reason": ...}."""

    async def run_tool(name: str, params: dict) -> tuple[str, bool]:
        call = await toolbox.call(name, params)
        if not call.ok:
            return call.error or "tool error", True
        return json.dumps(call.rows, default=str)[:MAX_RESULT_CHARS], False

    tools = await toolbox.claude_tools()
    note = await llm.tool_loop(
        SYSTEM,
        f"Question: {question}\nPlan: {plan.model_dump_json()}",
        tools,
        run_tool,
        effort="low",
    )

    recovery, reason = await recover_if_thin(plan, toolbox)
    return {"note": note, "recovery": recovery, "reason": reason}


async def recover_if_thin(plan: Plan, toolbox: ToolBox) -> tuple[bool, str]:
    """If any circuit-specific tool came back thin (or failed), fetch comparable circuits.

    Example: "safety cars at Monza" -> neutralisation_impact has 0 rows (Monza had
    none in 2023-25), so we look at similar circuits instead.
    """
    own_data_tools = SC_TOOLS if plan.intent == "sc_impact" else STRATEGY_TOOLS
    thin = [
        f"{c.tool} ({len(c.rows) if c.ok and isinstance(c.rows, list) else 'failed'})"
        for c in toolbox.calls
        if c.tool in own_data_tools and (not c.ok or not isinstance(c.rows, list) or len(c.rows) < THIN_DATA_ROWS)
    ]
    if not plan.circuits or not thin:
        return False, ""

    already = {(c.tool, json.dumps(c.params, sort_keys=True)) for c in toolbox.calls}
    for circuit in plan.circuits:
        for tool in RECOVERY_TOOLS:
            params = {"circuit": circuit}
            if (tool, json.dumps(params, sort_keys=True)) not in already:
                await toolbox.call(tool, params)
    return True, f"thin data: {', '.join(thin)} rows; added similar circuits and strategy clusters"


def evidence_pack(toolbox: ToolBox) -> list[dict]:
    """Everything retrieved, as the Analyst and Critic will see it."""
    return [
        {"tool": c.tool, "params": c.params, "ok": c.ok, "rows": c.rows, "error": c.error}
        for c in toolbox.calls
        if c.tool in RACE_TOOLS
    ]
