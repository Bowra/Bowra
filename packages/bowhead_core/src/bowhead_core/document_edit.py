"""Inline editing of Word document text (docs/sprints/docx-inline-edit-2026-11-26/tech-design.md §6).

Runs only inside the bounded parse child (``untrusted_parse_worker``): the document is untrusted
user or model output. Two entry points, shaped like ``presentation_edit``:

- ``build_inventory``: every body and table-cell paragraph of the main document part with its path
  key, flattened text, heading level and whether it can be edited here (and why not). Headers,
  footers, notes and comments are counted, not listed.
- ``apply_edits``: applies a list of paragraph edits to a copy and returns the new bytes. The writer
  edits each ``w:t`` in place, so unchanged characters keep their run and every container
  (hyperlink, field, content control, bookmark, comment range) stays where it was. Only touched
  story parts are re-serialised, every other zip entry is copied byte for byte, and every edit must
  match the text the person saw (``expect``) or the whole save is refused. python-docx never
  saves a file: it drops relationship parts and rewrites ``[Content_Types].xml`` (spike §2.2).

Keys are paths from the story root: ``document:b4.r2.c1.b0`` is paragraph 0 of row 2, cell 1 of
the table at body block 4; ``document:b7.x0.b1`` is paragraph 1 of the first text box anchored in
body paragraph 7. Text is flattened into characters, ``\\t`` for a tab, ``\\n`` for a soft line
break and one ``FIELD_TOKEN`` per atomic object (a field result, inline content control, drawing,
chart, object, footnote reference, symbol, page or column break, math). Tokens cannot change.
"""

from __future__ import annotations

import copy
import difflib
import io
import re
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote

from lxml import etree

from bowhead_core.presentation_edit import FIELD_TOKEN

MAX_EDITS = 200
MAX_NEW_TEXT_CHARS = 20_000
MAX_TEXT_CHARS = 5_000
#: Paragraphs walked in the main document part (body, cells, text boxes); about 600 pages.
MAX_PARAGRAPHS = 20_000

#: Paragraph reasons (design §9). The first two apply only to keys outside the main document.
READ_ONLY_REASONS = ("header_footer", "note", "tracked_change", "field_span", "content_control", "text_box", "merged_continuation")
DOCUMENT_REFUSALS = ("protection_enforced", "digitally_signed", "track_changes_on")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_MAIN_PART = "word/document.xml"
#: Page size when the first section names none (US Letter, in twips).
_DEFAULT_PAGE = (12240, 15840)
#: Run content that shows nothing in the paragraph's text (and is never touched by the writer).
_RUN_INVISIBLE = frozenset(
    {
        "rPr",
        "softHyphen",
        "lastRenderedPageBreak",
        "commentReference",
        "annotationRef",
        "footnoteRef",
        "endnoteRef",
        "separator",
        "continuationSeparator",
    }
)
#: Inline containers whose runs are ordinary paragraph text.
_TRANSPARENT = frozenset({"hyperlink", "smartTag", "customXml", "dir", "bdo"})
_TRACKED_INLINE = frozenset({"ins", "del", "moveFrom", "moveTo"})
#: Characters a new text may hold besides printable ones: tab and soft line break.
_ALLOWED_CONTROL = frozenset({"\t", "\n"})


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


def _parser() -> Any:
    """A parser for untrusted parts: no entity expansion, DTD or network (one per use; lxml parsers
    are not shared across threads)."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def is_word_document(mime: str | None, filename: str | None) -> bool:
    """A .docx document, judged the way Office uploads are judged (a conflicting type is not one)."""
    from bowhead_core.documents.package_io import OFFICE_MIMES, OfficePackageError, office_upload_mime

    try:
        return office_upload_mime(filename or "", mime or "") == OFFICE_MIMES[".docx"]
    except OfficePackageError:
        return False


class EditRefused(Exception):
    """One or more edits cannot be applied; nothing is written.

    ``refusals`` lists every refused edit as ``{"index", "key", "code"}`` plus ``reason`` when the
    code has one (``not_editable`` carries the read-only reason; ``invalid_request`` and
    ``text_too_long`` name the failed check). A document-level refusal has ``index`` and ``key``
    ``None``. ``code`` and ``detail`` (everything but the code) describe the first refusal.
    """

    def __init__(self, refusals: Sequence[Mapping[str, Any]]) -> None:
        self.refusals = [dict(item) for item in refusals]
        first = self.refusals[0] if self.refusals else {"code": "invalid_request"}
        self.code = str(first.get("code"))
        self.detail = {name: value for name, value in first.items() if name != "code"}
        super().__init__(",".join(str(item.get("code")) for item in self.refusals))


class DocumentUnreadable(Exception):
    """The document cannot be opened, exceeds a limit, or a saved copy failed verification.

    ``code`` is ``unreadable``, ``paragraph_limit`` or ``verification_failed``.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ── Package ──────────────────────────────────────────────────────────────────────────


@dataclass
class _Story:
    part: str
    prefix: str
    kind: str  # "document" | "header_footer" | "note" | "comment"
    root: Any
    #: The relationship that names the part: "header", "footer", "footnotes", "endnotes", "comments".
    role: str = ""


