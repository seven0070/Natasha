"""The declared architecture: which module belongs to which layer, and who may import whom.

This module is the single source of truth for the layering. `scripts/architecture_map.py` measures
the real import graph against it, `scripts/check_architecture.py` fails a build when a rule breaks,
and `tests/architecture/test_dependency_rules.py` enforces it in the test suite.

Layers
------
``presentation``
    Talking to a human or a client: the REST/WebSocket API, the web console, the CLI. It translates
    requests into calls on the business layer and formats answers.
``business``
    Natasha's own behaviour: the executive loop, cognition, missions, agents, verification, recovery,
    memory rules, world model, creation workflows, skill lifecycle, tool catalogue, model routing.
``ports``
    The interfaces business code depends on (``natasha.ports``). Adapters implement them.
``data``
    Persistence and storage: SQLite/PostgreSQL stores, repositories, migrations, the vault, the
    filesystem primitives that guard path traversal.
``infrastructure``
    Concrete external systems: model provider adapters, MCP transport, browser/OS control, audio and
    speech engines, observability backends.
``core``
    Configuration, events/audit, security policy, credentials broker, approvals, governance. This is
    the authority layer: nothing may depend on presentation, and it never depends on business.
``composition``
    The runtime wiring (``natasha.runtime``) and the app entry points: the only place allowed to
    import across every layer.

Rules are directional. ``RULES`` maps ``(source_layer, target_layer) -> (allowed, reason)``.
"""

from __future__ import annotations

#: Module prefix -> layer, longest prefix wins. File-level entries override package-level ones.
_PREFIXES: tuple[tuple[str, str], ...] = (
    # ---------------------------------------------------------------- composition
    ("natasha.runtime", "composition"),
    ("apps", "composition"),
    ("natasha.architecture", "core"),
    # ---------------------------------------------------------------- test/support code
    ("tests", "tests"),
    ("scripts", "scripts"),
    ("natasha", "core"),           # package metadata (__init__)
    # ---------------------------------------------------------------- presentation
    ("natasha.api.auth", "data"),          # the auth store; see the refactor report
    ("natasha.api", "presentation"),
    ("natasha.cli", "presentation"),
    ("frontend", "presentation"),
    # ---------------------------------------------------------------- core
    ("natasha.core", "core"),
    ("natasha.events", "core"),
    ("natasha.security", "core"),
    ("natasha.governance.constitution", "data"),
    ("natasha.governance.upgrade_governor", "data"),
    ("natasha.governance", "core"),
    ("natasha.approvals.engine", "data"),   # the store half; the decision rules are core
    ("natasha.approvals", "core"),
    ("natasha.credentials.vault", "data"),
    ("natasha.credentials.manager", "data"),
    ("natasha.credentials", "core"),        # broker: the credential boundary itself
    # ---------------------------------------------------------------- data
    ("natasha.db", "data"),
    ("natasha.memory.models", "ports"),      # entity vocabulary shared by the store and the rules
    ("natasha.memory.embeddings", "ports"),  # the Embedder contract + the dependency-free embedder
    ("natasha.memory.retrieval", "data"),    # scoring/ranking mechanics
    ("natasha.missions.models", "ports"),    # mission/step/artifact entities
    ("natasha.world.models", "ports"),       # world entities
    ("natasha.affect.state", "data"),        # affect table persistence
    ("natasha.memory.store", "data"),
    ("natasha.world.store", "data"),
    ("natasha.missions.store", "data"),
    ("natasha.marketplace.registry", "data"),
    ("natasha.mcp.registry", "data"),
    ("natasha.skills.lifecycle", "business"),   # lifecycle rules: state machine + install policy
    ("natasha.affect.engine", "data"),
    # ---------------------------------------------------------------- infrastructure
    ("natasha.brain.adapters.base", "ports"),   # the provider contract (adapters implement it)
    ("natasha.brain.adapters", "infrastructure"),
    ("natasha.mcp", "infrastructure"),
    ("natasha.integrations", "infrastructure"),
    ("natasha.computer", "infrastructure"),
    ("natasha.voice", "infrastructure"),
    ("natasha.perception", "infrastructure"),
    ("natasha.observability", "infrastructure"),
    ("natasha.creation.generators", "infrastructure"),
    ("natasha.skills.isolation", "infrastructure"),      # the subprocess sandbox
    ("natasha.skills.models", "ports"),                 # SkillRecord/SkillState vocabulary
    ("natasha.skills.store", "data"),                   # the installed-skills table
    ("natasha.marketplace.package", "infrastructure"),   # download/extract/scan pipeline
    ("natasha.perception.documents", "infrastructure"),  # concrete file parsers
    ("natasha.perception.vision", "business"),           # image understanding orchestration
    ("natasha.perception.hearing", "business"),          # speech understanding orchestration
    ("natasha.voice.conversation", "business"),          # voice orchestration (the loop)
    # ---------------------------------------------------------------- business
    ("natasha.tools.base", "ports"),            # the tool contract adapters implement
    ("natasha.verification.checks", "ports"),   # the VerificationCheck contract + concrete checks
    ("natasha.skills.manifest", "ports"),       # the skill contract
    ("natasha.affect", "business"),
    ("natasha.agents", "business"),
    ("natasha.brain", "business"),
    ("natasha.creation", "business"),
    ("natasha.executive", "business"),
    ("natasha.marketplace", "business"),
    ("natasha.memory", "business"),
    ("natasha.missions", "business"),
    ("natasha.recovery", "business"),
    ("natasha.skills", "business"),
    ("natasha.tools", "business"),
    ("natasha.verification", "business"),
    ("natasha.world", "business"),
)

