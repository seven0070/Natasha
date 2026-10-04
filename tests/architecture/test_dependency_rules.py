"""The architecture, enforced by the test suite.

These tests run the same scanner as ``scripts/architecture_map.py`` over the real repository, so a
pull request that adds an undeclared cross-layer import fails here even if nobody runs the script.
The declared exceptions in :data:`natasha.architecture.MODULE_EXCEPTIONS` are asserted to be *live*
(a stale exception is a lie about the state of the code) and to carry a real reason, so the exception
list can only ever shrink honestly, never grow quietly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from natasha.architecture import (
    BUSINESS_FORBIDDEN_MODULES,
    LAYERS,
    MODULE_EXCEPTIONS,
    PRESENTATION_FORBIDDEN_MODULES,
    RULES,
    collect_imports,
    forbidden_module,
    layer_of,
    module_name_for,
    rule_for,
    scan_tree,
    source_files,
)

# Scanned once: the repository does not change while the suite runs, and scanning 200 files twice
# would only slow the suite down.
REPORT = scan_tree()
MODULE_LAYERS = dict(REPORT.files)


def _edges(edges) -> str:
    return "\n".join(f"{edge.file}:{edge.line} {edge.source_layer}->{edge.target_layer} "
                     f"{edge.source} -> {edge.target}" for edge in edges)


# ------------------------------------------------------------------ the rules themselves


def test_every_layer_that_exists_is_declared_and_has_a_self_rule():
    assert LAYERS, "the layer table must not be empty"
    for layer in LAYERS:
        assert (layer, layer) in RULES, f"{layer} has no within-the-layer rule"
        assert RULES[(layer, layer)][0] is True, f"{layer} may not import itself"


def test_no_rule_references_an_undeclared_layer():
    for source, target in RULES:
        assert source in LAYERS, f"unknown source layer in a rule: {source}"
        assert target in LAYERS, f"unknown target layer in a rule: {target}"


def test_unknown_pairs_are_denied_by_default():
    allowed, reason = rule_for("presentation", "no-such-layer")
    assert allowed is False and reason
    allowed, reason = rule_for("composition", "ports")
    assert allowed is True and reason


def test_the_authority_layer_cannot_depend_on_behaviour_or_clients():
    for target in ("business", "presentation", "infrastructure"):
        allowed, reason = rule_for("core", target)
        assert allowed is False, f"core must not import {target}"
        assert reason


RUNTIME_LAYERS = ("presentation", "business", "ports", "data", "infrastructure", "core")


def test_composition_is_the_only_layer_that_sees_everything():
    for layer in RUNTIME_LAYERS:
        assert rule_for("composition", layer)[0] is True, f"composition cannot import {layer}"
    # ... and no runtime layer may import composition (tests and build tooling may, deliberately).
    for layer in LAYERS:
        if layer in ("composition", "tests", "scripts"):
            continue
        assert rule_for(layer, "composition")[0] is False, f"{layer} must not import composition"


def test_presentation_may_not_reach_a_store_or_an_adapter():
    for target in ("data", "infrastructure"):
        allowed, reason = rule_for("presentation", target)
        assert allowed is False
        assert reason


# --------------------------------------------------------------- the measured repository


def test_every_module_declares_a_layer():
    unclassified = sorted(module for module, layer in MODULE_LAYERS.items() if layer == "unclassified")
    assert not unclassified, ("these modules are not covered by the layer table; add them to "
                              f"natasha.architecture._PREFIXES: {unclassified}")


def test_the_repository_has_no_undeclared_layering_violations():
    violations = REPORT.violations()
    assert not violations, ("undeclared cross-layer imports (declare a rule, move the code, or add a "
                            f"reasoned exception):\n{_edges(violations)}")


@pytest.mark.parametrize("module,expected", [
    ("natasha.security.policy", "core"),
    ("natasha.approvals.engine", "data"),
    ("natasha.executive.orchestrator", "business"),
    ("natasha.api.routers.chat", "presentation"),
    ("natasha.cli.main", "presentation"),
    ("natasha.runtime", "composition"),
    ("natasha.tools.base", "ports"),
    ("natasha.brain.adapters.ollama", "infrastructure"),
    ("natasha.memory.store", "data"),
    ("natasha.memory.models", "ports"),
    ("tests.architecture.test_dependency_rules", "tests"),
    ("scripts.architecture_map", "scripts"),
])
def test_important_modules_sit_in_the_layer_they_claim(module, expected):
    assert layer_of(module) == expected


def test_the_scan_actually_saw_the_repository():
    # A scanner that silently finds nothing would make every test above pass vacuously.
    assert len(REPORT.files) > 150, f"only {len(REPORT.files)} modules scanned"
    assert len(REPORT.edges) > 300, f"only {len(REPORT.edges)} internal imports found"
    assert any(edge.source.startswith("natasha.executive") for edge in REPORT.edges)


def test_relative_imports_resolve_to_real_modules():
    known = set(MODULE_LAYERS)
    for edge in REPORT.edges:
        assert edge.target in known or any(
            module.startswith(edge.target + ".") for module in known
        ), f"unresolvable target {edge.target} (from {edge.source}:{edge.line})"


# ------------------------------------------------------------------- declared exceptions


def test_every_declared_exception_is_live_and_carries_a_reason():
    stale = REPORT.stale_exceptions()
    assert not stale, ("these exceptions match no import - remove them from MODULE_EXCEPTIONS "
                       f"instead of leaving the ratchet loose: {stale}")
    assert MODULE_EXCEPTIONS, "there must be an explicit list of accepted debt"
    for prefix, layer, reason in MODULE_EXCEPTIONS:
        assert layer in LAYERS, f"exception for {prefix} names an unknown layer {layer}"
        assert len(reason) > 30, f"exception for {prefix} -> {layer} needs a real reason"
    live = {(edge.source, edge.target_layer) for edge in REPORT.exceptions()}
    for prefix, layer, _reason in MODULE_EXCEPTIONS:
        assert any(source == prefix or source.startswith(prefix + ".")
                   for source, target in live if target == layer), (
            f"exception {prefix} -> {layer} is stale")


def test_declared_exceptions_are_a_small_minority_of_the_graph():
    # The debt may not quietly become the architecture: at most 10% of internal imports.
    exceptions = REPORT.exceptions()
    assert len(exceptions) <= max(30, len(REPORT.edges) // 10), (
        f"{len(exceptions)} of {len(REPORT.edges)} imports are exceptions")


# --------------------------------------------------------------- forbidden dependencies


def _third_party_imports_by_module() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        module = module_name_for(path)
        roots: set[str] = set()
        for name, _line, _lazy in collect_imports(tree):
            if not name.startswith("."):
                roots.add(name.split(".")[0])
        if roots:
            found[module] = roots
    return found


def test_business_code_never_imports_a_web_framework_store_or_provider_sdk():
    offenders: list[str] = []
    for module, roots in _third_party_imports_by_module().items():
        if layer_of(module) != "business":
            continue
        for root in roots:
            if forbidden_module(root, "business"):
                offenders.append(f"{module} imports {root}")
    assert not offenders, f"business modules depend on the outer world: {offenders}"


def test_presentation_never_imports_a_provider_sdk_or_opens_a_database():
    offenders: list[str] = []
    for module, roots in _third_party_imports_by_module().items():
        if layer_of(module) != "presentation":
            continue
        for root in roots:
            if forbidden_module(root, "presentation"):
                offenders.append(f"{module} imports {root}")
    assert not offenders, f"presentation owns infrastructure: {offenders}"


def test_the_forbidden_lists_do_not_contain_the_standard_library_by_accident():
    # sqlite3 is deliberately on the list (stores are the data layer's job), so the check is about
    # the lists being real rather than empty.
    assert "fastapi" in BUSINESS_FORBIDDEN_MODULES
    assert "sqlite3" in PRESENTATION_FORBIDDEN_MODULES


def test_no_module_uses_an_absolute_path_to_import_project_code():
    # `from backend.natasha.x import y` works only from the repository root and breaks packaging.
    offenders: list[str] = []
    for path in source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            for prefix in ("from backend.", "import backend.", "from backend import"):
                if stripped.startswith(prefix):
                    offenders.append(f"{path.relative_to(Path.cwd())}:{line_no}")
    assert not offenders, f"imports must go through the package, not the checkout layout: {offenders}"
