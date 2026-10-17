"""Critic: checks the Analyst's answer against the evidence. Approves it, or sends
it back once with a note on what to fix."""

from typing import Literal

from pydantic import BaseModel, Field

from pitgraph import llm
from pitgraph.agents.analyst import Analysis, pack_text

MAX_REVISIONS = 1
# Starts the note when the Critic still wanted changes but had no rounds left.
FORCED_APPROVAL = "Approved after final round."


class Review(BaseModel):
    verdict: Literal["approve", "revise"]
    note: str = Field(description="If revise: the specific problem to fix. If approve: one line on why it holds.")


SYSTEM = """You review an F1 strategy answer before it reaches the user. Check:
1. Every claim is supported by the evidence (numbers match the rows).
2. It answers the question that was asked.
3. Confidence fits the data (small samples or borrowed circuits = lower confidence).
4. Safety-car effects are considered when strategy outcomes are used.
5. No claims from outside the data, unless clearly labelled as general reasoning.
The race data is complete: if neutralisation_impact lists no SC/VSC/red flag for a race,
that race had none. Don't second-guess it with a circuit's reputation.
recall_insights rows are this user's conclusions from earlier sessions. Answers may use
them if they say so; check they don't conflict with the race data.
Judge the content only; the answer's format is fixed and not your concern.
Approve answers that are correct and supported, even if not perfect.
Ask for a revision only for a real error or a missing key point, and say exactly what to fix."""


def as_text(analysis: Analysis) -> str:
    """The answer as plain labelled text. (Showing the raw JSON confused the Critic:
    the answer's "verdict" field looks like its own approve/revise verdict.)"""
    lines = [
        f"Answer: {analysis.verdict}",
        f"Reasoning: {analysis.reasoning_summary}",
        f"Confidence: {analysis.confidence:.2f}",
        f"Leans on other circuits: {'yes' if analysis.used_similar_circuits else 'no'}",
        "Cited evidence: " + "; ".join(f"{e.ref_label} {e.ref_id} ({e.why})" for e in analysis.evidence),
    ]
    if analysis.brief:
        b = analysis.brief
        lines.append(f"Strategy brief: primary {b.primary_strategy}; fallback {b.fallback_strategy}; SC window {b.sc_window}")
    return "\n".join(lines)


async def review(question: str, analysis: Analysis, evidence_pack: list[dict], revisions: int) -> Review:
    if revisions >= MAX_REVISIONS:
        final_rule = "This is the final round: approve unless the answer is wrong."
    else:
        final_rule = "You may ask for one revision."
    prompt = (
        f"Question: {question}\n\n"
        f"Answer to review:\n{as_text(analysis)}\n\n"
        f"Evidence:\n{pack_text(evidence_pack)}\n\n"
        f"{final_rule}"
    )
    result = await llm.structured(SYSTEM, prompt, Review, effort="medium")
    if revisions >= MAX_REVISIONS and result.verdict == "revise":
        # Out of revision rounds: keep the review note, but let the answer through.
        result = Review(verdict="approve", note=f"{FORCED_APPROVAL} Open issue: {result.note}")
    return result