#: The layers that exist, and what each one is for.
LAYERS: dict[str, str] = {
    "presentation": "API, WebSocket, web console, CLI",
    "business": "executive loop, cognition, missions, agents, verification, memory rules",
    "ports": "interfaces and shared domain types business code depends on",
    "data": "stores, repositories, migrations, the vault",
    "infrastructure": "provider/OS/audio adapters and other concrete external systems",
    "core": "config, events, security, credentials, approvals, governance (the authority layer)",
    "composition": "runtime wiring and process entry points",
    "scripts": "repository build/audit tooling",
    "tests": "the test suite",
}

#: (source, target) -> (allowed, reason). Every pair that matters is listed; pairs absent from the
#: table are denied by default so a new dependency has to be declared deliberately.
RULES: dict[tuple[str, str], tuple[bool, str]] = {
    ("core", "data"): (True, "core persists its own authority state "
                             "(event log, approvals, constitution, vault, auth)"),
    ("composition", "composition"): (True, "wiring may use anything"),
    ("composition", "presentation"): (True, "entry points serve the API/UI"),
    ("composition", "business"): (True, "the runtime wires business subsystems"),
    ("composition", "ports"): (True, "the runtime binds adapters to ports"),
    ("composition", "data"): (True, "the runtime opens stores"),
    ("composition", "infrastructure"): (True, "the runtime builds adapters"),
    ("composition", "core"): (True, "the runtime applies security/governance"),

    ("presentation", "presentation"): (True, "within the layer"),
    ("presentation", "business"): (True, "translation into domain calls"),
    ("presentation", "ports"): (True, "the API depends on interfaces, not adapters"),
    ("presentation", "core"): (True, "authentication, approvals, audit"),
    ("presentation", "data"): (False, "presentation must not reach a database or store directly"),
    ("presentation", "infrastructure"): (False, "presentation must not use provider/OS adapters"),

    ("business", "business"): (True, "within the layer"),
    ("business", "ports"): (True, "depend on interfaces (dependency inversion)"),
    ("business", "core"): (True, "policy, approvals, audit, governance"),
    ("business", "data"): (False, "business must go through a port/repository, not a store"),
    ("business", "infrastructure"): (False, "business must not import an adapter directly"),

    ("ports", "ports"): (True, "protocols may compose"),
    ("ports", "core"): (True, "interfaces may reference domain errors/types"),
    ("ports", "business"): (False, "interfaces must not depend on behaviour"),
    ("ports", "data"): (False, "interfaces must not depend on storage"),
    ("ports", "infrastructure"): (False, "interfaces must not depend on adapters"),
    ("ports", "presentation"): (False, "interfaces must not depend on clients"),

    ("data", "data"): (True, "within the layer"),
    ("data", "core"): (True, "stores honour policy and write audit events"),
    ("data", "ports"): (True, "a repository may implement a port"),
    ("data", "business"): (False, "data must not contain business decisions"),
    ("data", "infrastructure"): (False, "data must not depend on adapters"),
    ("data", "presentation"): (False, "data must not depend on clients"),

    ("infrastructure", "infrastructure"): (True, "within the layer"),
    ("infrastructure", "ports"): (True, "an adapter implements a port"),
    ("infrastructure", "core"): (True, "adapters are audited and policy-checked"),
    ("infrastructure", "data"): (True, "adapters read configuration/state through stores"),
    ("infrastructure", "business"): (False, "adapters must not depend on the domain"),
    ("infrastructure", "presentation"): (False, "adapters must not depend on clients"),

    ("core", "core"): (True, "within the layer"),
    ("scripts", "scripts"): (True, "build scripts are self-contained"),
    ("scripts", "core"): (True, "build scripts read the architecture/governance modules"),
    ("scripts", "data"): (True, "migration generation reads the schema"),
    ("scripts", "business"): (True, "generators understand the domain"),
    ("scripts", "ports"): (True, "scripts may rely on interfaces"),
    ("scripts", "infrastructure"): (True, "scripts may probe adapters"),
    ("scripts", "presentation"): (True, "scripts may drive the API"),
    ("scripts", "composition"): (True, "scripts may wire a runtime"),
    ("composition", "scripts"): (False, "the runtime must not import build tooling"),
    ("composition", "tests"): (False, "the runtime must not import the test suite"),
    ("tests", "tests"): (True, "tests may use anything"),
    ("tests", "presentation"): (True, "end-to-end tests drive the API"),
    ("tests", "business"): (True, "tests exercise the executive"),
    ("tests", "ports"): (True, "tests implement the interfaces"),
    ("tests", "data"): (True, "tests inspect stores directly"),
    ("tests", "infrastructure"): (True, "tests drive adapters"),
    ("tests", "core"): (True, "tests verify policy/governance"),
    ("tests", "composition"): (True, "tests build the runtime"),
    ("tests", "scripts"): (True, "tests use build tooling"),
    ("core", "ports"): (True, "core may define or use interfaces"),
    ("core", "business"): (False, "core must not depend on behaviour"),
    ("core", "infrastructure"): (False, "core must not depend on adapters"),
    ("core", "presentation"): (False, "core must not depend on clients"),
}

