"""ADK ``FunctionTool`` bindings for log summarisation and explanation.

These tools fetch logs from a named source (system journal, a Docker
container, or Jenkins) and structure them into a shape the LLM can reason
about. Fetching happens *inside* the tool via a single call so weaker models
never have to copy raw log text between two tool calls (a fragile hand-off
that some models botch).
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.schemas import ToolError, ToolStatus
from app.services import get_services
from app.services.docker_service import DockerNotAvailable
from app.services.ssh_service import SSHConnectionError
from app.utils import get_logger, truncate_text

logger = get_logger(__name__)


# Regex patterns used to categorise log lines.
_SEVERITY_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("critical", re.compile(r"\b(FATAL|CRITICAL|EMERG|PANIC)\b", re.IGNORECASE)),
    ("error", re.compile(r"\b(ERROR|ERR|FAIL(?:ED)?|EXCEPTION|TRACEBACK|SEGFAULT)\b", re.IGNORECASE)),
    ("warning", re.compile(r"\b(WARN(?:ING)?|WARNING)\b", re.IGNORECASE)),
    ("notice", re.compile(r"\b(NOTICE|INFO)\b", re.IGNORECASE)),
]

_TIMESTAMP_RE = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    r"|\d{2}:\d{2}:\d{2})"
)

_NOISE_PATTERNS = (
    re.compile(r"^\s*$"),
    re.compile(r"^-- Journal begins", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


async def logs_summarize(
    source: str,
    container: str,
    lines: int,
    max_lines: int,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Fetch logs from a source and bucket them by severity.

    Use this for questions like *"Analyze the system logs"* or *"What went
    wrong in the n8n container?"*. This tool fetches the logs itself - you do
    NOT need to call another logs tool first.

    Args:
        source: Where to read logs from. One of: ``"system"`` (the systemd
            journal), ``"docker"`` (a container - set ``container``), or
            ``"jenkins"``. Defaults to ``"system"`` when empty.
        container: Container name or ID. Required only when ``source`` is
            ``"docker"``; pass an empty string otherwise.
        lines: How many recent log lines to fetch (1..2000). 200 is a good
            default.
        max_lines: Maximum lines to keep per severity bucket (1..200). 25 is
            a good default.
    """
    fetched = await _fetch_logs("logs_summarize", source, container, lines)
    if isinstance(fetched, dict):  # error payload
        return fetched
    log_text, resolved_source = fetched

    max_lines = max(1, min(int(max_lines or 25), 200))

    buckets: Dict[str, List[str]] = {
        "critical": [],
        "error": [],
        "warning": [],
        "notice": [],
        "other": [],
    }
    frequency: Counter[str] = Counter()

    for raw in log_text.splitlines():
        if any(pattern.match(raw) for pattern in _NOISE_PATTERNS):
            continue
        bucket = _classify_line(raw)
        if len(buckets[bucket]) < max_lines:
            buckets[bucket].append(raw.rstrip())
        if bucket in {"critical", "error"}:
            fingerprint = _fingerprint_line(raw)
            if fingerprint:
                frequency[fingerprint] += 1

    total_lines = sum(len(entries) for entries in buckets.values())
    top_signatures = [
        {"signature": sig, "count": count}
        for sig, count in frequency.most_common(10)
    ]

    return {
        "status": ToolStatus.SUCCESS.value,
        "source": resolved_source,
        "total_kept": total_lines,
        "counts": {name: len(entries) for name, entries in buckets.items()},
        "buckets": buckets,
        "top_error_signatures": top_signatures,
    }


