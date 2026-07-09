"""ADK ``FunctionTool`` implementations for the DevOps agent.

Each submodule exposes a ``build_*_tools()`` factory that returns a list of
Google ADK ``FunctionTool`` instances. Grouping the tools by domain keeps the
agent wiring in :mod:`app.agent.root_agent` short and readable.

Every tool follows the same contract:

* It never raises: unhandled errors are converted to a
  :class:`app.schemas.ToolError` payload so Gemini can react to failures
  intelligently.
* Return values are ``dict`` payloads produced by dumping a Pydantic model
  (structured tool responses).
* Destructive tools require an explicit ``confirm=True`` argument and honour
  :attr:`app.config.Settings.read_only_mode`.
"""

from __future__ import annotations

from typing import List

from google.adk.tools import FunctionTool

from app.tools.docker_tool import build_docker_tools
from app.tools.jenkins_tool import build_jenkins_tools
from app.tools.linux_tool import build_linux_tools
from app.tools.logs_tool import build_logs_tools
from app.tools.ssh_tool import build_ssh_tools


def build_all_tools() -> List[FunctionTool]:
    """Return every ADK tool the DevOps agent should own."""
    return [
        *build_ssh_tools(),
        *build_linux_tools(),
        *build_docker_tools(),
        *build_jenkins_tools(),
        *build_logs_tools(),
    ]


__all__ = [
    "build_all_tools",
    "build_docker_tools",
    "build_jenkins_tools",
    "build_linux_tools",
    "build_logs_tools",
    "build_ssh_tools",
]
