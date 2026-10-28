"""Markdown → Google Docs ``batchUpdate`` requests for inserting into an existing doc.

New documents are created by uploading markdown with Drive conversion (full fidelity).
Editing an existing document can only go through ``batchUpdate``; this module turns
a markdown fragment into one ``insertText`` plus styling requests at a known index.

Supported: headings (#..######), bullet and numbered lists (nesting by indentation),
paragraphs, **bold**, *italic*, [links](url). Markdown tables are inserted as
tab-separated text lines — the Docs API has no single request that builds a filled
table, and the tool contract says so.

Docs indexes are UTF-16 code units; every offset here is measured that way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
_INLINE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\((https?://[^)\s]+)\)")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass
class _Paragraph:
    text: str
    heading: int = 0
    bullet: str | None = None  # "bullet" | "number"
    level: int = 0
    spans: list[tuple[int, int, dict[str, Any], str]] = field(default_factory=list)  # (start, end, style, fields) in utf16 within text


def _inline(raw: str) -> tuple[str, list[tuple[int, int, dict[str, Any], str]]]:
    out: list[str] = []
    spans: list[tuple[int, int, dict[str, Any], str]] = []
    cursor = 0
    position = 0
    for match in _INLINE.finditer(raw):
        before = raw[cursor : match.start()]
        out.append(before)
        position += utf16_len(before)
        if match.group(1) is not None:
            text, style, fields = match.group(1), {"bold": True}, "bold"
        elif match.group(2) is not None:
            text, style, fields = match.group(2), {"italic": True}, "italic"
        else:
            text, style, fields = match.group(3), {"link": {"url": match.group(4)}}, "link"
        out.append(text)
        length = utf16_len(text)
        spans.append((position, position + length, style, fields))
        position += length
        cursor = match.end()
    out.append(raw[cursor:])
    return "".join(out), spans


def _indent_width(prefix: str) -> int:
    return len(prefix.replace("\t", "    "))


def _indent_unit(lines: list[str]) -> int:
    """Spaces per nesting level: the smallest non-zero bullet indent (models use 2 or 4)."""
    widths = [_indent_width(match.group(1)) for line in lines if (match := _BULLET.match(line))]
    nested = [width for width in widths if width > 0]
    return min(nested) if nested else 2


def _parse(markdown: str) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    unit = _indent_unit(lines)
    for line in lines:
        if not line.strip():
            continue
        if _TABLE_RULE.match(line.strip()):
            continue
        heading = _HEADING.match(line)
        if heading:
            text, spans = _inline(heading.group(2).strip())
            paragraphs.append(_Paragraph(text=text, heading=len(heading.group(1)), spans=spans))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            text, spans = _inline(bullet.group(3).strip())
            level = min(_indent_width(bullet.group(1)) // unit, 8)
            kind = "number" if bullet.group(2)[0].isdigit() else "bullet"
            paragraphs.append(_Paragraph(text=text, bullet=kind, level=level, spans=spans))
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            text, spans = _inline("\t".join(cells))
            paragraphs.append(_Paragraph(text=text, spans=spans))
            continue
        text, spans = _inline(stripped)
        paragraphs.append(_Paragraph(text=text, spans=spans))
    return paragraphs


def plain_text(markdown: str) -> str:
    """Text the user will see after insertion (for confirmation previews / checks)."""
    return "\n".join(p.text for p in _parse(markdown))


def insert_requests(markdown: str, index: int, *, leading_newline: bool) -> tuple[list[dict[str, Any]], int]:
    """Requests inserting ``markdown`` at ``index``; returns (requests, inserted utf16 length).

    ``leading_newline`` starts a new paragraph first (appending after existing text).
    Inserted text inherits the style of the paragraph it lands in, so the whole range
    is first reset to normal text without bullets or character styles.
    """
    paragraphs = _parse(markdown)
    if not paragraphs:
        return [], 0
    # Docs builds nesting from leading tabs when bullets are created (tabs are consumed).
    lines = [("\t" * p.level if p.bullet else "") + p.text for p in paragraphs]
    text = ("\n" if leading_newline else "") + "\n".join(lines) + ("" if leading_newline else "\n")
    total = utf16_len(text)
    start = index + (1 if leading_newline else 0)
    end = index + total
    requests: list[dict[str, Any]] = [
        {"insertText": {"location": {"index": index}, "text": text}},
        {"deleteParagraphBullets": {"range": {"startIndex": start, "endIndex": end}}},
        # Reset after removing bullets: deleteParagraphBullets keeps the list indent as paragraph indent.
        {
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {
                    "namedStyleType": "NORMAL_TEXT",
                    "indentStart": {"magnitude": 0, "unit": "PT"},
                    "indentFirstLine": {"magnitude": 0, "unit": "PT"},
                },
                "fields": "namedStyleType,indentStart,indentFirstLine",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {},
                "fields": "bold,italic,link,underline,foregroundColor",
            }
        },
    ]
    cursor = start
    positions: list[tuple[int, int]] = []
    for line in lines:
        length = utf16_len(line)
        positions.append((cursor, cursor + length))
        cursor += length + 1
    # Character styles first (offsets shift once tabs are consumed by bullet creation).
    for paragraph, (para_start, _para_end), line in zip(paragraphs, positions, lines, strict=True):
        prefix = utf16_len(line) - utf16_len(paragraph.text)
        for span_start, span_end, style, fields in paragraph.spans:
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": para_start + prefix + span_start, "endIndex": para_start + prefix + span_end},
                        "textStyle": style,
                        "fields": fields,
                    }
                }
            )
    for paragraph, (para_start, para_end) in zip(paragraphs, positions, strict=True):
        if paragraph.heading:
            requests.append(
                {
                    "updateParagraphStyle": {
                        # Touching the paragraph is enough; never reach past the segment end.
                        "range": {"startIndex": para_start, "endIndex": para_end},
                        "paragraphStyle": {"namedStyleType": f"HEADING_{paragraph.heading}"},
                        "fields": "namedStyleType",
                    }
                }
            )
    # Bullets last and from the end backwards: consumed tabs shift everything after.
    groups: list[tuple[str, int, int]] = []
    for paragraph, (para_start, para_end) in zip(paragraphs, positions, strict=True):
        if paragraph.bullet and groups and groups[-1][0] == paragraph.bullet and groups[-1][2] + 1 == para_start:
            groups[-1] = (paragraph.bullet, groups[-1][1], para_end)
        elif paragraph.bullet:
            groups.append((paragraph.bullet, para_start, para_end))
    for kind, group_start, group_end in reversed(groups):
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": group_start, "endIndex": group_end},
                    "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN" if kind == "number" else "BULLET_DISC_CIRCLE_SQUARE",
                }
            }
        )
    return requests, total


@dataclass(frozen=True)
class Section:
    heading_start: int
    content_start: int
    content_end: int


def _paragraph_text(paragraph: dict[str, Any]) -> str:
    return "".join(str((element.get("textRun") or {}).get("content") or "") for element in paragraph.get("elements") or []).strip()


def _heading_level(paragraph: dict[str, Any]) -> int:
    style = str((paragraph.get("paragraphStyle") or {}).get("namedStyleType") or "")
    if style == "TITLE":
        return 0
    if style.startswith("HEADING_"):
        return int(style.rsplit("_", 1)[1])
    return -1


def find_section(document: dict[str, Any], heading: str) -> tuple[Section | None, list[str]]:
    """Locate the section under ``heading`` (exact text); returns (section, all headings).

    The section runs from the end of the heading paragraph to the next heading of the
    same or a higher level, or to the end of the body. ``None`` when the heading is
    missing or not unique — the caller must not guess.
    """
    content = ((document.get("body") or {}).get("content")) or []
    headings: list[tuple[int, int, int, int, str]] = []  # (index in content, level, start, end, text)
    for position, element in enumerate(content):
        paragraph = element.get("paragraph")
        if not isinstance(paragraph, dict):
            continue
        level = _heading_level(paragraph)
        if level >= 0:
            headings.append((position, level, int(element.get("startIndex") or 0), int(element.get("endIndex") or 0), _paragraph_text(paragraph)))
    titles = [item[4] for item in headings]
    matches = [item for item in headings if item[4] == heading.strip()]
    if len(matches) != 1:
        return None, titles
    _pos, level, start, end, _text = matches[0]
    body_end = int((content[-1] if content else {}).get("endIndex") or end) - 1
    following = [item for item in headings if item[2] > start and item[1] <= level]
    content_end = following[0][2] if following else body_end
    return Section(heading_start=start, content_start=end, content_end=max(end, content_end)), titles


def body_end_index(document: dict[str, Any]) -> int:
    content = ((document.get("body") or {}).get("content")) or []
    return int((content[-1] if content else {}).get("endIndex") or 2) - 1
