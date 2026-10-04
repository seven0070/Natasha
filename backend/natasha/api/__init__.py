"""The local HTTP API and the ChatGPT-like UI it serves.

Local-first by design: the server binds to loopback by default, every mutating route is owner
authenticated, and the same policy engine that gates tool calls gates the API surface.
"""

from .app import app, create_app, get_app

__all__ = ["app", "create_app", "get_app"]
