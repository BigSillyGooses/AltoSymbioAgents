"""tests/test_design_assets.py — Design Studio asset loader.

Runs against the REAL vendored Open Design catalog under
core/assets/design/ (no synthetic fixtures), so a regression in the parser
or a bad vendor copy is caught directly. linear-app is used as the stable
anchor because its DESIGN.md documents well-known, unlikely-to-drift tokens
(the #5e6ad2 brand indigo, the "Productivity & SaaS" category).
"""

from __future__ import annotations

from core import paths
from services import design_assets


def _root():
    return paths.design_assets_dir()


class TestListDesignSystems:
    def test_catalog_is_present_and_nontrivial(self):
        systems = design_assets.list_design_systems(_root())
        assert len(systems) > 100  # 137 vendored at time of writing.
        ids = {s["id"] for s in systems}
        assert {"linear-app", "apple", "notion"} <= ids

    def test_linear_descriptor_real_values(self):
        systems = design_assets.list_design_systems(_root())
        linear = next(s for s in systems if s["id"] == "linear-app")
        # Title boilerplate ("Design System Inspired by …") is stripped.
        assert linear["title"] == "Linear"
        assert linear["category"] == "Productivity & SaaS"
        assert linear["surface"] == "web"
        # The brand indigo documented throughout the real DESIGN.md.
        assert "#5e6ad2" in linear["swatches"]
        assert len(linear["swatches"]) == 4
        assert linear["summary"]  # non-empty first paragraph
        assert "body" in linear and linear["body"].startswith("# Design System")

    def test_missing_directory_returns_empty(self, tmp_path):
        # A root with no design-systems/ subdir degrades to [].
        assert design_assets.list_design_systems(tmp_path) == []


class TestReadDesignSystem:
    def test_reads_real_body(self):
        body = design_assets.read_design_system(_root(), "linear-app")
        assert body is not None
        assert body.startswith("# Design System Inspired by Linear")

    def test_unknown_id_returns_none(self):
        assert design_assets.read_design_system(_root(), "does-not-exist") is None

    def test_path_traversal_is_rejected(self):
        # A crafted id must never escape the vendored tree.
        for bad in ["../../README", "..", "foo/bar", "a\\b", "."]:
            assert design_assets.read_design_system(_root(), bad) is None


class TestReadCraft:
    def test_default_set_concatenates_real_rules(self):
        craft = design_assets.read_craft(_root())
        assert craft
        # The anti-AI-slop rules are the load-bearing craft file.
        assert "anti-ai-slop" in craft.lower() or "Anti-AI-slop" in craft
        # README is excluded from the default rule set.
        assert "single source of truth" not in craft.lower()

    def test_explicit_slug_selection(self):
        craft = design_assets.read_craft(_root(), ["color"])
        assert craft  # color.md is real and non-empty
