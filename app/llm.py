"""Model-id resolution shared by every agent builder.

One rule (see docs/CONTRACTS.md): ids with a provider prefix (``openai/…``)
are routed through ADK's LiteLLM wrapper; bare ids (``gemini-…``) are passed
to ``LlmAgent`` as plain strings for the native Google path. Centralised here
so an all-OpenAI (or mixed) configuration is just an .env change - no agent
file hardcodes a provider.
"""

from __future__ import annotations

from typing import Any


OPENAI_REASONING_EFFORT = "high"


def resolve_model(model_id: str) -> Any:
    """Resolve a configured model id into what ``LlmAgent(model=...)`` expects.

    Args:
        model_id: The configured model identifier (e.g. ``gemini-3.7-flash``
            or ``openai/gpt-5.6-sol``).

    Returns:
        The plain string for native models, or a ``LiteLlm`` instance for
        provider-prefixed ids.
    """
    if "/" in model_id:
        # Imported lazily: pulling in litellm is slow and only needed when a
        # LiteLLM-routed model is actually configured.
        from google.adk.models.lite_llm import LiteLlm

        # gpt-5.6 reasoning models reject function tools on
        # /v1/chat/completions ("Function tools with reasoning_effort are not
        # supported ... use /v1/responses or set reasoning_effort to 'none'").
        # LiteLLM's Responses-API bridge keeps BOTH tools and reasoning
        # working, and structured output + token usage were verified through
        # it on 2026-08-21 (gpt-5.4-mini works fine on plain chat completions
        # and stays there).
        if model_id.startswith("openai/gpt-5.6"):
            model_id = "openai/responses/" + model_id.split("/", 1)[1]
        kwargs: dict[str, Any] = {}
        if model_id.startswith("openai/") and "/gpt-5" in model_id:
            # LiteLlm forwards this to Chat Completions or translates it for
            # the Responses bridge. Keep the quality setting centralized so
            # every GPT-5 agent (including structured-output agents) reasons
            # at the same requested level.
            kwargs["reasoning_effort"] = OPENAI_REASONING_EFFORT
        return LiteLlm(model=model_id, **kwargs)
    return model_id


__all__ = ["OPENAI_REASONING_EFFORT", "resolve_model"]
