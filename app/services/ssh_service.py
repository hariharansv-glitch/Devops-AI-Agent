"""SSH connectivity to the target Linux VM.

Wraps :mod:`paramiko` behind a small, blocking, thread-safe facade. Every
long-running or blocking operation is exposed both synchronously
(:meth:`SSHService.execute`) and asynchronously (:meth:`SSHService.execute_async`
which delegates to :func:`asyncio.to_thread`).

The service enforces a *deny list* of dangerous shell fragments (``rm -rf /``,
fork bombs, ``:(){ :|:& };:``, ``mkfs``, ``dd if=`` targeting block devices,
etc.). This is a defense-in-depth against the LLM ever emitting such a
command; the primary safeguard is the ADK ``before_tool_callback`` in
:mod:`app.agent.callbacks`.
"""

from __future__ import annotations

import asyncio
import io
import re
import socket
import threading
import time
from pathlib import Path
from typing import List, Optional

import paramiko

from app.config import Settings, get_settings
from app.schemas import CommandResult, ToolStatus
from app.utils import get_logger, truncate_text

logger = get_logger(__name__)


# Command patterns we will refuse to execute over the SSH channel regardless of
# who asked for them. These are compiled at import time.
_HARD_DENY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\brm\s+-rf\s+/(?:\s|$|\*)",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        r"\bmkfs(\.[a-z0-9]+)?\b",
        r"\bdd\b[^|]*\bof=/dev/(sd[a-z]|nvme|hd[a-z]|xvd)",
        r"\bshutdown\b\s+-h",
        r"\bpoweroff\b",
        r"\breboot\b",
        r"\bhalt\b",
        r">\s*/dev/sd[a-z]",
    )
)

# Output caps to keep the LLM context small.
_MAX_STDOUT_BYTES = 32 * 1024
_MAX_STDERR_BYTES = 16 * 1024


class SSHConnectionError(RuntimeError):
    """Raised when the SSH channel cannot be established."""


class SSHCommandBlocked(PermissionError):
    """Raised when the deny list rejects a command."""


