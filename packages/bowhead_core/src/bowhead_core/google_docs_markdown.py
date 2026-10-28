"""Google Docs / Slides API JSON → plain markdown for agents.

Docs are read with ``documents.get`` rather than Drive ``files.export`` because the
export endpoint only accepts Drive scopes, while ``documents.get`` accepts both
``drive.file`` (picked files) and ``documents.readonly`` (files opened by link).
Only structure that changes meaning is kept: headings, lists, tables, links and
footnotes. Styling (fonts, colors) is dropped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

_HEADINGS = {"TITLE": 1, "HEADING_1": 1, "HEADING_2": 2, "HEADING_3": 3, "HEADING_4": 4, "HEADING_5": 5, "HEADING_6": 6, "SUBTITLE": 2}


def _run_text(element: dict[str, Any], footnotes: dict[str, int], footnote_prefix: str) -> str:
    if "footnoteReference" in element:
        ref = element["footnoteReference"]
        number = footnotes.setdefault(str(ref.get("footnoteId") or ""), len(footnotes) + 1)
        return f"[^{footnote_prefix}{number}]"
    run = element.get("textRun")
    if not isinstance(run, dict):
        return ""
    text = str(run.get("content") or "")
    if not text.strip():
        return text
    style = run.get("textStyle") or {}
    stripped = text.rstrip("\n")
    trailing = text[len(stripped) :]
    body = stripped
    if style.get("bold") and body.strip():
        body = f"**{body}**"
    if style.get("italic") and body.strip():
        body = f"*{body}*"
    url = ((style.get("link") or {}).get("url")) if isinstance(style.get("link"), dict) else None
    if url and body.strip():
        body = f"[{body}]({url})"
    return body + trailing


def _paragraph(paragraph: dict[str, Any], lists: dict[str, Any], footnotes: dict[str, int], footnote_prefix: str) -> str:
    text = "".join(_run_text(element, footnotes, footnote_prefix) for element in paragraph.get("elements") or []).rstrip("\n")
    if not text.strip():
        return ""
    style = (paragraph.get("paragraphStyle") or {}).get("namedStyleType") or ""
    level = _HEADINGS.get(style)
    if level:
        return f"{'#' * level} {text.strip()}"
    bullet = paragraph.get("bullet")
    if isinstance(bullet, dict):
        nesting = int(bullet.get("nestingLevel") or 0)
        glyph = _list_glyph(lists.get(str(bullet.get("listId") or "")), nesting)
        return f"{'  ' * nesting}{glyph} {text.strip()}"
    return text


def _list_glyph(list_props: Any, nesting: int) -> str:
    try:
        levels = list_props["listProperties"]["nestingLevels"]
        level = levels[min(nesting, len(levels) - 1)]
    except (KeyError, TypeError, IndexError):
        return "-"
    return "1." if level.get("glyphType") not in (None, "", "GLYPH_TYPE_UNSPECIFIED") else "-"


def _cell_text(cell: dict[str, Any], lists: dict[str, Any], footnotes: dict[str, int], footnote_prefix: str) -> str:
    parts = [
        _paragraph(item["paragraph"], lists, footnotes, footnote_prefix)
        for item in cell.get("content") or []
        if isinstance(item.get("paragraph"), dict)
    ]
    return " ".join(part.strip() for part in parts if part.strip()).replace("|", "\\|")


def _table(table: dict[str, Any], lists: dict[str, Any], footnotes: dict[str, int], footnote_prefix: str) -> str:
    rows = [[_cell_text(cell, lists, footnotes, footnote_prefix) for cell in row.get("tableCells") or []] for row in table.get("tableRows") or []]
    rows = [row for row in rows if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _blocks(content: list[dict[str, Any]], lists: dict[str, Any], footnotes: dict[str, int], footnote_prefix: str = "") -> list[str]:
    blocks: list[str] = []
    for element in content:
        if isinstance(element.get("paragraph"), dict):
            text = _paragraph(element["paragraph"], lists, footnotes, footnote_prefix)
        elif isinstance(element.get("table"), dict):
            text = _table(element["table"], lists, footnotes, footnote_prefix)
        else:
            continue  # sectionBreak, tableOfContents: no reading value
        if text:
            blocks.append(text)
    return blocks


def _join(blocks: list[str]) -> str:
    out: list[str] = []
    for index, block in enumerate(blocks):
        if index and not (_is_list_item(block) and _is_list_item(blocks[index - 1])):
            out.append("")
        out.append(block)
    return "\n".join(out)


def _is_list_item(block: str) -> bool:
    head = block.lstrip()
    return head.startswith(("- ", "1. "))


def document_tab_id(reference: str) -> str | None:
    """The tab explicitly selected by a Google Docs link, if any."""
    return parse_qs(urlsplit(reference).query, keep_blank_values=True).get("tab", [None])[0]


def document_tabs(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the tab tree in document order, including nested tabs."""
    tabs: list[dict[str, Any]] = []
    for tab in document.get("tabs") or []:
        tabs.append(tab)
        tabs.extend(document_tabs({"tabs": tab.get("childTabs") or []}))
    return tabs


