"""Pydantic v2 models used across the app.

Every tool returns a Pydantic model that gets serialised to a plain ``dict``
before being handed back to the ADK runtime. The dict shape doubles as the
structured tool response that Gemini consumes.
"""

from __future__ import annotations

from app.schemas.models import (
    ChatRequest,
    ChatResponse,
    CommandResult,
    ConfirmationRequired,
    DiskPartition,
    DockerContainerInfo,
    DockerImageInfo,
    HealthStatus,
    JenkinsStatus,
    LinuxMetrics,
    ServiceInfo,
    ToolError,
    ToolStatus,
)

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
