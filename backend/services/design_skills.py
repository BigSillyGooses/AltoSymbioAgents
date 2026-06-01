"""
services/design_skills.py — Design Studio skill registry.

Adapts Open Design's apps/daemon/src/skills.ts + frontmatter.ts to the
AltoSymbioAgents chat model. Scans ``<root>/skills/<id>/SKILL.md``, parses the
YAML front-matter (via PyYAML — the real SKILL.md frontmatter has nested `od:`
blocks), and surfaces a compact descriptor for the picker plus the full skill
body for prompt injection.

**Key divergence from Open Design:** their code agents run on a real filesystem
and READ a skill's side files (`assets/template.html`, `references/*.md`) from
disk. AltoSymbioAgents workers get only a system prompt + messages — NO file
access (confirmed: hub_router only passes system/messages, no tools). So we
**inline** the seed template and references directly into the prompt, under
headers that match the relative paths the SKILL.md body references, with a hard
size budget so a large skill (e.g. guizang-ppt) can't blow out the context
window. This replaces Open Design's "read these files from disk" preamble.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("altosybioagents.design_skills")

_KNOWN_SURFACES = {"web", "image", "video", "audio"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Modes whose output is NOT a self-contained HTML <artifact>: media skills use
# a media-generation contract Design Studio doesn't provide; design-system
# emits a DESIGN.md; utility is a non-generative audit. Compatibility is judged
# on the EXPLICIT od.mode only — a prototype skill with no `od` block must not
# be excluded by prose-based mode inference.
_NON_HTML_MODES = {"image", "video", "audio", "design-system", "utility"}

# Front-matter: leading optional BOM, then a --- fenced YAML block, then body.
# Mirrors frontmatter.ts.
_FM_RE = re.compile(r"^﻿?---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)

# Inlining budget. The seed template is the load-bearing asset, so it gets the
# larger share; references (layouts/checklist/themes) split the rest. Total is
# kept moderate so a Design Studio turn (directive + DESIGN.md + craft + skill
# + assets) stays within a usable context window. Large skills are truncated
# with a visible marker rather than dropped.
_HTML_ASSET_CAP = 16000
_REFERENCES_CAP = 8000
# References ordered by usefulness to generation; unlisted names sort after,
# alphabetically. checklist is last — it's a self-review gate, least critical
# if the budget runs short.
_REFERENCE_PRIORITY = ["layouts", "components", "styles", "themes", "checklist"]


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split a SKILL.md into (front-matter dict, body). Mirrors frontmatter.ts.

    Best-effort: a missing/invalid front-matter block yields ({}, raw) so a
    plain-Markdown skill still loads with an empty descriptor.
    """
    m = _FM_RE.match(raw)
    if not m:
        return {}, raw
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        log.debug("SKILL.md front-matter parse failed: %s", exc)
        return {}, m.group(2)
    if not isinstance(data, dict):
        data = {}
    return data, m.group(2)


def list_skills(root: Path) -> list[dict]:
    """List every skill under ``<root>/skills`` (descriptors only, no body).

    Returns ``[]`` when the directory is missing.
    """
    skills_dir = Path(root) / "skills"
    out: list[dict] = []
    try:
        entries = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    except OSError:
        return out
    for entry in entries:
        skill_path = entry / "SKILL.md"
        try:
            if not skill_path.is_file():
                continue
            raw = skill_path.read_text(encoding="utf-8")
        except OSError:
            continue
        data, body = parse_frontmatter(raw)
        od = data.get("od") if isinstance(data.get("od"), dict) else {}
        mode = str(od.get("mode") or _infer_mode(body, data.get("description")))
        out.append(
            {
                # Folder name is the stable id (it's how read_skill resolves
                # the path); the front-matter `name` is the display label.
                "id": entry.name,
                "name": str(data.get("name") or entry.name),
                "description": str(data.get("description") or "").strip(),
                "mode": mode,
                "surface": _surface(od.get("surface"), mode),
                "craft_requires": _craft_requires(od),
                "design_system_required": _design_system_required(od),
                "studio_compatible": _studio_compatible(od),
            }
        )
    return out