def select_document_tab(document: dict[str, Any], tab_id: str | None) -> dict[str, Any] | None:
    """Select an explicit tab or the only tab; never guess among multiple tabs.

    Legacy single-body responses remain supported when no tab was selected.
    """
    tabs = document_tabs(document)
    if not tabs:
        return document if tab_id is None else None
    if tab_id is None:
        return tabs[0] if len(tabs) == 1 else None
    return next((tab for tab in tabs if (tab.get("tabProperties") or {}).get("tabId") == tab_id), None)


def document_tab_choices(document: dict[str, Any], file_id: str) -> list[dict[str, str]]:
    """Tab identities and links for reading or clarifying an edit target."""
    choices = []
    for tab in document_tabs(document):
        properties = tab.get("tabProperties") or {}
        tab_id = str(properties.get("tabId") or "")
        choices.append(
            {
                "id": tab_id,
                "title": str(properties.get("title") or tab_id),
                "url": f"https://docs.google.com/document/d/{file_id}/edit?{urlencode({'tab': tab_id})}",
            }
        )
    return choices


def document_to_markdown(document: dict[str, Any]) -> str:
    tabs = document_tabs(document)
    if not tabs:
        return _tab_to_markdown(document)
    if len(tabs) == 1:
        return _tab_to_markdown(tabs[0].get("documentTab") or {})
    sections = []
    for index, tab in enumerate(tabs, start=1):
        properties = tab.get("tabProperties") or {}
        title = properties.get("title") or properties.get("tabId") or "Untitled"
        sections.append(f"# Tab: {title}\n\n{_tab_to_markdown(tab.get('documentTab') or {}, footnote_prefix=f'tab{index}-')}")
    return "\n\n".join(sections).strip()


def _tab_to_markdown(document: dict[str, Any], *, footnote_prefix: str = "") -> str:
    lists = document.get("lists") or {}
    footnotes: dict[str, int] = {}
    blocks = _blocks(((document.get("body") or {}).get("content")) or [], lists, footnotes, footnote_prefix)
    notes = document.get("footnotes") or {}
    tail: list[str] = []
    for footnote_id, number in sorted(footnotes.items(), key=lambda item: item[1]):
        body = " ".join(_blocks((notes.get(footnote_id) or {}).get("content") or [], lists, {}))
        tail.append(f"[^{footnote_prefix}{number}]: {body.strip()}")
    text = _join(blocks)
    if tail:
        text = f"{text}\n\n" + "\n".join(tail)
    return text.strip()


def _shape_text(element: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    shape = element.get("shape")
    if isinstance(shape, dict):
        runs = ((shape.get("text") or {}).get("textElements")) or []
        joined = "".join(str((run.get("textRun") or {}).get("content") or "") for run in runs if isinstance(run, dict))
        texts.extend(line.strip() for line in joined.splitlines() if line.strip())
    table = element.get("table")
    if isinstance(table, dict):
        for row in table.get("tableRows") or []:
            cells = []
            for cell in row.get("tableCells") or []:
                runs = ((cell.get("text") or {}).get("textElements")) or []
                cells.append("".join(str((run.get("textRun") or {}).get("content") or "") for run in runs if isinstance(run, dict)).strip())
            if any(cells):
                texts.append(" | ".join(cells))
    group = element.get("elementGroup")
    if isinstance(group, dict):
        for child in group.get("children") or []:
            texts.extend(_shape_text(child))
    return texts


def presentation_to_markdown(presentation: dict[str, Any]) -> str:
    sections: list[str] = []
    for index, slide in enumerate(presentation.get("slides") or [], start=1):
        lines: list[str] = []
        for element in slide.get("pageElements") or []:
            lines.extend(_shape_text(element))
        notes_page = ((slide.get("slideProperties") or {}).get("notesPage")) or {}
        notes: list[str] = []
        for element in notes_page.get("pageElements") or []:
            notes.extend(_shape_text(element))
        body = "\n".join(lines) if lines else "(no text on this slide)"
        section = f"## Slide {index}\n\n{body}"
        if notes:
            section += "\n\nSpeaker notes: " + " ".join(notes)
        sections.append(section)
    return "\n\n".join(sections).strip()
