"""
tests/test_layer_fences.py — Architectural import-fence gate.

`backend/core/` (business logic, 2,259 LOC) and `backend/routes/` (HTTP
serialization, 5,061 LOC) are NOT duplicates — they are layered: routes
sit on top of core and consume the public ``core.api.API`` facade.
Reversing that dependency (or reaching across into the other layer's
private ``_*`` helpers) breaks the layering and re-introduces the
spaghetti the split was designed to prevent.

This test walks every ``.py`` under ``backend/core/`` and ``backend/routes/``,
parses the AST, and asserts:

  1. Nothing under ``backend/core/`` imports from ``routes`` at all
     (any flavor: ``import routes.x``, ``from routes import y``,
     ``from routes.x import y``). The lower layer never reaches up.

  2. Nothing under ``backend/routes/`` imports ``core.api._*`` private
     submodules. Route modules consume only the public facade
     ``core.api.API``; pulling in ``core.api._base`` etc. would let a
     route subclass internal helpers that are not part of the public
     contract.

Tests are excluded — fixtures legitimately reach into private
module-level state (e.g. ``system_routes._bundled_download_running``)
and that pragmatic licence stops at the test boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
CORE_DIR = BACKEND_DIR / "core"
ROUTES_DIR = BACKEND_DIR / "routes"


def _iter_py_files(root: Path) -> list[Path]:
    """Yield every ``.py`` under ``root`` excluding ``__pycache__``."""
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _resolve_module(node: ast.ImportFrom, source_file: Path) -> str | None:
    """Return the absolute dotted module a ``from X import …`` references.

    For relative imports (``from .foo import bar``), walks up ``level``
    package directories from ``source_file``'s parent and prepends the
    path back to ``backend/`` so the returned name is always rooted at the
    same level as ``routes`` / ``core``. Returns None when the relative
    walk escapes the backend tree.
    """
    if node.level == 0:
        return node.module
    # source_file is e.g. backend/core/api/agents.py. parent is its package
    # directory; step `level - 1` more up for each extra dot.
    package_dir = source_file.parent
    for _ in range(node.level - 1):
        package_dir = package_dir.parent
    try:
        rel = package_dir.relative_to(BACKEND_DIR)
    except ValueError:
        return None
    parts = list(rel.parts)
    if node.module:
        parts.append(node.module)
    return ".".join(parts) if parts else None


def _imported_modules(source_file: Path) -> list[str]:
    """Return every absolute dotted module name imported by ``source_file``.

    For ``from X import a, b``, expands to one entry per imported name as
    ``X.a`` / ``X.b``. This lets callers detect ``from core.api import _Foo``
    (private-name access through a public module) which a module-level
    check alone would miss.
    """
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_module(node, source_file)
            if resolved is None:
                continue
            names.append(resolved)
            # Expand the imported names so private-name access through a
            # public module surfaces. ``from core.api import API`` is fine;
            # ``from core.api import _Foo`` is a fence violation.
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.append(f"{resolved}.{alias.name}")
    return names


# ── Fence 1: core/ MUST NOT depend on routes/ ───────────────────────────────

@pytest.mark.parametrize(
    "source_file",
    _iter_py_files(CORE_DIR),
    ids=lambda p: str(p.relative_to(BACKEND_DIR)).replace("\\", "/"),
)
def test_core_does_not_import_routes(source_file: Path) -> None:
    """Files under ``backend/core/`` must not depend on ``routes``.

    Routes is the upper layer. If core ever needs something that today
    lives in routes, the right move is to lift the shared logic down
    into core (or into a neutral helper module), not to import upward.
    """
    bad = [m for m in _imported_modules(source_file) if m == "routes" or m.startswith("routes.")]
    assert not bad, (
        f"{source_file.relative_to(BACKEND_DIR)} imports from the routes layer: {bad}. "
        f"core/ is the lower layer and must not depend on routes/. Lift the shared "
        f"logic into core/ (or a neutral helper) instead of importing upward."
    )


# ── Fence 2: routes/ MUST NOT reach into core.api._* private helpers ────────

@pytest.mark.parametrize(
    "source_file",
    _iter_py_files(ROUTES_DIR),
    ids=lambda p: str(p.relative_to(BACKEND_DIR)).replace("\\", "/"),
)
def test_routes_does_not_import_core_api_private(source_file: Path) -> None:
    """Route modules consume only the public ``core.api.API`` facade.

    Importing ``core.api._base`` (or any other ``core.api._*`` private
    module) couples routes to an internal contract that the core layer
    has explicitly marked unstable. Use the facade.
    """
    bad: list[str] = []
    for m in _imported_modules(source_file):
        # Match e.g. ``core.api._base`` or ``core.api._anything.sub`` —
        # the leading ``_`` on the segment right after ``core.api`` is
        # what makes it private. ``core.api.agents``, ``core.api.chat``
        # etc. are public and allowed.
        if m == "core.api":
            continue
        if m.startswith("core.api."):
            segment = m[len("core.api.") :].split(".", 1)[0]
            if segment.startswith("_"):
                bad.append(m)
    assert not bad, (
        f"{source_file.relative_to(BACKEND_DIR)} imports core.api private modules: {bad}. "
        f"Route modules must consume only the public core.api.API facade — internal "
        f"core.api._* helpers are not part of the stable contract."
    )
