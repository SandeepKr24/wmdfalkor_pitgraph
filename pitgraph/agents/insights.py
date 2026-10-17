"""MemoryWriter, insight step: turns an approved answer into 0-2 lasting insights.

The model compares the answer with what this user's memory already holds about the
same circuits and decides, for each insight, whether it is:
  new          nothing stored covers it
  confirms     same conclusion as a stored insight -> times_confirmed + 1
  contradicts  conflicts with a stored insight, both stay visible (CONTRADICTS)
  supersedes   corrects or refines a stored insight, which is retired (SUPERSEDES)
The model only decides; `apply` checks every choice before anything is written.
"""

from typing import Literal

from falkordb import Graph
from pydantic import BaseModel, Field

from pitgraph import llm
from pitgraph.agents import memory
from pitgraph.agents.analyst import Analysis

MAX_INSIGHTS = 2

Relation = Literal["new", "confirms", "contradicts", "supersedes"]


class InsightDecision(BaseModel):
    statement: str = Field(description="One sentence, a durable fact with numbers, e.g. 'At Monza the M-H one-stop won all 3 races (2023-25)'")
    circuits: list[str] = Field(description="circuit_id values this insight is about")
    confidence: float = Field(description="0 to 1, no higher than the answer's confidence")
    relation: Relation = Field(description="How it relates to the stored insights")
    existing_i_id: str | None = Field(description="The stored insight's i_id for confirms/contradicts/supersedes; else null")


class InsightUpdate(BaseModel):
    insights: list[InsightDecision] = Field(description=f"0 to {MAX_INSIGHTS} insights")


SYSTEM = f"""You keep a user's long-term memory of Formula 1 strategy findings.
From an approved answer, write 0 to {MAX_INSIGHTS} insights worth remembering next time:
short, specific facts about a circuit, with the numbers that back them. Skip anything
vague, obvious, or only about this one question's wording.

Compare each one with the stored insights (given with their i_id):
- confirms: same circuit and same conclusion as a stored insight, even if worded
  differently or with other numbers. Use that insight's i_id. This is the usual case
  when the user asks about a circuit again.
- supersedes: same topic, but the new answer corrects or sharpens the stored one
  (e.g. better evidence, or the stored claim was wrong). The stored one is retired.
- contradicts: it conflicts with a stored insight and neither clearly replaces the
  other (e.g. different conditions). Both stay, flagged as a conflict.
- new: no stored insight covers it.
- new: only for a conclusion that no stored insight states.
Never write a 'new' insight that repeats a stored one. If nothing new was learned,
confirm the matching insights and add nothing else."""


async def decide(question: str, analysis: Analysis, circuits: list[str], stored: list[dict]) -> InsightUpdate:
    stored_text = "\n".join(
        f"- {i['i_id']}: {i['statement']} (confidence {i['confidence']:.2f}, confirmed {i['times_confirmed']}x)"
        for i in stored
    ) or "(none)"
    prompt = (
        f"Question: {question}\n"
        f"Circuits in the question: {', '.join(circuits) or '(none)'}\n\n"
        f"Approved answer: {analysis.verdict}\n"
        f"Reasoning: {analysis.reasoning_summary}\n"
        f"Confidence: {analysis.confidence:.2f}\n\n"
        f"Stored insights about these circuits:\n{stored_text}"
    )
    return await llm.structured(SYSTEM, prompt, InsightUpdate, effort="medium")


def apply(
    graph: Graph,
    a_id: str,
    update: InsightUpdate,
    stored: list[dict],
    valid_circuits: set[str],
    max_confidence: float,
) -> list[dict]:
    """Validate the model's decisions and write them. Returns what was written.

    Guards: only known circuits; a link must point at an insight that was actually
    recalled for this question (otherwise it's written as new), at most once;
    confidence is capped at the answer's own; at most MAX_INSIGHTS.
    """
    stored_ids = {i["i_id"] for i in stored}
    linked: set[str] = set()
    written = []
    for decision in update.insights[:MAX_INSIGHTS]:
        circuits = [c for c in decision.circuits if c in valid_circuits]
        if not circuits or not decision.statement.strip():
            continue
        confidence = max(0.0, min(decision.confidence, max_confidence))
        target = decision.existing_i_id if decision.existing_i_id in stored_ids else None
        relation = decision.relation if target else "new"  # a link needs a recalled insight
        if relation == "new":
            target = None
        elif target in linked:
            continue  # a second decision about the same stored insight: a duplicate
        else:
            linked.add(target)

        if relation == "confirms":
            memory.confirm_insight(graph, target, a_id, confidence)
            i_id = target
        else:
            i_id = memory.new_insight(graph, a_id, decision.statement, confidence, circuits)
            if relation in memory.INSIGHT_LINKS:
                memory.link_insight(graph, relation, i_id, target)
        written.append({"i_id": i_id, "relation": relation, "target": target,
                        "statement": decision.statement, "circuits": circuits})
    return written
