"""Google Agent Development Kit (ADK) integration.

This subpackage wires the DevOps agent together:

* :mod:`app.agent.instructions` - system prompt (professional, DevOps-focused).
* :mod:`app.agent.callbacks`   - ADK ``before_/after_`` callbacks (safety, logs).
* :mod:`app.agent.session`     - :class:`InMemorySessionService` wrapper.
* :mod:`app.agent.memory`      - :class:`InMemoryMemoryService` wrapper.
* :mod:`app.agent.root_agent`  - :class:`LlmAgent` factory.
* :mod:`app.agent.runner`      - :class:`Runner` factory + helpers.
"""

from __future__ import annotations

from app.agent.runner import AgentReply, AgentService, get_agent_service

__all__ = ["AgentReply", "AgentService", "get_agent_service"]
