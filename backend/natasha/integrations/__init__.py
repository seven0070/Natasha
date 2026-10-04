"""Integrations: external services behind a uniform connector contract.

Connectors never hold secrets: they reference a credential and ask the broker for a scoped handle
at call time. Responses are treated as untrusted external content.
"""

from .base import Connector, ConnectorAction, ConnectorResult, IntegrationRegistry, get_integration_registry
from .connectors import (
    EmailConnector,
    GitHubConnector,
    RestConnector,
    SlackConnector,
    WebhookConnector,
    default_connectors,
)

__all__ = [
    "Connector", "ConnectorAction", "ConnectorResult", "IntegrationRegistry", "get_integration_registry",
    "EmailConnector", "GitHubConnector", "RestConnector", "SlackConnector", "WebhookConnector",
    "default_connectors",
]