def _resolve(base_dir: str, target: str) -> str:
    raw = unquote(target)
    raw = raw.lstrip("/") if raw.startswith("/") else (f"{base_dir}/{raw}" if base_dir else raw)
    parts: list[str] = []
    for piece in raw.split("/"):
        if piece in {"", "."}:
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts)


class _Package:
    def __init__(self, data: bytes) -> None:
        self.parser = _parser()
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.infos = self.zip.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError) as exc:
            raise DocumentUnreadable("unreadable") from exc
        self.names = {info.filename for info in self.infos}
        self.main = next((target for kind, target in self.relationships("") if kind == "officeDocument"), _MAIN_PART)
        root = self.parse(self.main)
        if root is None or root.tag != _w("document") or root.find(_w("body")) is None:
            raise DocumentUnreadable("unreadable")
        self.stories = [_Story(self.main, "document", "document", root)]
        seen = {self.main}
        kinds = {"header": "header_footer", "footer": "header_footer", "footnotes": "note", "endnotes": "note", "comments": "comment"}
        for kind, target in self.relationships(self.main):
            if kind not in kinds or target in seen or target not in self.names:
                continue
            seen.add(target)
            story_root = self.parse(target)
            if story_root is not None:
                self.stories.append(_Story(target, PurePosixPath(target).stem, kinds[kind], story_root, kind))

    def read(self, name: str) -> bytes:
        try:
            return self.zip.read(name)
        except Exception as exc:  # noqa: BLE001 - a damaged, encrypted or unsupported entry means the file is unreadable
            raise DocumentUnreadable("unreadable") from exc

    def parse(self, name: str) -> Any:
        if name not in self.names:
            return None
        try:
            return etree.fromstring(self.read(name), self.parser)
        except etree.XMLSyntaxError as exc:
            raise DocumentUnreadable("unreadable") from exc

    def relationships(self, source: str) -> Iterator[tuple[str, str]]:
        """``(type, target part)`` for each internal relationship of ``source`` ("" is the package)."""
        head, _, tail = source.rpartition("/")
        rels = f"{head}/_rels/{tail}.rels" if head else (f"_rels/{tail}.rels" if tail else "_rels/.rels")
        root = self.parse(rels)
        if root is None:
            return
        for rel in root.iter(f"{{{_REL}}}Relationship"):
            target = rel.get("Target")
            if not target or rel.get("TargetMode") == "External":
                continue
            yield (rel.get("Type") or "").rsplit("/", 1)[-1], _resolve(head, target)


# ── Walk ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Scope:
    text_box: bool = False
    control: bool = False
    tracked: bool = False
    continuation: bool = False
    cell: Mapping[str, Any] | None = None


@dataclass(eq=False, slots=True)
class _Atom:
    """One visible thing in a paragraph: a character, tab, soft break or atomic object.

    ``group`` is the outermost atomic object the atom belongs to: a complex field (its begin token
    atom) or an element (``w:fldSimple``, inline ``w:sdt``). A complex field's token keeps where it
    began and ended so a field that spans paragraphs can be told apart.
    """

    ch: str
    kind: str  # "text" | "tab" | "br" | "token"
    el: Any
    group: Any = None
    para: Any = None
    end_el: Any = None
    end_para: Any = None


@dataclass(eq=False)
class _Paragraph:
    key: str
    el: Any
    scope: _Scope
    starts_in_field: bool
    atoms: list[_Atom] = field(default_factory=list)
    tracked: bool = False
    ends_in_field: bool = False
    boxes: int = 0


