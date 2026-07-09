"""ADK memory service factory.

The DevOps assistant uses :class:`InMemoryMemoryService` for long-term
recall across sessions. It is process-local (data is lost on restart) which
is the right trade-off for a CLI/API tool.

To upgrade to persistent memory swap this out for
:class:`google.adk.memory.VertexAiMemoryBankService` or
:class:`google.adk.memory.VertexAiRagMemoryService` and set
``GOOGLE_GENAI_USE_VERTEXAI=TRUE``.
"""

from __future__ import annotations

from google.adk.memory import BaseMemoryService, InMemoryMemoryService

from app.utils import get_logger

logger = get_logger(__name__)


def build_memory_service() -> BaseMemoryService:
    """Return a fresh :class:`InMemoryMemoryService`."""
    logger.debug("memory service: InMemoryMemoryService created")
    return InMemoryMemoryService()


async def ingest_session(
    memory_service: BaseMemoryService,
    session,
) -> None:
    """Ingest a completed :class:`Session` into long-term memory."""
    if session is None:
        return
    try:
        await memory_service.add_session_to_memory(session)
        logger.info(
            "memory: ingested session id={session_id}",
            session_id=getattr(session, "id", None),
        )
    except Exception:  # noqa: BLE001 - memory ingest must never break the API
        logger.opt(exception=True).warning("memory: add_session_to_memory failed")


__all__ = ["build_memory_service", "ingest_session"]
