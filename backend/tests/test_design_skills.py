"""tests/test_design_skills.py — Design Studio skill loader.

Runs against the REAL vendored Open Design skills under
core/assets/design/skills/ (no synthetic fixtures). web-prototype is the
stable anchor: it ships a seed template + references, exercising the inlining
path; saas-landing has no side files; guizang-ppt exercises the budget cap.
"""

from __future__ import annotations

from core import paths
from services import design_skills


def _root():
    return paths.design_assets_dir()


class TestParseFrontmatter:
    def test_parses_nested_od_block(self):
        raw = (
            "---\n"
            "name: demo\n"
            "od:\n"
            "  mode: prototype\n"
            "  craft:\n"
            "    requires: [color, typography]\n"
            "---\n"
            "# Body\nhello"
        )
        data, body = design_skills.parse_frontmatter(raw)
        assert data["name"] == "demo"
        assert data["od"]["mode"] == "prototype"
        assert data["od"]["craft"]["requires"] == ["color", "typography"]
        assert body.strip() == "# Body\nhello"

    def test_no_frontmatter_returns_raw_body(self):
        data, body = design_skills.parse_frontmatter("# Just a heading")
        assert data == {}
        assert body == "# Just a heading"


class TestListSkills:
    def test_catalog_present(self):
        skills = design_skills.list_skills(_root())
        assert len(skills) > 40  # 59 vendored at time of writing.
        ids = {s["id"] for s in skills}
        assert {"web-prototype", "saas-landing", "dashboard"} <= ids

    def test_web_prototype_descriptor(self):
        skills = design_skills.list_skills(_root())
        wp = next(s for s in skills if s["id"] == "web-prototype")
        assert wp["mode"] == "prototype"
        assert wp["surface"] == "web"
        assert wp["design_system_required"] is True

    def test_craft_requires_extracted(self):
        skills = design_skills.list_skills(_root())
        sl = next(s for s in skills if s["id"] == "saas-landing")
        # saas-landing declares od.craft.requires in its real front-matter.
        assert sl["craft_requires"] == ["typography", "color", "anti-ai-slop"]

    def test_missing_directory_returns_empty(self, tmp_path):
        assert design_skills.list_skills(tmp_path) == []


class TestReadSkill:
    def test_inlines_seed_and_references(self):
        skill = design_skills.read_skill(_root(), "web-prototype")
        assert skill is not None
        assert skill["body"].startswith("# Web Prototype Skill")
        # Seed template + layouts inlined under path-matching headers.
        assert "Skill assets (inlined)" in skill["assets"]
        assert "### assets/template.html" in skill["assets"]
        assert "### references/layouts.md" in skill["assets"]
        assert "```html" in skill["assets"]

    def test_skill_without_side_files_has_empty_assets(self):
        # saas-landing ships only SKILL.md + example.html (no assets/ or
        # references/ dirs), so nothing is inlined.
        skill = design_skills.read_skill(_root(), "saas-landing")
        assert skill is not None
        assert skill["assets"] == ""
        assert skill["craft_requires"] == ["typography", "color", "anti-ai-slop"]

    def test_inlined_assets_are_budget_bounded(self):
        # guizang-ppt has a ~30KB template + large references; the inlined
        # block must stay capped (HTML cap + references cap + headers).
        skill = design_skills.read_skill(_root(), "guizang-ppt")
        assert skill is not None
        assert len(skill["assets"]) < 28000

    def test_unknown_and_traversal_return_none(self):
        assert design_skills.read_skill(_root(), "does-not-exist") is None
        for bad in ["../../README", "..", "a/b", "x\\y", "."]:
            assert design_skills.read_skill(_root(), bad) is None
