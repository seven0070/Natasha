"""Marketplace: skills, plugins, MCP servers, providers, agents, workflows and connectors.

Install pipeline (every step enforced, not advisory):
download -> checksum -> manifest validation -> publisher verification -> dependency scan ->
static scan -> sandbox test -> permission review -> owner approval (when required) -> install.

Version pinning and rollback are first-class: an installed item records the exact version and
checksum, and a rollback re-activates a previously verified version.
"""

from .installer import MarketplaceInstaller, get_marketplace_installer
from .package import MarketplacePackage, PackageSource, SecurityReport, inspect_package
from .registry import MarketplaceRegistry, get_marketplace_registry

__all__ = [
    "MarketplaceInstaller", "get_marketplace_installer", "MarketplacePackage", "PackageSource",
    "SecurityReport", "inspect_package", "MarketplaceRegistry", "get_marketplace_registry",
]
