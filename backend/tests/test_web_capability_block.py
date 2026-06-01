"""tests/test_web_capability_block.py — MemoryRecall web-capability prompt block.

The block must appear only for a research agent when web research is enabled,
and be byte-absent otherwise (so a flag-off turn is unchanged). Uses the real
Settings instance — no stub settings.
"""

from __future__ import annotations

import json

import pytest

from services.memory_recall import MemoryRecall


def _recall(settings):
    # memory + mcp_registry are unused by _web_capability_block.
    return MemoryRecall(memory=None, settings=settings, mcp_registry=None)


def _agent(*skill_names):
    return {"skills": json.dumps([{"name": n} for n in skill_names])}


def test_block_present_for_research_agent_when_enabled(settings):
    settings.set("web_research_enabled", True)
    block = _recall(settings)._web_capability_block(_agent("researcher"))
    assert "## Web research" in block


def test_block_absent_when_flag_off(settings):
    # default is off
    assert _recall(settings)._web_capability_block(_agent("researcher")) == ""


def test_block_absent_for_non_research_agent(settings):
    settings.set("web_research_enabled", True)
    assert _recall(settings)._web_capability_block(_agent("writer")) == ""


def test_block_absent_without_agent(settings):
    settings.set("web_research_enabled", True)
    assert _recall(settings)._web_capability_block(None) == ""


@pytest.mark.parametrize("skill", ["researcher", "web-search", "Deep Research"])
def test_research_like_skills_match(settings, skill):
    settings.set("web_research_enabled", True)
    assert _recall(settings)._web_capability_block(_agent(skill))
