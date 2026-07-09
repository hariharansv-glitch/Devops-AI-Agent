"""Root ADK ``LlmAgent`` factory.

Everything the agent needs (tools, instructions, callbacks) is wired here.
Instantiation is deferred to :func:`build_root_agent` so that unit tests can
build isolated agents with fake tools or a different model.
"""

from __future__ import annotations

from typing import Iterable, Optional

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

    logger.info(
        "building LlmAgent model={model} tools={n}",
        model=settings.model_name,
        n=len(tool_list),
    )

    return LlmAgent(
        name=AGENT_NAME,
        model=settings.model_name,
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
