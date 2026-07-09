"""Runtime configuration for the AI DevOps Assistant.

Configuration is centralized in :class:`app.config.settings.Settings` and is
loaded from environment variables (with a ``.env`` fallback). The
:func:`get_settings` helper caches a single ``Settings`` instance for the
lifetime of the process, so import-time side effects stay minimal.
"""

from __future__ import annotations

from app.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
