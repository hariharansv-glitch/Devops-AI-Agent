"""ADK ``FunctionTool`` bindings for Jenkins inspection.

``jenkins_restart`` follows the same explicit-confirmation flow as its
Docker counterparts.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.schemas import ConfirmationRequired, ToolError, ToolStatus
from app.services import get_services
from app.services.ssh_service import SSHConnectionError
from app.utils import get_logger

logger = get_logger(__name__)


async def jenkins_status(tool_context: ToolContext) -> Dict[str, Any]:
    """Return whether Jenkins is installed, running, and its detected version."""
    jenkins = get_services().jenkins
    try:
        status = await asyncio.to_thread(jenkins.status)
    except SSHConnectionError as exc:
        return _err("jenkins_status", exc)
    return status.model_dump()


async def jenkins_health(tool_context: ToolContext) -> Dict[str, Any]:
    """Return a Jenkins health snapshot including systemd ``ActiveState``."""
    jenkins = get_services().jenkins
    try:
        health = await asyncio.to_thread(jenkins.health)
    except SSHConnectionError as exc:
        return _err("jenkins_health", exc)
    return health.model_dump()


async def jenkins_logs(lines: int, tool_context: ToolContext) -> Dict[str, Any]:
    """Return the last ``lines`` of the Jenkins log file.

    Args:
        lines: Number of trailing lines to return (1..2000). 200 is a good
            default for a quick investigation.
    """
    lines = max(1, min(int(lines or 200), 2000))
    jenkins = get_services().jenkins
    try:
        text = await asyncio.to_thread(jenkins.logs, lines=lines)
    except SSHConnectionError as exc:
        return _err("jenkins_logs", exc)
    return {"status": ToolStatus.SUCCESS.value, "lines": lines, "log": text}


async def jenkins_restart(confirm: bool, tool_context: ToolContext) -> Dict[str, Any]:
    """Restart the Jenkins systemd unit (destructive).

    Requires the user to explicitly agree before ``confirm=true`` is set by
    the LLM. Returns a :class:`ConfirmationRequired` payload otherwise.
    """
    services = get_services()
    if not confirm:
        return ConfirmationRequired(
            action="jenkins.restart",
            target="jenkins.service",
            prompt=(
                f"I am about to restart Jenkins on {services.settings.vm_host}. "
                "Do you want to continue?"
            ),
            reversible=False,
        ).model_dump()

    if services.settings.read_only_mode:
        return ToolError(
            status=ToolStatus.BLOCKED,
            error="Read-only mode is enabled; destructive operations are disabled.",
            detail="Set READ_ONLY_MODE=FALSE in the environment to allow this action.",
            tool="jenkins_restart",
            hint="Ask an operator to disable READ_ONLY_MODE before retrying.",
        ).model_dump()

    try:
        result = await asyncio.to_thread(services.jenkins.restart)
    except SSHConnectionError as exc:
        return _err("jenkins_restart", exc)
    return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_jenkins_tools() -> List[FunctionTool]:
    """Return the list of ADK tools exposed by :mod:`app.tools.jenkins_tool`."""
    return [
        FunctionTool(func=jenkins_status),
        FunctionTool(func=jenkins_health),
        FunctionTool(func=jenkins_logs),
        FunctionTool(func=jenkins_restart),
    ]


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _err(tool: str, exc: BaseException) -> Dict[str, Any]:
    logger.warning("{tool} error: {exc}", tool=tool, exc=exc)
    return ToolError(
        error=f"{tool} failed",
        detail=str(exc),
        tool=tool,
        hint="Verify SSH connectivity to the target VM.",
    ).model_dump()


__all__ = [
    "build_jenkins_tools",
    "jenkins_health",
    "jenkins_logs",
    "jenkins_restart",
    "jenkins_status",
]
