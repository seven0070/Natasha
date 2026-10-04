"""Specialist workers.

Natasha is one agent, but complex work is delegated to *roles*: a researcher, a coder, a writer, an
analyst and a reviewer. Each role is a frozen capability profile plus a handler; the supervisor in
:mod:`natasha.missions.supervisor` owns spawning, budgets and the rule that a worker can never touch
governance, credentials or approvals. Roles produce *proposals* - the executive still verifies.
"""

from .roles import AGENT_ROLES, AgentRole, default_roles, get_role
from .team import AgentTeam, get_agent_team

__all__ = ["AGENT_ROLES", "AgentRole", "default_roles", "get_role", "AgentTeam", "get_agent_team"]
