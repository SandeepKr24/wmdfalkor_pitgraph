"""The one place the app talks to the LLM (Claude, via the Anthropic SDK).

Two calls cover every agent:
  structured()  one request, answer validated against a Pydantic model
                (Planner, Analyst, Critic)
  tool_loop()   the model calls tools until it has what it needs (Retriever)

Settings (from .env):
  LLM_API_KEY       Anthropic API key
  LLM_MODEL         model ID, default claude-sonnet-5-5
  LLM_WORKSPACE_ID  only needed if the key isn't scoped to a workspace

Server-side fallbacks are on: if the model declines a request, the API retries
it on a suitable fallback model inside the same call.
"""

import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 16000
MAX_TOOL_TURNS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Schema = TypeVar("Schema", bound=BaseModel)
# A tool runner takes (tool name, input) and returns (result text, is_error).
ToolRunner = Callable[[str, dict], Awaitable[tuple[str, bool]]]

_client: anthropic.AsyncAnthropic | None = None


class LLMError(RuntimeError):
    """The model couldn't produce a usable answer (refusal, cut off, no output)."""


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("Missing environment variable: LLM_API_KEY")
        # Keys not scoped to a workspace must name one on every request.
        workspace_id = os.getenv("LLM_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        _client = anthropic.AsyncAnthropic(api_key=api_key, default_headers=headers)
    return _client


def request_settings(system: str, effort: str) -> dict:
    """Arguments every request shares, including the server-side refusal fallback."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "output_config": {"effort": effort},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


def check_stop(response) -> None:
    """Fail loudly instead of reading a refused or truncated response."""
    if response.stop_reason == "refusal":
        raise LLMError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise LLMError("The model's answer was cut off (max_tokens).")


async def structured(system: str, prompt: str, schema: type[Schema], effort: str = "low") -> Schema:
    """Ask once and get back a validated instance of `schema`.

    effort: "low" for routine steps, "medium" for judgment calls.
    """
    response = await client().beta.messages.parse(
        **request_settings(system, effort),
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    check_stop(response)
    if response.parsed_output is None:
        raise LLMError("The model returned no structured output.")
    return response.parsed_output


async def tool_loop(
    system: str,
    prompt: str,
    tools: list[dict],
    run_tool: ToolRunner,
    effort: str = "low",
    max_turns: int = MAX_TOOL_TURNS,
) -> str:
    """Let the model call tools until it stops asking, then return its final text.

    Every tool call goes through run_tool, so the caller decides how tools run
    (here: over MCP, with each call logged as a QueryRun).
    """
    messages = [{"role": "user", "content": prompt}]
    for _ in range(max_turns):
        response = await client().beta.messages.create(
            **request_settings(system, effort), messages=messages, tools=tools
        )
        check_stop(response)
        # Keep the full content (including thinking blocks) so the next turn is valid.
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            return "".join(block.text for block in response.content if block.type == "text")

        # All results go back in one message, as the API expects for parallel calls.
        results = []
        for block in tool_uses:
            text, is_error = await run_tool(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    return f"(stopped after {max_turns} tool turns)"