class _Walker:
    """Walks one story part. Complex-field state carries across paragraphs (a TOC spans many)."""

    def __init__(self, prefix: str, limit: int | None = None) -> None:
        self.prefix = prefix
        self.limit = limit
        self.paragraphs: list[_Paragraph] = []
        self.fields: list[list[Any]] = []  # open complex fields: [in_result]
        self.group: Any = None
        self.group_depth = 0

    # Blocks
    def blocks(self, container: Any, path: str, scope: _Scope) -> None:
        index = 0
        for child in container:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            here = f"{path}.b{index}" if path else f"b{index}"
            if tag == _w("p"):
                self.paragraph(child, here, scope)
            elif tag == _w("tbl"):
                self.table(child, here, scope)
            elif tag == _w("sdt"):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    self.blocks(content, here, replace(scope, control=True))
            elif tag == _w("customXml"):
                self.blocks(child, here, scope)
            elif tag in {_w("ins"), _w("moveTo")}:
                self.blocks(child, here, replace(scope, tracked=True))
            elif tag == f"{{{_MC}}}AlternateContent":
                choice = _choice(child)
                if choice is not None:
                    self.blocks(choice, here, scope)
            else:
                continue
            index += 1

    def table(self, tbl: Any, path: str, scope: _Scope) -> None:
        rows: list[list[tuple[Any, int, int, str | None, bool, bool]]] = []
        for tr, row_control in _wrapped(tbl, _w("tr")):
            trpr = tr.find(_w("trPr"))
            col = _int_val(trpr.find(_w("gridBefore")) if trpr is not None else None, 0)
            row_tracked = trpr is not None and any(trpr.find(_w(tag)) is not None for tag in ("ins", "del", "trPrChange"))
            cells = []
            for tc, cell_control in _wrapped(tr, _w("tc")):
                tcpr = tc.find(_w("tcPr"))
                span = max(1, _int_val(tcpr.find(_w("gridSpan")) if tcpr is not None else None, 1))
                merge = tcpr.find(_w("vMerge")) if tcpr is not None else None
                state = None if merge is None else ("restart" if merge.get(_w("val")) == "restart" else "continue")
                cell_tracked = tcpr is not None and any(tcpr.find(_w(tag)) is not None for tag in ("cellIns", "cellDel", "cellMerge", "tcPrChange"))
                cells.append((tc, col, span, state, row_control or cell_control, row_tracked or cell_tracked))
                col += span
            rows.append(cells)
        # Continuation cells directly below each grid column, counted from the bottom row up.
        below: list[dict[int, int]] = [{} for _ in rows]
        following: dict[int, int] = {}
        for row_index in range(len(rows) - 1, -1, -1):
            below[row_index] = following
            following = {col: 1 + following.get(col, 0) for _, col, _, state, _, _ in rows[row_index] if state == "continue"}
        table_key = f"{self.prefix}:{path}"
        for row_index, cells in enumerate(rows):
            for cell_index, (tc, col, span, state, control, tracked) in enumerate(cells):
                row_span = 1 + below[row_index].get(col, 0) if state == "restart" else 1
                cell = {"key": table_key, "row": row_index, "col": col, "row_span": row_span, "col_span": span}
                cell_scope = replace(
                    scope,
                    cell=cell,
                    control=scope.control or control,
                    tracked=scope.tracked or tracked,
                    continuation=state == "continue",
                )
                self.blocks(tc, f"{path}.r{row_index}.c{cell_index}", cell_scope)

    # Paragraphs
    def paragraph(self, p: Any, path: str, scope: _Scope) -> None:
        if self.limit is not None and len(self.paragraphs) >= self.limit:
            raise DocumentUnreadable("paragraph_limit")
        para = _Paragraph(key=f"{self.prefix}:{path}", el=p, scope=scope, starts_in_field=bool(self.fields), tracked=scope.tracked)
        self.paragraphs.append(para)
        ppr = p.find(_w("pPr"))
        if ppr is not None:
            mark = ppr.find(_w("rPr"))
            if ppr.find(_w("pPrChange")) is not None or (mark is not None and any(mark.find(_w(tag)) is not None for tag in _TRACKED_INLINE)):
                para.tracked = True
        self.inline(p, para, path)
        para.ends_in_field = bool(self.fields)

    def _token(self, para: _Paragraph, el: Any) -> _Atom:
        atom = _Atom(FIELD_TOKEN, "token", el, group=self.group)
        para.atoms.append(atom)
        return atom

    def _open_group(self, para: _Paragraph, el: Any) -> bool:
        if self.group is not None:
            return False
        self._token(para, el).group = el
        self.group = el
        return True

    def inline(self, parent: Any, para: _Paragraph, path: str) -> None:
        for child in parent:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag in {f"{{{_M}}}oMath", f"{{{_M}}}oMathPara"}:
                self._token(para, child)
                continue
            if tag == f"{{{_MC}}}AlternateContent":
                # One token: an edit in the chosen branch would leave the fallback branch stale.
                opened = self._open_group(para, child)
                choice = _choice(child)
                if choice is not None:
                    self.inline(choice, para, path)
                if opened:
                    self.group = None
                continue
            if not tag.startswith(f"{{{_W}}}"):
                continue
            name = tag[len(_W) + 2 :]
            if name == "r":
                self.run(child, para, path)
            elif name in _TRANSPARENT:
                self.inline(child, para, path)
            elif name in _TRACKED_INLINE:
                para.tracked = True
                self.inline(child, para, path)
            elif name == "sdt":
                opened = self._open_group(para, child)
                content = child.find(_w("sdtContent"))
                if content is not None:
                    self.inline(content, para, path)
                if opened:
                    self.group = None
            elif name == "fldSimple":
                opened = self._open_group(para, child)
                self.inline(child, para, path)
                if opened:
                    self.group = None

    def run(self, r: Any, para: _Paragraph, path: str) -> None:
        rpr = r.find(_w("rPr"))
        if rpr is not None and rpr.find(_w("rPrChange")) is not None:
            para.tracked = True
        for child in r:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == f"{{{_MC}}}AlternateContent":
                if not self.fields or self.fields[-1][0]:
                    self.objects(child, para, path)
                continue
            if not tag.startswith(f"{{{_W}}}"):
                continue
            name = tag[len(_W) + 2 :]
            if name == "fldChar":
                kind = child.get(_w("fldCharType"))
                if kind == "begin":
                    if self.group is None:
                        token = self._token(para, child)
                        token.group, token.para = token, para
                        self.group, self.group_depth = token, len(self.fields) + 1
                    self.fields.append([False])
                elif kind == "separate" and self.fields:
                    self.fields[-1][0] = True
                elif kind == "end" and self.fields:
                    self.fields.pop()
                    if isinstance(self.group, _Atom) and len(self.fields) < self.group_depth:
                        self.group.end_el, self.group.end_para = child, para
                        self.group = None
                continue
            if name in {"instrText", "delInstrText"} or name in _RUN_INVISIBLE:
                continue
            if self.fields and not self.fields[-1][0]:
                continue  # a field's code is not shown
            if name == "t":
                para.atoms.extend(_Atom(ch, "text", child, group=self.group) for ch in child.text or "")
            elif name == "delText":
                para.tracked = True
            elif name == "tab":
                para.atoms.append(_Atom("\t", "tab", child, group=self.group))
            elif name in {"br", "cr"}:
                if child.get(_w("type")) in {"page", "column"}:
                    self._token(para, child)
                else:
                    para.atoms.append(_Atom("\n", "br", child, group=self.group))
            elif name in {"drawing", "pict", "object"}:
                self.objects(child, para, path)
            else:  # symbol, no-break hyphen, footnote reference, page number, date part, ruby, anything unknown
                self._token(para, child)

    def objects(self, el: Any, para: _Paragraph, path: str) -> None:
        """A drawing, picture or object is one token; text boxes inside it become their own paragraphs."""
        self._token(para, el)
        search = _choice(el) if el.tag == f"{{{_MC}}}AlternateContent" else el
        if search is None:
            return
        boxes = list(search.iter(_w("txbxContent")))
        outer = [box for box in boxes if not any(ancestor.tag == _w("txbxContent") for ancestor in box.iterancestors())]
        for box in outer:
            box_path = f"{path}.x{para.boxes}"
            para.boxes += 1
            saved = (self.fields, self.group, self.group_depth)
            self.fields, self.group, self.group_depth = [], None, 0
            self.blocks(box, box_path, _Scope(text_box=True))
            self.fields, self.group, self.group_depth = saved


