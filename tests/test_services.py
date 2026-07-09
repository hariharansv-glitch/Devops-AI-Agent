"""Tests for :mod:`app.services` (using the SSH fake)."""

from __future__ import annotations

from tests.conftest import FakeSSHResponse, FakeSSHService

from app.services.docker_service import DockerService
from app.services.jenkins_service import JenkinsService
from app.services.linux_service import (
    LinuxService,
    _parse_df,
    _parse_free,
    _parse_systemctl,
)


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


class TestLinuxParsers:
    def test_parse_free(self) -> None:
        raw = (
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:     16000000000  8000000000  4000000000    50000000  4000000000  6500000000\n"
            "Swap:     2000000000   500000000  1500000000\n"
        )
        parsed = _parse_free(raw)
        assert parsed["memory"]["total_bytes"] == 16_000_000_000
        assert parsed["memory"]["available_bytes"] == 6_500_000_000
        assert parsed["memory"]["used_percent"] == 50.0
        assert parsed["swap"]["total_bytes"] == 2_000_000_000
        assert parsed["swap"]["used_percent"] == 25.0

    def test_parse_df_with_type(self) -> None:
        raw = (
            "Filesystem     Type     Size  Used Avail Use% Mounted on\n"
            "/dev/sda1      ext4     100G   35G   60G  37% /\n"
            "/dev/sdb1      ext4     500G  480G   20G  96% /data\n"
        )
        partitions = _parse_df(raw)
        assert [p.mounted_on for p in partitions] == ["/", "/data"]
        assert partitions[1].use_percent == 96

    def test_parse_df_without_type(self) -> None:
        raw = (
            "Filesystem     Size  Used Avail Use% Mounted on\n"
            "/dev/sda1      100G   35G   60G  37% /\n"
        )
        partitions = _parse_df(raw)
        assert partitions[0].filesystem == "/dev/sda1"
        assert partitions[0].use_percent == 37

    def test_parse_systemctl(self) -> None:
        raw = (
            "docker.service              loaded active running   Docker Application Container Engine\n"
            "jenkins.service             loaded active running   Jenkins Continuous Integration Server\n"
            "cron.service                loaded active running   Regular background program processing daemon\n"
            "not-a-service.timer         loaded active running   Some timer\n"
        )
        services = _parse_systemctl(raw, limit=10)
        assert [s.unit for s in services] == [
            "docker.service",
            "jenkins.service",
            "cron.service",
        ]


# ---------------------------------------------------------------------------
# Linux service (using the SSH fake)
# ---------------------------------------------------------------------------


