"""Loguru-based logging configuration.

Logging is initialized exactly once, at process startup, by
:func:`configure_logging`. The rest of the codebase acquires a logger via
:func:`get_logger` which returns a Loguru :class:`~loguru.Logger` bound to the
calling module.

The configuration emits one line per event to stderr (colorised) and, in
parallel, appends a plain-text file rotated at 10 MB (7-day retention).
"""

from __future__ import annotations

import logging
import sys
from threading import Lock
from typing import Any

from loguru import logger as _loguru_logger

_CONFIGURED = False
_LOCK = Lock()


class _InterceptHandler(logging.Handler):
    """Bridge stdlib ``logging`` records into Loguru.

    Libraries like ``paramiko``, ``uvicorn``, ``fastapi``, and Google's ADK
    use the standard :mod:`logging` module. Installing this handler on the
    root logger routes every record through Loguru so we have a single sink
    with a single format.
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            level: str | int = _loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Preserve the original caller information.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        _loguru_logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging(
    level: str = "INFO",
    log_dir: str | None = "logs",
    json_logs: bool = False,
) -> None:
    """Configure the process-wide Loguru sink.

    Args:
        level: Minimum level to emit (``"TRACE"`` ... ``"CRITICAL"``).
        log_dir: Directory where the rotating log file is written. Set to
            ``None`` to disable file logging.
        json_logs: When ``True``, records are serialised as JSON (recommended
            for production log aggregation).
    """
    global _CONFIGURED
    with _LOCK:
        _loguru_logger.remove()

        if json_logs:
            _loguru_logger.add(
                sys.stderr,
                level=level,
                serialize=True,
                backtrace=False,
                diagnose=False,
                enqueue=True,
            )
        else:
            _loguru_logger.add(
                sys.stderr,
                level=level,
                colorize=True,
                backtrace=False,
                diagnose=False,
                enqueue=True,
                format=(
                    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
                    "| <level>{level: <8}</level> "
                    "| <cyan>{name}</cyan>:<cyan>{line}</cyan> "
                    "- <level>{message}</level>"
                ),
            )

        if log_dir:
            from pathlib import Path

            path = Path(log_dir).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            _loguru_logger.add(
                path / "devops_agent.log",
                level=level,
                rotation="10 MB",
                retention="7 days",
                compression="zip",
                encoding="utf-8",
                enqueue=True,
                backtrace=False,
                diagnose=False,
                serialize=json_logs,
                format=(
                    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                    "{name}:{line} - {message}"
                ),
            )

        logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
        for noisy in ("paramiko.transport", "urllib3", "docker.utils.config"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    """Return a Loguru logger bound to ``name``.

    Args:
        name: Logical logger name. Typically the calling module's
            ``__name__``. If ``None``, the root logger is returned.

    Returns:
        A configured Loguru :class:`~loguru.Logger`.
    """
    if not _CONFIGURED:
        configure_logging()
    if name:
        return _loguru_logger.bind(name=name)
    return _loguru_logger


__all__ = ["configure_logging", "get_logger"]
