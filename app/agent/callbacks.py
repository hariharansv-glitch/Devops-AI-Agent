"""ADK callbacks used by the DevOps agent.

Two callbacks are wired onto the :class:`LlmAgent`:

* :func:`before_tool_callback` - defense-in-depth safety layer. It logs every
  tool call, enforces ``READ_ONLY_MODE`` at the framework level (not just
  inside the tool), and records timing information in the session state.
* :func:`after_tool_callback`  - captures duration, tool status, and the
  ordered list of tools invoked in the current turn. That list is used by
  the FastAPI ``/chat`` endpoint to report ``tool_calls`` back to the user.

Both callbacks follow the ADK 2.x signatures documented in
``docs/callbacks/types-of-callbacks.md``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.config import get_settings
from app.utils import format_duration, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "docker_restart_container",
        "docker_prune",
        "jenkins_restart",
    }
)

# State keys (namespaced so they don't collide with user data).
STATE_TOOL_CALLS = "devops.tool_calls"
STATE_TIMERS = "devops.tool_timers"
STATE_TURN_STARTED_AT = "devops.turn_started_at"
STATE_LAST_ERROR = "devops.last_error"


# ---------------------------------------------------------------------------
# Agent-level callbacks
# ---------------------------------------------------------------------------


def before_agent_callback(callback_context: CallbackContext) -> None:
    """Initialise per-turn bookkeeping in the ADK session state."""
    state = callback_context.state
    state[STATE_TOOL_CALLS] = []
    state[STATE_TIMERS] = {}
    state[STATE_TURN_STARTED_AT] = time.perf_counter()
    state[STATE_LAST_ERROR] = None
    logger.debug("before_agent: state initialised for a new turn")


def after_agent_callback(callback_context: CallbackContext) -> None:
    """Log the total wall-clock duration of the turn."""
    started = callback_context.state.get(STATE_TURN_STARTED_AT)
    if started is None:
        return
    elapsed = time.perf_counter() - float(started)
    logger.info(
        "agent turn complete duration={dur} tool_calls={tools}",
        dur=format_duration(elapsed),
        tools=callback_context.state.get(STATE_TOOL_CALLS, []),
    )


# ---------------------------------------------------------------------------
# Tool-level callbacks
# ---------------------------------------------------------------------------


def before_tool_callback(
    tool: BaseTool,
    args: Dict[str, Any],
    tool_context: ToolContext,
) -> Optional[Dict[str, Any]]:
    """Guard every tool call and record its start time.

    Returning ``None`` lets the tool run normally. Returning a dictionary
    short-circuits execution and the returned dictionary is used as if it
    were the tool's own response.
    """
    tool_name = tool.name
    logger.info(
        "tool>> name={name} agent={agent} args_keys={keys}",
        name=tool_name,
        agent=tool_context.agent_name,
        keys=sorted(args.keys()) if isinstance(args, dict) else "?",
    )

    # ---- Safety: block destructive tools in read-only mode ----------
    if tool_name in DESTRUCTIVE_TOOLS and get_settings().read_only_mode:
        # Only the *executing* call (confirm=True) is blocked. When the LLM
        # is still asking for confirmation (confirm != True) we let the tool
        # run so it can respond with the standard ConfirmationRequired
        # payload; that behaviour is nicer for the user than a blunt error.
        if bool(args.get("confirm")) is True:
            logger.warning(
                "tool blocked by READ_ONLY_MODE name={name}", name=tool_name
            )
            return {
                "status": "blocked",
                "tool": tool_name,
                "error": "Read-only mode is enabled; destructive operations are disabled.",
                "detail": "Set READ_ONLY_MODE=FALSE to allow this action.",
                "hint": "Ask an operator to disable READ_ONLY_MODE and try again.",
            }

    # ---- Record start time so after_tool can compute the duration ----
    timers = _ensure_dict(tool_context.state, STATE_TIMERS)
    timers[_timer_key(tool_context, tool_name)] = time.perf_counter()
    tool_context.state[STATE_TIMERS] = timers
    return None


def after_tool_callback(
    tool: BaseTool,
    args: Dict[str, Any],
    tool_context: ToolContext,
    tool_response: Any,
) -> Optional[Dict[str, Any]]:
    """Log tool completion, capture timing, and update state bookkeeping.

    Returning ``None`` keeps the tool's actual response. We never rewrite the
    response here to avoid confusing the LLM.
    """
    tool_name = tool.name
    timers: Dict[str, float] = _ensure_dict(tool_context.state, STATE_TIMERS)
    key = _timer_key(tool_context, tool_name)
    started = timers.pop(key, None)
    tool_context.state[STATE_TIMERS] = timers

    duration_ms: Optional[float] = None
    if started is not None:
        duration_ms = (time.perf_counter() - float(started)) * 1000

    status = _extract_status(tool_response)

    # Append to the per-turn tool_calls list so the API can echo it back.
    tool_calls: List[Dict[str, Any]] = list(
        tool_context.state.get(STATE_TOOL_CALLS, []) or []
    )
    tool_calls.append(
        {
            "tool": tool_name,
            "status": status,
            "duration_ms": duration_ms,
        }
    )
    tool_context.state[STATE_TOOL_CALLS] = tool_calls

    if status == "error":
        tool_context.state[STATE_LAST_ERROR] = {
            "tool": tool_name,
            "response": _shrink(tool_response),
        }

    logger.info(
        "tool<< name={name} status={status} duration_ms={ms}",
        name=tool_name,
        status=status,
        ms=f"{duration_ms:.1f}" if duration_ms is not None else "n/a",
    )
    return None


# ---------------------------------------------------------------------------
# Public helpers used by :mod:`app.agent.runner`
# ---------------------------------------------------------------------------


def tool_calls_from_state(state: Dict[str, Any]) -> List[str]:
    """Return the ordered list of tool names invoked during the current turn."""
    raw = state.get(STATE_TOOL_CALLS, []) or []
    return [entry["tool"] for entry in raw if isinstance(entry, dict) and "tool" in entry]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _timer_key(tool_context: ToolContext, tool_name: str) -> str:
    """Return a state key unique to the current tool invocation."""
    # ``function_call_id`` is populated by the ADK runtime for each tool call.
    call_id = getattr(tool_context, "function_call_id", None) or "single"
    return f"{tool_name}:{call_id}"


def _ensure_dict(state: Any, key: str) -> Dict[str, Any]:
    """Return ``state[key]`` as a dict, creating it if missing."""
    value = state.get(key)
    if not isinstance(value, dict):
        value = {}
    return value


def _extract_status(response: Any) -> str:
    """Best-effort extraction of the ``status`` field from a tool response."""
    if isinstance(response, dict):
        raw = response.get("status")
        if isinstance(raw, str):
            return raw
    return "success"


def _shrink(response: Any, *, max_chars: int = 500) -> Any:
    """Return a compact representation of ``response`` suitable for logging."""
    if isinstance(response, dict):
        summary: Dict[str, Any] = {}
        for key in ("status", "error", "detail", "tool", "hint"):
            if key in response:
                summary[key] = response[key]
        if summary:
            return summary
    text = repr(response)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [+{len(text) - max_chars} chars]"


__all__ = [
    "DESTRUCTIVE_TOOLS",
    "STATE_LAST_ERROR",
    "STATE_TIMERS",
    "STATE_TOOL_CALLS",
    "STATE_TURN_STARTED_AT",
    "after_agent_callback",
    "after_tool_callback",
    "before_agent_callback",
    "before_tool_callback",
    "tool_calls_from_state",
]
