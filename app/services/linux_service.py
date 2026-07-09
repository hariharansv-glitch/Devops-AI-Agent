"""Linux telemetry collection over SSH.

Each method issues one or two well-defined shell commands and parses the
output into a strongly-typed model. Commands are chosen to be available on
any modern Linux distribution (Debian/Ubuntu, RHEL/Fedora, Alpine with
``coreutils`` installed).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.schemas import (
    CommandResult,
    DiskPartition,
    LinuxMetrics,
    ServiceInfo,
    ToolStatus,
)
from app.services.ssh_service import SSHConnectionError, SSHService
from app.utils import get_logger

logger = get_logger(__name__)


class LinuxService:
    """High-level Linux telemetry collector.

    All methods are synchronous; the calling :mod:`app.tools` layer wraps them
    in :func:`asyncio.to_thread` when invoked from an ADK tool.
    """

    def __init__(self, ssh: SSHService) -> None:
        self._ssh = ssh

    # ---------------------------------------------------------- Simple wrappers
    def run(self, command: str, *, timeout: Optional[float] = None) -> CommandResult:
        """Execute a raw command; convenience proxy used by higher layers."""
        return self._ssh.execute(command, timeout=timeout)

    # ------------------------------------------------------------ Static facts
    def hostname(self) -> Optional[str]:
        """Return the remote hostname (``uname -n``)."""
        try:
            res = self._ssh.execute("uname -n")
        except SSHConnectionError:
            return None
        return res.stdout.strip() or None

    def kernel(self) -> Optional[str]:
        """Return the kernel release string (``uname -r``)."""
        try:
            res = self._ssh.execute("uname -r")
        except SSHConnectionError:
            return None
        return res.stdout.strip() or None

    def uptime(self) -> str:
        """Return a human-friendly uptime string via ``uptime -p``.

        Falls back to raw ``uptime`` output on distributions that do not
        support ``-p`` (e.g. Alpine's default ``busybox`` build).
        """
        res = self._ssh.execute("uptime -p 2>/dev/null || uptime")
        return res.stdout.strip()

    # -------------------------------------------------------------- Load / CPU
    def load_average(self) -> Dict[str, float]:
        """Parse ``/proc/loadavg`` into 1/5/15-minute floats."""
        res = self._ssh.execute("cat /proc/loadavg")
        parts = res.stdout.split()
        if len(parts) < 3:
            raise RuntimeError(f"Unexpected /proc/loadavg output: {res.stdout!r}")
        return {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}

    def cpu_usage(self) -> float:
        """Return the current CPU usage as a percentage (0-100).

        Uses ``top -bn1`` which is available on every distribution. The line
        of interest looks like: ``%Cpu(s):  3.2 us, 1.1 sy, ...``.
        """
        res = self._ssh.execute("top -bn1 | grep -E '^%?Cpu' | head -n 1")
        line = res.stdout.strip()
        if not line:
            raise RuntimeError("Could not read CPU usage from `top -bn1`")
        # Extract everything up to the "id" (idle) field.
        idle_match = re.search(r"([\d.]+)\s*(?:%?)\s*id", line)
        if not idle_match:
            raise RuntimeError(f"Could not parse idle CPU from: {line!r}")
        idle = float(idle_match.group(1))
        return max(0.0, min(100.0, round(100.0 - idle, 2)))

    def top_processes_by_cpu(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return the top-N processes by CPU usage."""
        return self._top_processes(sort_field="%cpu", limit=limit)

    def top_processes_by_memory(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return the top-N processes by RSS memory."""
        return self._top_processes(sort_field="%mem", limit=limit)

    # ---------------------------------------------------------------- Memory
    def memory_usage(self) -> Dict[str, Any]:
        """Return a dict describing RAM and swap usage in bytes and percent."""
        res = self._ssh.execute("free -b")
        return _parse_free(res.stdout)

    # ----------------------------------------------------------------- Disk
    def disk_usage(self) -> List[DiskPartition]:
        """Return a list of :class:`DiskPartition` for each mounted filesystem."""
        # -x ignores pseudo filesystems that only add noise.
        res = self._ssh.execute(
            "df -hPT -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null || df -hP"
        )
        return _parse_df(res.stdout)

    # -------------------------------------------------------------- Services
    def running_services(self, limit: int = 50) -> List[ServiceInfo]:
        """Return running systemd units.

        On systems without systemd (e.g. some minimal containers) this falls
        back to parsing ``service --status-all``.
        """
        res = self._ssh.execute(
            "systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null "
            "|| service --status-all 2>&1"
        )
        return _parse_systemctl(res.stdout, limit=limit)

    # ------------------------------------------------------------- Networking
    def open_ports(self) -> List[Dict[str, Any]]:
        """Return listening TCP/UDP sockets via ``ss -tulpen``."""
        res = self._ssh.execute("ss -tulpenH 2>/dev/null || netstat -tulpen")
        return _parse_ss(res.stdout)

    # ----------------------------------------------------------- System logs
    def system_logs(self, *, lines: int = 200, priority: str = "err") -> str:
        """Return recent journald / syslog lines.

        Tries ``journalctl`` first (systemd) and falls back to
        ``/var/log/syslog`` or ``/var/log/messages`` on other distributions.
        """
        lines = max(1, min(int(lines), 2000))
        command = (
            f"journalctl -p {priority}..emerg -n {lines} --no-pager 2>/dev/null "
            f"|| tail -n {lines} /var/log/syslog 2>/dev/null "
            f"|| tail -n {lines} /var/log/messages 2>/dev/null "
            f"|| echo 'No system log source available (journalctl/syslog/messages).'"
        )
        return self._ssh.execute(command).stdout

    # --------------------------------------------------------------- Snapshot
    def snapshot(self) -> LinuxMetrics:
        """Collect a single, well-rounded telemetry snapshot.

        Individual failures do not abort the snapshot: any missing field is
        left as ``None`` (or an empty list). This is intentional because tools
        like ``top`` or ``ss`` may be unavailable in stripped-down images.
        """
        metrics = LinuxMetrics()
        collectors: List[tuple[str, Any]] = [
            ("hostname", lambda: setattr(metrics, "hostname", self.hostname())),
            ("kernel", lambda: setattr(metrics, "kernel", self.kernel())),
            ("uptime", lambda: setattr(metrics, "uptime", self.uptime())),
            ("load_average", lambda: setattr(metrics, "load_average", self.load_average())),
            ("cpu_percent", lambda: setattr(metrics, "cpu_percent", self.cpu_usage())),
            (
                "memory",
                lambda: _apply_memory(metrics, self.memory_usage()),
            ),
            ("disks", lambda: setattr(metrics, "disks", self.disk_usage())),
            (
                "top_processes_cpu",
                lambda: setattr(
                    metrics, "top_processes_cpu", self.top_processes_by_cpu(5)
                ),
            ),
            (
                "top_processes_mem",
                lambda: setattr(
                    metrics, "top_processes_mem", self.top_processes_by_memory(5)
                ),
            ),
        ]
        for name, fn in collectors:
            try:
                fn()
            except Exception:  # noqa: BLE001 - snapshot is best-effort
                logger.warning("linux snapshot: {field} collection failed", field=name)
                logger.opt(exception=True).debug("collector traceback")
        metrics.status = ToolStatus.SUCCESS
        return metrics

    # ---------------------------------------------------------------- Internals
    def _top_processes(self, *, sort_field: str, limit: int) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 25))
        cmd = (
            f"ps -eo pid,user,pcpu,pmem,rss,comm --sort=-{sort_field} "
            f"| head -n {limit + 1}"
        )
        res = self._ssh.execute(cmd)
        rows = res.stdout.strip().splitlines()
        if len(rows) <= 1:
            return []
        parsed: List[Dict[str, Any]] = []
        for line in rows[1:]:
            fields = line.split(None, 5)
            if len(fields) < 6:
                continue
            pid, user, pcpu, pmem, rss, comm = fields
            parsed.append(
                {
                    "pid": int(pid),
                    "user": user,
                    "cpu_percent": float(pcpu),
                    "mem_percent": float(pmem),
                    "rss_kib": int(rss),
                    "command": comm.strip(),
                }
            )
        return parsed


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-testable without SSH)
# ---------------------------------------------------------------------------


def _parse_free(output: str) -> Dict[str, Any]:
    """Parse ``free -b`` output into a memory+swap dict."""
    mem: Dict[str, Any] = {}
    swap: Dict[str, Any] = {}
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        head = parts[0].rstrip(":").lower()
        if head == "mem" and len(parts) >= 4:
            total = int(parts[1])
            used = int(parts[2])
            free = int(parts[3])
            available = int(parts[6]) if len(parts) >= 7 else free
            mem = {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "available_bytes": available,
                "used_percent": round((used / total) * 100.0, 2) if total else 0.0,
            }
        elif head == "swap" and len(parts) >= 4:
            total = int(parts[1])
            used = int(parts[2])
            free = int(parts[3])
            swap = {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_percent": round((used / total) * 100.0, 2) if total else 0.0,
            }
    return {"memory": mem, "swap": swap}


def _apply_memory(metrics: LinuxMetrics, parsed: Dict[str, Any]) -> None:
    """Copy the output of :func:`_parse_free` onto a :class:`LinuxMetrics`."""
    metrics.memory = parsed.get("memory") or None
    metrics.swap = parsed.get("swap") or None


_DF_TYPE_HEADER = re.compile(r"^\s*Filesystem\s+Type\s+")


def _parse_df(output: str) -> List[DiskPartition]:
    """Parse ``df -hPT`` (or fallback ``df -hP``) output."""
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return []

    has_type_column = bool(_DF_TYPE_HEADER.match(lines[0]))
    partitions: List[DiskPartition] = []
    for line in lines[1:]:
        fields = line.split()
        # With Type column: filesystem type size used avail use% mount
        # Without: filesystem size used avail use% mount
        expected_min = 7 if has_type_column else 6
        if len(fields) < expected_min:
            continue
        if has_type_column:
            fs, _type, size, used, avail, use_percent, mount = fields[:7]
        else:
            fs, size, used, avail, use_percent, mount = fields[:6]
        use_percent = use_percent.rstrip("%")
        try:
            pct = int(use_percent)
        except ValueError:
            continue
        partitions.append(
            DiskPartition(
                filesystem=fs,
                size=size,
                used=used,
                available=avail,
                use_percent=max(0, min(100, pct)),
                mounted_on=mount,
            )
        )
    return partitions


def _parse_systemctl(output: str, *, limit: int) -> List[ServiceInfo]:
    """Parse ``systemctl list-units`` output."""
    services: List[ServiceInfo] = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        unit, load, active, sub, description = parts
        if not unit.endswith(".service"):
            continue
        services.append(
            ServiceInfo(
                unit=unit,
                load=load,
                active=active,
                sub=sub,
                description=description,
            )
        )
        if len(services) >= limit:
            break
    return services


_SS_HEADER = re.compile(r"^Netid\s+")


def _parse_ss(output: str) -> List[Dict[str, Any]]:
    """Parse ``ss -tulpenH`` output into a list of dicts."""
    entries: List[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip() or _SS_HEADER.match(line):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        netid, state = parts[0], parts[1]
        local, peer = parts[4], parts[5] if len(parts) > 5 else ""
        entries.append(
            {
                "protocol": netid,
                "state": state,
                "local": local,
                "peer": peer,
                "process": " ".join(parts[6:]) if len(parts) > 6 else "",
            }
        )
    return entries


__all__ = ["LinuxService"]
