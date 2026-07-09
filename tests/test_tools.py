"""Tests for ADK tools (using the SSH fake)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas import ToolStatus
from app.tools.docker_tool import (
    docker_health,
    docker_prune,
    docker_restart_container,
)
from app.tools.jenkins_tool import jenkins_restart, jenkins_status
from app.tools.linux_tool import (
    linux_cpu_usage,
    linux_disk_usage,
    linux_memory_usage,
    linux_uptime,
)
from app.tools.logs_tool import _classify_line, _fingerprint_line, logs_summarize
from app.tools.ssh_tool import ssh_execute
from tests.conftest import FakeSSHResponse


class _FakeToolContext:
    """Minimal stand-in for :class:`google.adk.tools.tool_context.ToolContext`."""

    def __init__(self) -> None:
        self.state: dict = {}
        self.agent_name = "test-agent"
        self.function_call_id = "test-call"
        self.actions = SimpleNamespace(skip_summarization=False)


@pytest.fixture
def tool_ctx() -> _FakeToolContext:
    return _FakeToolContext()


# ---------------------------------------------------------------------------
# Linux tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linux_memory_usage(fake_services, fake_ssh, tool_ctx):
    fake_ssh.register_exact(
        "free -b",
        FakeSSHResponse(
            stdout=(
                "               total        used        free      shared  buff/cache   available\n"
                "Mem:     16000000000  8000000000  4000000000    50000000  4000000000  6500000000\n"
                "Swap:     2000000000   500000000  1500000000\n"
            )
        ),
    )
    result = await linux_memory_usage(tool_ctx)
    assert result["status"] == ToolStatus.SUCCESS.value
    assert result["memory"]["total_bytes"] == 16_000_000_000
    assert "GiB" in result["memory"]["available_human"]


@pytest.mark.asyncio
async def test_linux_cpu_usage(fake_services, fake_ssh, tool_ctx):
    fake_ssh.register_predicate(
        lambda cmd: cmd.startswith("top -bn1"),
        FakeSSHResponse(
            stdout="%Cpu(s):  10.0 us, 5.0 sy, 0.0 ni, 80.0 id, 4.0 wa, 0.0 hi, 1.0 si, 0.0 st"
        ),
    )
    result = await linux_cpu_usage(tool_ctx)
    assert result["cpu_percent"] == 20.0


@pytest.mark.asyncio
async def test_linux_uptime(fake_services, fake_ssh, tool_ctx):
    fake_ssh.register_substr(
        "uptime -p",
        FakeSSHResponse(stdout="up 4 hours\n"),
    )
    result = await linux_uptime(tool_ctx)
    assert result["uptime"] == "up 4 hours"


@pytest.mark.asyncio
async def test_linux_disk_usage(fake_services, fake_ssh, tool_ctx):
    fake_ssh.register_substr(
        "df -hPT -x tmpfs",
        FakeSSHResponse(
            stdout=(
                "Filesystem     Type     Size  Used Avail Use% Mounted on\n"
                "/dev/sda1      ext4     100G   80G   15G  85% /\n"
            )
        ),
    )
    result = await linux_disk_usage(tool_ctx)
    assert result["count"] == 1
    assert result["partitions"][0]["use_percent"] == 85


# ---------------------------------------------------------------------------
# SSH tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ssh_execute_empty_rejected(fake_services, tool_ctx):
    result = await ssh_execute("", None, tool_ctx)
    assert result["status"] == ToolStatus.ERROR.value


@pytest.mark.asyncio
async def test_ssh_execute_denylist_blocked(fake_services, tool_ctx):
    result = await ssh_execute("rm -rf /", None, tool_ctx)
    assert result["status"] == ToolStatus.BLOCKED.value


# ---------------------------------------------------------------------------
# Docker tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docker_health(fake_services, fake_ssh, tool_ctx):
    fake_ssh.register_substr("command -v docker", FakeSSHResponse(exit_code=0))
    fake_ssh.register_substr(
        "docker info --format", FakeSSHResponse(stdout="24.0.7", exit_code=0)
    )
    fake_ssh.register_substr(
        "docker version --format", FakeSSHResponse(stdout="24.0.7", exit_code=0)
    )
    fake_ssh.register_substr(
        "docker ps -a --format",
        FakeSSHResponse(
            stdout='{"ID":"abc","Names":"web","Image":"nginx","Status":"Up","State":"running"}\n'
        ),
    )
    result = await docker_health(tool_ctx)
    assert result["healthy"] is True
    assert result["service"] == "docker"


@pytest.mark.asyncio
async def test_docker_restart_requires_confirmation(fake_services, tool_ctx):
    result = await docker_restart_container("web", False, tool_ctx)
    assert result["status"] == ToolStatus.CONFIRMATION_REQUIRED.value
    assert "restart" in result["prompt"].lower()


@pytest.mark.asyncio
async def test_docker_restart_blocked_in_readonly(
    monkeypatch: pytest.MonkeyPatch, fake_services, tool_ctx
):
    # Confirm=True should still be blocked because READ_ONLY_MODE is on.
    result = await docker_restart_container("web", True, tool_ctx)
    assert result["status"] == ToolStatus.BLOCKED.value


@pytest.mark.asyncio
async def test_docker_prune_requires_confirmation(fake_services, tool_ctx):
    result = await docker_prune("system", False, False, tool_ctx)
    assert result["status"] == ToolStatus.CONFIRMATION_REQUIRED.value
    assert "prune" in result["prompt"].lower()


# ---------------------------------------------------------------------------
# Jenkins tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jenkins_status_not_installed(fake_services, fake_ssh, tool_ctx):
    fake_ssh.register_substr(
        "systemctl status jenkins.service",
        FakeSSHResponse(stdout="4\n"),
    )
    result = await jenkins_status(tool_ctx)
    assert result["installed"] is False


@pytest.mark.asyncio
async def test_jenkins_restart_requires_confirmation(fake_services, tool_ctx):
    result = await jenkins_restart(False, tool_ctx)
    assert result["status"] == ToolStatus.CONFIRMATION_REQUIRED.value


# ---------------------------------------------------------------------------
# Logs tools
# ---------------------------------------------------------------------------


class TestLogsHelpers:
    def test_classify_line_error(self) -> None:
        assert _classify_line("2024-01-01 ERROR failed to connect") == "error"

    def test_classify_line_critical(self) -> None:
        assert _classify_line("kernel: PANIC oops") == "critical"

    def test_fingerprint_normalises_numbers(self) -> None:
        a = _fingerprint_line("2024-01-01 12:00:00 ERROR pid=1234 socket=0xff")
        b = _fingerprint_line("2024-01-02 13:00:00 ERROR pid=5678 socket=0xaa")
        assert a == b


@pytest.mark.asyncio
async def test_logs_summarize_buckets(tool_ctx):
    text = "\n".join(
        [
            "2024-01-01 INFO booted",
            "2024-01-01 ERROR failed to open /etc/foo",
            "2024-01-01 WARN quota exceeded",
            "2024-01-01 CRITICAL disk full",
            "2024-01-01 ERROR failed to open /etc/foo",
        ]
    )
    result = await logs_summarize(text, 25, tool_ctx)
    assert result["counts"]["error"] == 2
    assert result["counts"]["critical"] == 1
    assert result["counts"]["warning"] == 1
    top = result["top_error_signatures"]
    assert top and top[0]["count"] == 2
