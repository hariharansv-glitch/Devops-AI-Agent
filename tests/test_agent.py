"""Tests for the ADK agent wiring (no LLM calls, no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.callbacks import (
    STATE_TIMERS,
    STATE_TOOL_CALLS,
    after_tool_callback,
    before_tool_callback,
    tool_calls_from_state,
)
from app.agent.instructions import (
    AGENT_DESCRIPTION,
    AGENT_NAME,
    SYSTEM_INSTRUCTION,
)
from app.agent.root_agent import build_root_agent


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCtx:
    def __init__(self) -> None:
        self.state: dict = {}
        self.agent_name = "test-agent"
        self.function_call_id = "abc-123"
        self.actions = SimpleNamespace(skip_summarization=False)


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def test_system_instruction_covers_non_negotiables() -> None:
    for phrase in (
        "Ground every answer",
        "Never invent data",
        "Confirm before you change anything",
        "Respect read-only mode",
        "Prefer high-level tools over raw SSH",
    ):
        assert phrase in SYSTEM_INSTRUCTION, f"missing rule: {phrase!r}"


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


class TestCallbacks:
    def test_before_tool_records_start_time(self) -> None:
        ctx = _FakeCtx()
        result = before_tool_callback(_FakeTool("linux_cpu_usage"), {}, ctx)  # type: ignore[arg-type]
        assert result is None
        assert STATE_TIMERS in ctx.state
        assert ctx.state[STATE_TIMERS]  # non-empty

    def test_before_tool_blocks_destructive_in_readonly(self, settings) -> None:
        # READ_ONLY_MODE is TRUE by default in the test settings, and confirm=True
        # is the "executing" path — the callback must short-circuit with 'blocked'.
        assert settings.read_only_mode is True
        ctx = _FakeCtx()
        payload = before_tool_callback(
            _FakeTool("docker_restart_container"), {"confirm": True}, ctx  # type: ignore[arg-type]
        )
        assert payload is not None
        assert payload["status"] == "blocked"

    def test_before_tool_allows_confirmation_request(self, settings) -> None:
        # confirm=False path must be allowed even in read-only mode so the
        # tool can respond with its "confirmation required" message.
        assert settings.read_only_mode is True
        ctx = _FakeCtx()
        payload = before_tool_callback(
            _FakeTool("docker_restart_container"), {"confirm": False}, ctx  # type: ignore[arg-type]
        )
        assert payload is None

    def test_after_tool_updates_state(self) -> None:
        ctx = _FakeCtx()
        before_tool_callback(_FakeTool("linux_cpu_usage"), {}, ctx)  # type: ignore[arg-type]
        after_tool_callback(
            _FakeTool("linux_cpu_usage"), {}, ctx, {"status": "success"}  # type: ignore[arg-type]
        )

        assert STATE_TOOL_CALLS in ctx.state
        assert len(ctx.state[STATE_TOOL_CALLS]) == 1
        assert ctx.state[STATE_TOOL_CALLS][0]["tool"] == "linux_cpu_usage"
        assert ctx.state[STATE_TOOL_CALLS][0]["status"] == "success"

    def test_tool_calls_from_state(self) -> None:
        state = {
            STATE_TOOL_CALLS: [
                {"tool": "a", "status": "success", "duration_ms": 1.0},
                {"tool": "b", "status": "success", "duration_ms": 2.0},
            ]
        }
        assert tool_calls_from_state(state) == ["a", "b"]


# ---------------------------------------------------------------------------
# Root agent factory
# ---------------------------------------------------------------------------


class TestRootAgent:
    def test_factory_builds_agent(self, settings) -> None:
        agent = build_root_agent(settings)
        assert agent.name == AGENT_NAME
        assert agent.description == AGENT_DESCRIPTION
        # The devops toolbelt has many tools + the memory recall tool.
        assert len(agent.tools) >= 20
        # Callbacks are wired.
        assert agent.before_tool_callback is not None
        assert agent.after_tool_callback is not None

    def test_factory_respects_custom_tool_list(self, settings) -> None:
        from google.adk.tools import FunctionTool

        async def noop(tool_context) -> dict:
            """A no-op tool used only for testing."""
            return {"status": "success"}

        tool = FunctionTool(func=noop)
        agent = build_root_agent(settings, tools=[tool])
        assert agent.tools == [tool]
