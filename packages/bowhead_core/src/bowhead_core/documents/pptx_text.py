"""Original-package PPTX text editing, without a renderer or publication claim.

The caller owns authorization of the complete edit scope and must distinguish
private, unreviewed draft construction from semantic approval for publication.
Models may propose slot IDs and text, never OOXML addresses or extra permissions.
Run this parser in the document worker before exposing it to uploaded documents.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import posixpath
from dataclasses import dataclass
from typing import cast
from xml.dom import Node, minidom
from zipfile import ZipFile

from bowhead_core.documents.package_io import OFFICE_MIMES, OfficePackageError, inspect_office_package

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PROFILE = "pptx-text-v1"
MAX_TEXT_CHARS = 4000


@dataclass(frozen=True)
class TextSlot:
    """Compact, non-executable context; IDs survive value-only refreshes."""

    slot_id: str
    slide: int
    shape_id: str
    shape_name: str
    cell: tuple[int, int] | None
    paragraph: int
    text: str
    blocked_reason: str | None


@dataclass(frozen=True)
class UnsupportedObject:
    slide: int
    shape_id: str
    reason: str


@dataclass(frozen=True)
class TextInventory:
    source_sha256: str
    slide_count: int
    slots: tuple[TextSlot, ...]
    unsupported_objects: tuple[UnsupportedObject, ...]
    profile: str = PROFILE


@dataclass(frozen=True)
class TextEditScope:
    """Trusted, source-bound write scope. Every listed slot must be supplied.

    This is NOT a model response: a trusted caller must establish permission and
    the exact write set. An unreviewed draft scope grants no publication rights.
    Completeness here covers this scope, not all business facts.
    """

    source_sha256: str
    slot_ids: tuple[str, ...]


@dataclass(frozen=True)
class TextReplacement:
    slot_id: str
    text: str


@dataclass(frozen=True)
class TextEditReceipt:
    source_sha256: str
    candidate_sha256: str
    slide_count: int
    changed_parts: tuple[str, ...]
    replacement_count: int
    profile: str = PROFILE
    scope_verdict: str = "passed"
    visual_verdict: str = "not_run"


@dataclass
class _Target:
    slot: TextSlot
    part: str
    nodes: tuple[minidom.Element, ...]


def _children(node: Node, ns: str | None = None, local: str | None = None) -> list[minidom.Element]:
    return [
        cast(minidom.Element, child)
        for child in node.childNodes
        if child.nodeType == Node.ELEMENT_NODE and (ns is None or child.namespaceURI == ns) and (local is None or child.localName == local)
    ]


def _one(node: Node, ns: str, local: str) -> minidom.Element:
    children = _children(node, ns, local)
    if len(children) != 1:
        raise OfficePackageError("pptx_structure_unsupported")
    return children[0]


def _parse(data: bytes) -> minidom.Document:
    # inspect_office_package has already rejected DTDs and bounded XML depth,
    # nodes and expanded bytes, including UTF-16. Preserve namespace prefixes.
    return minidom.parseString(data)  # noqa: S318


def _parts(source: bytes) -> dict[str, bytes]:
    inspect_office_package(source, filename="source.pptx", declared_mime=OFFICE_MIMES[".pptx"])
    with ZipFile(io.BytesIO(source)) as archive:
        return {entry.filename: archive.read(entry) for entry in archive.infolist()}


def _text(node: minidom.Element) -> str:
    return "".join(child.data for child in node.childNodes if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE))


def _set_text(node: minidom.Element, text: str) -> None:
    for child in list(node.childNodes):
        node.removeChild(child)
        child.unlink()
    if text:
        node.appendChild(node.ownerDocument.createTextNode(text))


def _paragraph_nodes(paragraph: minidom.Element) -> tuple[tuple[minidom.Element, ...], str | None]:
    texts = tuple(paragraph.getElementsByTagNameNS(A, "t"))
    if any(paragraph.getElementsByTagNameNS(A, name) for name in ("hlinkClick", "hlinkMouseOver")):
        return texts, "hyperlinked_text_unsupported"
    children = _children(paragraph)
    runs = _children(paragraph, A, "r")
    if not runs or any(child.namespaceURI != A or child.localName not in ("r", "pPr", "endParaRPr") for child in children):
        return texts, "text_boundary_unsupported"
    styles = []
    for run in runs:
        if len(_children(run, A, "t")) != 1 or any(child.namespaceURI != A or child.localName not in ("rPr", "t") for child in _children(run)):
            return texts, "text_boundary_unsupported"
        properties = _children(run, A, "rPr")
        if len(properties) > 1:
            return texts, "text_boundary_unsupported"
        styles.append(properties[0].toxml() if properties else "")
    if len(texts) != len(runs) or any(any(child.nodeType != Node.TEXT_NODE for child in text.childNodes) for text in texts):
        return texts, "text_boundary_unsupported"
    if len(set(styles)) > 1:
        return texts, "mixed_run_styles"
    return texts, None


def _inventory(parts: dict[str, bytes], source_sha256: str) -> tuple[TextInventory, dict[str, _Target]]:
    presentation = _parse(parts["ppt/presentation.xml"]).documentElement
    if presentation.getElementsByTagNameNS(P, "modifyVerifier"):
        raise OfficePackageError("pptx_protected_unsupported")
    relationships = _parse(parts.get("ppt/_rels/presentation.xml.rels", b"<Relationships/>"))
    slides = {
        rel.getAttribute("Id"): posixpath.normpath(posixpath.join("ppt", rel.getAttribute("Target")))
        if not rel.getAttribute("Target").startswith("/")
        else rel.getAttribute("Target").lstrip("/")
        for rel in relationships.getElementsByTagNameNS(REL, "Relationship")
        if rel.getAttribute("Type") == R + "/slide" and rel.getAttribute("TargetMode") != "External"
    }
    ordered = _children(_one(presentation, P, "sldIdLst"), P, "sldId")
    seen_parts: set[str] = set()
    seen_slide_ids: set[str] = set()
    targets: dict[str, _Target] = {}
    unsupported: list[UnsupportedObject] = []

    def add_body(
        body: minidom.Element, part: str, slide: int, shape_id: str, shape_name: str, cell: tuple[int, int] | None, blocked: str | None
    ) -> None:
        for index, paragraph in enumerate(_children(body, A, "p")):
            nodes, reason = _paragraph_nodes(paragraph)
            if not nodes:
                continue
            identity = json.dumps([PROFILE, part, shape_id, cell, index], separators=(",", ":"))
            slot_id = "ps_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            slot = TextSlot(slot_id, slide, shape_id, shape_name, cell, index, "".join(_text(node) for node in nodes), blocked or reason)
            if slot_id in targets:
                raise OfficePackageError("pptx_structure_unsupported")
            targets[slot_id] = _Target(slot, part, nodes)

    def visit(tree: minidom.Element, part: str, slide: int, seen_ids: set[str]) -> None:
        for shape in _children(tree):
            if shape.namespaceURI == MC and shape.localName == "AlternateContent":
                unsupported.append(UnsupportedObject(slide, "", "alternate_content"))
                continue
            if shape.namespaceURI != P or shape.localName in ("nvGrpSpPr", "grpSpPr", "extLst"):
                continue
            property_names = {"grpSp": "nvGrpSpPr", "sp": "nvSpPr", "graphicFrame": "nvGraphicFramePr", "pic": "nvPicPr", "cxnSp": "nvCxnSpPr"}
            property_name = property_names.get(shape.localName)
            if property_name is None:
                unsupported.append(UnsupportedObject(slide, "", "non_text_shape"))
                continue
            properties = _one(_one(shape, P, property_name), P, "cNvPr")
            shape_id, name = properties.getAttribute("id"), properties.getAttribute("name")
            if not shape_id or shape_id in seen_ids:
                raise OfficePackageError("pptx_shape_identity_invalid")
            seen_ids.add(shape_id)
            if shape.localName == "grpSp":
                visit(shape, part, slide, seen_ids)
                continue
            if shape.localName == "sp":
                bodies = _children(shape, P, "txBody")
                if len(bodies) > 1:
                    raise OfficePackageError("pptx_structure_unsupported")
                if bodies:
                    extended = bool(shape.getElementsByTagNameNS("*", "extLst") or shape.getElementsByTagNameNS(MC, "AlternateContent"))
                    add_body(bodies[0], part, slide, shape_id, name, None, "extended_text_unsupported" if extended else None)
            elif shape.localName == "graphicFrame":
                data = _one(_one(shape, A, "graphic"), A, "graphicData")
                tables = _children(data, A, "tbl")
                if data.getAttribute("uri") != "http://schemas.openxmlformats.org/drawingml/2006/table" or len(tables) != 1:
                    unsupported.append(UnsupportedObject(slide, shape_id, "non_text_graphic"))
                    continue
                table = tables[0]
                columns = len(_children(_one(table, A, "tblGrid"), A, "gridCol"))
                rows = _children(table, A, "tr")
                cells = [cell for row in rows for cell in _children(row, A, "tc")]
                blocked = (
                    "merged_table_unsupported"
                    if any(cell.hasAttribute(key) for cell in cells for key in ("gridSpan", "rowSpan", "hMerge", "vMerge"))
                    else None
                )
                if not columns or any(len(_children(row, A, "tc")) != columns for row in rows):
                    raise OfficePackageError("pptx_structure_unsupported")
                for row_index, row in enumerate(rows):
                    for col_index, cell in enumerate(_children(row, A, "tc")):
                        add_body(_one(cell, A, "txBody"), part, slide, shape_id, name, (row_index, col_index), blocked)

    for number, slide in enumerate(ordered, 1):
        part = slides.get(slide.getAttributeNS(R, "id"))
        slide_id = slide.getAttribute("id")
        if not slide_id or slide_id in seen_slide_ids or part not in parts or part in seen_parts:
            raise OfficePackageError("pptx_slide_identity_invalid")
        seen_parts.add(part)
        seen_slide_ids.add(slide_id)
        root = _parse(parts[part]).documentElement
        if (root.namespaceURI, root.localName) != (P, "sld"):
            raise OfficePackageError("pptx_structure_unsupported")
        tree = _one(_one(root, P, "cSld"), P, "spTree")
        root_id = _one(_one(tree, P, "nvGrpSpPr"), P, "cNvPr").getAttribute("id")
        # Choice/fallback branches can repeat IDs legally. They are retained
        # opaquely, never indexed as two independent editable shapes.
        visit(tree, part, number, {root_id})
    return TextInventory(source_sha256, len(ordered), tuple(target.slot for target in targets.values()), tuple(unsupported)), targets


def inventory_pptx_text(source: bytes) -> TextInventory:
    """Read slide-order text context, keeping unsupported objects visible."""
    inventory, _ = _inventory(_parts(source), hashlib.sha256(source).hexdigest())
    return inventory


def parse_text_replacements(response: str) -> tuple[TextReplacement, ...]:
    """Decode the only model-facing edit payload, with no permissive repair."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise OfficePackageError("text_proposal_invalid")
            result[key] = value
        return result

    if len(response) > 256_000:
        raise OfficePackageError("text_proposal_invalid")
    try:
        payload = json.loads(response, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise OfficePackageError("text_proposal_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"updates"} or not isinstance(payload["updates"], list) or len(payload["updates"]) > 4096:
        raise OfficePackageError("text_proposal_invalid")
    replacements = []
    for item in payload["updates"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"slot_id", "text"}
            or not isinstance(item["slot_id"], str)
            or not isinstance(item["text"], str)
        ):
            raise OfficePackageError("text_proposal_invalid")
        replacements.append(TextReplacement(item["slot_id"], item["text"]))
    return tuple(replacements)


def _approved_targets(
    source: bytes, scope: TextEditScope, replacements: tuple[TextReplacement, ...]
) -> tuple[dict[str, bytes], TextInventory, dict[str, _Target]]:
    if hashlib.sha256(source).hexdigest() != scope.source_sha256:
        raise OfficePackageError("source_hash_mismatch")
    parts = _parts(source)
    inventory, targets = _inventory(parts, scope.source_sha256)
    if len(set(scope.slot_ids)) != len(scope.slot_ids) or len({item.slot_id for item in replacements}) != len(replacements):
        raise OfficePackageError("overlapping_text_update")
    if set(scope.slot_ids) != {item.slot_id for item in replacements}:
        raise OfficePackageError("text_scope_mismatch")
    if sum(len(item.text) for item in replacements) > 200_000:
        raise OfficePackageError("text_update_limit")
    for item in replacements:
        target = targets.get(item.slot_id)
        if target is None or target.slot.blocked_reason:
            raise OfficePackageError("unsupported_text_target")
        if len(item.text) > MAX_TEXT_CHARS or any(
            ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF) for char in item.text
        ):
            raise OfficePackageError("unsupported_replacement")
        if item.text.strip() != item.text and target.nodes[0].getAttribute("xml:space") != "preserve":
            raise OfficePackageError("whitespace_requires_format_change")
    return parts, inventory, targets