def _choice(alternate: Any) -> Any:
    choice = alternate.find(f"{{{_MC}}}Choice")
    return choice if choice is not None else alternate.find(f"{{{_MC}}}Fallback")


def _wrapped(parent: Any, tag: str, control: bool = False) -> Iterator[tuple[Any, bool]]:
    """Rows of a table or cells of a row, including those wrapped in a content control or custom XML."""
    for child in parent:
        if child.tag == tag:
            yield child, control
        elif child.tag == _w("sdt"):
            content = child.find(_w("sdtContent"))
            if content is not None:
                yield from _wrapped(content, tag, True)
        elif child.tag == _w("customXml"):
            yield from _wrapped(child, tag, control)


def _int_val(el: Any, default: int) -> int:
    if el is None:
        return default
    try:
        return int(el.get(_w("val"), default))
    except (TypeError, ValueError):
        return default


def _walk(story: _Story, limit: int | None = None) -> list[_Paragraph]:
    walker = _Walker(story.prefix, limit)
    if story.kind == "document":
        walker.blocks(story.root.find(_w("body")), "", _Scope())
    elif story.kind == "header_footer":
        walker.blocks(story.root, "", _Scope())
    else:  # one container per note or comment
        for index, note in enumerate(story.root):
            if not isinstance(note.tag, str) or note.get(_w("type")) in {"separator", "continuationSeparator", "continuationNotice"}:
                continue
            walker.blocks(note, f"n{index}", _Scope())
    return walker.paragraphs


# ── Paragraph text and reasons ──────────────────────────────────────────────────────


@dataclass(eq=False, slots=True)
class _Unit:
    """One character of the editor text. A token's ``el``/``last`` are its first and last elements."""

    ch: str
    kind: str  # "text" | "tab" | "br" | "token"
    el: Any
    last: Any = None


def _whole(group: Any, para: _Paragraph) -> bool:
    """Whether an atomic object begins and ends inside this paragraph."""
    return not isinstance(group, _Atom) or (group.para is para and group.end_para is para)


def _units(para: _Paragraph) -> list[_Unit]:
    """The editor text of a paragraph. An object inside the paragraph is one token; the content of a
    field that spans paragraphs (a TOC) is shown as its characters, since it is never edited."""
    units: list[_Unit] = []
    seen: set[int] = set()
    for atom in para.atoms:
        group = atom.group
        if group is not None and _whole(group, para):
            if id(group) in seen:
                continue
            seen.add(id(group))
            if isinstance(group, _Atom):
                units.append(_Unit(FIELD_TOKEN, "token", group.el, group.end_el))
            else:
                units.append(_Unit(FIELD_TOKEN, "token", group, group))
        elif atom.kind == "token":
            if atom.group is not atom:  # the begin of a spanning field shows nothing
                units.append(_Unit(FIELD_TOKEN, "token", atom.el, atom.el))
        else:
            units.append(_Unit(atom.ch, atom.kind, atom.el))
    return units


def _text(para: _Paragraph) -> str:
    return "".join(unit.ch for unit in _units(para))


def _reason(para: _Paragraph) -> str | None:
    """Why a main-document paragraph is read-only, most lasting cause first (design §9)."""
    if para.scope.text_box:
        return "text_box"
    if para.scope.continuation:
        return "merged_continuation"
    if para.starts_in_field or para.ends_in_field:
        return "field_span"
    if para.scope.control:
        return "content_control"
    if para.tracked:
        return "tracked_change"
    return None


# ── Style-keeping writer ─────────────────────────────────────────────────────────────


@dataclass
class _Plan:
    keep: list[bool]
    before: dict[int, list[str]]
    after: dict[int, list[str]]


