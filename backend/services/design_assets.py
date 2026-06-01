"""
services/design_assets.py — Design Studio asset registry.

A faithful Python port of Open Design's ``apps/daemon/src/design-systems.ts``.
Scans ``<root>/design-systems/<id>/DESIGN.md`` files and surfaces a compact
descriptor (id, title, category, summary, swatches, surface) plus the raw
Markdown body for prompt injection. Also resolves ``<root>/craft/*.md`` rules.

The DESIGN.md schema is documented upstream (9 sections). Here we only parse
the metadata the picker needs:
  - title    — the first ``# H1`` (boilerplate "Design System Inspired by X"
               stripped to "X" for a clean dropdown).
  - category — a ``> Category: <name>`` blockquote beneath the H1.
  - summary  — the first paragraph between the H1 and the next heading.
  - surface  — a ``> Surface: <web|image|video|audio>`` blockquote; default web.
  - swatches — up to 4 representative hex colors [bg, support, fg, accent].

The regexes mirror the TS source line-for-line so a future upstream refresh
stays mechanical. Everything is read-only and best-effort: an unreadable
directory is skipped rather than failing the whole listing, exactly like the
TS ``catch {}``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("altosybioagents.design_assets")

_KNOWN_SURFACES = {"web", "image", "video", "audio"}

# Form A: "- **Background:** `#FAFAFA`"
_RE_SWATCH_A = re.compile(
    r"^[\s>*-]*\**\s*([A-Za-z][A-Za-z0-9 /&()+_-]{1,40}?)\s*\**\s*[:：]\s*`?(#[0-9a-fA-F]{3,8})",
    re.MULTILINE,
)
# Form B: "**Stripe Purple** (`#533afd`)"
_RE_SWATCH_B = re.compile(
    r"\*\*([A-Za-z][A-Za-z0-9 /&()+_-]{1,40}?)\*\*\s*\(?\s*`?(#[0-9a-fA-F]{3,8})",
)
_RE_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_RE_HEADING = re.compile(r"^#{1,6}\s+")
_RE_CATEGORY = re.compile(r"^>\s*Category:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_RE_SURFACE = re.compile(r"^>\s*Surface:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_RE_CATEGORY_LINE = re.compile(r"^>\s*Category:.*$", re.IGNORECASE | re.MULTILINE)
_RE_BLOCKQUOTE = re.compile(r"^>\s*", re.MULTILINE)
_RE_HEX = re.compile(r"^#([0-9a-fA-F]{3,8})$")


def list_design_systems(root: Path) -> list[dict]:
    """List every design system under ``<root>/design-systems``.

    Returns a list of dicts with id/title/category/summary/swatches/surface
    and the raw ``body``. Returns ``[]`` when the directory is missing.
    """
    systems_dir = Path(root) / "design-systems"
    out: list[dict] = []
    try:
        entries = sorted(p for p in systems_dir.iterdir() if p.is_dir())
    except OSError:
        return out
    for entry in entries:
        design_path = entry / "DESIGN.md"
        try:
            if not design_path.is_file():
                continue
            raw = design_path.read_text(encoding="utf-8")
        except OSError:
            continue  # Skip — mirrors the TS catch {}.
        title_match = _RE_H1.search(raw)
        title = _clean_title(title_match.group(1) if title_match else entry.name)
        out.append(
            {
                "id": entry.name,
                "title": title,
                "category": _extract_category(raw) or "Uncategorized",
                "summary": _summarize(raw),
                "swatches": _extract_swatches(raw),
                "surface": _extract_surface(raw),
                "body": raw,
            }
        )
    return out


def read_design_system(root: Path, design_id: str) -> Optional[str]:
    """Return the raw DESIGN.md body for ``design_id``, or ``None`` if absent.

    ``design_id`` is treated as a single path segment; any value containing a
    path separator or traversal is rejected so a settings value can never read
    outside the vendored tree.
    """
    if not design_id or "/" in design_id or "\\" in design_id or design_id in (".", ".."):
        return None
    file = Path(root) / "design-systems" / design_id / "DESIGN.md"
    try:
        return file.read_text(encoding="utf-8")
    except OSError:
        return None


def read_craft(root: Path, slugs: Optional[list[str]] = None) -> str:
    """Concatenate craft rule files with section headers.

    ``slugs`` selects specific files (without extension); ``None`` loads the
    universal set in a stable order. Missing files are skipped. The README is
    intentionally excluded from the default set — it documents the directory,
    it is not a rule the agent should follow.
    """
    craft_dir = Path(root) / "craft"
    default_order = ["anti-ai-slop", "color", "typography"]
    wanted = slugs if slugs else default_order
    parts: list[str] = []
    for slug in wanted:
        path = craft_dir / f"{slug}.md"
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            parts.append(body)
    return "\n\n---\n\n".join(parts)


# ── Internals (ported from design-systems.ts) ───────────────────────────────


def _summarize(raw: str) -> str:
    lines = raw.splitlines()
    first_h1 = next((i for i, ln in enumerate(lines) if _RE_HEADING.match(ln) and ln.lstrip().startswith("# ")), -1)
    if first_h1 == -1:
        return ""
    after_h1 = lines[first_h1 + 1:]
    next_heading = next((i for i, ln in enumerate(after_h1) if _RE_HEADING.match(ln)), -1)
    window_lines = after_h1 if next_heading == -1 else after_h1[:next_heading]
    window = "\n".join(window_lines)
    # Drop the Category metadata line — surfaced separately — then strip "> ".
    window = _RE_CATEGORY_LINE.sub("", window)
    window = _RE_BLOCKQUOTE.sub("", window).strip()
    first_para = window.split("\n\n")[0] if window else ""
    return first_para[:240]


def _extract_category(raw: str) -> Optional[str]:
    m = _RE_CATEGORY.search(raw)
    return m.group(1) if m else None


def _extract_surface(raw: str) -> str:
    m = _RE_SURFACE.search(raw)
    if not m:
        return "web"
    v = m.group(1).strip().lower()
    return v if v in _KNOWN_SURFACES else "web"


def _clean_title(raw: str) -> str:
    """"Design System Inspired by Cohere" → "Cohere"; others pass through."""
    return re.sub(r"^Design System (Inspired by|for)\s+", "", raw, flags=re.IGNORECASE).strip()


def _normalize_hex(raw: str) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    m = _RE_HEX.match(raw.strip())
    if not m:
        return None
    hex_part = m.group(1)
    if len(hex_part) == 3:
        hex_part = "".join(c + c for c in hex_part)
    if len(hex_part) == 4:
        hex_part = "".join(c + c for c in hex_part)[:8]
    return "#" + hex_part.lower()


def _is_neutral(hex_value: str) -> bool:
    if not re.match(r"^#[0-9a-f]{6}$", hex_value):
        return False
    r = int(hex_value[1:3], 16)
    g = int(hex_value[3:5], 16)
    b = int(hex_value[5:7], 16)
    return (max(r, g, b) - min(r, g, b)) < 10


def _extract_swatches(raw: str) -> list[str]:
    """Pull 4 representative colors: [bg, support, fg, accent]. [] on failure."""
    colors: list[dict] = []
    seen: set[str] = set()

    def push(name: str, value: str) -> None:
        clean_name = re.sub(r"[*_`]+", "", name)
        clean_name = re.sub(r"\s+", " ", clean_name).strip().lower()
        v = _normalize_hex(value)
        if not v or len(clean_name) > 60:
            return
        key = f"{clean_name}|{v}"
        if key in seen:
            return
        seen.add(key)
        colors.append({"name": clean_name, "value": v})

    for m in _RE_SWATCH_A.finditer(raw):
        push(m.group(1), m.group(2))
    for m in _RE_SWATCH_B.finditer(raw):
        push(m.group(1), m.group(2))
    if not colors:
        return []

    def pick(hints: list[str]) -> Optional[str]:
        for h in hints:
            found = next((c for c in colors if h in c["name"]), None)
            if found:
                return found["value"]
        return None

    bg = pick(["page background", "background", "canvas", "paper", "surface"]) or "#ffffff"
    fg = pick(["heading", "foreground", "ink", "fg", "text", "navy", "graphite"]) or "#111111"
    accent = (
        pick(["primary brand", "brand primary", "accent", "brand", "primary"])
        or next((c["value"] for c in colors if not _is_neutral(c["value"])), None)
        or (colors[0]["value"] if colors else None)
        or "#888888"
    )
    support = (
        pick(["border", "divider", "rule", "muted", "secondary", "subtle"])
        or next(
            (c["value"] for c in colors if _is_neutral(c["value"]) and c["value"] != bg and c["value"] != fg),
            None,
        )
        or "#cccccc"
    )
    return [bg, support, fg, accent]
