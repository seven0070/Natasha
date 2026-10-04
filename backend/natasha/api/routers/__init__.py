"""API routers, one module per subsystem group."""

from . import (activity, agents, approvals, artifacts, auth, chat, computer, creation, governance,
               integrations, marketplace, mcp, memory, missions, observability, providers, security,
               settings as settings_router, skills, system, tools, vision, voice)

ROUTERS = (
    system.router, auth.router, chat.router, missions.router, memory.router, approvals.router,
    activity.router, security.router, governance.router, tools.router, skills.router,
    marketplace.router, creation.router, computer.router, voice.router, agents.router,
    mcp.router, integrations.router, observability.router, settings_router.router,
    providers.router, artifacts.router, vision.router,
)

__all__ = ["ROUTERS"]
