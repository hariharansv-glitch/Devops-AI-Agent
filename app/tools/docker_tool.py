"""ADK ``FunctionTool`` bindings for Docker inspection.

Destructive tools (``docker_restart_container``, ``docker_prune``) require
an explicit ``confirm=True`` argument. When ``confirm`` is missing or
``False`` the tool returns a structured ``ConfirmationRequired`` response
that the LLM relays to the user. This is the confirmation flow described in
the project brief.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.schemas import ConfirmationRequired, ToolError, ToolStatus
from app.services import get_services
from app.services.docker_service import DockerNotAvailable
from app.services.ssh_service import SSHConnectionError
from app.utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


async def docker_running_containers(tool_context: ToolContext) -> Dict[str, Any]:
    """Return every currently running Docker container."""
    return await _list_containers(only_running=True, tool="docker_running_containers")


async def docker_stopped_containers(tool_context: ToolContext) -> Dict[str, Any]:
    """Return containers whose state is not ``running`` (exited, dead, ...)."""
    docker = get_services().docker
    try:
        containers = await asyncio.to_thread(docker.stopped_containers)
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_stopped_containers", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "containers": [c.model_dump() for c in containers],
        "count": len(containers),
    }


async def docker_images(tool_context: ToolContext) -> Dict[str, Any]:
    """Return every Docker image on the remote host."""
    docker = get_services().docker
    try:
        images = await asyncio.to_thread(docker.images)
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_images", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "images": [i.model_dump() for i in images],
        "count": len(images),
    }


async def docker_logs(
    container: str,
    tail: int,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Return the last ``tail`` lines from a container's logs.

    Args:
        container: Container name or ID (letters, digits, ``.-_``; max 128
            chars).
        tail: Number of trailing lines to return (1..2000). 200 is a good
            default.
    """
    tail = max(1, min(int(tail or 200), 2000))
    docker = get_services().docker
    try:
        text = await asyncio.to_thread(docker.logs, container, tail=tail)
    except ValueError as exc:
        return ToolError(error=str(exc), tool="docker_logs").model_dump()
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_logs", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "container": container,
        "tail": tail,
        "log": text,
    }


async def docker_stats(tool_context: ToolContext) -> Dict[str, Any]:
    """Return a single sample of ``docker stats --no-stream``.

    Useful for answering *"Which Docker container is consuming memory?"* or
    *"How much CPU is Docker using?"*.
    """
    docker = get_services().docker
    try:
        entries = await asyncio.to_thread(docker.stats)
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_stats", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "stats": entries,
        "count": len(entries),
    }