def _plan(units: Sequence[_Unit], new: str) -> _Plan:
    """Where each new character goes, without touching the tree. Raises ``EditRefused`` when a token would change."""
    old = "".join(unit.ch for unit in units)
    if old.count(FIELD_TOKEN) != new.count(FIELD_TOKEN):
        raise EditRefused([{"code": "token_changed"}])
    plan = _Plan(keep=[True] * len(units), before={}, after={})
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if FIELD_TOKEN in old[i1:i2] or FIELD_TOKEN in new[j1:j2]:
            raise EditRefused([{"code": "token_changed"}])
        for index in range(i1, i2):
            plan.keep[index] = False
        if j1 == j2:
            continue
        text = new[j1:j2]
        first_text = next((index for index in range(i1, i2) if units[index].kind == "text"), None)
        if tag == "replace" and first_text is not None:
            plan.before.setdefault(first_text, []).append(text)  # into the first replaced character's w:t
        elif i1 > 0:
            plan.after.setdefault(i1 - 1, []).append(text)  # after the left neighbour
        else:
            plan.before.setdefault(i2 if tag == "replace" and i2 < len(units) else 0, []).append(text)
    return plan


def _materialize(text: str, literal: Sequence[bool] | None = None) -> list[Any]:
    """Run content for ``text``: ``\\t`` and ``\\n`` become ``w:tab``/``w:br`` unless ``literal`` keeps
    that character as ``w:t`` text (a tab or newline that was already stored inside a ``w:t``)."""
    elements: list[Any] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            t = etree.Element(_w("t"))
            t.text = "".join(buffer)
            t.set(_XML_SPACE, "preserve")
            elements.append(t)
            buffer.clear()

    for index, ch in enumerate(text):
        if literal is not None and literal[index]:
            buffer.append(ch)
        elif ch == "\t":
            flush()
            elements.append(etree.Element(_w("tab")))
        elif ch == "\n":
            flush()
            elements.append(etree.Element(_w("br")))
        else:
            buffer.append(ch)
    flush()
    return elements


def _clean_rpr(rpr: Any, tag: str = "rPr") -> Any:
    rpr = copy.deepcopy(rpr)
    rpr.tag = _w(tag)
    for child in list(rpr):
        if child.tag in {_w("rPrChange"), _w("ins"), _w("del"), _w("moveFrom"), _w("moveTo")}:
            rpr.remove(child)
    return rpr


def _new_run(p: Any, style_run: Any, text: str) -> Any:
    """A run for ``text`` styled like ``style_run``, or like the paragraph mark when there is none."""
    run = etree.Element(_w("r"))
    source = style_run.find(_w("rPr")) if style_run is not None else None
    if source is None and style_run is None:
        ppr = p.find(_w("pPr"))
        source = ppr.find(_w("rPr")) if ppr is not None else None
    if source is not None:
        run.append(_clean_rpr(source))
    run.extend(_materialize(text))
    return run


def _split_run(run: Any, child: Any, side: str) -> None:
    """Move the content after (``side="after"``) or before ``child`` into a sibling run with the same
    properties, so a new run can sit right next to ``child``."""
    kids = list(run)
    index = kids.index(child)
    move = kids[index + 1 :] if side == "after" else [kid for kid in kids[:index] if kid.tag != _w("rPr")]
    if not [kid for kid in move if kid.tag != _w("lastRenderedPageBreak")]:
        return
    clone = etree.Element(_w("r"))
    rpr = run.find(_w("rPr"))
    if rpr is not None:
        clone.append(copy.deepcopy(rpr))
    clone.extend(move)
    if side == "after":
        run.addnext(clone)
    else:
        run.addprevious(clone)


def _place_next_to(el: Any, side: str, new: Any) -> None:
    """Put ``new`` right before or after ``el``; inside a run, the run is split there first."""
    parent = el.getparent()
    if parent is not None and parent.tag == _w("r"):
        _split_run(parent, el, side)
        el = parent
    if side == "after":
        el.addnext(new)
    else:
        el.addprevious(new)


def _has_content(run: Any) -> bool:
    return any(isinstance(kid.tag, str) and kid.tag not in {_w("rPr"), _w("lastRenderedPageBreak")} for kid in run)


