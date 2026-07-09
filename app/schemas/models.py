"""Pydantic v2 models shared across services, tools, and the API.

The models describe the *structured* return payload of every ADK tool.
Gemini consumes these dictionaries directly, so keep the field names short
and self-descriptive.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums / primitives
# ---------------------------------------------------------------------------


class ToolStatus(str, Enum):
    """Standard status codes returned by every tool."""

    SUCCESS = "success"
    ERROR = "error"
    CONFIRMATION_REQUIRED = "confirmation_required"
    BLOCKED = "blocked"


class _Base(BaseModel):
    """Common base class enabling strict, well-documented models."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Chat / API models
# ---------------------------------------------------------------------------


class ChatRequest(_Base):
    """Payload accepted by the ``POST /chat`` endpoint."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="The user's natural-language DevOps question.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Reuse an existing ADK session to keep multi-turn context. "
            "When omitted, a new session is created."
        ),
    )
    user_id: Optional[str] = Field(
        default=None,
        description="Optional stable identifier for the calling user.",
    )


class ChatResponse(_Base):
    """Response returned by the ``POST /chat`` endpoint."""

    answer: str = Field(..., description="The assistant's final answer.")
    session_id: str = Field(..., description="Session used to serve the reply.")
    user_id: str = Field(..., description="User id associated with the reply.")
    tool_calls: List[str] = Field(
        default_factory=list,
        description="Names of ADK tools invoked while producing the answer.",
    )
    duration_ms: float = Field(
        ...,
        ge=0.0,
        description="Total wall-clock time spent producing the answer.",
    )


# ---------------------------------------------------------------------------
# Tool payloads
# ---------------------------------------------------------------------------


class ToolError(_Base):
    """Structured error returned when a tool cannot fulfil the request."""

    status: ToolStatus = Field(default=ToolStatus.ERROR)
    error: str = Field(..., description="Short, human-readable summary.")
    detail: Optional[str] = Field(
        default=None,
        description="Full technical detail. Safe to expose to the LLM.",
    )
    tool: Optional[str] = Field(default=None, description="Originating tool.")
    hint: Optional[str] = Field(
        default=None,
        description="Optional recovery suggestion the LLM can relay to the user.",
    )


class ConfirmationRequired(_Base):
    """Returned by destructive tools when the user has not yet confirmed."""

    status: ToolStatus = Field(default=ToolStatus.CONFIRMATION_REQUIRED)
    action: str = Field(..., description="Machine-readable action identifier.")
    prompt: str = Field(..., description="Human-readable confirmation prompt.")
    target: Optional[str] = Field(
        default=None,
        description="Target of the action (container name, service, ...).",
    )
    reversible: bool = Field(
        default=False,
        description="Whether the action can be undone without data loss.",
    )


class CommandResult(_Base):
    """Result of executing a shell command via SSH."""

    status: ToolStatus = Field(default=ToolStatus.SUCCESS)
    command: str = Field(..., description="The exact command that ran.")
    stdout: str = Field(default="", description="Captured standard output.")
    stderr: str = Field(default="", description="Captured standard error.")
    exit_code: int = Field(..., description="Process exit code.")
    duration_ms: float = Field(..., ge=0.0, description="Wall-clock duration.")
    truncated: bool = Field(
        default=False,
        description="True when stdout/stderr were truncated for safety.",
    )
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the command was executed.",
    )

    @property
    def ok(self) -> bool:
        """True iff the command exited successfully."""
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Linux telemetry
# ---------------------------------------------------------------------------


class DiskPartition(_Base):
    """A single mount point reported by ``df``."""

    filesystem: str
    size: str
    used: str
    available: str
    use_percent: int = Field(..., ge=0, le=100)
    mounted_on: str


class LinuxMetrics(_Base):
    """Composite Linux metrics snapshot."""

    status: ToolStatus = Field(default=ToolStatus.SUCCESS)
    hostname: Optional[str] = None
    kernel: Optional[str] = None
    uptime: Optional[str] = None
    load_average: Optional[Dict[str, float]] = None
    cpu_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    memory: Optional[Dict[str, Any]] = None
    swap: Optional[Dict[str, Any]] = None
    disks: List[DiskPartition] = Field(default_factory=list)
    top_processes_cpu: List[Dict[str, Any]] = Field(default_factory=list)
    top_processes_mem: List[Dict[str, Any]] = Field(default_factory=list)


class ServiceInfo(_Base):
    """A systemd unit as reported by ``systemctl``."""

    unit: str
    load: str
    active: str
    sub: str
    description: str


# ---------------------------------------------------------------------------
# Docker / Jenkins
# ---------------------------------------------------------------------------


class DockerContainerInfo(_Base):
    """A single container as reported by ``docker ps``."""

    container_id: str
    name: str
    image: str
    status: str
    state: str
    ports: str = ""
    created: str = ""


class DockerImageInfo(_Base):
    """A single image as reported by ``docker images``."""

    repository: str
    tag: str
    image_id: str
    created: str
    size: str


class HealthStatus(_Base):
    """Health snapshot for a service (Docker daemon, Jenkins, ...)."""

    status: ToolStatus = Field(default=ToolStatus.SUCCESS)
    service: str
    healthy: bool
    detail: str = ""
    checks: Dict[str, Any] = Field(default_factory=dict)


class JenkinsStatus(_Base):
    """Simplified Jenkins runtime status."""

    status: ToolStatus = Field(default=ToolStatus.SUCCESS)
    installed: bool
    running: bool
    version: Optional[str] = None
    active_state: Optional[str] = None
    sub_state: Optional[str] = None
    detail: str = ""


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "CommandResult",
    "ConfirmationRequired",
    "DiskPartition",
    "DockerContainerInfo",
    "DockerImageInfo",
    "HealthStatus",
    "JenkinsStatus",
    "LinuxMetrics",
    "ServiceInfo",
    "ToolError",
    "ToolStatus",
]