class SSHService:
    """Blocking, thread-safe SSH client for a single target host.

    The service holds a single :class:`paramiko.SSHClient` connection which
    is created lazily on first use and reused across calls. Reconnection is
    automatic when the transport becomes unusable.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings: Settings = settings or get_settings()
        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.RLock()
        self._extra_denylist: List[re.Pattern[str]] = [
            re.compile(p) for p in self._settings.ssh_extra_denylist_list
        ]

    # ---------------------------------------------------------------- Lifecycle
    @property
    def is_connected(self) -> bool:
        """Return ``True`` when the underlying transport is active."""
        with self._lock:
            if self._client is None:
                return False
            transport = self._client.get_transport()
            return bool(transport and transport.is_active())

    def connect(self) -> None:
        """Establish the SSH connection. Idempotent when already connected."""
        with self._lock:
            if self.is_connected:
                return

            settings = self._settings
            logger.info(
                "SSH connect host={host} port={port} user={user}",
                host=settings.vm_host,
                port=settings.vm_port,
                user=settings.vm_user,
            )

            client = paramiko.SSHClient()
            client.load_system_host_keys()
            if settings.ssh_auto_add_host_keys:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())

            try:
                client.connect(**self._build_connect_kwargs())
            except paramiko.AuthenticationException as exc:
                client.close()
                raise SSHConnectionError(
                    f"SSH authentication failed for user "
                    f"{settings.vm_user}@{settings.vm_host}: {exc}"
                ) from exc
            except (paramiko.SSHException, socket.error, socket.timeout) as exc:
                client.close()
                raise SSHConnectionError(
                    f"Could not open SSH connection to "
                    f"{settings.vm_host}:{settings.vm_port}: {exc}"
                ) from exc

            self._client = client

    def disconnect(self) -> None:
        """Close the SSH connection if it is currently open."""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                    logger.info("SSH disconnected host={host}", host=self._settings.vm_host)
                except Exception:  # pragma: no cover - best-effort cleanup
                    logger.exception("Error while closing SSH client")
                finally:
                    self._client = None

    def __enter__(self) -> "SSHService":  # pragma: no cover - trivial
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover
        self.disconnect()

    # ---------------------------------------------------------------- Public API
    def execute(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Execute ``command`` on the remote host and return a :class:`CommandResult`.

        Args:
            command: The exact shell command to execute. Interpreted by the
                remote user's login shell (usually ``bash``).
            timeout: Optional override for :attr:`Settings.ssh_command_timeout`.

        Raises:
            SSHCommandBlocked: When ``command`` matches the deny list.
            SSHConnectionError: When the SSH channel is unavailable.
        """
        if not command or not command.strip():
            raise ValueError("SSHService.execute requires a non-empty command")

        self._enforce_denylist(command)

        effective_timeout = timeout or self._settings.ssh_command_timeout
        started = time.perf_counter()

        with self._lock:
            if not self.is_connected:
                self.connect()
            assert self._client is not None  # for type checkers

            logger.debug("SSH exec: {cmd!r}", cmd=command)
            try:
                _stdin, stdout_stream, stderr_stream = self._client.exec_command(
                    command,
                    timeout=effective_timeout,
                    get_pty=False,
                )
                # Enforce read cap; anything larger is truncated in-place.
                stdout_bytes = stdout_stream.read(_MAX_STDOUT_BYTES + 1)
                stderr_bytes = stderr_stream.read(_MAX_STDERR_BYTES + 1)
                exit_code = stdout_stream.channel.recv_exit_status()
            except (paramiko.SSHException, socket.timeout, EOFError) as exc:
                # Kill the transport so the next call reconnects cleanly.
                self.disconnect()
                duration_ms = (time.perf_counter() - started) * 1000
                logger.warning(
                    "SSH exec failed cmd={cmd!r} err={err} duration_ms={ms:.1f}",
                    cmd=command,
                    err=exc,
                    ms=duration_ms,
                )
                raise SSHConnectionError(
                    f"SSH command failed: {exc}"
                ) from exc

        stdout, stdout_truncated = _decode_and_cap(stdout_bytes, _MAX_STDOUT_BYTES)
        stderr, stderr_truncated = _decode_and_cap(stderr_bytes, _MAX_STDERR_BYTES)

        duration_ms = (time.perf_counter() - started) * 1000

        result = CommandResult(
            status=ToolStatus.SUCCESS if exit_code == 0 else ToolStatus.ERROR,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            truncated=stdout_truncated or stderr_truncated,
        )
        logger.info(
            "SSH exec done exit={exit} duration_ms={ms:.1f} cmd={cmd!r}",
            exit=exit_code,
            ms=duration_ms,
            cmd=truncate_text(command, max_chars=200, tail=False),
        )
        return result

    async def execute_async(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Async wrapper around :meth:`execute` (runs in a worker thread)."""
        return await asyncio.to_thread(self.execute, command, timeout=timeout)

    # ---------------------------------------------------------------- Internals
    def _build_connect_kwargs(self) -> dict:
        """Assemble the ``paramiko.SSHClient.connect`` keyword arguments."""
        settings = self._settings
        kwargs: dict = {
            "hostname": settings.vm_host,
            "port": settings.vm_port,
            "username": settings.vm_user,
            "timeout": settings.ssh_connect_timeout,
            "auth_timeout": settings.ssh_connect_timeout,
            "banner_timeout": settings.ssh_connect_timeout,
            "allow_agent": True,
            "look_for_keys": False,
        }

        key = self._load_private_key()
        if key is not None:
            kwargs["pkey"] = key
        elif settings.vm_password:
            kwargs["password"] = settings.vm_password
            kwargs["allow_agent"] = False

        return kwargs

    def _load_private_key(self) -> Optional[paramiko.PKey]:
        """Load the configured private key, if any, trying all key types."""
        settings = self._settings
        raw = settings.vm_private_key
        if not raw:
            return None

        passphrase = settings.vm_private_key_passphrase or None
        candidate_stream: io.StringIO | None = None
        key_path: Path | None = None

        raw_stripped = raw.strip()
        if raw_stripped.startswith("-----BEGIN"):
            candidate_stream = io.StringIO(raw)
        else:
            key_path = Path(raw).expanduser()
            if not key_path.is_file():
                raise SSHConnectionError(
                    f"VM_PRIVATE_KEY points to non-existent path: {key_path}"
                )

        # Try all standard key classes in order of prevalence. We resolve them
        # via ``getattr`` so we degrade gracefully when a future paramiko
        # release removes a key type (paramiko 5.0 already dropped ``DSSKey``,
        # since DSA is insecure).
        loaders: List[type[paramiko.PKey]] = []
        for candidate_name in ("Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey"):
            candidate = getattr(paramiko, candidate_name, None)
            if candidate is not None:
                loaders.append(candidate)
        last_exc: Exception | None = None
        for loader in loaders:
            try:
                if candidate_stream is not None:
                    candidate_stream.seek(0)
                    return loader.from_private_key(candidate_stream, password=passphrase)
                assert key_path is not None
                return loader.from_private_key_file(str(key_path), password=passphrase)
            except paramiko.PasswordRequiredException as exc:
                raise SSHConnectionError(
                    "Private key is encrypted; set VM_PRIVATE_KEY_PASSPHRASE."
                ) from exc
            except paramiko.SSHException as exc:
                last_exc = exc
                continue

        raise SSHConnectionError(
            f"Could not decode private key with any supported algorithm "
            f"(last error: {last_exc})"
        )

    def _enforce_denylist(self, command: str) -> None:
        """Raise :class:`SSHCommandBlocked` if ``command`` is unsafe."""
        for pattern in _HARD_DENY_PATTERNS:
            if pattern.search(command):
                logger.error(
                    "SSH denylist blocked command: pattern={p} cmd={cmd!r}",
                    p=pattern.pattern,
                    cmd=command,
                )
                raise SSHCommandBlocked(
                    f"Command rejected by hard-coded safety denylist "
                    f"(pattern={pattern.pattern!r})."
                )
        for pattern in self._extra_denylist:
            if pattern.search(command):
                logger.error(
                    "SSH extra denylist blocked command: pattern={p} cmd={cmd!r}",
                    p=pattern.pattern,
                    cmd=command,
                )
                raise SSHCommandBlocked(
                    f"Command rejected by configured extra denylist "
                    f"(pattern={pattern.pattern!r})."
                )


def _decode_and_cap(raw: bytes, cap: int) -> tuple[str, bool]:
    """Decode a captured bytestring, marking truncation when it overflowed."""
    truncated = len(raw) > cap
    if truncated:
        raw = raw[:cap]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text = truncate_text(text, max_chars=cap, tail=False)
    return text, truncated


__all__ = [
    "SSHCommandBlocked",
    "SSHConnectionError",
    "SSHService",
]
