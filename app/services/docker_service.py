"""Docker inspection over SSH.

The service talks to the Docker daemon on the remote host by shelling out to
the ``docker`` CLI. This is intentional: it avoids exposing the Docker socket
over TCP and works out of the box whenever the SSH user has permission to run
``docker`` (either directly or via ``sudo``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.schemas import DockerContainerInfo, DockerImageInfo, HealthStatus, ToolStatus
from app.services.ssh_service import SSHConnectionError, SSHService
from app.utils import get_logger

logger = get_logger(__name__)


class DockerNotAvailable(RuntimeError):
    """Raised when ``docker`` is not installed / accessible on the remote host."""


class DockerService:
    """High-level Docker inspector."""

    def __init__(self, ssh: SSHService, *, docker_bin: str = "docker") -> None:
        self._ssh = ssh
        self._docker_bin = docker_bin

    # ---------------------------------------------------------------- Health
    def is_installed(self) -> bool:
        """Return ``True`` when the ``docker`` binary exists on ``$PATH``."""
        try:
            res = self._ssh.execute(f"command -v {self._docker_bin} >/dev/null 2>&1")
        except SSHConnectionError:
            return False
        return res.exit_code == 0

    def is_daemon_running(self) -> bool:
        """Return ``True`` when the Docker daemon is responsive."""
        if not self.is_installed():
            return False
        try:
            res = self._ssh.execute(f"{self._docker_bin} info --format '{{{{.ServerVersion}}}}'")
        except SSHConnectionError:
            return False
        return res.exit_code == 0 and bool(res.stdout.strip())

    def version(self) -> Optional[str]:
        """Return the Docker daemon version, or ``None`` if unavailable."""
        try:
            res = self._ssh.execute(
                f"{self._docker_bin} version --format '{{{{.Server.Version}}}}'"
            )
        except SSHConnectionError:
            return None
        return res.stdout.strip() or None

    def health(self) -> HealthStatus:
        """Return a structured :class:`HealthStatus` snapshot."""
        installed = self.is_installed()
        checks: Dict[str, Any] = {"installed": installed}
        if not installed:
            return HealthStatus(
                service="docker",
                healthy=False,
                detail="docker binary not found on remote host",
                checks=checks,
            )

        running = self.is_daemon_running()
        checks["daemon_running"] = running
        version = self.version() if running else None
        checks["version"] = version

        if not running:
            return HealthStatus(
                service="docker",
                healthy=False,
                detail="docker daemon is not responsive (systemctl status docker for details)",
                checks=checks,
            )

        counts = self._container_counts()
        checks.update(counts)
        return HealthStatus(
            service="docker",
            healthy=True,
            detail=f"docker {version} running with {counts['running']} running / {counts['total']} total containers",
            checks=checks,
        )

    # ------------------------------------------------------------ Containers
    def running_containers(self) -> List[DockerContainerInfo]:
        """Return containers that are currently running."""
        return self._list_containers(only_running=True)

    def stopped_containers(self) -> List[DockerContainerInfo]:
        """Return containers whose state is ``exited`` / ``dead`` / ``created``."""
        containers = self._list_containers(only_running=False)
        return [c for c in containers if c.state.lower() not in {"running", "restarting"}]

    def all_containers(self) -> List[DockerContainerInfo]:
        """Return every container, running or not."""
        return self._list_containers(only_running=False)

    def logs(self, container: str, *, tail: int = 200) -> str:
        """Return the last ``tail`` lines of a container's stdout+stderr."""
        self._require_docker()
        _validate_ref(container)
        tail = max(1, min(int(tail), 2000))
        res = self._ssh.execute(
            f"{self._docker_bin} logs --tail {tail} --timestamps {container} 2>&1"
        )
        if res.exit_code != 0:
            raise DockerNotAvailable(
                f"Could not read logs for container {container!r}: {res.stderr or res.stdout}"
            )
        return res.stdout

    def stats(self) -> List[Dict[str, Any]]:
        """Return a single sample of ``docker stats --no-stream`` per container."""
        self._require_docker()
        res = self._ssh.execute(
            f"{self._docker_bin} stats --no-stream --format "
            "'{{json .}}'"
        )
        return _parse_json_lines(res.stdout)

    def inspect(self, container: str) -> Dict[str, Any]:
        """Return the JSON output of ``docker inspect`` for a container."""
        self._require_docker()
        _validate_ref(container)
        res = self._ssh.execute(f"{self._docker_bin} inspect {container}")
        if res.exit_code != 0:
            raise DockerNotAvailable(
                f"docker inspect {container!r} failed: {res.stderr or res.stdout}"
            )
        try:
            parsed = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise DockerNotAvailable(
                f"Could not parse docker inspect output: {exc}"
            ) from exc
        if isinstance(parsed, list):
            return parsed[0] if parsed else {}
        return parsed

    # ---------------------------------------------------------------- Images
    def images(self) -> List[DockerImageInfo]:
        """Return every image on the remote host."""
        self._require_docker()
        res = self._ssh.execute(
            f"{self._docker_bin} images --format '{{{{json .}}}}'"
        )
        entries = _parse_json_lines(res.stdout)
        images: List[DockerImageInfo] = []
        for entry in entries:
            images.append(
                DockerImageInfo(
                    repository=str(entry.get("Repository", "")),
                    tag=str(entry.get("Tag", "")),
                    image_id=str(entry.get("ID", "")),
                    created=str(entry.get("CreatedSince", entry.get("CreatedAt", ""))),
                    size=str(entry.get("Size", "")),
                )
            )
        return images

    # ------------------------------------------------------------ Disk usage
    def disk_usage(self) -> Dict[str, Any]:
        """Return the parsed output of ``docker system df``."""
        self._require_docker()
        res = self._ssh.execute(
            f"{self._docker_bin} system df --format '{{{{json .}}}}'"
        )
        entries = _parse_json_lines(res.stdout)
        return {"types": entries, "raw": res.stdout}

    # ------------------------------------------------------- Destructive ops
    def restart_container(self, container: str, *, timeout: int = 30) -> Dict[str, Any]:
        """Restart a single container (destructive).

        Callers **must** enforce user confirmation before invoking this.
        """
        self._require_docker()
        _validate_ref(container)
        res = self._ssh.execute(
            f"{self._docker_bin} restart -t {int(timeout)} {container}"
        )
        return {
            "status": ToolStatus.SUCCESS.value if res.exit_code == 0 else ToolStatus.ERROR.value,
            "container": container,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    def prune(self, *, scope: str = "system", volumes: bool = False) -> Dict[str, Any]:
        """Run ``docker <scope> prune -f`` (destructive).

        Args:
            scope: One of ``system``, ``container``, ``image``, ``network``,
                ``volume``, ``builder``.
            volumes: When ``scope == "system"``, also prune volumes.
        """
        self._require_docker()
        if scope not in {"system", "container", "image", "network", "volume", "builder"}:
            raise ValueError(f"Unsupported prune scope: {scope!r}")
        cmd = f"{self._docker_bin} {scope} prune -f"
        if scope == "system" and volumes:
            cmd += " --volumes"
        res = self._ssh.execute(cmd)
        return {
            "status": ToolStatus.SUCCESS.value if res.exit_code == 0 else ToolStatus.ERROR.value,
            "scope": scope,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    def stop_container(self, container: str, *, timeout: int = 10) -> Dict[str, Any]:
        """Stop a running container with ``docker stop`` (destructive)."""
        self._require_docker()
        _validate_ref(container)
        res = self._ssh.execute(
            f"{self._docker_bin} stop -t {int(timeout)} {container}"
        )
        return self._op_result("stop_container", container, res)

    def start_container(self, container: str) -> Dict[str, Any]:
        """Start an existing stopped container with ``docker start``."""
        self._require_docker()
        _validate_ref(container)
        res = self._ssh.execute(f"{self._docker_bin} start {container}")
        return self._op_result("start_container", container, res)

    def remove_container(self, container: str, *, force: bool = False) -> Dict[str, Any]:
        """Remove a container with ``docker rm`` (destructive, data loss).

        Args:
            container: Container name or ID.
            force: When ``True``, pass ``-f`` so a running container is killed
                and removed in one step.
        """
        self._require_docker()
        _validate_ref(container)
        flag = "-f " if force else ""
        res = self._ssh.execute(f"{self._docker_bin} rm {flag}{container}")
        return self._op_result("remove_container", container, res)

    def pull_image(self, image: str) -> Dict[str, Any]:
        """Pull an image with ``docker pull``."""
        self._require_docker()
        _validate_image(image)
        res = self._ssh.execute(f"{self._docker_bin} pull {image}")
        return {
            "status": ToolStatus.SUCCESS.value if res.exit_code == 0 else ToolStatus.ERROR.value,
            "image": image,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    def run_container(
        self,
        image: str,
        *,
        name: Optional[str] = None,
        ports: Optional[str] = None,
        env: Optional[str] = None,
        restart: str = "unless-stopped",
        detach: bool = True,
    ) -> Dict[str, Any]:
        """Create and start a new container with ``docker run`` (destructive).

        Args:
            image: Image reference, e.g. ``nginx:latest`` or
                ``registry.example.com/org/app:1.2.3``.
            name: Optional container name.
            ports: Comma-separated ``host:container`` port mappings, e.g.
                ``"8080:80,4500:4500"``. Each side must be numeric.
            env: Comma-separated ``KEY=value`` pairs, e.g. ``"TZ=UTC,DEBUG=1"``.
                Values may not contain shell metacharacters.
            restart: Restart policy (``no``, ``on-failure``, ``always``,
                ``unless-stopped``).
            detach: Run detached (``-d``). Almost always ``True`` for services.
        """
        self._require_docker()
        _validate_image(image)

        parts: List[str] = [self._docker_bin, "run"]
        if detach:
            parts.append("-d")
        if name:
            _validate_ref(name)
            parts += ["--name", name]
        if restart:
            if restart not in {"no", "on-failure", "always", "unless-stopped"}:
                raise ValueError(f"Unsupported restart policy: {restart!r}")
            parts += ["--restart", restart]
        for mapping in _split_csv(ports):
            _validate_port_mapping(mapping)
            parts += ["-p", mapping]
        for pair in _split_csv(env):
            _validate_env_pair(pair)
            parts += ["-e", pair]
        parts.append(image)

        res = self._ssh.execute(" ".join(parts))
        return {
            "status": ToolStatus.SUCCESS.value if res.exit_code == 0 else ToolStatus.ERROR.value,
            "image": image,
            "name": name,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    def _op_result(self, op: str, container: str, res: Any) -> Dict[str, Any]:
        return {
            "status": ToolStatus.SUCCESS.value if res.exit_code == 0 else ToolStatus.ERROR.value,
            "operation": op,
            "container": container,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    # ---------------------------------------------------------------- Internals
    def _require_docker(self) -> None:
        if not self.is_installed():
            raise DockerNotAvailable("docker binary is not installed on the remote host")

    def _container_counts(self) -> Dict[str, int]:
        containers = self._list_containers(only_running=False)
        running = sum(1 for c in containers if c.state.lower() == "running")
        return {"running": running, "total": len(containers)}

    def _list_containers(self, *, only_running: bool) -> List[DockerContainerInfo]:
        if not self.is_installed():
            return []
        flag = "" if only_running else "-a"
        try:
            res = self._ssh.execute(
                f"{self._docker_bin} ps {flag} --format '{{{{json .}}}}'"
            )
        except SSHConnectionError:
            return []
        if res.exit_code != 0:
            logger.warning(
                "docker ps failed exit={code} stderr={err!r}",
                code=res.exit_code,
                err=res.stderr,
            )
            return []
        entries = _parse_json_lines(res.stdout)
        containers: List[DockerContainerInfo] = []
        for entry in entries:
            containers.append(
                DockerContainerInfo(
                    container_id=str(entry.get("ID", "")),
                    name=str(entry.get("Names", entry.get("Name", ""))),
                    image=str(entry.get("Image", "")),
                    status=str(entry.get("Status", "")),
                    state=str(entry.get("State", "")),
                    ports=str(entry.get("Ports", "")),
                    created=str(entry.get("CreatedAt", entry.get("RunningFor", ""))),
                )
            )
        return containers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
# Image refs allow registry host, path, tag and digest: registry:5000/org/app:1.2@sha256:...
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@\-]{0,255}$")
_PORT_PATTERN = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}:)?\d{1,5}:\d{1,5}(?:/(?:tcp|udp))?$")
_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^;&|`$<>\n\\]*$")


def _validate_ref(name: str) -> None:
    """Validate a Docker container/image reference to prevent shell injection."""
    if not name or not _REF_PATTERN.match(name):
        raise ValueError(
            f"Invalid Docker reference {name!r}. Names must match {_REF_PATTERN.pattern}."
        )


def _validate_image(image: str) -> None:
    """Validate a Docker image reference to prevent shell injection."""
    if not image or not _IMAGE_PATTERN.match(image):
        raise ValueError(
            f"Invalid Docker image {image!r}. Must match {_IMAGE_PATTERN.pattern}."
        )


def _validate_port_mapping(mapping: str) -> None:
    """Validate a single ``[host_ip:]host:container[/proto]`` port mapping."""
    if not _PORT_PATTERN.match(mapping):
        raise ValueError(
            f"Invalid port mapping {mapping!r}. Use HOST:CONTAINER, e.g. '8080:80'."
        )


def _validate_env_pair(pair: str) -> None:
    """Validate a single ``KEY=value`` environment pair (no shell metachars)."""
    if not _ENV_PATTERN.match(pair):
        raise ValueError(
            f"Invalid env pair {pair!r}. Use KEY=value without shell metacharacters."
        )


def _split_csv(value: Optional[str]) -> List[str]:
    """Split a comma-separated argument string into trimmed, non-empty items."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_json_lines(output: str) -> List[Dict[str, Any]]:
    """Parse newline-delimited JSON emitted by ``docker ... --format json``."""
    entries: List[Dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("skipping malformed json line: {line!r}", line=line)
            continue
    return entries


__all__ = ["DockerNotAvailable", "DockerService"]