def verify_pptx_text_update(source: bytes, candidate: bytes, scope: TextEditScope, replacements: tuple[TextReplacement, ...]) -> TextEditReceipt:
    """Independently reread both packages; mask only approved leaf values."""
    before, inventory, old_targets = _approved_targets(source, scope, replacements)
    after = _parts(candidate)
    if before.keys() != after.keys():
        raise OfficePackageError("part_set_changed")
    _, new_targets = _inventory(after, hashlib.sha256(candidate).hexdigest())
    changed: set[str] = set()
    documents: dict[str, tuple[minidom.Document, minidom.Document]] = {}
    for item in replacements:
        old, new = old_targets[item.slot_id], new_targets.get(item.slot_id)
        if new is None or new.slot.text != item.text or len(old.nodes) != len(new.nodes):
            raise OfficePackageError("replacement_missing")
        # A no-op approval must not authorize flattening runs or reserializing.
        if item.text == old.slot.text:
            continue
        changed.add(old.part)
        documents[old.part] = (old.nodes[0].ownerDocument, new.nodes[0].ownerDocument)
        for index, (old_node, new_node) in enumerate(zip(old.nodes, new.nodes, strict=True)):
            if _text(new_node) != (item.text if index == 0 else ""):
                raise OfficePackageError("replacement_missing")
            _set_text(old_node, "MASKED")
            _set_text(new_node, "MASKED")
    for name in before:
        if name not in changed and before[name] != after[name]:
            raise OfficePackageError("untouched_part_changed")
    for old_document, new_document in documents.values():
        if old_document.toxml() != new_document.toxml():
            raise OfficePackageError("unapproved_node_change")
    return TextEditReceipt(
        scope.source_sha256, hashlib.sha256(candidate).hexdigest(), inventory.slide_count, tuple(sorted(changed)), len(replacements)
    )


def apply_pptx_text_update(source: bytes, scope: TextEditScope, replacements: tuple[TextReplacement, ...]) -> tuple[bytes, TextEditReceipt]:
    """Return a new editable source, never overwrite or rebuild the original.

    Equal-style run skeletons, properties and all unrelated parts remain intact.
    This returns an internal candidate: visual validation is still required.
    """
    parts, _, targets = _approved_targets(source, scope, replacements)
    documents: dict[str, minidom.Document] = {}
    for item in replacements:
        target = targets[item.slot_id]
        if item.text == target.slot.text:
            continue
        documents[target.part] = target.nodes[0].ownerDocument
        for index, node in enumerate(target.nodes):
            _set_text(node, item.text if index == 0 else "")
    if not documents:
        return source, verify_pptx_text_update(source, source, scope, replacements)
    for name, document in documents.items():
        parts[name] = document.toxml(encoding="UTF-8")
    output = io.BytesIO()
    with ZipFile(io.BytesIO(source)) as original, ZipFile(output, "w") as result:
        result.comment = original.comment
        for entry in original.infolist():
            result.writestr(copy.copy(entry), parts[entry.filename])
    candidate = output.getvalue()
    return candidate, verify_pptx_text_update(source, candidate, scope, replacements)
