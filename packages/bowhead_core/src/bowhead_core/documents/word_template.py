"""Source-bound DOCX text edits without reserializing unaffected XML.

This helper neither executes model code nor renders, authorizes or publishes a
document. Its original-source locators and offsets remain stable across edits.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.parsers import expat
from zipfile import ZipFile

from lxml import etree

from bowhead_core.documents.package_io import MAX_PART_BYTES, OFFICE_MIMES, REL_NS, WORD_NS, OfficePackageError, inspect_office_package

__all__ = ["DocumentTemplate", "WordChange"]

W = "{" + WORD_NS + "}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_INSPECTION_BYTES = 256_000


@dataclass(frozen=True)
class WordChange:
    """An observed source-span change, not semantic or publication approval."""

    target_id: str
    part: str
    start: int
    end: int
    before: str
    after: str


@dataclass
class _Span:
    start: int
    content_start: int
    content_end: int = 0
    end: int = 0
    empty: bool = False


@dataclass(frozen=True)
class _Text:
    element: etree._Element
    span: _Span
    run_id: str
    start: int
    end: int
    value: str


@dataclass
class _Block:
    id: str
    part: str
    element: etree._Element
    text: str
    limitation: str | None
    texts: list[_Text] = field(default_factory=list)
    runs: list[tuple[str, etree._Element]] = field(default_factory=list)
    table_id: str | None = None
    row: int | None = None
    column: int | None = None


@dataclass
class _Table:
    id: str
    part: str
    rows: list[list[etree._Element]]
    limitation: str | None


def _tag_end(raw: bytes, start: int) -> int:
    quote = 0
    for index in range(start, len(raw)):
        value = raw[index]
        if quote:
            if value == quote:
                quote = 0
        elif value in (34, 39):
            quote = value
        elif value == 62:
            return index + 1
    raise OfficePackageError("file_office_invalid_package")


def _spans(raw: bytes, root: etree._Element) -> dict[etree._Element, _Span]:
    """Locate original UTF-8 element bytes after the shared OPC/XML validation."""
    names = {W + name for name in ("p", "r", "t")}
    found: dict[str, list[_Span]] = {name: [] for name in names}
    stack: list[_Span | None] = []
    parser = expat.ParserCreate(namespace_separator="}")

    def start(name: str, attributes: dict[str, str]) -> None:
        span = None
        if "{" + name in names:
            offset = parser.CurrentByteIndex
            end = _tag_end(raw, offset)
            span = _Span(offset, end, empty=raw[offset:end].rstrip().endswith(b"/>"))
            found["{" + name].append(span)
        stack.append(span)

    def end(name: str) -> None:
        span = stack.pop()
        if span is not None:
            span.content_end = span.content_start if span.empty else parser.CurrentByteIndex
            span.end = span.content_start if span.empty else _tag_end(raw, parser.CurrentByteIndex)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(raw, True)
    return {node: span for name in names for node, span in zip(root.iter(name), found[name], strict=True)}


def _table_limitation(table: etree._Element) -> str | None:
    if any(node is not table for node in table.iter(W + "tbl")):
        return "word_nested_table_unsupported"
    if any(node.tag in {W + "vMerge", W + "hMerge"} or (node.tag == W + "gridSpan" and node.get(W + "val") != "1") for node in table.iter()):
        return "word_merged_table_unsupported"
    if any(node.tag not in {W + "tblPr", W + "tblGrid", W + "tr"} for node in table):
        return "word_table_structure_unsupported"
    rows = table.findall(W + "tr")
    widths = {len(row.findall(W + "tc")) for row in rows}
    if (
        not rows
        or len(widths) != 1
        or 0 in widths
        or any(row.find(W + "trPr/" + W + "gridBefore") is not None or row.find(W + "trPr/" + W + "gridAfter") is not None for row in rows)
    ):
        return "word_table_structure_unsupported"
    return None


def _block_limitation(paragraph: etree._Element) -> str | None:
    tags = {node.tag for node in paragraph.iter()} | {node.tag for node in paragraph.iterancestors()}
    if tags & {W + name for name in ("ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange")}:
        return "word_tracked_change_unsupported"
    if W + "sdt" in tags:
        return "word_content_control_unsupported"
    if tags & {W + name for name in ("fldSimple", "fldChar", "instrText")}:
        return "word_field_unsupported"
    if tags & {W + name for name in ("drawing", "pict", "txbxContent")}:
        return "word_drawing_text_unsupported"
    if any(node.tag not in {W + name for name in ("pPr", "r", "proofErr", "bookmarkStart", "bookmarkEnd")} for node in paragraph):
        return "word_block_structure_unsupported"
    if any(child.tag not in {W + "rPr", W + "t", W + "lastRenderedPageBreak"} for run in paragraph.findall(W + "r") for child in run):
        return "word_run_structure_unsupported"
    if any(len(node) for node in paragraph.iter(W + "t")):
        return "word_run_structure_unsupported"
    return None


def _text_value(value: str) -> None:
    if not isinstance(value, str) or any(
        not (32 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF) for char in value
    ):
        raise OfficePackageError("word_replacement_text_invalid")
    if len(value.encode("utf-8")) > MAX_PART_BYTES:
        raise OfficePackageError("word_replacement_text_limit")


def _visible_text(element: etree._Element) -> str:
    return "".join(node.text or "" for node in element.iter(W + "t"))


def _properties(element: etree._Element | None) -> list[dict]:
    """Return explicit XML properties without resolving inherited formatting."""
    if element is None:
        return []
    return [
        {
            "name": node.tag.removeprefix(W),
            "attributes": {name.removeprefix(W): value for name, value in node.attrib.items()},
            "children": _properties(node),
        }
        for node in element
        if isinstance(node.tag, str)
    ]


def _escaped(value: str) -> bytes:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8")


def _qualified(raw: bytes, span: _Span) -> bytes:
    return raw[span.start + 1 : span.content_start].split(None, 1)[0].rstrip(b"/>")


def _element_value(raw: bytes, span: _Span, value: str) -> bytes:
    opening = raw[span.start : span.content_start]
    if span.empty:
        opening = opening.rstrip()[:-2] + b">"
    if value and (value[0].isspace() or value[-1].isspace()):
        if re.search(rb"\bxml:space\s*=", opening):
            opening = re.sub(rb"\bxml:space\s*=\s*(['\"])[^'\"]*\1", b'xml:space="preserve"', opening)
        else:
            opening = opening[:-1] + b' xml:space="preserve">'
    closing = b"</" + _qualified(raw, span) + b">" if span.empty else raw[span.content_end : span.end]
    return opening + _escaped(value) + closing


class DocumentTemplate:
    """An exact DOCX source with finite edits to ordinary text and fixed cells.

    Every locator, run and Unicode offset refers to the original source.
    Non-overlapping edits compose independently of call order. Inspection does
    not expose a mutable intermediate snapshot or resolve inherited formatting.
    """

    def __init__(self, source: bytes) -> None:
        info = inspect_office_package(source, filename="source.docx", declared_mime=OFFICE_MIMES[".docx"])
        self.source_sha256: str = info.sha256
        self._source = source
        self._source_path: Path | None = None
        with ZipFile(io.BytesIO(source)) as archive:
            self._parts = {item.filename: archive.read(item) for item in archive.infolist() if not item.is_dir()}
        self._roots: dict[str, etree._Element] = {}
        self._positions: dict[str, dict[etree._Element, _Span]] = {}
        self._blocks: dict[str, _Block] = {}
        self._tables: dict[str, _Table] = {}
        self._text_edits: dict[tuple[str, int], tuple[_Text, list[tuple[int, int, str]]]] = {}
        self._insertions: dict[str, list[tuple[int, int, bytes]]] = {}
        self._changes: list[WordChange] = []
        self._stories = self._stories_from_source()
        for story in self._stories:
            self._index_story(story["part"])

    @classmethod
    def open(cls, source: str | Path | bytes) -> DocumentTemplate:
        """Read an already authorized input; this helper grants no file access."""
        document = cls(source if isinstance(source, bytes) else Path(source).read_bytes())
        if not isinstance(source, bytes):
            document._source_path = Path(source).resolve()
        return document

    @property
    def changes(self) -> tuple[WordChange, ...]:
        return tuple(self._changes)

    def _root(self, part: str) -> etree._Element:
        if part not in self._roots:
            self._roots[part] = etree.fromstring(self._parts[part], parser=etree.XMLParser(resolve_entities=False, no_network=True))
        return self._roots[part]

    def _stories_from_source(self) -> list[dict]:
        sections = list(self._root("word/document.xml").iter(W + "sectPr"))
        self._section_properties = [{"section": index, "properties": _properties(section)} for index, section in enumerate(sections, 1)]
        stories = {
            "word/document.xml": {
                "part": "word/document.xml",
                "kind": "body",
                "sections": list(range(1, max(1, len(sections)) + 1)),
                "references": [],
            }
        }
        rel_part = "word/_rels/document.xml.rels"
        relationships = (
            {} if rel_part not in self._parts else {node.get("Id"): node for node in self._root(rel_part).findall("{" + REL_NS + "}Relationship")}
        )
        inherited: dict[tuple[str, str], str] = {}
        for section_index, section in enumerate(sections, 1):
            explicit = set()
            for kind in ("header", "footer"):
                for reference in section.findall(W + kind + "Reference"):
                    relationship = relationships.get(reference.get(R + "id"))
                    if relationship is None or relationship.get("TargetMode") == "External" or relationship.get("Type") != R[1:-1] + "/" + kind:
                        raise OfficePackageError("word_story_reference_invalid")
                    target = relationship.get("Target", "")
                    part = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("word", target))
                    if self._root(part).tag != W + ("hdr" if kind == "header" else "ftr"):
                        raise OfficePackageError("word_story_reference_invalid")
                    key = kind, reference.get(W + "type", "default")
                    inherited[key] = part
                    explicit.add(key)
            for (kind, reference_type), part in inherited.items():
                story = stories.setdefault(part, {"part": part, "kind": kind, "sections": [], "references": []})
                if section_index not in story["sections"]:
                    story["sections"].append(section_index)
                story["references"].append({"section": section_index, "type": reference_type, "inherited": (kind, reference_type) not in explicit})
        return list(stories.values())

    def _id(self, part: str, kind: str, index: int) -> str:
        return f"{self.source_sha256}:{part}#{kind}:{index}"

    def _index_story(self, part: str) -> None:
        root = self._root(part)
        encoding = root.getroottree().docinfo.encoding.lower().replace("-", "")
        supported_encoding = encoding in {"utf8", "ascii", "usascii"}
        positions = _spans(self._parts[part], root) if supported_encoding else {}
        self._positions[part] = positions
        tables = {}
        for index, element in enumerate(root.iter(W + "tbl")):
            table = _Table(
                self._id(part, "table", index), part, [row.findall(W + "tc") for row in element.findall(W + "tr")], _table_limitation(element)
            )
            tables[element] = table
            self._tables[table.id] = table
        for index, paragraph in enumerate(root.iter(W + "p")):
            block_id = self._id(part, "paragraph", index)
            limitation = _block_limitation(paragraph) if supported_encoding else "word_story_encoding_unsupported"
            block = _Block(block_id, part, paragraph, _visible_text(paragraph), limitation)
            ancestors = [node for node in paragraph.iterancestors() if node.tag == W + "tbl"]
            if ancestors:
                table = tables[ancestors[0]]
                cell = next(node for node in paragraph.iterancestors() if node.tag == W + "tc")
                block.table_id = table.id
                for row_index, row in enumerate(table.rows):
                    if cell in row:
                        block.row, block.column = row_index, row.index(cell)
                block.limitation = table.limitation or block.limitation
                if len(ancestors) > 1:
                    block.limitation = "word_nested_table_unsupported"
            offset = 0
            for run_index, run in enumerate(paragraph.findall(W + "r")):
                run_id = block_id + f"/run:{run_index}"
                block.runs.append((run_id, run))
                for node in run.findall(W + "t"):
                    value = node.text or ""
                    if supported_encoding:
                        block.texts.append(_Text(node, positions[node], run_id, offset, offset + len(value), value))
                    offset += len(value)
            self._blocks[block.id] = block

    def inspect(self, *, offset: int = 0, limit: int = 50, detail: Literal["index", "content"] = "index") -> dict:
        """Return bounded original-source facts, with zero-based cell addresses."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50 or detail not in {"index", "content"}:
            raise OfficePackageError("word_inspection_query_invalid")
        blocks = list(self._blocks.values())
        selected = []
        for block in blocks[offset : offset + limit]:
            value = {
                "id": block.id,
                "part": block.part,
                "text": block.text if detail == "content" else block.text[:120],
                "text_length": len(block.text),
                "editable": block.limitation is None,
                "limitation": block.limitation,
                "table_id": block.table_id,
                "row": block.row,
                "column": block.column,
            }
            if detail == "content":
                value["paragraph_properties"] = _properties(block.element.find(W + "pPr"))
                value["runs"] = [
                    {"id": run_id, "text": _visible_text(run), "properties": _properties(run.find(W + "rPr"))} for run_id, run in block.runs
                ]
            selected.append(value)
        result = {
            "source_sha256": self.source_sha256,
            "basis": "original_source",
            "stories": self._stories,
            "sections": self._section_properties,
            "blocks": selected,
            "tables": [
                {
                    "id": table.id,
                    "part": table.part,
                    "rows": len(table.rows),
                    "columns": len(table.rows[0]) if table.rows else 0,
                    "limitation": table.limitation,
                }
                for table in self._tables.values()
            ],
            "next_offset": offset + limit if offset + limit < len(blocks) else None,
            "limitations": [
                "Inherited effective formatting and page positions are not resolved.",
                "Fields, revisions, content controls, drawings, merged/nested tables and structural edits are not editable.",
                "Only UTF-8/ASCII stories and single-line replacement text are editable.",
            ],
        }
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_INSPECTION_BYTES:
            raise OfficePackageError("word_inspection_limit")
        return result

    def replace_text(self, target_id: str, *, start: int, end: int, text: str, style_run: str | None = None) -> WordChange | None:
        """Patch an original Unicode span; retain every unaffected byte outside text elements."""
        block = self._blocks.get(target_id)
        if block is None:
            raise OfficePackageError("word_target_not_found")
        if block.limitation:
            raise OfficePackageError(block.limitation)
        _text_value(text)
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(block.text):
            raise OfficePackageError("word_text_range_invalid")
        if any(
            change.target_id == target_id
            and (
                max(start, change.start) < min(end, change.end)
                or (start == end and change.start <= start <= change.end)
                or (change.start == change.end and start <= change.start <= end)
            )
            for change in self._changes
        ):
            raise OfficePackageError("word_text_ranges_overlap")
        if block.text[start:end] == text:
            return None
        covered = [node for node in block.texts if max(start, node.start) < min(end, node.end)]
        if start == end:
            covered = [node for node in block.texts if node.start <= start <= node.end]
        anchors = [node for node in covered if style_run is None or node.run_id == style_run]
        if block.texts:
            if not anchors:
                raise OfficePackageError("word_style_run_invalid")
            anchor = anchors[0]
            for node in covered:
                if start == end and node is not anchor:
                    continue
                edits = self._text_edits.setdefault((block.part, node.span.start), (node, []))[1]
                edits.append((max(start, node.start) - node.start, min(end, node.end) - node.start, text if node is anchor else ""))
        else:
            self._insert_empty(block, text, style_run)
        change = WordChange(target_id, block.part, start, end, block.text[start:end], text)
        self._changes.append(change)
        return change

    def _insert_empty(self, block: _Block, text: str, style_run: str | None) -> None:
        runs = [run for run_id, run in block.runs if style_run is None or style_run == run_id]
        if style_run is not None and not runs:
            raise OfficePackageError("word_style_run_invalid")
        container = runs[0] if runs else block.element
        span = self._positions[block.part][container]
        raw = self._parts[block.part]
        name = _qualified(raw, span)
        prefix = name.rsplit(b":", 1)[0] + b":" if b":" in name else b""
        value = b"<" + prefix + b't xml:space="preserve">' + _escaped(text) + b"</" + prefix + b"t>"
        if not runs:
            value = b"<" + prefix + b"r>" + value + b"</" + prefix + b"r>"
        if span.empty:
            patch = span.start, span.end, raw[span.start : span.content_start].rstrip()[:-2] + b">" + value + b"</" + name + b">"
        else:
            patch = span.content_end, span.content_end, value
        self._insertions.setdefault(block.part, []).append(patch)

    def set_table_cell(self, table_id: str, *, row: int, column: int, text: str) -> WordChange | None:
        """Fill one ordinary single-paragraph cell without rebuilding its table."""
        table = self._tables.get(table_id)
        if table is None:
            raise OfficePackageError("word_target_not_found")
        if table.limitation:
            raise OfficePackageError(table.limitation)
        if type(row) is not int or type(column) is not int or not 0 <= row < len(table.rows) or not 0 <= column < len(table.rows[row]):
            raise OfficePackageError("word_cell_address_invalid")
        cell = table.rows[row][column]
        paragraphs = cell.findall(W + "p")
        if len(paragraphs) != 1:
            raise OfficePackageError("word_cell_structure_unsupported")
        block = next(item for item in self._blocks.values() if item.element is paragraphs[0])
        return self.replace_text(block.id, start=0, end=len(block.text), text=text)

    def to_bytes(self) -> bytes:
        """Preserve ZIP entries and apply only explicit original-byte patches."""
        patches = {part: list(items) for part, items in self._insertions.items()}
        for (part, _), (node, edits) in self._text_edits.items():
            value = node.value
            for start, end, text in sorted(edits, reverse=True):
                value = value[:start] + text + value[end:]
            if value != node.value:
                patches.setdefault(part, []).append((node.span.start, node.span.end, _element_value(self._parts[part], node.span, value)))
        if not patches:
            return self._source
        changed = {}
        for part, items in patches.items():
            raw = self._parts[part]
            for start, end, replacement in sorted(items, reverse=True):
                raw = raw[:start] + replacement + raw[end:]
            changed[part] = raw
        output = io.BytesIO()
        with ZipFile(io.BytesIO(self._source)) as source, ZipFile(output, "w") as destination:
            for entry in source.infolist():
                destination.writestr(entry, changed.get(entry.filename, source.read(entry)))
            destination.comment = source.comment
        result = output.getvalue()
        inspect_office_package(result, filename="candidate.docx", declared_mime=OFFICE_MIMES[".docx"])
        return result

    def save(self, path: str | Path) -> None:
        """Write a separate candidate; never overwrite this helper's source path."""
        destination = Path(path)
        if self._source_path is not None and (
            destination.resolve() == self._source_path or (destination.exists() and destination.samefile(self._source_path))
        ):
            raise OfficePackageError("word_source_overwrite_forbidden")
        destination.write_bytes(self.to_bytes())