async def docker_inspect(container: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Return the full JSON ``docker inspect`` payload for a container."""
    docker = get_services().docker
    try:
        payload = await asyncio.to_thread(docker.inspect, container)
    except ValueError as exc:
        return ToolError(error=str(exc), tool="docker_inspect").model_dump()
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_inspect", exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "container": container,
        "inspect": payload,
    }


async def docker_disk_usage(tool_context: ToolContext) -> Dict[str, Any]:
    """Return ``docker system df`` output (space used by images/containers/volumes)."""
    docker = get_services().docker
    try:
        usage = await asyncio.to_thread(docker.disk_usage)
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_disk_usage", exc)
    return {"status": ToolStatus.SUCCESS.value, **usage}


async def docker_health(tool_context: ToolContext) -> Dict[str, Any]:
    """Return a compact Docker health report (daemon reachable, counts, version)."""
    docker = get_services().docker
    try:
        health = await asyncio.to_thread(docker.health)
    except SSHConnectionError as exc:
        return _err("docker_health", exc)
    return health.model_dump()


# ---------------------------------------------------------------------------
# Destructive tools (require confirm=True + not in READ_ONLY_MODE)
# ---------------------------------------------------------------------------


async def docker_restart_container(
    container: str,
    confirm: bool,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Restart a Docker container (destructive).

    The first invocation with ``confirm=False`` (the default from the LLM)
    returns a ``ConfirmationRequired`` payload rather than executing. Only
    when the user has explicitly agreed and the LLM re-issues the call with
    ``confirm=True`` will the restart run.

    Args:
        container: Container name or ID.
        confirm: MUST be ``true`` and only after the user has verbally agreed
            to the restart in the current session.
    """
    services = get_services()
    if not confirm:
        return ConfirmationRequired(
            action="docker.restart_container",
            target=container,
            prompt=(
                f"I am about to restart Docker container {container!r} on "
                f"{services.settings.vm_host}. Do you want to continue?"
            ),
            reversible=False,
        ).model_dump()

    if services.settings.read_only_mode:
        return _read_only("docker_restart_container")

    try:
        result = await asyncio.to_thread(services.docker.restart_container, container)
    except ValueError as exc:
        return ToolError(error=str(exc), tool="docker_restart_container").model_dump()
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_restart_container", exc)
    return result


async def docker_prune(
    scope: str,
    volumes: bool,
    confirm: bool,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Reclaim disk space using ``docker <scope> prune -f`` (destructive).

    Args:
        scope: One of ``system``, ``container``, ``image``, ``network``,
            ``volume``, ``builder``. Defaults to ``system``.
        volumes: When ``scope=='system'``, also prune anonymous volumes.
            **This deletes data**; require an explicit user confirmation.
        confirm: MUST be ``true`` and only after the user has verbally agreed.
    """
    scope = (scope or "system").strip().lower()
    services = get_services()

    if not confirm:
        prompt = (
            f"I am about to run `docker {scope} prune -f"
            + (" --volumes" if scope == "system" and volumes else "")
            + f"` on {services.settings.vm_host}. Do you want to continue?"
        )
        return ConfirmationRequired(
            action=f"docker.prune.{scope}",
            target=("volumes" if volumes else "cache/dangling"),
            prompt=prompt,
            reversible=False,
        ).model_dump()

    if services.settings.read_only_mode:
        return _read_only("docker_prune")

    try:
        result = await asyncio.to_thread(
            services.docker.prune, scope=scope, volumes=volumes
        )
    except ValueError as exc:
        return ToolError(error=str(exc), tool="docker_prune").model_dump()
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err("docker_prune", exc)
    return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_docker_tools() -> List[FunctionTool]:
    """Return the list of ADK tools exposed by :mod:`app.tools.docker_tool`."""
    return [
        FunctionTool(func=docker_running_containers),
        FunctionTool(func=docker_stopped_containers),
        FunctionTool(func=docker_images),
        FunctionTool(func=docker_logs),
        FunctionTool(func=docker_stats),
        FunctionTool(func=docker_inspect),
        FunctionTool(func=docker_disk_usage),
        FunctionTool(func=docker_health),
        FunctionTool(func=docker_restart_container),
        FunctionTool(func=docker_prune),
    ]


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


async def _list_containers(*, only_running: bool, tool: str) -> Dict[str, Any]:
    docker = get_services().docker
    try:
        containers = await asyncio.to_thread(
            docker.running_containers if only_running else docker.all_containers
        )
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return _err(tool, exc)
    return {
        "status": ToolStatus.SUCCESS.value,
        "containers": [c.model_dump() for c in containers],
        "count": len(containers),
    }


def _err(tool: str, exc: BaseException) -> Dict[str, Any]:
    logger.warning("{tool} error: {exc}", tool=tool, exc=exc)
    hint: str | None = None
    if isinstance(exc, DockerNotAvailable):
        hint = "Ensure Docker is installed and the SSH user can execute `docker`."
    elif isinstance(exc, SSHConnectionError):
        hint = "Verify SSH connectivity to the target VM."
    return ToolError(
        error=f"{tool} failed", detail=str(exc), tool=tool, hint=hint
    ).model_dump()


def _read_only(tool: str) -> Dict[str, Any]:
    return ToolError(
        status=ToolStatus.BLOCKED,
        error="Read-only mode is enabled; destructive operations are disabled.",
        detail="Set READ_ONLY_MODE=FALSE in the environment to allow this action.",
        tool=tool,
        hint="Ask an operator to disable READ_ONLY_MODE before retrying.",
    ).model_dump()


__all__ = [
    "build_docker_tools",
    "docker_disk_usage",
    "docker_health",
    "docker_images",
    "docker_inspect",
    "docker_logs",
    "docker_prune",
    "docker_restart_container",
    "docker_running_containers",
    "docker_stats",
    "docker_stopped_containers",
]