#: Imports that are forbidden in any business module, whatever layer they resolve to.
BUSINESS_FORBIDDEN_MODULES: tuple[str, ...] = (
    "fastapi", "starlette", "uvicorn", "pydantic_settings",
    "sqlite3", "psycopg", "sqlalchemy",
    "playwright", "selenium", "pynput", "pyautogui",
    "openai", "anthropic", "google.generativeai", "mistralai",
    "frontend", "tauri",
)

#: Same idea for the presentation layer: it must not own a database or a provider SDK.
PRESENTATION_FORBIDDEN_MODULES: tuple[str, ...] = (
    "sqlite3", "psycopg", "sqlalchemy", "playwright", "pynput", "pyautogui", "tauri",
    "openai", "anthropic",
)


#: module -> (target layer, reason). Accepted edges. Each one is a known structural compromise with
#: the direction of the fix recorded in ARCHITECTURE_REFACTOR_REPORT.md. The checker fails on any
#: violation *not* listed here, and `--strict` fails on these too.
MODULE_EXCEPTIONS: tuple[tuple[str, str, str], ...] = (
    ("natasha.api.app", "composition",
     "the API entry point builds the composition root in its lifespan"),
    ("natasha.cli.main", "composition",
     "the CLI is an entry point: it composes a runtime per command"),
    ("natasha.cli.main", "data",
     "CLI commands drive stores directly (migrate, mcp, governance, auth); an application-service "
     "layer is the planned fix"),
    ("natasha.api.routers.governance", "data",
     "reads constitution state through its store module; the governance service is the planned fix"),
    ("natasha.api.routers.mcp", "data",
     "lists configured MCP servers; an MCP service facade is the planned fix"),
    ("natasha.api.routers.observability", "infrastructure",
     "the console exposes the observability adapter's metrics/traces"),
    ("natasha.api.routers.system", "infrastructure",
     "the doctor endpoint reports adapter health"),
    ("natasha.api.routers.vision", "infrastructure",
     "vision endpoints drive the perception engines"),
    ("natasha.skills.lifecycle", "infrastructure",
     "the lifecycle orchestrates the scanner and the sandbox; splitting them behind ports is planned"),
    ("natasha.tools.builtin", "infrastructure",
     "the document tool calls the parsers; injecting a DocumentReader port through ToolContext is "
     "planned"),
    ("natasha.api.app", "data",
     "the app builds the auth store for the login middleware; an auth service port is planned"),
    ("natasha.api.routers.auth", "data",
     "the login/logout endpoints are the auth store's HTTP face; an auth service port is planned"),
    ("natasha.brain.registry", "infrastructure",
     "the provider registry instantiates adapters; the adapter factory belongs to the composition "
     "root (planned)"),
    ("natasha.marketplace.installer", "infrastructure",
     "the installer orchestrates the package pipeline (download/extract/scan); these steps will be "
     "injected as ports"),
    ("natasha.marketplace.installer", "data",
     "the installer records the install in the marketplace registry; a repository port is planned"),
    ("natasha.mcp.registry", "infrastructure",
     "the MCP registry opens the client transport for a configured server; the transport will be "
     "injected"),
    ("natasha.memory.manager", "data",
     "the memory facade drives its store; a MemoryRepository port is planned"),
    ("natasha.missions.engine", "data",
     "the mission engine drives its store; a MissionRepository port is planned"),
    ("natasha.skills.lifecycle", "data",
     "the lifecycle drives the skills store; a SkillRepository port is planned"),
    ("natasha.skills.runtime", "infrastructure",
     "the skill runtime runs skills through the sandbox; a SkillSandbox port is planned"),
    ("natasha.memory.embeddings", "business",
     "the provider-backed embedder is built by a lazy factory; injecting the brain is the planned "
     "fix"),
)


