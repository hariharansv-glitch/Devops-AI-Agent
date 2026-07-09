"""Tests for :mod:`app.schemas`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    ChatRequest,
    ChatResponse,
    CommandResult,
    ConfirmationRequired,
    ToolError,
    ToolStatus,
)


class TestChat:
    def test_empty_message_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(message="")

    def test_long_message_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(message="x" * 9001)

    def test_default_response(self) -> None:
        response = ChatResponse(
            answer="ok",
            session_id="s1",
            user_id="u1",
            duration_ms=12.5,
        )
        assert response.tool_calls == []
        assert response.duration_ms == 12.5


class TestCommandResult:
    def test_ok_property(self) -> None:
        result = CommandResult(
            command="uptime",
            stdout="up 1 day",
            exit_code=0,
            duration_ms=1.0,
        )
        assert result.ok is True

    def test_error_status_when_nonzero_exit(self) -> None:
        result = CommandResult(
            status=ToolStatus.ERROR,
            command="false",
            exit_code=1,
            duration_ms=1.0,
        )
        assert result.ok is False
        assert result.status is ToolStatus.ERROR


class TestToolPayloads:
    def test_tool_error_defaults(self) -> None:
        error = ToolError(error="boom")
        assert error.status is ToolStatus.ERROR
        assert error.tool is None

    def test_confirmation_required(self) -> None:
        payload = ConfirmationRequired(action="a", prompt="p")
        assert payload.status is ToolStatus.CONFIRMATION_REQUIRED
        assert payload.reversible is False

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ToolError(error="boom", bogus_field=1)  # type: ignore[call-arg]
