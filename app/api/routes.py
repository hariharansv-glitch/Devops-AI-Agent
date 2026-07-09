"""HTTP routes for the AI DevOps Assistant.

The API is intentionally minimal:

* ``GET  /``                - single-page chat UI (``app/web/index.html``).
* ``GET  /api/info``        - service index (self-describing JSON).
* ``POST /chat``            - synchronous chat.
* ``POST /chat/stream``     - streaming chat (SSE-friendly, plain text/event-stream).
* ``GET  /healthz``         - liveness probe.
* ``GET  /readyz``          - readiness probe (SSH reachable + agent ready).
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.agent import AgentService, get_agent_service
from app.config import Settings, get_settings
from app.schemas import ChatRequest, ChatResponse
from app.services import get_services, reset_services
from app.utils import configure_logging, get_logger

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _friendly_llm_error(exc: BaseException) -> tuple[str, int]:
    """Translate a raw LLM/provider exception into a clean message + HTTP code.

    Rate-limit and overload errors are common on free tiers; surfacing the
    provider's giant JSON blob to the UI is unhelpful. We detect the usual
    cases and return a short, actionable sentence instead.
    """
    msg = str(exc).lower()

    # Extract a "try again in Xs / Xm" hint if the provider gave one.
    hint = ""
    match = re.search(
        r"try again in\s*((?:\d+m)?\d+(?:\.\d+)?s)", str(exc), re.IGNORECASE
    )
    if match:
        hint = f" Try again in ~{match.group(1)}."

    is_rate_limit = (
        "rate limit" in msg
        or "rate_limit_exceeded" in msg
        or "resource_exhausted" in msg
        or "429" in msg
    )
    if is_rate_limit:
        scope = ""
        if "per day" in msg or "tpd" in msg or "requestsperday" in msg:
            scope = " (daily quota)"
        elif "per minute" in msg or "tpm" in msg or "perminute" in msg:
            scope = " (per-minute quota)"
        return (
            f"The LLM provider's free-tier rate limit was reached{scope}.{hint} "
            "Switch MODEL_NAME to a model with more headroom, or add billing to "
            "your provider account.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if "unavailable" in msg or "503" in msg or "high demand" in msg:
        return (
            "The LLM provider is temporarily overloaded (503). "
            "Please retry in a moment.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if "tool_use_failed" in msg or "failed to call a function" in msg:
        return (
            "The model produced a malformed tool call. Please rephrase your "
            "request or try again.",
            status.HTTP_502_BAD_GATEWAY,
        )

    if "api key" in msg or "unauthorized" in msg or "401" in msg or "permission" in msg:
        return (
            "The LLM provider rejected the API key. Check the key for the "
            "configured MODEL_NAME provider in your .env.",
            status.HTTP_401_UNAUTHORIZED,
        )

    # Fallback: keep it short, don't dump the whole stack/JSON.
    first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return (f"Agent error: {first_line[:300]}", status.HTTP_500_INTERNAL_SERVER_ERROR)

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

    # Mount /static for any additional web assets (icons, images, ...).
    # The main index.html is served explicitly by the GET / handler so we can
    # keep the API routes strictly above the static mount in match priority.
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    """Attach every HTTP handler to ``app``."""

    @app.get("/", tags=["ui"], include_in_schema=False)
    async def root() -> FileResponse:
        """Serve the chat UI."""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/info", tags=["meta"])
    async def api_info(settings: Settings = Depends(get_settings)) -> dict:
        """Return a self-describing JSON blob for the UI + external clients."""
        return {
            "name": "ai-devops-assistant",
            "version": __version__,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "endpoints": ["/chat", "/chat/stream", "/healthz", "/readyz", "/api/info"],
            "model": settings.model_name,
            "vm_host": settings.vm_host,
            "vm_port": settings.vm_port,
            "ssh_user": settings.vm_user,
            "read_only_mode": settings.read_only_mode,
            "app_env": settings.app_env,
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
            detail, http_code = _friendly_llm_error(exc)
            raise HTTPException(status_code=http_code, detail=detail) from exc

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
