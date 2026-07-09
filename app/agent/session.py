"""ADK session service factory.

The DevOps assistant uses :class:`InMemorySessionService` because sessions
are ephemeral (one interactive chat = one session). Swap this out for
:class:`google.adk.sessions.DatabaseSessionService` or
:class:`VertexAiSessionService` in production if you need persistence.
"""

from __future__ import annotations

from typing import Optional

from google.adk.sessions import BaseSessionService, InMemorySessionService, Session

from app.utils import get_logger

logger = get_logger(__name__)


def build_session_service() -> BaseSessionService:
    """Return a fresh :class:`InMemorySessionService`."""
    logger.debug("session service: InMemorySessionService created")
    return InMemorySessionService()


async def ensure_session(
    service: BaseSessionService,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
) -> Session:
    """Return an existing session or create a new one.

    ADK's ``get_session`` returns ``None`` when the session id is unknown;
    this helper hides that branch from calling code.
    """
    session = await service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    if session is not None:
        return session
    logger.info(
        "creating new session app={app} user={user} session={session}",
        app=app_name,
        user=user_id,
        session=session_id,
    )
    return await service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )


async def read_state(
    service: BaseSessionService,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
) -> Optional[dict]:
    """Return the current session state, or ``None`` if the session is gone."""
    session = await service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        return None
    return dict(session.state or {})


__all__ = ["build_session_service", "ensure_session", "read_state"]