def _write(para: _Paragraph, units: list[_Unit], plan: _Plan, new: str) -> None:
    """Edit the paragraph in place (design §6.3): kept characters stay in their ``w:t``; replaced
    characters go into the first replaced character's ``w:t``; inserted ones after their left
    neighbour; text next to a token goes into a run cloned from the nearest text run."""
    p = para.el
    if not units:
        if new:
            p.append(_new_run(p, None, new))
        return

    def nearest_text_run(start: int, step: int) -> Any:
        index = start
        while 0 <= index < len(units):
            if units[index].kind == "text":
                return units[index].el.getparent()
            index += step
        return None

    emptied: list[Any] = []
    # 1. Text that sits next to a tab, break or token: new elements or runs beside it.
    for index, unit in enumerate(units):
        if unit.kind == "text":
            continue
        for side, table in (("before", plan.before), ("after", plan.after)):
            if index not in table:
                continue
            text = "".join(table[index])
            if unit.kind in {"tab", "br"}:
                anchor = unit.el
                for element in _materialize(text):
                    if side == "after":
                        anchor.addnext(element)
                        anchor = element
                    else:
                        anchor.addprevious(element)
                continue
            if side == "after":
                style = nearest_text_run(index - 1, -1)
                style = style if style is not None else nearest_text_run(index + 1, 1)
            else:
                style = nearest_text_run(index + 1, 1)
                style = style if style is not None else nearest_text_run(index - 1, -1)
            boundary = unit.last if side == "after" else unit.el
            _place_next_to(boundary, side, _new_run(p, style, text))

    # 2. Rewrite every w:t that holds a deleted character or an insertion point.
    by_t: dict[int, list[int]] = {}
    for index, unit in enumerate(units):
        if unit.kind == "text":
            by_t.setdefault(id(unit.el), []).append(index)
    for indexes in by_t.values():
        if all(plan.keep[i] for i in indexes) and not any(i in plan.before or i in plan.after for i in indexes):
            continue
        t = units[indexes[0]].el
        pieces: list[str] = []
        literal: list[bool] = []
        for i in indexes:
            for piece in plan.before.get(i, []):
                pieces.append(piece)
                literal.extend([False] * len(piece))
            if plan.keep[i]:
                pieces.append(units[i].ch)
                literal.append(True)
            for piece in plan.after.get(i, []):
                pieces.append(piece)
                literal.extend([False] * len(piece))
        text = "".join(pieces)
        run = t.getparent()
        emptied.append(run)
        if any(ch in _ALLOWED_CONTROL and not literal[k] for k, ch in enumerate(text)):
            for element in _materialize(text, literal):
                t.addprevious(element)
            run.remove(t)
        elif text:
            t.text = text
            t.set(_XML_SPACE, "preserve")
        else:
            run.remove(t)

    # 3. Deleted tabs and breaks.
    for index, unit in enumerate(units):
        if unit.kind in {"tab", "br"} and not plan.keep[index]:
            run = unit.el.getparent()
            run.remove(unit.el)
            emptied.append(run)

    # 4. Runs left without content disappear (markers outside the run stay).
    for run in emptied:
        parent = run.getparent()
        if parent is not None and not _has_content(run):
            parent.remove(run)


# ── Document facts ───────────────────────────────────────────────────────────────────

_TWIPS_PER_UNIT = {"mm": 1440 / 25.4, "cm": 1440 / 2.54, "in": 1440.0, "pt": 20.0, "pc": 240.0, "pi": 240.0}
_MEASURE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|pt|pc|pi)?\s*$")
_HEADING_NAME = re.compile(r"^heading ([1-9])$")


def _twips(value: str | None) -> int | None:
    match = _MEASURE.match(value or "")
    if match is None:
        return None
    number = float(match.group(1)) * (_TWIPS_PER_UNIT[match.group(2)] if match.group(2) else 1.0)
    return int(round(number)) if number > 0 else None


def _page(root: Any) -> dict[str, int]:
    section = next(root.find(_w("body")).iter(_w("sectPr")), None)
    size = section.find(_w("pgSz")) if section is not None else None
    width = _twips(size.get(_w("w"))) if size is not None else None
    height = _twips(size.get(_w("h"))) if size is not None else None
    return {"width": width or _DEFAULT_PAGE[0], "height": height or _DEFAULT_PAGE[1]}


def _settings(package: _Package) -> Any:
    target = next((target for kind, target in package.relationships(package.main) if kind == "settings"), "word/settings.xml")
    return package.parse(target)


def _on(el: Any, attribute: str = "val", default: bool = True) -> bool:
    value = el.get(_w(attribute))
    return default if value is None else value.strip().lower() not in {"0", "false", "off"}


def _document_refusal(package: _Package) -> str | None:
    settings = _settings(package)
    if settings is not None:
        protection = settings.find(_w("documentProtection"))
        if (protection is not None and _on(protection, "enforcement", default=False)) or settings.find(_w("writeProtection")) is not None:
            return "protection_enforced"
    signed = any(name.startswith("_xmlsignatures/") for name in package.names) or any(kind == "origin" for kind, _ in package.relationships(""))
    if signed:
        return "digitally_signed"
    if settings is not None:
        track = settings.find(_w("trackRevisions"))
        if track is not None and _on(track):
            return "track_changes_on"
    return None


class _Headings:
    """Outline levels of paragraph styles, following ``w:basedOn``."""

    def __init__(self, package: _Package) -> None:
        target = next((target for kind, target in package.relationships(package.main) if kind == "styles"), "word/styles.xml")
        root = package.parse(target)
        self.styles: dict[str, tuple[int | None, str | None, str]] = {}
        self.default: str | None = None
        self.cache: dict[str, int | None] = {}
        if root is None:
            return
        for style in root.iter(_w("style")):
            if style.get(_w("type")) not in {None, "paragraph"}:
                continue
            style_id = style.get(_w("styleId"))
            if not style_id:
                continue
            level = style.find(f"{_w('pPr')}/{_w('outlineLvl')}")
            based = style.find(_w("basedOn"))
            name = style.find(_w("name"))
            self.styles[style_id] = (
                _int_val(level, 9) if level is not None else None,
                based.get(_w("val")) if based is not None else None,
                (name.get(_w("val")) or "").strip().lower() if name is not None else "",
            )
            if _on(style, "default", default=False):
                self.default = style_id

    def style_level(self, style_id: str | None) -> int | None:
        if style_id is None:
            return None
        if style_id not in self.cache:
            self.cache[style_id] = None  # cycle guard
            level, based, name = self.styles.get(style_id, (None, None, ""))
            if level is None:
                match = _HEADING_NAME.match(name)
                level = int(match.group(1)) - 1 if match else self.style_level(based)
            self.cache[style_id] = level
        return self.cache[style_id]

    def heading_level(self, p: Any) -> int | None:
        ppr = p.find(_w("pPr"))
        direct = ppr.find(_w("outlineLvl")) if ppr is not None else None
        if direct is not None:
            level: int | None = _int_val(direct, 9)
        else:
            style = ppr.find(_w("pStyle")) if ppr is not None else None
            level = self.style_level(style.get(_w("val")) if style is not None else self.default)
        return level + 1 if level is not None and 0 <= level <= 8 else None


