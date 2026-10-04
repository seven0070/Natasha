"""Role implementations live in :mod:`natasha.agents.roles`.

The package exists to keep the documented repository layout (`agents/workers/`) meaningful: the
worker roles are defined here, and the runtime that supervises them is `natasha.missions.supervisor`.
"""

from ..roles import AGENT_ROLES, AgentRole, default_roles, get_role
from ..team import AgentTeam, get_agent_team

__all__ = ["AGENT_ROLES", "AgentRole", "default_roles", "get_role", "AgentTeam", "get_agent_team"]
