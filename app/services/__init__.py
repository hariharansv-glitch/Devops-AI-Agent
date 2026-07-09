"""Low-level infrastructure services.

Services encapsulate all I/O with the outside world (SSH, Docker CLI on the
remote host, Jenkins CLI, ...). They are intentionally thin: their only job
is to run a command and parse the result into a Pydantic model. Business
policy (safety, prompts, retries) lives one layer up in
:mod:`app.tools`.

Services are wired together by :class:`ServiceContainer`, a minimal
dependency-injection container that lets tests swap real services for fakes
without monkey-patching module globals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

from app.config import Settings, get_settings
from app.services.docker_service import DockerService
from app.services.jenkins_service import JenkinsService
from app.services.linux_service import LinuxService
from app.services.ssh_service import SSHService


@dataclass
class ServiceContainer:
    """Aggregate of the four singleton services.

    The container is intentionally trivial: it just wires each service to a
    single shared :class:`SSHService`. Tests can construct a container with
    fakes to exercise tools in isolation.
    """

    settings: Settings = field(default_factory=get_settings)
    ssh: SSHService = field(init=False)
    linux: LinuxService = field(init=False)
    docker: DockerService = field(init=False)
    jenkins: JenkinsService = field(init=False)

    def __post_init__(self) -> None:
        self.ssh = SSHService(self.settings)
        self.linux = LinuxService(self.ssh)
        self.docker = DockerService(self.ssh)
        self.jenkins = JenkinsService(self.ssh)

    def close(self) -> None:
        """Release every underlying resource. Idempotent."""
        self.ssh.disconnect()


_LOCK = Lock()
_CONTAINER: Optional[ServiceContainer] = None


def get_services() -> ServiceContainer:
    """Return the process-wide :class:`ServiceContainer` (lazy)."""
    global _CONTAINER
    with _LOCK:
        if _CONTAINER is None:
            _CONTAINER = ServiceContainer()
        return _CONTAINER


def set_services(container: Optional[ServiceContainer]) -> None:
    """Replace the current container (primarily for tests)."""
    global _CONTAINER
    with _LOCK:
        if _CONTAINER is not None and _CONTAINER is not container:
            try:
                _CONTAINER.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        _CONTAINER = container


def reset_services() -> None:
    """Close and clear the current container. Safe to call multiple times."""
    set_services(None)


__all__ = [
    "DockerService",
    "JenkinsService",
    "LinuxService",
    "SSHService",
    "ServiceContainer",
    "get_services",
    "reset_services",
    "set_services",
]