def read_skill(root: Path, skill_id: str) -> Optional[dict]:
    """Load one skill with its body + inlined side files, or ``None``.

    ``skill_id`` is a single path segment; traversal is rejected.
    """
    if not skill_id or "/" in skill_id or "\\" in skill_id or skill_id in (".", ".."):
        return None
    skill_dir = Path(root) / "skills" / skill_id
    skill_path = skill_dir / "SKILL.md"
    try:
        raw = skill_path.read_text(encoding="utf-8")
    except OSError:
        return None
    data, body = parse_frontmatter(raw)
    od = data.get("od") if isinstance(data.get("od"), dict) else {}
    mode = str(od.get("mode") or _infer_mode(body, data.get("description")))
    return {
        "id": skill_id,
        "name": str(data.get("name") or skill_id),
        "mode": mode,
        "craft_requires": _craft_requires(od),
        "studio_compatible": _studio_compatible(od),
        "body": body.strip(),
        "assets": _inline_assets(skill_dir),
    }


# ── Internals ────────────────────────────────────────────────────────────────


def _surface(value, mode: str) -> str:
    if isinstance(value, str) and value.strip().lower() in _KNOWN_SURFACES:
        return value.strip().lower()
    if mode in ("image", "video", "audio"):
        return mode
    return "web"


def _craft_requires(od: dict) -> list[str]:
    """Extract + normalize `od.craft.requires` slugs (alphanumeric+dash)."""
    craft = od.get("craft") if isinstance(od.get("craft"), dict) else {}
    value = craft.get("requires")
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in value:
        if not isinstance(v, str):
            continue
        slug = v.strip().lower()
        if not _SLUG_RE.match(slug) or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def _design_system_required(od: dict) -> bool:
    ds = od.get("design_system") if isinstance(od.get("design_system"), dict) else {}
    return bool(ds.get("requires", True))


def _infer_mode(body, description) -> str:
    """Fallback when `od.mode` is absent — only deck vs prototype.

    Every real media skill in the catalog declares an explicit `od.mode`, so
    inference never needs to emit image/video/audio. It previously did (matching
    "motion"/"animation"/"poster" in prose), which mislabelled prototype "taste"
    variants (a soft, animated landing page) as video. Restricting inference to
    deck-vs-prototype removes those false positives.
    """
    hay = f"{description or ''}\n{body or ''}".lower()
    if re.search(r"\b(ppt|deck|slides?|presentation|keynote)\b", hay):
        return "deck"
    return "prototype"


def _studio_compatible(od: dict) -> bool:
    """True unless the skill's EXPLICIT od.mode declares a non-HTML output."""
    raw = od.get("mode")
    explicit = raw.strip().lower() if isinstance(raw, str) else ""
    return explicit not in _NON_HTML_MODES


def _reference_sort_key(path: Path):
    stem = path.stem.lower()
    try:
        return (_REFERENCE_PRIORITY.index(stem), stem)
    except ValueError:
        return (len(_REFERENCE_PRIORITY), stem)


def _truncate(text: str, cap: int, marker: str) -> tuple[str, int]:
    """Return (text capped to ``cap``, chars consumed). Appends ``marker`` if cut."""
    if len(text) <= cap:
        return text, len(text)
    return text[:cap] + marker, cap


def _inline_assets(skill_dir: Path) -> str:
    """Build the inlined seed-template + references block, budget-capped.

    Workers cannot read disk, so the SKILL.md's referenced side files are
    pasted inline under headers matching their relative paths. HTML seeds are
    fenced (the body says "copy assets/template.html"); reference Markdown is
    inlined raw (it's instructions, not code to copy).
    """
    parts: list[str] = []

    # Seed templates: assets/*.html.
    assets_dir = skill_dir / "assets"
    if assets_dir.is_dir():
        budget = _HTML_ASSET_CAP
        for f in sorted(assets_dir.glob("*.html")):
            if budget <= 0:
                break
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            body, used = _truncate(text, budget, "\n<!-- … truncated (system-prompt budget) … -->")
            budget -= used
            parts.append(f"### assets/{f.name}\n\n```html\n{body}\n```")

    # References: references/*.md, priority-ordered.
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        budget = _REFERENCES_CAP
        for f in sorted(refs_dir.glob("*.md"), key=_reference_sort_key):
            if budget <= 0:
                break
            try:
                text = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            body, used = _truncate(text, budget, "\n\n_(… truncated — system-prompt budget reached …)_")
            budget -= used
            parts.append(f"### references/{f.name}\n\n{body}")

    if not parts:
        return ""
    preamble = (
        "## Skill assets (inlined)\n\n"
        "The skill workflow references the files below. They are provided "
        "inline here — you have NO filesystem access, so do not try to open "
        "them from disk; use the inlined copies. Paths match the references in "
        "the skill body above.\n\n"
    )
    return preamble + "\n\n".join(parts)