async def logs_explain(
    source: str,
    container: str,
    lines: int,
    focus: str,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Fetch logs from a source and package them for the LLM to explain.

    This tool fetches the logs itself - you do NOT need to call another logs
    tool first. It does not paraphrase; it returns a compact, labelled excerpt
    (plus severity counts and timestamps) for you to reason over honestly.

    Args:
        source: Where to read logs from. One of ``"system"``, ``"docker"``
            (set ``container``), or ``"jenkins"``. Defaults to ``"system"``.
        container: Container name or ID. Required only when ``source`` is
            ``"docker"``; pass an empty string otherwise.
        lines: How many recent log lines to fetch (1..2000). 200 is a good
            default.
        focus: Free-form guidance describing what the user cares about (e.g.
            ``"authentication failures"``). Empty string means general.
    """
    fetched = await _fetch_logs("logs_explain", source, container, lines)
    if isinstance(fetched, dict):  # error payload
        return fetched
    log_text, resolved_source = fetched

    excerpt = truncate_text(log_text, max_chars=6000, tail=True)
    counts: Counter[str] = Counter()
    timestamps: List[str] = []
    for line in log_text.splitlines():
        counts[_classify_line(line)] += 1
        stamp = _TIMESTAMP_RE.search(line)
        if stamp and len(timestamps) < 10:
            timestamps.append(stamp.group(0))

    return {
        "status": ToolStatus.SUCCESS.value,
        "source": resolved_source,
        "focus": (focus or "").strip() or "general",
        "excerpt": excerpt,
        "severity_counts": dict(counts),
        "sample_timestamps": timestamps,
        "instructions_for_agent": (
            "Use the excerpt and severity counts to write a concise, honest "
            "explanation. Never invent facts that are not in the excerpt."
        ),
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_logs_tools() -> List[FunctionTool]:
    """Return the list of ADK tools exposed by :mod:`app.tools.logs_tool`."""
    return [
        FunctionTool(func=logs_summarize),
        FunctionTool(func=logs_explain),
    ]


# ---------------------------------------------------------------------------
# Log fetching
# ---------------------------------------------------------------------------


async def _fetch_logs(
    tool: str,
    source: str,
    container: str,
    lines: int,
):
    """Fetch raw log text from the requested source.

    Returns a ``(log_text, resolved_source)`` tuple on success, or a
    ``ToolError`` dict on failure (empty logs, bad args, SSH/Docker errors).
    """
    resolved = (source or "system").strip().lower()
    lines = max(1, min(int(lines or 200), 2000))
    services = get_services()

    try:
        if resolved in {"system", "syslog", "journal", ""}:
            resolved = "system"
            text = await asyncio.to_thread(
                services.linux.system_logs, lines=lines, priority="err"
            )
        elif resolved in {"docker", "container"}:
            resolved = "docker"
            if not container or not container.strip():
                return ToolError(
                    error="`container` is required when source='docker'.",
                    tool=tool,
                    hint="Pass the container name/ID, e.g. container='n8n'.",
                ).model_dump()
            text = await asyncio.to_thread(
                services.docker.logs, container.strip(), tail=lines
            )
        elif resolved == "jenkins":
            text = await asyncio.to_thread(services.jenkins.logs, lines=lines)
        else:
            return ToolError(
                error=f"Unknown log source {source!r}.",
                tool=tool,
                hint="Use one of: 'system', 'docker', 'jenkins'.",
            ).model_dump()
    except ValueError as exc:
        return ToolError(error=str(exc), tool=tool).model_dump()
    except (DockerNotAvailable, SSHConnectionError) as exc:
        return ToolError(
            error=f"{tool} could not fetch {resolved} logs",
            detail=str(exc),
            tool=tool,
            hint="Verify SSH connectivity (and Docker, for container logs).",
        ).model_dump()

    if not text or not text.strip():
        return ToolError(
            error=f"No {resolved} log lines were returned.",
            tool=tool,
            hint="The source may have no recent entries at this priority.",
        ).model_dump()

    return text, resolved


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in :mod:`tests.test_tools`)
# ---------------------------------------------------------------------------


def _classify_line(line: str) -> str:
    """Return the severity bucket for a single log line."""
    for name, pattern in _SEVERITY_PATTERNS:
        if pattern.search(line):
            return name
    return "other"


_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM_RE = re.compile(r"\b\d+\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")


def _fingerprint_line(line: str) -> str:
    """Normalise a log line so similar errors collapse into one signature."""
    stripped = _TIMESTAMP_RE.sub("<ts>", line)
    stripped = _UUID_RE.sub("<uuid>", stripped)
    stripped = _HEX_RE.sub("<hex>", stripped)
    stripped = _NUM_RE.sub("<n>", stripped)
    return stripped.strip()[:240]


__all__ = ["build_logs_tools", "logs_explain", "logs_summarize"]
