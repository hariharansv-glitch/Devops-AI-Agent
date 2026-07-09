"""ADK ``FunctionTool`` bindings for Linux telemetry.

Each tool is a thin async wrapper around :class:`app.services.LinuxService`.
Blocking work is delegated to a worker thread via :func:`asyncio.to_thread`
so the FastAPI event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.schemas import ToolError, ToolStatus
from app.services import get_services
from app.services.ssh_service import SSHConnectionError
from app.utils import bytes_to_human, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


async def linux_disk_usage(tool_context: ToolContext) -> Dict[str, Any]:
    """Return disk usage (``df -hPT``) for every mounted filesystem.

    Use this tool to answer questions like *"How much disk space is left?"* or
    *"Which filesystem is full?"*. Results include size, used, available,
    percent used, and mount point.
    """
    linux = get_services().linux
    try:
        partitions = await asyncio.to_thread(linux.disk_usage)
    except SSHConnectionError as exc:
        return _err("linux_disk_usage", exc)

    return {
        "status": ToolStatus.SUCCESS.value,
        "partitions": [p.model_dump() for p in partitions],
        "count": len(partitions),
    }


async def linux_memory_usage(tool_context: ToolContext) -> Dict[str, Any]:
    """Return RAM and swap usage in bytes, percent, and human-readable form.

    Use this tool to answer questions like *"How much RAM is available?"* or
    *"Is the server swapping?"*.
    """
    linux = get_services().linux
    try:
        parsed = await asyncio.to_thread(linux.memory_usage)
    except SSHConnectionError as exc:
        return _err("linux_memory_usage", exc)

    memory = parsed.get("memory") or {}
    swap = parsed.get("swap") or {}
    return {
        "status": ToolStatus.SUCCESS.value,
        "memory": {
            **memory,
            "total_human": bytes_to_human(memory.get("total_bytes", 0)),
            "used_human": bytes_to_human(memory.get("used_bytes", 0)),
            "available_human": bytes_to_human(memory.get("available_bytes", 0)),
        },
        "swap": {
            **swap,
            "total_human": bytes_to_human(swap.get("total_bytes", 0)),
            "used_human": bytes_to_human(swap.get("used_bytes", 0)),
        },
    }


async def linux_cpu_usage(tool_context: ToolContext) -> Dict[str, Any]:
    """Return the current CPU busy percentage (0-100)."""
    linux = get_services().linux
    try:
        cpu = await asyncio.to_thread(linux.cpu_usage)
    except SSHConnectionError as exc:
        return _err("linux_cpu_usage", exc)
    return {"status": ToolStatus.SUCCESS.value, "cpu_percent": cpu}


async def linux_load_average(tool_context: ToolContext) -> Dict[str, Any]:
    """Return the 1/5/15-minute load averages."""
    linux = get_services().linux
    try:
        load = await asyncio.to_thread(linux.load_average)
    except SSHConnectionError as exc:
        return _err("linux_load_average", exc)
    return {"status": ToolStatus.SUCCESS.value, "load_average": load}


async def linux_uptime(tool_context: ToolContext) -> Dict[str, Any]:
    """Return the server's uptime string (``uptime -p``)."""
    linux = get_services().linux
    try:
        uptime = await asyncio.to_thread(linux.uptime)
    except SSHConnectionError as exc:
        return _err("linux_uptime", exc)
    return {"status": ToolStatus.SUCCESS.value, "uptime": uptime}


async def linux_running_services(
    limit: int, tool_context: ToolContext
) -> Dict[str, Any]:
    """Return the currently active systemd services.

    Args:
        limit: Maximum number of services to return (1..200). Pass 50 for a
            typical DevOps overview.
    """
    limit = max(1, min(int(limit or 50), 200))
    linux = get_services().linux
    try:
        services = await asyncio.to_thread(linux.running_services, limit=limit)
    except SSHConnectionError as exc:
        return _err("linux_running_services", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "services": [s.model_dump() for s in services],
        "count": len(services),
    }


async def linux_open_ports(tool_context: ToolContext) -> Dict[str, Any]:
    """Return TCP/UDP sockets in the LISTEN state (via ``ss -tulpen``)."""
    linux = get_services().linux
    try:
        ports = await asyncio.to_thread(linux.open_ports)
    except SSHConnectionError as exc:
        return _err("linux_open_ports", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "listeners": ports,
        "count": len(ports),
    }


async def linux_system_logs(
    lines: int, priority: str, tool_context: ToolContext
) -> Dict[str, Any]:
    """Return recent journald / syslog entries at or above ``priority``.

    Args:
        lines: How many trailing lines to return (1..2000). 200 is a good
            default for a quick investigation.
        priority: journalctl priority filter (``emerg``, ``alert``, ``crit``,
            ``err``, ``warning``, ``notice``, ``info``, ``debug``). Defaults
            to ``err`` to surface actual problems.
    """
    lines = max(1, min(int(lines or 200), 2000))
    priority = (priority or "err").strip().lower()
    if priority not in {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}:
        return ToolError(
            error=f"Unsupported priority: {priority!r}",
            tool="linux_system_logs",
            hint="Use one of: emerg, alert, crit, err, warning, notice, info, debug.",
        ).model_dump()

    linux = get_services().linux
    try:
        text = await asyncio.to_thread(linux.system_logs, lines=lines, priority=priority)
    except SSHConnectionError as exc:
        return _err("linux_system_logs", exc)

    return {
        "status": ToolStatus.SUCCESS.value,
        "lines": lines,
        "priority": priority,
        "log": text,
    }


async def linux_snapshot(tool_context: ToolContext) -> Dict[str, Any]:
    """Collect a broad snapshot of Linux telemetry in a single tool call.

    This is the recommended tool when the user asks a vague question like
    *"Why is my server slow?"*. It returns hostname, kernel, uptime, load
    average, CPU percent, memory, swap, disks, and the top CPU/memory
    processes.
    """
    linux = get_services().linux
    try:
        snapshot = await asyncio.to_thread(linux.snapshot)
    except SSHConnectionError as exc:
        return _err("linux_snapshot", exc)
    return snapshot.model_dump()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_linux_tools() -> List[FunctionTool]:
    """Return the list of ADK tools exposed by :mod:`app.tools.linux_tool`."""
    return [
        FunctionTool(func=linux_disk_usage),
        FunctionTool(func=linux_memory_usage),
        FunctionTool(func=linux_cpu_usage),
        FunctionTool(func=linux_load_average),
        FunctionTool(func=linux_uptime),
        FunctionTool(func=linux_running_services),
        FunctionTool(func=linux_open_ports),
        FunctionTool(func=linux_system_logs),
        FunctionTool(func=linux_snapshot),
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
        hint="Verify SSH connectivity and that the required binary exists on the remote host.",
    ).model_dump()


__all__ = [
    "build_linux_tools",
    "linux_cpu_usage",
    "linux_disk_usage",
    "linux_load_average",
    "linux_memory_usage",
    "linux_open_ports",
    "linux_running_services",
    "linux_snapshot",
    "linux_system_logs",
    "linux_uptime",
]
