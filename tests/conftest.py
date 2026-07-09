"""Pytest fixtures and hermetic fakes.

The test suite must not open real SSH connections, so this module provides a
``FakeSSHService`` that responds to registered commands with canned output. A
``fake_services`` fixture wires that fake into the process-wide
:class:`~app.services.ServiceContainer` and reverts the swap in teardown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import pytest

from app.config import Settings, get_settings
from app.schemas import CommandResult, ToolStatus
from app.services import ServiceContainer, reset_services, set_services
from app.services.docker_service import DockerService
from app.services.jenkins_service import JenkinsService
from app.services.linux_service import LinuxService
from app.services.ssh_service import SSHCommandBlocked, SSHService, _HARD_DENY_PATTERNS


# ---------------------------------------------------------------------------
# Fake SSH service
# ---------------------------------------------------------------------------


@dataclass
class FakeSSHResponse:
    """Response returned by the :class:`FakeSSHService` for a given command."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class FakeSSHService:
    """A :class:`SSHService`-compatible fake used by the test suite.

    Commands are matched by *exact string* or *substring* (whichever is
    registered). Unknown commands raise ``LookupError``. Every call increments
    a counter that tests can assert against.
    """

    _exact: Dict[str, FakeSSHResponse] = field(default_factory=dict)
    _substr: Dict[str, FakeSSHResponse] = field(default_factory=dict)
    _predicate: Dict[Callable[[str], bool], FakeSSHResponse] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    connected: bool = False

    # ---- Public API mirroring the real SSHService --------------------
    @property
    def is_connected(self) -> bool:
        return self.connected

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def execute(self, command: str, *, timeout: Optional[float] = None) -> CommandResult:
        self.calls.append(command)
        self.connected = True

        # Mirror the real SSHService safety layer so tools that rely on the
        # deny list behave identically against the fake.
        for pattern in _HARD_DENY_PATTERNS:
            if pattern.search(command):
                raise SSHCommandBlocked(
                    f"Command rejected by hard-coded safety denylist "
                    f"(pattern={pattern.pattern!r})."
                )

        response = self._lookup(command)
        return CommandResult(
            status=ToolStatus.SUCCESS if response.exit_code == 0 else ToolStatus.ERROR,
            command=command,
            stdout=response.stdout,
            stderr=response.stderr,
            exit_code=response.exit_code,
            duration_ms=1.0,
        )

    async def execute_async(self, command: str, *, timeout: Optional[float] = None) -> CommandResult:
        return self.execute(command, timeout=timeout)

    # ---- Test helpers ------------------------------------------------
    def register_exact(self, command: str, response: FakeSSHResponse) -> None:
        self._exact[command] = response

    def register_substr(self, needle: str, response: FakeSSHResponse) -> None:
        self._substr[needle] = response

    def register_predicate(
        self, predicate: Callable[[str], bool], response: FakeSSHResponse
    ) -> None:
        self._predicate[predicate] = response

    def _lookup(self, command: str) -> FakeSSHResponse:
        if command in self._exact:
            return self._exact[command]
        for needle, response in self._substr.items():
            if needle in command:
                return response
        for predicate, response in self._predicate.items():
            if predicate(command):
                return response
        raise LookupError(f"FakeSSHService received unregistered command: {command!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return a fresh :class:`Settings` with safe test defaults."""
    monkeypatch.setenv("READ_ONLY_MODE", "TRUE")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("VM_HOST", "test.local")
    monkeypatch.setenv("VM_USER", "tester")
    monkeypatch.setenv("VM_PORT", "22")
    monkeypatch.setenv("SSH_AUTO_ADD_HOST_KEYS", "TRUE")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.5-flash")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def fake_ssh() -> FakeSSHService:
    """A pristine :class:`FakeSSHService` per test."""
    return FakeSSHService()


@pytest.fixture
def fake_services(settings: Settings, fake_ssh: FakeSSHService) -> ServiceContainer:
    """Wire a :class:`ServiceContainer` around the :class:`FakeSSHService`.

    The container is registered as the process-wide singleton so tools and
    the API pick it up automatically.
    """
    container = ServiceContainer.__new__(ServiceContainer)
    # Bypass ``__init__`` (which would create a real SSHService) and wire the
    # fake into each service manually.
    container.settings = settings
    container.ssh = fake_ssh  # type: ignore[assignment]
    container.linux = LinuxService(fake_ssh)  # type: ignore[arg-type]
    container.docker = DockerService(fake_ssh)  # type: ignore[arg-type]
    container.jenkins = JenkinsService(fake_ssh)  # type: ignore[arg-type]

    set_services(container)
    try:
        yield container
    finally:
        reset_services()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[], float]:
    """Return a monotonically-increasing fake ``time.perf_counter``."""
    counter = {"t": 0.0}

    def _tick() -> float:
        counter["t"] += 0.001
        return counter["t"]

    monkeypatch.setattr(time, "perf_counter", _tick)
    return _tick


# Ensure the real SSHService type is imported (avoids "unused import" lints).
assert SSHService is not None  # pragma: no cover
