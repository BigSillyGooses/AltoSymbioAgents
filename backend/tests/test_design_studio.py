"""tests/test_design_studio.py — Design Studio prompt composition.

Asserts the ordering contract (directive → DESIGN.md → craft) and the
flag-gating of build_design_block, composing over the REAL vendored
linear-app DESIGN.md + craft rules (no synthetic fixtures).
"""

from __future__ import annotations

from core import paths
from services import design_assets, design_studio


class _FakeSettings:
    """Minimal Settings stand-in exposing only .get(key, default)."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def _real_inputs():
    root = paths.design_assets_dir()
    return (
        design_assets.read_design_system(root, "linear-app"),
        design_assets.read_craft(root),
    )


class TestComposeDesignPrompt:
    def test_ordering_directive_then_design_then_craft(self):
        body, craft = _real_inputs()
        prompt = design_studio.compose_design_prompt(
            design_system_body=body,
            design_system_title="Linear",
            craft_body=craft,
        )
        i_contract = prompt.find("Output contract")
        i_design = prompt.find("Active design system")
        i_craft = prompt.find("Craft references")
        assert -1 < i_contract < i_design < i_craft

    def test_artifact_contract_and_brand_tokens_present(self):
        body, craft = _real_inputs()
        prompt = design_studio.compose_design_prompt(
            design_system_body=body, design_system_title="Linear", craft_body=craft,
        )
        assert '<artifact identifier=' in prompt
        assert 'type="text/html"' in prompt
        assert "#5e6ad2" in prompt  # real Linear brand token injected verbatim
        assert "Active design system — Linear" in prompt

    def test_directive_always_present_even_with_no_inputs(self):
        prompt = design_studio.compose_design_prompt()
        assert "Design Studio mode" in prompt
        assert "Active design system" not in prompt
        assert "Craft references" not in prompt


class TestBuildDesignBlock:
    def test_flag_off_returns_empty(self):
        s = _FakeSettings({"design_studio_enabled": False, "design_system_id": "linear-app"})
        assert design_studio.build_design_block(s) == ""

    def test_flag_on_with_system_includes_brand_and_separator(self):
        s = _FakeSettings({"design_studio_enabled": True, "design_system_id": "linear-app"})
        block = design_studio.build_design_block(s)
        assert block.startswith("\n\n---\n\n")
        assert "Active design system — Linear" in block
        assert "#5e6ad2" in block

    def test_flag_on_without_system_still_has_directive_and_craft(self):
        s = _FakeSettings({"design_studio_enabled": True, "design_system_id": ""})
        block = design_studio.build_design_block(s)
        assert "Design Studio mode" in block
        assert "Craft references" in block
        assert "Active design system" not in block

    def test_unknown_system_id_falls_back_gracefully(self):
        s = _FakeSettings({"design_studio_enabled": True, "design_system_id": "no-such-brand"})
        block = design_studio.build_design_block(s)
        # No brand body resolved, but the directive + craft still apply.
        assert "Design Studio mode" in block
        assert "Active design system" not in block
