"""Natasha's command line interface.

The CLI runs on the owner's machine and therefore acts as the *owner* (``local-owner``): it can do
things the model cannot, such as approving a request, applying an upgrade or reading the audit log.
That is deliberate - the terminal is the owner's own hands.
"""

from .main import build_parser, main

__all__ = ["build_parser", "main"]
