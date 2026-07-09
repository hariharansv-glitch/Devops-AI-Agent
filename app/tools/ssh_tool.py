"""ADK ``FunctionTool`` bindings for raw SSH operations.

The SSH tools are the *lowest-level* capability the agent has. In practice
the LLM should prefer the higher-level Linux / Docker / Jenkins tools; the
SSH tools exist as an escape hatch for the diagnostics Gemini decides are
worth running directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.schemas import ToolError, ToolStatus
from app.services import get_services
from app.services.ssh_service import SSHCommandBlocked, SSHConnectionError
from app.utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool functions (Python signatures = ADK function-declaration schema)
# ---------------------------------------------------------------------------


async def ssh_connect(tool_context: ToolContext) -> Dict[str, Any]:
    """Open the SSH connection to the target Linux VM.

    Call this once at the beginning of a diagnostic session to fail fast when
    the VM is unreachable. Subsequent tools reconnect automatically if needed.

    Returns:
        A dictionary with ``status`` (``success``/``error``) and, on success,
        the target ``host``, ``port`` and ``user``.
    """
    services = get_services()
    try:
        await _run(services.ssh.connect)
    except SSHConnectionError as exc:
        return _error("ssh_connect", "SSH connection failed", exc)

    settings = services.settings
    logger.info("ssh_connect ok host={host}", host=settings.vm_host)
    return {
        "status": ToolStatus.SUCCESS.value,
        "host": settings.vm_host,
        "port": settings.vm_port,
        "user": settings.vm_user,
    }


async def ssh_disconnect(tool_context: ToolContext) -> Dict[str, Any]:
    """Close the SSH connection to the target Linux VM.

    Use at the end of a diagnostic session, or when the user explicitly asks
    to disconnect. It is safe to call this even when no connection is open.
    """
    services = get_services()
    await _run(services.ssh.disconnect)
    return {"status": ToolStatus.SUCCESS.value}


async def ssh_execute(
    command: str,
    timeout_seconds: Optional[float],
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Execute a raw shell command on the remote VM.

    Use only when no higher-level tool covers the diagnostic. The command is
    validated against a hard-coded deny list (``rm -rf /``, ``mkfs``,
    ``reboot``, fork bombs, ...) before execution.

    Args:
        command: The command to run (interpreted by the remote login shell).
        timeout_seconds: Optional per-command timeout override. Leave null to
            use the configured default (``SSH_COMMAND_TIMEOUT``).
    """
    if not command or not command.strip():
        return ToolError(
            error="`command` is required.",
            tool="ssh_execute",
            hint="Pass a non-empty command string.",
        ).model_dump()

    services = get_services()
    try:
        result = await services.ssh.execute_async(command, timeout=timeout_seconds)
    except SSHCommandBlocked as exc:
        logger.warning("ssh_execute blocked: {exc}", exc=exc)
        return ToolError(
            status=ToolStatus.BLOCKED,
            error="Command blocked by safety denylist.",
            detail=str(exc),
            tool="ssh_execute",
        ).model_dump()
    except SSHConnectionError as exc:
        return _error("ssh_execute", "SSH connection failed", exc)
    except ValueError as exc:
        return ToolError(
            error=str(exc), tool="ssh_execute", hint="Command cannot be empty."
        ).model_dump()

    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_ssh_tools() -> List[FunctionTool]:
    """Return the list of ADK tools exposed by :mod:`app.tools.ssh_tool`."""
    return [
        FunctionTool(func=ssh_connect),
        FunctionTool(func=ssh_disconnect),
        FunctionTool(func=ssh_execute),
    ]


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


async def _run(fn, *args, **kwargs):
    """Run a blocking callable in a worker thread (helper for ADK tools)."""
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


def _error(tool: str, message: str, exc: BaseException) -> Dict[str, Any]:
    """Return a serialised :class:`ToolError` for the given exception."""
    logger.warning("{tool} error: {msg} ({exc})", tool=tool, msg=message, exc=exc)
    return ToolError(
        error=message, detail=str(exc), tool=tool
    ).model_dump()


__all__ = [
    "build_ssh_tools",
    "ssh_connect",
    "ssh_disconnect",
    "ssh_execute",
]
