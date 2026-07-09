"""Tests for :mod:`app.api.routes` (no LLM calls, agent is faked)."""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent import runner as runner_module
from app.agent.runner import AgentReply


class _FakeAgentService:
    """Stand-in for :class:`AgentService` that never touches Gemini."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.calls: list[str] = []

    async def chat(self, message, *, session_id=None, user_id=None):
        self.calls.append(message)
        return AgentReply(
            answer=f"echo: {message}",
            session_id=session_id or "s-fake",
            user_id=user_id or "u-fake",
            tool_calls=["linux_cpu_usage"],
            duration_ms=12.3,
        )

    async def stream(self, message, *, session_id=None, user_id=None):
        for chunk in ("echo: ", message):
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.fixture
def api_client(settings, monkeypatch) -> Iterator[TestClient]:
    fake = _FakeAgentService(settings)
    # Register the fake as the process-wide agent singleton so the FastAPI
    # lifespan shutdown finds and closes it instead of building a real one.
    monkeypatch.setattr(runner_module, "_INSTANCE", fake)

    from app.agent import get_agent_service as _get_agent_service
    from app.api.routes import create_app

    app = create_app(settings)
    app.dependency_overrides[_get_agent_service] = lambda: fake

    with TestClient(app) as client:
        yield client


class TestRoutes:
    def test_root(self, api_client: TestClient) -> None:
        response = api_client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "ai-devops-assistant"
        assert "/chat" in body["endpoints"]

    def test_healthz(self, api_client: TestClient) -> None:
        response = api_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_chat_success(self, api_client: TestClient) -> None:
        response = api_client.post(
            "/chat",
            json={"message": "Hello", "session_id": "s1", "user_id": "u1"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["answer"].startswith("echo: Hello")
        assert body["session_id"] == "s1"
        assert body["user_id"] == "u1"
        assert body["tool_calls"] == ["linux_cpu_usage"]
        assert body["duration_ms"] == 12.3

    def test_chat_rejects_empty(self, api_client: TestClient) -> None:
        response = api_client.post("/chat", json={"message": ""})
        assert response.status_code == 422

    def test_chat_stream(self, api_client: TestClient) -> None:
        response = api_client.post(
            "/chat/stream",
            json={"message": "hi", "session_id": "s1", "user_id": "u1"},
        )
        assert response.status_code == 200
        assert response.text == "echo: hi"
