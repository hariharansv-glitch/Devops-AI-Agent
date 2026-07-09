"""Root ADK ``LlmAgent`` factory.

Everything the agent needs (tools, instructions, callbacks) is wired here.
Instantiation is deferred to :func:`build_root_agent` so that unit tests can
build isolated agents with fake tools or a different model.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, load_memory
from google.genai import types

from app.agent.callbacks import (
    after_agent_callback,
    after_tool_callback,
    before_agent_callback,
    before_tool_callback,
)
from app.agent.instructions import AGENT_DESCRIPTION, AGENT_NAME, SYSTEM_INSTRUCTION
from app.config import Settings, get_settings
from app.tools import build_all_tools
from app.utils import get_logger

logger = get_logger(__name__)


def _resolve_model(settings: Settings) -> Any:
    """Return the model spec to hand to :class:`LlmAgent`.

    Gemini models are passed as a plain string (ADK's default path). Every
    other provider (Groq, OpenAI, Anthropic, Ollama, ...) is routed through
    ADK's :class:`~google.adk.models.lite_llm.LiteLlm` wrapper, which in turn
    delegates to LiteLLM. The provider is inferred from :attr:`model_name`.
    """
    if settings.llm_provider == "gemini":
        return settings.model_name

    try:
        from google.adk.models.lite_llm import LiteLlm  # noqa: WPS433 - lazy import
    except ImportError as exc:  # pragma: no cover - install-time issue
        raise ImportError(
            "MODEL_NAME points at a non-Gemini provider "
            f"({settings.model_name!r}), which requires the LiteLLM adapter. "
            "Run `pip install litellm` and restart."
        ) from exc

    return LiteLlm(model=settings.model_name)


def build_root_agent(
    settings: Optional[Settings] = None,
    *,
    tools: Optional[Iterable[FunctionTool]] = None,
) -> LlmAgent:
    """Return a fully-configured DevOps :class:`LlmAgent`.

    Args:
        settings: Optional override for :func:`app.config.get_settings`. When
            omitted the process-wide settings singleton is used.
        tools: Optional override for the tool list. When omitted the default
            devops toolbelt (SSH + Linux + Docker + Jenkins + Logs + memory
            recall) is used. Passing an explicit list is convenient for tests.
    """
    settings = settings or get_settings()
    tool_list = list(tools) if tools is not None else [*build_all_tools(), load_memory]

    model_spec = _resolve_model(settings)

    logger.info(
        "building LlmAgent model={model} provider={provider} tools={n}",
        model=settings.model_name,
        provider=settings.llm_provider,
        n=len(tool_list),
    )

    return LlmAgent(
        name=AGENT_NAME,
        model=model_spec,
        description=AGENT_DESCRIPTION,
        instruction=SYSTEM_INSTRUCTION,
        tools=tool_list,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            top_p=0.95,
            candidate_count=1,
        ),
        before_agent_callback=before_agent_callback,
        after_agent_callback=after_agent_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
    )


__all__ = ["build_root_agent"]