def _read_only_parts(package: _Package) -> dict[str, int]:
    """Headers and footers with text, notes with text, and comments: read-only here, counted only."""
    counts = {"header_footer": 0, "note": 0, "comment": 0}
    for story in package.stories:
        if story.kind == "header_footer":
            counts["header_footer"] += int(any(_text(para).strip() for para in _walk(story)))
        elif story.kind in {"note", "comment"}:
            containers = {para.key.split(".", 1)[0] for para in _walk(story) if _text(para).strip()}
            counts[story.kind] += len(containers)
    return counts


#: Read-only paragraphs listed with their text, at most (headers repeat the same few paragraphs).
MAX_READ_ONLY_PARAGRAPHS = 2_000
_READ_ONLY_PART = {"header": "header", "footer": "footer", "footnotes": "footnote", "endnotes": "endnote"}


def _read_only_paragraphs(package: _Package) -> list[dict[str, Any]]:
    """The text of headers, footers and notes, read-only here (docx inline edit design §9).

    Listed so the editor can find them on the rendered pages and say why they cannot change there.
    Comments are not drawn on the pages, so they stay a count in ``read_only_parts``.
    """
    items: list[dict[str, Any]] = []
    for story in package.stories:
        part = _READ_ONLY_PART.get(story.role)
        if part is None:
            continue
        for para in _walk(story):
            text = _text(para)
            if not text.strip():
                continue
            items.append({"key": para.key, "part": part, "text": text, "reason": "header_footer" if story.kind == "header_footer" else "note"})
            if len(items) >= MAX_READ_ONLY_PARAGRAPHS:
                return items
    return items


# ── Inventory ────────────────────────────────────────────────────────────────────────


def build_inventory(data: bytes) -> dict[str, Any]:
    """Every non-blank paragraph of the main document part in reading order, with the document's facts.

    A paragraph whose text is empty or only whitespace is left out: the editor finds paragraphs on
    the rendered pages by their text, so it could not place one. Its key still counts in the path,
    so listed keys never shift. ``document.refusal``, when set, refuses every edit.
    """
    package = _Package(data)
    document = package.stories[0]
    headings = _Headings(package)
    paragraphs: list[dict[str, Any]] = []
    for para in _walk(document, MAX_PARAGRAPHS):
        text = _text(para)
        if not text.strip():
            continue
        reason = _reason(para)
        cell = para.scope.cell
        paragraphs.append(
            {
                "key": para.key,
                "kind": "cell" if cell is not None else "body",
                "table": dict(cell) if cell is not None else None,
                "text": text,
                "heading_level": headings.heading_level(para.el),
                "editable": reason is None,
                "reason": reason,
            }
        )
    return {
        "page": _page(document.root),
        "field_token": FIELD_TOKEN,
        "document": {"refusal": _document_refusal(package)},
        "paragraphs": paragraphs,
        "read_only_parts": _read_only_parts(package),
        "read_only_paragraphs": _read_only_paragraphs(package),
    }


# ── Apply ────────────────────────────────────────────────────────────────────────────


def _bad_character(text: str, expect: str) -> bool:
    """A character XML cannot store, or a new control character such as a paragraph break.

    Tab and soft line break are allowed; a control character the paragraph already holds may stay.
    """
    for ch in set(text):
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF or code in {0xFFFE, 0xFFFF}:
            return True
        control = (code < 0x20 and ch not in _ALLOWED_CONTROL) or 0x7F <= code <= 0x9F or code in {0x2028, 0x2029}
        if control and ch not in expect:
            return True
    return False


