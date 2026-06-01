"""
services/design_studio.py — Design Studio system-prompt composition.

Mirrors the DESIGN.md + craft slices of Open Design's
``composeSystemPrompt`` (apps/daemon/src/prompts/system.ts), adapted for
AltoSymbioAgents' single-string system-prompt model. When Design Studio is
enabled, ``build_design_block(settings)`` produces a block that MemoryRecall
appends to the turn's system prompt so the active agent behaves as a designer
that emits a self-contained HTML ``<artifact>``.

Ordering (matches upstream precedence):
  1. Designer directive + artifact output contract — the load-bearing rules.
  2. Active DESIGN.md — authoritative tokens (color, type, spacing).
  3. Craft references — universal rules layered on top; brand wins on token
     *values*, craft rules cover everything the brand doesn't override
     (letter-spacing, accent caps, anti-AI-slop).

Phase 1 scope: DESIGN.md + craft only. Skills, the deck framework, and media
contracts are deferred to a later phase; the function signature leaves room
for them without a breaking change.
"""

from __future__ import annotations

import logging
from typing import Optional

from core import paths
from services import design_assets, design_skills

log = logging.getLogger("altosybioagents.design_studio")

# The artifact output contract. Kept compact (the full Open Design designer
# charter is large and not vendored in Phase 1) but faithful to the wire
# format the renderer's parser expects: a single <artifact> wrapper with
# identifier/type/title attributes around one self-contained HTML document.
_DESIGNER_DIRECTIVE = """\
# Design Studio mode

For this turn you are an expert product designer. When the user asks you to
build, design, or prototype a visual artifact (a landing page, dashboard,
pricing page, marketing page, deck, email, or any UI), produce **one
self-contained HTML document** and wrap it in an artifact tag so the app can
render it in a live preview.

## Output contract

Emit exactly one artifact, using this shape:

```
<artifact identifier="kebab-case-slug" type="text/html" title="Human Title">
<!doctype html>
<html>
  <!-- a complete, self-contained page: inline <style>, no external CSS/JS -->
</html>
</artifact>
```

Rules:
- The artifact body MUST be a complete HTML document (`<!doctype html>` … `</html>`).
- Inline all CSS in a `<style>` block. Do NOT link external stylesheets, fonts
  served from a CDN you can't guarantee, or external scripts. The preview runs
  in a locked-down sandbox with no network and no same-origin access.
- Use image placeholders (e.g. a styled `<div>` block), never hot-linked stock
  photos.
- Write one short sentence before the artifact describing what you built.
  Output nothing after `</artifact>`.
- For ordinary, non-design questions, answer normally in Markdown — only emit
  an artifact when the user actually wants a visual design.\
"""


def compose_design_prompt(
    *,
    design_system_body: Optional[str] = None,
    design_system_title: Optional[str] = None,
    craft_body: Optional[str] = None,
    craft_sections: Optional[list[str]] = None,
    skill_body: Optional[str] = None,
    skill_name: Optional[str] = None,
    skill_assets: Optional[str] = None,
) -> str:
    """Compose the Design Studio system-prompt block.

    Returns ``""`` when there is nothing to contribute (no directive needed).
    The directive is always included so the agent knows the output contract;
    DESIGN.md, craft, and the active skill are appended only when present.

    Ordering mirrors Open Design: directive → DESIGN.md (authoritative tokens)
    → craft (universal rules) → skill workflow (+ inlined seed/references).
    The skill goes last so its concrete workflow/seed wins over the general
    directive, while brand tokens above still bind the palette.
    """
    parts: list[str] = [_DESIGNER_DIRECTIVE]

    if design_system_body and design_system_body.strip():
        title_suffix = f" — {design_system_title}" if design_system_title else ""
        parts.append(
            f"\n\n## Active design system{title_suffix}\n\n"
            "Treat the following DESIGN.md as authoritative for color, "
            "typography, spacing, and component rules. Do not invent tokens "
            "outside this palette; bind these tokens into the document's "
            "`:root` before generating any layout.\n\n"
            f"{design_system_body.strip()}"
        )

    if craft_body and craft_body.strip():
        section_label = f" — {', '.join(craft_sections)}" if craft_sections else ""
        parts.append(
            f"\n\n## Craft references{section_label}\n\n"
            "These craft rules are universal — they apply on top of the active "
            "design system above, regardless of brand. The DESIGN.md decides "
            "*which* tokens to use; craft rules decide *how* to use them. On "
            "any conflict, the brand wins for token values; craft rules still "
            "apply to anything the brand does not override (letter-spacing, "
            "accent overuse caps, anti-AI-slop patterns).\n\n"
            f"{craft_body.strip()}"
        )

    if skill_body and skill_body.strip():
        name_suffix = f" — {skill_name}" if skill_name else ""
        parts.append(
            f"\n\n## Active skill{name_suffix}\n\n"
            "Follow this skill's workflow exactly. It is more specific than the "
            "general directive above and wins where they overlap.\n\n"
            f"{skill_body.strip()}"
        )
        if skill_assets and skill_assets.strip():
            parts.append(f"\n\n{skill_assets.strip()}")

    return "".join(parts)


def build_design_block(settings) -> str:
    """Build the prompt block to append for a chat turn, or ``""``.

    Flag-gated on ``design_studio_enabled`` and fully best-effort: any failure
    (missing assets, bad id) yields ``""`` so the turn proceeds with the base
    prompt unchanged. The returned string is prefixed with the block separator
    so callers can append it directly to ``full_system``.
    """
    try:
        if not settings.get("design_studio_enabled", False):
            return ""
        root = paths.design_assets_dir()

        design_system_body: Optional[str] = None
        design_system_title: Optional[str] = None
        design_id = (settings.get("design_system_id", "") or "").strip()
        if design_id:
            design_system_body = design_assets.read_design_system(root, design_id)
            if design_system_body:
                meta = next(
                    (s for s in design_assets.list_design_systems(root) if s["id"] == design_id),
                    None,
                )
                design_system_title = meta["title"] if meta else None

        # Active skill (optional). Its craft.requires selects which craft
        # sections inject; with no skill (or none declared) we fall back to the
        # universal default set.
        skill_body: Optional[str] = None
        skill_name: Optional[str] = None
        skill_assets: Optional[str] = None
        craft_requires: Optional[list[str]] = None
        skill_id = (settings.get("design_skill_id", "") or "").strip()
        if skill_id:
            skill = design_skills.read_skill(root, skill_id)
            # Skip non-HTML skills (media / design-system / utility) even if one
            # was set directly via the API — they don't emit an <artifact> and
            # would contradict the directive. The picker already filters these.
            if skill and skill.get("studio_compatible", True):
                skill_body = skill["body"]
                skill_name = skill["name"]
                skill_assets = skill["assets"]
                if skill["craft_requires"]:
                    craft_requires = skill["craft_requires"]

        craft_body = design_assets.read_craft(root, craft_requires)

        block = compose_design_prompt(
            design_system_body=design_system_body,
            design_system_title=design_system_title,
            craft_body=craft_body,
            craft_sections=craft_requires,
            skill_body=skill_body,
            skill_name=skill_name,
            skill_assets=skill_assets,
        )
        if not block:
            return ""
        return "\n\n---\n\n" + block
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the turn.
        log.debug("design block skipped: %s", exc)
        return ""
