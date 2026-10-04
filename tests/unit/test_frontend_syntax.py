"""The console has no build step, so nothing else checks that it parses.

`frontend/` is served to the browser exactly as it is on disk: plain ES modules, no bundler, no
transpiler, no lint on save. A stray brace or a duplicated declaration therefore reaches the user as a
blank screen. This test runs the JavaScript engine over every module (`node --check`), verifies the
import graph the browser will follow, and checks the CSS class names the views rely on still exist in
the stylesheet - which is the closest thing this project has to a frontend build.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
JS_ROOT = FRONTEND / "assets" / "js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _js_files() -> list[Path]:
    return sorted(path for path in JS_ROOT.rglob("*.js"))


def test_there_is_something_to_check():
    files = _js_files()
    assert len(files) >= 20, f"only {len(files)} modules found; the checks below would be vacuous"
    assert (JS_ROOT / "views").is_dir()


def test_every_module_parses(tmp_path):
    """`node --check` needs a module extension; .js is source-type-script by default."""
    failures: list[str] = []
    for path in _js_files():
        copy = tmp_path / (path.stem + ".mjs")
        copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        result = subprocess.run([NODE, "--check", str(copy)], capture_output=True, text=True)
        if result.returncode != 0:
            failures.append(f"{path.relative_to(REPO_ROOT)}:\n{result.stderr.strip()}")
    assert not failures, "these modules do not parse:\n" + "\n".join(failures)


def test_every_import_points_at_a_file_that_exists():
    """The browser resolves `./x.js` relative to the module: a typo is a 404 at runtime."""
    problems: list[str] = []
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"""\bfrom\s+["']([^"']+)["']""", text):
            if not target.startswith("."):
                problems.append(f"{path.relative_to(REPO_ROOT)}: bare import {target!r} "
                                "(the console has no bundler and no node_modules)")
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.is_file():
                problems.append(f"{path.relative_to(REPO_ROOT)}: {target} does not exist")
    assert not problems, "\n".join(problems)


def test_every_view_is_reachable_from_the_entry_module():
    """A view that no module imports is a screen nobody can open (app.js registers them)."""
    views = {path.stem for path in (JS_ROOT / "views").glob("*.js")}
    graph = "\n".join(path.read_text(encoding="utf-8") for path in _js_files())
    unreachable = sorted(name for name in views if f"views/{name}.js" not in graph)
    assert not unreachable, (
        f"views exist but no module imports them (dead screens): {unreachable}")


def test_the_stylesheet_defines_the_classes_the_views_use():
    """`assets/app.css` is the visual contract, and there is no CSS tooling to catch drift."""
    css = (FRONTEND / "assets" / "app.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    assert len(defined) > 50, f"only {len(defined)} classes found in app.css"

    used: dict[str, set[str]] = {}
    for path in _js_files():
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r"""class(?:Name)?\s*[:=]\s*["']([^"']+)["']""", text):
            for token in name.split():
                used.setdefault(token, set()).add(path.name)
        for name in re.findall(r"""classList\.(?:add|toggle)\(\s*["']([^"']+)["']""", text):
            used.setdefault(name, set()).add(path.name)

    missing = {name: sorted(files) for name, files in used.items() if name not in defined}
    assert not missing, f"classes used in JavaScript but absent from app.css: {missing}"


def test_the_shell_loads_the_entry_module_and_the_manifest():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'type="module"' in html, "the console is an ES module app"
    assert "assets/js/app.js" in html
    assert 'rel="manifest"' in html, "the PWA manifest must be linked"
    assert (FRONTEND / "manifest.webmanifest").is_file()
    assert (FRONTEND / "assets" / "logo.svg").is_file()
