"""FastAPI application factory.

The FastAPI app is constructed lazily by :func:`create_app` so that unit
tests can build a fresh instance with a mocked :class:`AgentService`.
"""

from __future__ import annotations

from app.api.routes import create_app

__all__ = ["create_app"]
