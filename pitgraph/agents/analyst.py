"""Analyst: turns the retrieved rows into a verdict, a confidence, and the evidence
(f1 nodes) behind it. For strategy questions it also drafts a strategy brief."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from pitgraph import llm

MAX_PACK_CHARS = 20000


class EvidenceRef(BaseModel):
    ref_label: Literal["Race", "Entry", "Circuit"] = Field(description="Kind of f1 node")
    ref_id: str = Field(description="race_id, entry_id or circuit_id exactly as in the data")
    why: str = Field(description="What this node shows, in a few words")


class Brief(BaseModel):
    primary_strategy: str = Field(description='e.g. "M-H, pit laps 18-24"')
    fallback_strategy: str = Field(description="What to switch to if things change")
    sc_window: str = Field(description='Laps where a safety car would trigger a stop, e.g. "laps 12-25", or "n/a"')


class Analysis(BaseModel):
    verdict: str = Field(description="The direct answer, 1-3 sentences")
    reasoning_summary: str = Field(description="How the evidence supports it, 2-5 sentences, citing numbers")
    confidence: float = Field(description="0 to 1: how well the data supports the verdict")
    evidence: list[EvidenceRef] = Field(description="2-6 nodes from the data that matter most")
    used_similar_circuits: bool = Field(description="True if the answer leans on other circuits' data")
    brief: Brief | None = Field(description="Only for 'which strategy' questions; otherwise null")


SYSTEM = """You are a Formula 1 strategy analyst. Answer only from the evidence given
(2023-2025 race data). Strategy codes: S=soft, M=medium, H=hard, I=intermediate, W=wet,
e.g. "M-H" = start on mediums, one stop to hards.

Read the tools' results like this:
- winning_strategies: what the top finishers did (what worked)
- strategy_outcomes: avg_positions_gained per strategy; negative = what to avoid.
  A safety car can make a bad strategy look good, so check neutralisation_impact.
- neutralisation_impact: every safety car (SC), VSC and red flag in the official race-control
  data, with how many drivers pitted during it. The data is complete: a race that isn't
  listed had no neutralisation. Trust it over general knowledge of a circuit's reputation.
- similar_circuits / strategy_clusters / key_races: comparable evidence from other circuits.
  If you rely on them, say so and lower confidence.
- recall_insights: what this user's earlier sessions concluded (times_confirmed = how
  often since). Say when you're using memory. If the race data disagrees, trust the data
  and say the earlier insight looks wrong; mention any listed conflicts.

Be honest about small samples (3 races per circuit at most). Cite evidence by the exact
race_id, entry_id or circuit_id that appears in the data.
If a reviewer's note is included, fix what it points out."""


def pack_text(evidence_pack: list[dict]) -> str:
    """The retrieved rows as JSON text, capped so the prompt stays a sensible size."""
    return json.dumps(evidence_pack, default=str)[:MAX_PACK_CHARS]


async def analyse(question: str, evidence_pack: list[dict], retrieval: dict, review_note: str | None) -> Analysis:
    prompt = (
        f"Question: {question}\n\n"
        f"Retrieval note: {retrieval['note']}\n"
        f"Recovery used: {retrieval['recovery']} {retrieval['reason']}\n\n"
        f"Evidence:\n{pack_text(evidence_pack)}"
    )
    if review_note:
        prompt += f"\n\nReviewer's note on your previous answer: {review_note}"
    result = await llm.structured(SYSTEM, prompt, Analysis, effort="medium")
    result.confidence = max(0.0, min(1.0, result.confidence))
    return result


def ids_in(value) -> set[str]:
    """Every string anywhere in the retrieved data (to check cited IDs exist)."""
    if isinstance(value, str):
        return {value}
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return set().union(*map(ids_in, items))


def grounded_evidence(analysis: Analysis, evidence_pack: list[dict]) -> list[dict]:
    """Keep only evidence the model cited that really appears in the retrieved rows."""
    known = ids_in([call["rows"] for call in evidence_pack if call["ok"]])
    kept = [e for e in analysis.evidence if e.ref_id in known]
    weight = round(1 / len(kept), 2) if kept else 0
    return [{"ref_label": e.ref_label, "ref_id": e.ref_id, "why": e.why, "weight": weight} for e in kept]
