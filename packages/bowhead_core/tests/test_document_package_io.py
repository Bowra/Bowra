"""Outer Office package admission, not renderer/semantic compatibility tests."""

from __future__ import annotations

import hashlib
import io
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
from bowhead_core.documents import package_io as office

pytestmark = pytest.mark.unit


def _package(kind="pptx", *, extra=None, replace=None, compression=ZIP_STORED) -> bytes:
    mime = office.OFFICE_MIMES["." + kind]
    word = kind in ("docx", "dotx")
    main = "word/document.xml" if word else "ppt/presentation.xml"
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{office.CT_NS}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="bin" ContentType="application/octet-stream"/><Override PartName="/{main}" ContentType="{mime}.main+xml"/></Types>'.encode(),
        "_rels/.rels": f'<Relationships xmlns="{office.REL_NS}"><Relationship Id="rId1" Type="{office.OFFICE_REL}" Target="{main}"/></Relationships>'.encode(),
        main: (
            f'<w:document xmlns:w="{office.WORD_NS}"><w:body><w:p><w:r><w:t>Example</w:t></w:r></w:p></w:body></w:document>'
            if word
            else f'<p:presentation xmlns:p="{office.PPT_NS}"><p:sldIdLst/></p:presentation>'
        ).encode(),
        **(extra or {}),
    }
    for name, data in (replace or {}).items():
        if data is None:
            parts.pop(name, None)
        else:
            parts[name] = data
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        for name, data in parts.items():
            entry = ZipInfo(name, date_time=(2027, 1, 1, 0, 0, 0))
            entry.compress_type = compression
            archive.writestr(entry, data)
    return result.getvalue()


@pytest.mark.parametrize("kind", ["docx", "dotx", "pptx", "potx"])
@pytest.mark.parametrize("mime", ["", "application/octet-stream", "application/zip", "application/x-zip-compressed", "exact"])
def test_all_four_containers_inspect_without_rewriting(kind, mime) -> None:
    content = _package(kind)
    declared = office.OFFICE_MIMES["." + kind] if mime == "exact" else mime
    info = office.inspect_office_package(content, filename=f"Quarter.{kind.upper()}", declared_mime=declared)
    assert info.format == kind and info.mime == office.OFFICE_MIMES["." + kind]
    assert info.sha256 == hashlib.sha256(content).hexdigest()
    assert info.size_bytes == len(content) and info.part_count == 3
    assert info.external_relationship_count == info.opaque_embedding_count == 0


@pytest.mark.parametrize(
    "filename,mime",
    [
        ("x.pptx", office.OFFICE_MIMES[".docx"]),
        ("x.dotx", office.OFFICE_MIMES[".docx"]),
        ("x.zip", office.OFFICE_MIMES[".pptx"]),
        ("x.potx", "image/png"),
    ],
)
def test_explicit_declaration_conflict_is_not_a_fallback(filename, mime) -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_type_mismatch"):
        office.office_upload_mime(filename, mime)


def test_true_container_type_must_match_extension() -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_type_mismatch"):
        office.inspect_office_package(_package("docx"), filename="x.pptx", declared_mime="application/zip")


@pytest.mark.parametrize("data", [b"<html>not a presentation</html>", b"%PDF-1.7", b"PK\x03\x04broken"])
def test_non_office_bytes_are_not_admitted_as_office(data) -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(data, filename="x.pptx", declared_mime="")


@pytest.mark.parametrize(
    "part", ["../outside.xml", "/absolute.xml", "ppt/../other.xml", "ppt\\other.xml", "ppt/%2e%2e/a.xml", "PPT/PRESENTATION.XML"]
)
def test_unsafe_or_case_alias_parts_rejected(part) -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(_package(extra={part: b"<x/>"}), filename="x.pptx", declared_mime="")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_is_rejected_before_entity_expansion_in_all_encodings(encoding) -> None:
    xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE p:presentation [<!ENTITY test "injected">]><p:presentation xmlns:p="{office.PPT_NS}">&test;</p:presentation>'
    with pytest.raises(office.OfficePackageError, match="file_office_unsafe_xml"):
        office.inspect_office_package(_package(replace={"ppt/presentation.xml": xml.encode(encoding)}), filename="x.pptx", declared_mime="")


@pytest.mark.parametrize("name", ["ppt/vbaProject.bin", "_xmlsignatures/sig1.xml"])
def test_active_or_signed_container_requires_separate_support(name) -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_active_content_unsupported"):
        office.inspect_office_package(_package(extra={name: b"opaque"}), filename="x.pptx", declared_mime="")


def test_macro_content_type_cannot_hide_behind_normal_filename() -> None:
    data = _package()
    with ZipFile(io.BytesIO(data)) as z:
        types = z.read("[Content_Types].xml").replace(
            (office.OFFICE_MIMES[".pptx"] + ".main+xml").encode(), b"application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
        )
    with pytest.raises(office.OfficePackageError, match="file_office_active_content_unsupported"):
        office.inspect_office_package(_package(replace={"[Content_Types].xml": types}), filename="x.pptx", declared_mime="")


