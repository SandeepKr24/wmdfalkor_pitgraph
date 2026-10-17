"""Ask PitGraph a question from the command line.

  python -m pitgraph.ask --user demo "Best strategy at Monza for a top-5 finish?"
"""

import argparse
import asyncio

from pitgraph.agents.flow import ask


def print_result(state: dict) -> None:
    analysis = state["analysis"]
    print(f"\n{analysis.verdict}\n")
    print(analysis.reasoning_summary)
    print(f"\nConfidence: {analysis.confidence:.0%}")

    retrieval = state["retrieval"]
    if retrieval["recovery"]:
        print(f"Recovery path: {retrieval['reason']}")

    print("\nTools called:")
    for call in state["tool_calls"]:
        status = f"{len(call.rows)} rows" if call.ok and isinstance(call.rows, list) else ("ok" if call.ok else f"ERROR {call.error}")
        print(f"  {call.tool}({call.params}) -> {status}")

    print("\nHandoffs:")
    for review in state["reviews"]:
        print(f"  Critic: {review['verdict']} - {review['note']}")

    if analysis.brief and state.get("act_id"):
        print(f"\nStrategy brief saved ({state['act_id']}): {analysis.brief.model_dump()}")
    print(f"\nTrace this answer with a_id = {state['a_id']}  (session {state['session_id']})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask PitGraph an F1 strategy question.")
    parser.add_argument("question")
    parser.add_argument("--user", required=True, help="user ID (letters, digits, underscore)")
    parser.add_argument("--session", help="continue an existing session")
    args = parser.parse_args()
    print_result(asyncio.run(ask(args.question, args.user, args.session)))


if __name__ == "__main__":
    main()
