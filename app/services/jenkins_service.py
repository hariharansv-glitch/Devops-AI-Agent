"""Jenkins inspection over SSH.

Jenkins is usually installed as a systemd unit (``jenkins.service``). This
service therefore talks to Jenkins indirectly through ``systemctl`` and the
Jenkins log file (``/var/log/jenkins/jenkins.log``).
"""

from __future__ import annotations

import re
from typing import Optional

from app.schemas import HealthStatus, JenkinsStatus, ToolStatus
from app.services.ssh_service import SSHConnectionError, SSHService
from app.utils import get_logger

logger = get_logger(__name__)

_UNIT = "jenkins.service"
_LOG_PATH = "/var/log/jenkins/jenkins.log"


class JenkinsService:
    """High-level Jenkins inspector."""

    def __init__(self, ssh: SSHService) -> None:
        self._ssh = ssh

    # ------------------------------------------------------------------ Status
    def is_installed(self) -> bool:
        """Return ``True`` when a ``jenkins.service`` unit exists."""
        try:
            res = self._ssh.execute(
                f"systemctl status {_UNIT} >/dev/null 2>&1; echo $?"
            )
        except SSHConnectionError:
            return False
        # `systemctl status` returns 0 (active), 3 (inactive), or 4 (not found).
        # Anything except 4 implies the unit is *known*.
        try:
            code = int(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return False
        return code != 4

    def is_running(self) -> bool:
        """Return ``True`` iff ``systemctl is-active jenkins`` reports ``active``."""
        try:
            res = self._ssh.execute(f"systemctl is-active {_UNIT}")
        except SSHConnectionError:
            return False
        return res.stdout.strip() == "active"

    def _active_states(self) -> tuple[Optional[str], Optional[str]]:
        """Return ``(ActiveState, SubState)`` from ``systemctl show``."""
        try:
            res = self._ssh.execute(
                f"systemctl show {_UNIT} --no-page -p ActiveState -p SubState 2>/dev/null"
            )
        except SSHConnectionError:
            return None, None
        active: Optional[str] = None
        sub: Optional[str] = None
        for line in res.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "ActiveState":
                active = value.strip() or None
            elif key == "SubState":
                sub = value.strip() or None
        return active, sub

    def version(self) -> Optional[str]:
        """Return the installed Jenkins version, if we can figure it out."""
        # Preferred: parse the last "Jenkins X.Y" line from the log.
        try:
            res = self._ssh.execute(
                f"grep -Eo 'Jenkins [0-9]+\\.[0-9]+(\\.[0-9]+)?' {_LOG_PATH} 2>/dev/null | tail -n 1"
            )
        except SSHConnectionError:
            return None
        stdout = res.stdout.strip()
        if stdout:
            match = re.search(r"Jenkins\s+([\d.]+)", stdout)
            if match:
                return match.group(1)

        # Fallback: `dpkg -s jenkins` (Debian/Ubuntu) or `rpm -q jenkins` (RHEL).
        try:
            res = self._ssh.execute(
                "dpkg -s jenkins 2>/dev/null | awk '/Version:/ {print $2; exit}' "
                "|| rpm -q --qf '%{VERSION}\\n' jenkins 2>/dev/null"
            )
        except SSHConnectionError:
            return None
        return res.stdout.strip() or None

    def status(self) -> JenkinsStatus:
        """Return a rich :class:`JenkinsStatus` snapshot."""
        installed = self.is_installed()
        if not installed:
            return JenkinsStatus(
                installed=False,
                running=False,
                detail="jenkins.service is not registered on this host",
            )
        running = self.is_running()
        active_state, sub_state = self._active_states()
        return JenkinsStatus(
            installed=True,
            running=running,
            version=self.version(),
            active_state=active_state,
            sub_state=sub_state,
            detail=(
                f"jenkins.service active_state={active_state!r} sub_state={sub_state!r}"
            ),
        )

    def health(self) -> HealthStatus:
        """Return a :class:`HealthStatus` snapshot suitable for the LLM."""
        status = self.status()
        checks = status.model_dump()
        return HealthStatus(
            status=ToolStatus.SUCCESS,
            service="jenkins",
            healthy=status.installed and status.running,
            detail=status.detail,
            checks=checks,
        )

    # ------------------------------------------------------------------- Logs
    def logs(self, *, lines: int = 200) -> str:
        """Return the last ``lines`` of ``/var/log/jenkins/jenkins.log``.

        Falls back to ``journalctl -u jenkins`` when the log file is absent.
        """
        lines = max(1, min(int(lines), 2000))
        cmd = (
            f"if [ -r {_LOG_PATH} ]; then tail -n {lines} {_LOG_PATH}; "
            f"else journalctl -u {_UNIT} -n {lines} --no-pager 2>/dev/null "
            f"|| echo 'No Jenkins log source available.'; fi"
        )
        return self._ssh.execute(cmd).stdout

    # ----------------------------------------------------------- Destructive
    def restart(self) -> dict:
        """Restart the Jenkins systemd unit (destructive).

        Callers **must** enforce user confirmation before invoking this.
        """
        res = self._ssh.execute(f"sudo -n systemctl restart {_UNIT}")
        return {
            "status": ToolStatus.SUCCESS.value if res.exit_code == 0 else ToolStatus.ERROR.value,
            "service": "jenkins",
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }


__all__ = ["JenkinsService"]
