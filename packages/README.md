# Shared packages

Code that is consumed by more than one client lives here. The rule for this directory: it must be
consumed by something real in this repository, and it must not depend on the backend's internals —
otherwise it belongs in `backend/natasha`.

| package | what it is | who consumes it |
| --- | --- | --- |
| [`api-contract/`](api-contract/) | the exported OpenAPI schema of the REST API, committed as a file | `tests/unit/test_api_contract.py` (drift gate), API clients, and the desktop shell when it lands |

The desktop shell (`desktop/`, Tauri) is deferred to repository phase 2; this directory is where the
pieces it will share with the web console will live, so the API surface it targets is already pinned
by a test rather than by memory.
