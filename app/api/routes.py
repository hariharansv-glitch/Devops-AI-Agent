"""HTTP routes for the AI DevOps Assistant.

The API is intentionally minimal:

* ``POST /chat``            - synchronous chat.
* ``POST /chat/stream``     - streaming chat (SSE-friendly, plain text/event-stream).
* ``GET  /healthz``         - liveness probe.
* ``GET  /readyz``          - readiness probe (SSH reachable + agent ready).
* ``GET  /``                - service index (self-describing JSON).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app import __version__
from app.agent import AgentService, get_agent_service
from app.config import Settings, get_settings
from app.schemas import ChatRequest, ChatResponse
from app.services import get_services, reset_services
from app.utils import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup and release resources on shutdown."""
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        json_logs=settings.log_json,
    )
    logger.info("FastAPI startup env={env}", env=settings.app_env)
    # Warm the agent + services lazily on first request; nothing to eagerly
    # spin up here beyond logging.
    try:
        yield
    finally:
        logger.info("FastAPI shutdown: releasing services")
        try:
            # Only close the agent if it was actually instantiated during the
            # process lifetime; never trigger creation just to close it.
            from app.agent import runner as _runner_module

            instance = getattr(_runner_module, "_INSTANCE", None)
            if instance is not None and hasattr(instance, "aclose"):
                await instance.aclose()
        except Exception:  # noqa: BLE001 - shutdown must never crash the API
            logger.opt(exception=True).warning("agent shutdown raised")
        try:
            reset_services()
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("service reset raised")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Return a fresh FastAPI application."""
    settings = settings or get_settings()
    app = FastAPI(
        title="AI DevOps Assistant",
        version=__version__,
        summary="Google ADK-powered DevOps assistant for a remote Linux VM.",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    """Attach every HTTP handler to ``app``."""

    @app.get("/", tags=["meta"])
    async def root() -> dict:
        return {
            "name": "ai-devops-assistant",
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "endpoints": ["/chat", "/chat/stream", "/healthz", "/readyz"],
        }

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz(agent: AgentService = Depends(get_agent_service)) -> dict:
        services = get_services()
        ssh_ok = False
        try:
            await asyncio.to_thread(services.ssh.connect)
            ssh_ok = services.ssh.is_connected
        except Exception as exc:  # noqa: BLE001 - readiness must not raise
            logger.warning("readyz: ssh not ready: {exc}", exc=exc)
            ssh_ok = False

        docker_ok = False
        try:
            docker_ok = await asyncio.to_thread(services.docker.is_installed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("readyz: docker probe raised: {exc}", exc=exc)

        payload = {
            "status": "ready" if ssh_ok else "degraded",
            "model": agent.settings.model_name,
            "ssh": {
                "host": agent.settings.vm_host,
                "connected": ssh_ok,
            },
            "docker_installed": docker_ok,
        }
        return payload

    @app.post(
        "/chat",
        response_model=ChatResponse,
        tags=["chat"],
        summary="Send a DevOps question to the agent (synchronous).",
    )
    async def chat(
        request: ChatRequest,
        agent: AgentService = Depends(get_agent_service),
    ) -> ChatResponse:
        try:
            reply = await agent.chat(
                request.message,
                session_id=request.session_id,
                user_id=request.user_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001 - never leak internals
            logger.opt(exception=True).error("chat handler failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"agent error: {exc}",
            ) from exc

        return ChatResponse(
            answer=reply.answer,
            session_id=reply.session_id,
            user_id=reply.user_id,
            tool_calls=reply.tool_calls,
            duration_ms=reply.duration_ms,
        )

    @app.post(
        "/chat/stream",
        tags=["chat"],
        summary="Send a DevOps question and receive a streamed response.",
    )
    async def chat_stream(
        request: ChatRequest,
        agent: AgentService = Depends(get_agent_service),
    ) -> StreamingResponse:
        async def _iterate() -> AsyncIterator[bytes]:
            try:
                async for chunk in agent.stream(
                    request.message,
                    session_id=request.session_id,
                    user_id=request.user_id,
                ):
                    yield chunk.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.opt(exception=True).error("chat_stream handler failed")
                yield f"\n[error] {exc}\n".encode("utf-8")

        return StreamingResponse(_iterate(), media_type="text/plain; charset=utf-8")


# Create a module-level app so ``uvicorn app.api.routes:app`` works out of
# the box for tools like ``adk web`` and container orchestrators.
app = create_app()


__all__ = ["app", "create_app"]
