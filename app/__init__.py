"""AI DevOps Assistant.

A production-grade AI assistant that inspects a remote Linux VM over SSH and
answers DevOps questions using live server data. The assistant is built on
Google's Agent Development Kit (ADK) and Gemini 2.5.

Every top-level subpackage has a single, well-defined responsibility:

* :mod:`app.config`   - runtime configuration (Pydantic Settings).
* :mod:`app.schemas`  - Pydantic v2 models used across the app.
* :mod:`app.utils`    - cross-cutting helpers (logging, formatters).
* :mod:`app.services` - low-level infrastructure clients (SSH, Docker, ...).
* :mod:`app.tools`    - ADK ``FunctionTool`` implementations.
* :mod:`app.agent`    - ADK ``LlmAgent`` wiring: instructions, callbacks,
                        session/memory services, and the runner.
* :mod:`app.api`      - FastAPI application exposing ``/chat``.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "1.0.0"