@pytest.mark.parametrize("missing", ["[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"])
def test_required_parts_cannot_be_omitted(missing) -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(_package(replace={missing: None}), filename="x.pptx", declared_mime="")


@pytest.mark.parametrize("limit", ["MAX_OFFICE_BYTES", "MAX_PART_BYTES", "MAX_EXPANDED_BYTES", "MAX_PARTS"])
def test_budget_checks_happen_before_expensive_work(monkeypatch, limit) -> None:
    monkeypatch.setattr(office, limit, 1)
    with pytest.raises(office.OfficePackageError, match="file_office_package_limit"):
        office.inspect_office_package(_package(compression=ZIP_DEFLATED), filename="x.pptx", declared_mime="")


def test_too_deep_xml_rejected() -> None:
    with pytest.raises(office.OfficePackageError, match="file_office_package_limit"):
        office.inspect_office_package(_package(extra={"ppt/extra.xml": b"<a>" * 129 + b"</a>" * 129}), filename="x.pptx", declared_mime="")


def test_xml_node_budget_is_shared_across_parts() -> None:
    xml = b"<a>" + b"<b/>" * 100_000 + b"</a>"
    with pytest.raises(office.OfficePackageError, match="file_office_package_limit"):
        office.inspect_office_package(_package(extra={"ppt/one.xml": xml, "ppt/two.xml": xml}), filename="x.pptx", declared_mime="")


def test_corrupt_crc_rejected() -> None:
    data = _package("docx").replace(b">Example<", b">Tamper!<")
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(data, filename="x.docx", declared_mime="")


def test_opaque_objects_and_external_links_are_observations_only() -> None:
    rel = f'<Relationships xmlns="{office.REL_NS}"><Relationship Id="rId2" Type="urn:hyperlink" Target="http://127.0.0.1:12345/no-fetch" TargetMode="External"/></Relationships>'.encode()
    info = office.inspect_office_package(
        _package(extra={"ppt/_rels/presentation.xml.rels": rel, "ppt/embeddings/oleObject1.bin": b"opaque"}), filename="x.pptx", declared_mime=""
    )
    assert info.external_relationship_count == 1 and info.opaque_embedding_count == 1


@pytest.mark.parametrize("target", ["missing.xml", "../../outside.xml", "https://example.com/main.xml", "//["])
def test_broken_internal_relationship_rejected(target) -> None:
    rel = f'<Relationships xmlns="{office.REL_NS}"><Relationship Id="x" Type="urn:chart" Target="{target}"/></Relationships>'.encode()
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(_package(extra={"ppt/_rels/presentation.xml.rels": rel}), filename="x.pptx", declared_mime="")


def test_main_document_relationship_must_resolve_to_the_declared_main() -> None:
    rel = f'<Relationships xmlns="{office.REL_NS}"><Relationship Id="x" Type="{office.OFFICE_REL}" Target="other.xml"/></Relationships>'.encode()
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(_package(extra={"other.xml": b"<other/>"}, replace={"_rels/.rels": rel}), filename="x.pptx", declared_mime="")


@pytest.mark.parametrize("path,allowed", [("ppt/embeddings/data.xlsb", True), ("ppt/data.xlsb", False)])
def test_binary_workbooks_remain_opaque_only_in_embedding_locations(path, allowed) -> None:
    with ZipFile(io.BytesIO(_package())) as archive:
        types = archive.read("[Content_Types].xml").replace(
            b"</Types>", f'<Default Extension="xlsb" ContentType="{office.XLSB_MIME}"/></Types>'.encode()
        )
    data = _package(extra={path: b"opaque workbook, not parsed"}, replace={"[Content_Types].xml": types})
    if allowed:
        result = office.inspect_office_package(data, filename="x.pptx", declared_mime="")
        assert result.opaque_embedding_count == 1
    else:
        with pytest.raises(office.OfficePackageError, match="file_office_active_content_unsupported"):
            office.inspect_office_package(data, filename="x.pptx", declared_mime="")


def test_corrupt_deflate_stream_is_a_structured_rejection() -> None:
    data = bytearray(_package(compression=ZIP_DEFLATED))
    with ZipFile(io.BytesIO(data)) as archive:
        entry = archive.getinfo("ppt/presentation.xml")
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    data[offset] = 0x07  # BFINAL=1, reserved DEFLATE block type=3.
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(bytes(data), filename="x.pptx", declared_mime="")


def test_unknown_xml_encoding_is_a_structured_rejection() -> None:
    xml = b'<?xml version="1.0" encoding="not-a-real-encoding"?><a/>'
    with pytest.raises(office.OfficePackageError, match="file_office_invalid_package"):
        office.inspect_office_package(_package(extra={"ppt/extra.xml": xml}), filename="x.pptx", declared_mime="")
