"""High-level runner glue that connects the ADK agent to the outside world.

This module is deliberately I/O-friendly:

* :class:`AgentService` is a thin, thread-safe wrapper around
  :class:`google.adk.runners.Runner` that exposes an :meth:`chat` coroutine
  and a :meth:`stream` async-iterator.
* :func:`get_agent_service` returns a process-wide singleton so both the
  FastAPI app and the Typer CLI reuse the same runner, sessions, and
  memory.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import AsyncIterator, List, Optional

from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.adk.memory import BaseMemoryService
from google.genai import types

from app.agent.callbacks import tool_calls_from_state
from app.agent.instructions import AGENT_NAME
from app.agent.memory import build_memory_service, ingest_session
from app.agent.root_agent import build_root_agent
from app.agent.session import build_session_service, ensure_session, read_state
from app.config import Settings, get_settings
from app.utils import configure_logging, get_logger

logger = get_logger(__name__)


DEFAULT_USER_ID = "cli-user"


@dataclass(frozen=True)
class AgentReply:
    """A single, complete reply returned by :meth:`AgentService.chat`."""

    answer: str
    session_id: str
    user_id: str
    tool_calls: List[str]
    duration_ms: float


class AgentService:
    """Facade over an ADK :class:`Runner`."""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        session_service: Optional[BaseSessionService] = None,
        memory_service: Optional[BaseMemoryService] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_service = session_service or build_session_service()
        self._memory_service = memory_service or build_memory_service()

        self._configure_gemini_env()

        self._agent = build_root_agent(self._settings)
        self._runner = Runner(
            agent=self._agent,
            app_name=self._settings.app_name,
            session_service=self._session_service,
            memory_service=self._memory_service,
        )
        logger.info(
            "AgentService ready app={app} model={model}",
            app=self._settings.app_name,
            model=self._settings.model_name,
        )

    # -------------------------------------------------------------- Accessors
    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def session_service(self) -> BaseSessionService:
        return self._session_service

    @property
    def memory_service(self) -> BaseMemoryService:
        return self._memory_service

    @property
    def app_name(self) -> str:
        return self._settings.app_name

    # ------------------------------------------------------------------ Chat
    async def chat(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> AgentReply:
        """Send ``message`` to the agent and return the final response."""
        message = (message or "").strip()
        if not message:
            raise ValueError("message must be a non-empty string")

        session_id = session_id or _new_session_id()
        user_id = user_id or DEFAULT_USER_ID

        await ensure_session(
            self._session_service,
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )

        started = time.perf_counter()
        content = types.Content(role="user", parts=[types.Part(text=message)])

        final_text = ""
        async for event in self._runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.is_final_response():
                final_text = _extract_text(event)

        duration_ms = (time.perf_counter() - started) * 1000
        state = await read_state(
            self._session_service,
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        ) or {}
        tool_calls = tool_calls_from_state(state)

        session = await self._session_service.get_session(
            app_name=self.app_name, user_id=user_id, session_id=session_id
        )
        await ingest_session(self._memory_service, session)

        logger.info(
            "chat done session={sid} user={uid} tools={tools} duration_ms={ms:.1f}",
            sid=session_id,
            uid=user_id,
            tools=tool_calls,
            ms=duration_ms,
        )

        return AgentReply(
            answer=final_text or "(no response)",
            session_id=session_id,
            user_id=user_id,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
        )

    # ----------------------------------------------------------------- Stream
    async def stream(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Yield successive text chunks as the agent produces them.

        Uses ADK streaming events. Intermediate tool events are skipped —
        only text chunks from the model reach the caller.
        """
        message = (message or "").strip()
        if not message:
            raise ValueError("message must be a non-empty string")

        session_id = session_id or _new_session_id()
        user_id = user_id or DEFAULT_USER_ID

        await ensure_session(
            self._session_service,
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        content = types.Content(role="user", parts=[types.Part(text=message)])

        async for event in self._runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            chunk = _extract_text(event)
            if chunk:
                yield chunk

    # --------------------------------------------------------------- Cleanup
    async def aclose(self) -> None:
        """Release the runner and any transient resources. Idempotent."""
        try:
            close = getattr(self._runner, "close", None)
            if close is None:
                return
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - shutdown should never crash the API
            logger.opt(exception=True).warning("runner shutdown raised")

    # ---------------------------------------------------------------- Internals
    def _configure_gemini_env(self) -> None:
        """Push settings into the env vars the ADK / Gen AI SDK inspect."""
        settings = self._settings
        if settings.uses_vertex_ai:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
            if settings.google_cloud_project:
                os.environ["GOOGLE_CLOUD_PROJECT"] = settings.google_cloud_project
            if settings.google_cloud_location:
                os.environ["GOOGLE_CLOUD_LOCATION"] = settings.google_cloud_location
        else:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
            if settings.google_api_key:
                os.environ["GOOGLE_API_KEY"] = settings.google_api_key


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_LOCK = Lock()
_INSTANCE: Optional[AgentService] = None


def get_agent_service() -> AgentService:
    """Return the process-wide :class:`AgentService`, creating it lazily."""
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            settings = get_settings()
            configure_logging(
                level=settings.log_level,
                log_dir=settings.log_dir,
                json_logs=settings.log_json,
            )
            logger.info(
                "starting agent service app={app} agent={agent} model={model}",
                app=settings.app_name,
                agent=AGENT_NAME,
                model=settings.model_name,
            )
            _INSTANCE = AgentService(settings=settings)
        return _INSTANCE


def reset_agent_service() -> None:
    """Drop the cached :class:`AgentService` (for tests / hot-reload)."""
    global _INSTANCE
    with _LOCK:
        _INSTANCE = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:16]}"


def _extract_text(event) -> str:
    """Return the plain-text part of an ADK event, or an empty string."""
    content = getattr(event, "content", None)
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            return text
    return ""


__all__ = [
    "AgentReply",
    "AgentService",
    "DEFAULT_USER_ID",
    "get_agent_service",
    "reset_agent_service",
]
