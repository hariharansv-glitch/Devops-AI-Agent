"""ADK ``FunctionTool`` bindings for log summarisation and explanation.

These tools are stateless helpers that structure raw log text into a shape
Gemini can reason about. They don't call the LLM directly; the LLM invokes
them to *prepare* the material it will summarise using its own reasoning.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Tuple

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.schemas import ToolError, ToolStatus
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
    log_text: str,
    max_lines: int,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Bucket log lines by severity and surface the most frequent errors.

    Use this tool right before answering questions like *"Analyze system logs"*
    or *"What went wrong in Docker last night?"*. Feed the raw text captured
    from ``linux_system_logs``, ``docker_logs``, or ``jenkins_logs``.

    Args:
        log_text: Raw log text.
        max_lines: Maximum number of recent lines to keep per bucket
            (1..200). 25 is a good default.
    """
    if not log_text or not log_text.strip():
        return ToolError(
            error="`log_text` is empty; nothing to summarise.",
            tool="logs_summarize",
            hint="Call a *_logs tool first to capture the text.",
        ).model_dump()

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
        "total_kept": total_lines,
        "counts": {name: len(entries) for name, entries in buckets.items()},
        "buckets": buckets,
        "top_error_signatures": top_signatures,
    }


async def logs_explain(
    log_text: str,
    focus: str,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """Return a structured hand-off for the LLM to explain a log excerpt.

    The tool does **not** paraphrase the log by itself; that reasoning is
    Gemini's job. Instead it packages the excerpt (truncated to a safe
    length), the caller's ``focus`` instruction, and useful hints (severity
    counts, timestamps observed) so the LLM has a compact, well-labelled
    payload to reason over.

    Args:
        log_text: Raw log text to explain.
        focus: Free-form guidance describing what the user cares about
            (e.g. ``"authentication failures in the last hour"``). Empty
            string means: give a general explanation.
    """
    if not log_text or not log_text.strip():
        return ToolError(
            error="`log_text` is empty; nothing to explain.",
            tool="logs_explain",
        ).model_dump()

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