def _validate_request(edits: Any) -> list[Mapping[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise EditRefused([{"index": None, "key": None, "code": "invalid_request", "reason": "no_edits"}])
    if len(edits) > MAX_EDITS:
        raise EditRefused([{"index": None, "key": None, "code": "too_many_edits"}])
    refusals: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for index, edit in enumerate(edits):
        key = edit.get("key") if isinstance(edit, Mapping) else None
        refusal = {"index": index, "key": key if isinstance(key, str) else None}
        if not isinstance(edit, Mapping) or not isinstance(key, str) or not all(isinstance(edit.get(name), str) for name in ("expect", "text")):
            refusals.append({**refusal, "code": "invalid_request", "reason": "malformed_edit"})
            continue
        text, expect = edit["text"], edit["expect"]
        if len(text) > MAX_TEXT_CHARS or len(expect) > MAX_TEXT_CHARS:
            refusals.append({**refusal, "code": "text_too_long", "reason": "paragraph"})
            continue
        if _bad_character(text, expect):
            refusals.append({**refusal, "code": "invalid_request", "reason": "control_character"})
            continue
        if key in seen:
            refusals.append({**refusal, "code": "invalid_request", "reason": "duplicate_key"})
            continue
        seen.add(key)
        total += len(text)
    if total > MAX_NEW_TEXT_CHARS:
        refusals.append({"index": None, "key": None, "code": "text_too_long", "reason": "total"})
    if refusals:
        raise EditRefused(refusals)
    return list(edits)


def _serialize(root: Any) -> bytes:
    tree = root.getroottree()
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=tree.docinfo.standalone)


def _preserving_save(package: _Package, rewritten: Mapping[str, bytes]) -> bytes:
    """Write ``rewritten`` parts; copy every other zip entry's bytes unchanged."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        target.comment = package.zip.comment
        for info in package.infos:
            # writestr records the new archive's offsets and sizes on the ZipInfo it gets; a copy
            # keeps the source archive readable for verification.
            entry = copy.copy(info)
            if info.filename in rewritten:
                target.writestr(entry, rewritten[info.filename], compress_type=zipfile.ZIP_DEFLATED)
            else:
                target.writestr(entry, package.read(info.filename), compress_type=info.compress_type)
    return out.getvalue()


def _verify(package: _Package, output: bytes, touched: set[str], expected: Mapping[str, str]) -> None:
    """Changed entries are a subset of the touched parts, which parse; every paragraph of the main
    document reads as before except the edited ones, which read as requested."""
    try:
        with zipfile.ZipFile(io.BytesIO(output)) as new:
            if new.namelist() != package.zip.namelist():
                raise DocumentUnreadable("verification_failed")
            for name in new.namelist():
                data = new.read(name)
                if name in touched:
                    etree.fromstring(data, package.parser)
                elif data != package.read(name):
                    raise DocumentUnreadable("verification_failed")
        after = {para.key: _text(para) for para in _walk(_Package(output).stories[0])}
    except DocumentUnreadable as exc:
        raise DocumentUnreadable("verification_failed") from exc
    except Exception as exc:  # noqa: BLE001 - any failure to reread the copy fails verification
        raise DocumentUnreadable("verification_failed") from exc
    if after != expected:
        raise DocumentUnreadable("verification_failed")


def apply_edits(data: bytes, edits: Any) -> tuple[bytes, dict[str, Any]]:
    """Apply ``edits`` (``[{"key", "expect", "text"}]``) to a copy of ``data``.

    Returns the new bytes and ``{"touched_parts", "changes"}``; an edit whose text equals the
    current text changes nothing and is not listed. Raises ``EditRefused`` (nothing written) or
    ``DocumentUnreadable``.
    """
    requested = _validate_request(edits)
    package = _Package(data)
    refusal = _document_refusal(package)
    if refusal is not None:
        raise EditRefused([{"index": None, "key": None, "code": refusal}])
    document = package.stories[0]
    walked = {story.prefix: _walk(story, MAX_PARAGRAPHS if story is document else None) for story in package.stories}
    by_key = {para.key: (story, para) for story in package.stories for para in walked[story.prefix]}

    refusals: list[dict[str, Any]] = []
    plans: list[tuple[int, Mapping[str, Any], _Paragraph, list[_Unit], _Plan]] = []
    for index, edit in enumerate(requested):
        base = {"index": index, "key": edit["key"]}
        found = by_key.get(edit["key"])
        if found is None or found[0].kind == "comment":
            refusals.append({**base, "code": "not_found"})
            continue
        story, para = found
        reason = {"header_footer": "header_footer", "note": "note"}.get(story.kind) or _reason(para)
        if reason is not None:
            refusals.append({**base, "code": "not_editable", "reason": reason})
            continue
        units = _units(para)
        if "".join(unit.ch for unit in units) != edit["expect"]:
            refusals.append({**base, "code": "text_changed"})
            continue
        try:
            plan = _plan(units, edit["text"])
        except EditRefused as exc:
            refusals.extend({**base, "code": item.get("code")} for item in exc.refusals)
            continue
        plans.append((index, edit, para, units, plan))
    if refusals:
        raise EditRefused(refusals)

    expected = {para.key: _text(para) for para in walked["document"]}
    changes: list[dict[str, Any]] = []
    for index, edit, para, units, plan in plans:
        if edit["text"] == edit["expect"]:
            continue
        _write(para, units, plan, edit["text"])
        expected[para.key] = edit["text"]
        changes.append({"index": index, "key": edit["key"], "before": edit["expect"], "after": edit["text"]})
    if not changes:
        return data, {"touched_parts": [], "changes": []}
    touched = {document.part}
    output = _preserving_save(package, {document.part: _serialize(document.root)})
    _verify(package, output, touched, expected)
    return output, {"touched_parts": sorted(touched), "changes": changes}


_SUFFIX = re.compile(r"\.docx$", re.IGNORECASE)

# The marker an edited upload's new file carries, in the person's interface language.
_EDITED_MARKERS = {
    "zh-CN": ("（已修改）", "文档"),
    "en-US": (" (edited)", "Document"),
    "de-DE": (" (bearbeitet)", "Dokument"),
    "fr-FR": (" (modifié)", "Document"),
}


def edited_filename(filename: str, language: str = "zh-CN") -> str:
    """The name of a new file saved from an edited upload, such as ``<stem>（已修改）.docx``."""
    marker, fallback = _EDITED_MARKERS.get(language, _EDITED_MARKERS["en-US"])
    stem = _SUFFIX.sub("", filename or "").strip() or fallback
    return f"{stem}{marker}.docx"