class TestLinuxService:
    def test_load_average(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_exact(
            "cat /proc/loadavg",
            FakeSSHResponse(stdout="0.20 0.30 0.40 2/312 1234"),
        )
        load = LinuxService(fake_ssh).load_average()  # type: ignore[arg-type]
        assert load == {"1m": 0.20, "5m": 0.30, "15m": 0.40}

    def test_cpu_usage(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_predicate(
            lambda cmd: cmd.startswith("top -bn1"),
            FakeSSHResponse(
                stdout="%Cpu(s):  3.2 us, 1.1 sy, 0.0 ni, 94.5 id, 1.0 wa, 0.0 hi, 0.2 si, 0.0 st"
            ),
        )
        cpu = LinuxService(fake_ssh).cpu_usage()  # type: ignore[arg-type]
        assert cpu == 5.5

    def test_uptime(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_substr(
            "uptime -p",
            FakeSSHResponse(stdout="up 3 hours, 12 minutes\n"),
        )
        assert (
            LinuxService(fake_ssh).uptime() == "up 3 hours, 12 minutes"  # type: ignore[arg-type]
        )

    def test_snapshot_is_resilient(self, fake_ssh: FakeSSHService) -> None:
        # Register only a subset of the commands used by snapshot; the rest
        # will raise LookupError inside the fake, which the snapshot should
        # swallow gracefully.
        fake_ssh.register_exact("uname -n", FakeSSHResponse(stdout="test-host"))
        fake_ssh.register_exact("uname -r", FakeSSHResponse(stdout="6.0.0"))
        fake_ssh.register_substr(
            "uptime -p",
            FakeSSHResponse(stdout="up 1 hour"),
        )
        snapshot = LinuxService(fake_ssh).snapshot()  # type: ignore[arg-type]
        assert snapshot.hostname == "test-host"
        assert snapshot.kernel == "6.0.0"
        assert snapshot.uptime == "up 1 hour"


# ---------------------------------------------------------------------------
# Docker service
# ---------------------------------------------------------------------------


class TestDockerService:
    def _install_common(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_substr(
            "command -v docker",
            FakeSSHResponse(exit_code=0),
        )
        fake_ssh.register_substr(
            "docker info --format",
            FakeSSHResponse(stdout="24.0.7\n", exit_code=0),
        )
        fake_ssh.register_substr(
            "docker version --format",
            FakeSSHResponse(stdout="24.0.7\n", exit_code=0),
        )

    def test_health_when_all_good(self, fake_ssh: FakeSSHService) -> None:
        self._install_common(fake_ssh)
        fake_ssh.register_substr(
            "docker ps -a --format",
            FakeSSHResponse(
                stdout=(
                    '{"ID":"abc","Names":"web","Image":"nginx:1","Status":"Up 3h","State":"running","Ports":"80/tcp","CreatedAt":"2024-01-01"}\n'
                    '{"ID":"def","Names":"db","Image":"postgres:16","Status":"Exited","State":"exited","Ports":"","CreatedAt":"2024-01-01"}\n'
                )
            ),
        )
        docker = DockerService(fake_ssh)  # type: ignore[arg-type]
        health = docker.health()
        assert health.healthy is True
        assert health.checks["daemon_running"] is True
        assert health.checks["running"] == 1
        assert health.checks["total"] == 2

    def test_running_containers_parses_json(self, fake_ssh: FakeSSHService) -> None:
        self._install_common(fake_ssh)
        fake_ssh.register_substr(
            "docker ps  --format",
            FakeSSHResponse(
                stdout='{"ID":"abc","Names":"web","Image":"nginx","Status":"Up 3h","State":"running","Ports":"80/tcp","CreatedAt":"2024-01-01"}\n'
            ),
        )
        docker = DockerService(fake_ssh)  # type: ignore[arg-type]
        containers = docker.running_containers()
        assert len(containers) == 1
        assert containers[0].name == "web"
        assert containers[0].image == "nginx"

    def test_health_when_docker_missing(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_substr(
            "command -v docker",
            FakeSSHResponse(exit_code=1),
        )
        docker = DockerService(fake_ssh)  # type: ignore[arg-type]
        health = docker.health()
        assert health.healthy is False
        assert "not found" in health.detail

    def test_ref_validation(self, fake_ssh: FakeSSHService) -> None:
        import pytest

        docker = DockerService(fake_ssh)  # type: ignore[arg-type]
        self._install_common(fake_ssh)
        with pytest.raises(ValueError):
            docker.logs("bad ref;rm -rf /", tail=10)


# ---------------------------------------------------------------------------
# Jenkins service
# ---------------------------------------------------------------------------


class TestJenkinsService:
    def test_status_running(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_substr(
            "systemctl status jenkins.service",
            FakeSSHResponse(stdout="0\n"),
        )
        fake_ssh.register_substr(
            "systemctl is-active jenkins.service",
            FakeSSHResponse(stdout="active"),
        )
        fake_ssh.register_substr(
            "systemctl show jenkins.service",
            FakeSSHResponse(stdout="ActiveState=active\nSubState=running\n"),
        )
        fake_ssh.register_substr(
            "grep -Eo 'Jenkins [0-9]",
            FakeSSHResponse(stdout="Jenkins 2.440.3\n"),
        )
        status = JenkinsService(fake_ssh).status()  # type: ignore[arg-type]
        assert status.installed is True
        assert status.running is True
        assert status.active_state == "active"
        assert status.version == "2.440.3"

    def test_status_not_installed(self, fake_ssh: FakeSSHService) -> None:
        fake_ssh.register_substr(
            "systemctl status jenkins.service",
            FakeSSHResponse(stdout="4\n"),
        )
        status = JenkinsService(fake_ssh).status()  # type: ignore[arg-type]
        assert status.installed is False
        assert status.running is False