def exception_for(module: str, target_layer: str) -> str:
    """The declared reason this edge is accepted, or an empty string."""
    # A submodule inherits its parent's exception (natasha.cli.main covers natasha.cli.main.*).
    for prefix, layer, reason in MODULE_EXCEPTIONS:
        if layer == target_layer and (module == prefix or module.startswith(prefix + ".")):
            return reason
    return ""


def layer_of(module: str) -> str:
    """Classify a dotted module path. Unmapped modules report ``unclassified``."""
    best = ""
    layer = "unclassified"
    for prefix, name in _PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            if len(prefix) >= len(best):
                best, layer = prefix, name
    return layer


def rule_for(source_layer: str, target_layer: str) -> tuple[bool, str]:
    """Is an import from *source_layer* to *target_layer* allowed?"""
    if source_layer == "unclassified" or target_layer == "unclassified":
        return False, "unclassified module: add it to natasha.architecture._PREFIXES"
    return RULES.get((source_layer, target_layer), (False, "no rule: denied by default"))


def forbidden_module(module: str, layer: str) -> str:
    """Return the forbidden root module if *module* may not be imported by this layer."""
    root = module.split(".")[0]
    if layer == "business" and root in BUSINESS_FORBIDDEN_MODULES:
        return root
    if layer == "presentation" and root in PRESENTATION_FORBIDDEN_MODULES:
        return root
    return ""


def layer_table() -> dict[str, list[str]]:
    """The prefix table grouped by layer, for documentation and reports."""
    grouped: dict[str, list[str]] = {}
    for prefix, layer in _PREFIXES:
        grouped.setdefault(layer, []).append(prefix)
    return {layer: sorted(prefixes) for layer, prefixes in sorted(grouped.items())}


