"""Process entry points.

The application itself lives in ``backend/natasha``. What is left here is *composition*: the small
wrappers that a service manager, a container runtime or a developer can execute. They contain no
business logic - they only decide how the runtime is started and hand off to the canonical CLI, so
there is exactly one startup path to test and document.
"""

__all__: list[str] = []