# --------------------------------------------------------------------------------------------
# The import scanner
#
# The rules above are declarations; this part measures the repository against them. It is pure
# stdlib and imports nothing from the project, so it can be used by the CLI (scripts/), by the test
# suite (tests/architecture/) and by a future CI job without creating a dependency of its own.
# --------------------------------------------------------------------------------------------

from dataclasses import dataclass, field  # noqa: E402 - kept next to the scanner for readability
from pathlib import Path  # noqa: E402
import ast  # noqa: E402

#: Repository root, derived from this file: backend/natasha/architecture.py -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories that are scanned, and the importer each one stands for.
SOURCE_ROOTS: dict[str, str] = {
    "backend/natasha": "runtime",
    "apps": "apps",
    "packages": "packages",
    "scripts": "scripts",
    "tests": "tests",
}

#: Root packages that count as project-internal; anything else is a third-party import.
INTERNAL_ROOTS = ("natasha", "apps", "packages")


@dataclass
class Import:
    """One import statement that resolves inside the repository."""
    source: str
    target: str
    line: int
    lazy: bool = False


@dataclass
class Edge(Import):
    """An import plus the layers it connects."""
    source_layer: str = ""
    target_layer: str = ""
    file: str = ""

    @property
    def exception(self) -> str:
        """A declared, reasoned exception - empty when this edge is a real violation."""
        return exception_for(self.source, self.target_layer)

    @property
    def allowed(self) -> bool:
        allowed, _ = rule_for(self.source_layer, self.target_layer)
        return allowed


@dataclass
class Report:
    """The measured import graph."""
    edges: list[Edge] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)

    def violations(self) -> list[Edge]:
        """Cross-layer imports that break a rule and are not a declared exception."""
        return [edge for edge in self.edges
                if not edge.allowed and not edge.exception
                and edge.source_layer != edge.target_layer]

    def exceptions(self) -> list[Edge]:
        """Imports that break a rule but are accepted for now, each with a declared reason."""
        return [edge for edge in self.edges if not edge.allowed and edge.exception]

    def layer_matrix(self) -> dict[str, dict[str, dict[str, int]]]:
        """source layer -> target layer -> {"ok"|"exception"|"violation": count}."""
        matrix: dict[str, dict[str, dict[str, int]]] = {}
        for edge in self.edges:
            if edge.source_layer == edge.target_layer:
                continue
            kind = "ok" if edge.allowed else ("exception" if edge.exception else "violation")
            row = matrix.setdefault(edge.source_layer, {}).setdefault(
                edge.target_layer, {"ok": 0, "exception": 0, "violation": 0})
            row[kind] += 1
        return matrix

    def stale_exceptions(self) -> list[tuple[str, str]]:
        """Declared exceptions that no longer match any import (the ratchet is loose)."""
        live = {(edge.source, edge.target_layer) for edge in self.exceptions()}
        return [(prefix, layer) for prefix, layer, _ in MODULE_EXCEPTIONS
                if not any(source == prefix or source.startswith(prefix + ".")
                           for source, target_layer in live if target_layer == layer)]


def module_name_for(path: Path, root: Path | None = None) -> str:
    """Map a file to the importable module name used to classify it."""
    root = root or REPO_ROOT
    relative = path.resolve().relative_to(root.resolve())
    if relative.parts and relative.parts[0] == "backend":
        relative = Path(*relative.parts[1:])
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def collect_imports(tree: ast.AST) -> list[tuple[str, int, bool]]:
    """Every import in a syntax tree, with its line and whether it runs lazily.

    Module-level imports run on import; imports inside a function body only run when the function is
    called (class bodies still run eagerly). The distinction matters for the report: a lazy import
    is still a dependency, but it is not a startup-order problem.
    """
    found: list[tuple[str, int, bool]] = []

    def walk(node: ast.AST, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                found.extend((alias.name, child.lineno, lazy) for alias in child.names)
                walk(child, lazy)
            elif isinstance(child, ast.ImportFrom):
                found.append((("." * child.level) + (child.module or ""), child.lineno, lazy))
                walk(child, lazy)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, True)
            else:
                walk(child, lazy)

    walk(tree, False)
    return [(name, line, lazy) for name, line, lazy in found if name]


def import_candidates(name: str, module: str, *, is_package: bool = False,
                      known: set[str] | None = None) -> list[str]:
    """Every module a relative import could mean, best guess first.

    ``__init__.py`` files make ``from .x import y`` ambiguous on paper: the package itself is a
    candidate base (``natasha.skills`` + ``.runtime`` -> ``natasha.skills.runtime``) and so is its
    parent (``natasha.brain.adapters`` + ``.ollama`` -> ``natasha.brain.ollama``, because the package
    re-exports a module that lives one level up). Both are legal Python; only the filesystem knows
    which one is meant, so when *known* holds the real module set the candidates are filtered by it.
    """
    if not name.startswith("."):
        return [name]
    levels = len(name) - len(name.lstrip("."))
    tail = name.lstrip(".")
    parts = module.split(".") if module else []
    bases = [parts if is_package else parts[:-1]]
    if is_package:
        bases.append(parts[:-1])
    candidates: list[str] = []
    for base in bases:
        package = list(base)
        for _ in range(max(0, levels - 1)):
            if package:
                package.pop()
        candidate = ".".join(package + ([tail] if tail else []))
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    if known:
        existing = [candidate for candidate in candidates if candidate in known]
        if existing:
            return existing
    return candidates or [name]


def resolve_import(name: str, module: str, *, is_package: bool = False,
                   known: set[str] | None = None) -> str:
    """Resolve a possibly relative import (``from ..brain import base``) to an absolute name."""
    return import_candidates(name, module, is_package=is_package, known=known)[0]


def scan_file(path: Path, *, root: Path | None = None, known: set[str] | None = None) -> Report:
    """Scan a single Python file."""
    root = root or REPO_ROOT
    report = Report()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, UnicodeDecodeError):
        return report
    module = module_name_for(path, root)
    is_package = path.name == "__init__.py"
    report.files[module] = layer_of(module)
    for imported, line, lazy in collect_imports(tree):
        resolved = resolve_import(imported, module, is_package=is_package, known=known)
        if resolved.split(".")[0] not in INTERNAL_ROOTS:
            continue
        if resolved == module or resolved.startswith(module + "."):
            continue                                    # self-reference
        report.edges.append(Edge(
            source=module, target=resolved, line=line, lazy=lazy,
            source_layer=layer_of(module), target_layer=layer_of(resolved),
            file=str(path.resolve().relative_to(root.resolve())),
        ))
    return report


def source_files(root: Path | str | None = None) -> list[Path]:
    """Every Python file the scanner looks at, in a stable order."""
    root = Path(root) if root else REPO_ROOT
    files: list[Path] = []
    for directory in SOURCE_ROOTS:
        base = root / directory
        if not base.is_dir():
            continue
        files.extend(path for path in sorted(base.rglob("*.py")) if "__pycache__" not in path.parts)
    return files


def scan_tree(root: Path | str | None = None) -> Report:
    """Scan every source root under *root* and return the combined import graph."""
    root = Path(root) if root else REPO_ROOT
    files = source_files(root)
    known = {module_name_for(path, root) for path in files}
    report = Report()
    for path in files:
        found = scan_file(path, root=root, known=known)
        report.files.update(found.files)
        report.edges.extend(found.edges)
    return report


def forbidden_in_layer(module: str, layer: str) -> str:
    """Alias kept for symmetry with :func:`forbidden_module`."""
    return forbidden_module(module, layer)


__all__ = [
    "BUSINESS_FORBIDDEN_MODULES", "INTERNAL_ROOTS", "LAYERS", "MODULE_EXCEPTIONS",
    "PRESENTATION_FORBIDDEN_MODULES", "REPO_ROOT", "RULES", "SOURCE_ROOTS", "Edge", "Import",
    "Report", "collect_imports", "exception_for", "forbidden_module", "import_candidates",
    "layer_of", "layer_table", "module_name_for", "resolve_import", "rule_for", "scan_file",
    "scan_tree", "source_files",
]
