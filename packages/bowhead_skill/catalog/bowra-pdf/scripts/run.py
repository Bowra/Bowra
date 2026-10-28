# SPDX-License-Identifier: MIT
"""Standalone document CLI using the Bowhead Skill input/workdir protocol."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

# JSON schemas constrain these heterogeneous input/output mappings at the CLI boundary.
JsonObject = dict[str, Any]
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON numbers are unsupported")


def load_request(path: Path) -> JsonObject:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Request exceeds the 2 MiB limit")
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Request exceeds the 2 MiB limit")
    request = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    schema = json.loads((Path(__file__).resolve().parents[1] / "input-schema.json").read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(request)
    except ValidationError as exc:
        raise ValueError(f"Request violates the input schema ({exc.validator})") from None
    return request


def source_bytes(workdir: Path, source: str, suffix: str) -> bytes:
    parts = source.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in source or ":" in source:
        raise ValueError("Source must be a canonical path relative to the workdir")
    candidate = workdir
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Source symlinks are unsupported")
    if candidate.suffix.lower() != suffix or not candidate.is_file():
        raise ValueError(f"Source must be an existing {suffix} file in the workdir")
    if candidate.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the 16 MiB limit")
    raw = candidate.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the 16 MiB limit")
    return raw


def check_office_archive(raw: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 4096 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
            raise ValueError("Office package exceeds the expanded-size or entry limit")
        names = [entry.filename for entry in entries]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate Office package entries are unsupported")


def write_output(workdir: Path, name: str, raw: bytes) -> JsonObject:
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("Output exceeds the 16 MiB limit")
    with (workdir / name).open("xb") as output:
        output.write(raw)
    return {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def encode_json(value: JsonObject) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def run(request: JsonObject, workdir: Path) -> JsonObject:
    """Create a document or inspect staged source bytes."""
    if request["operation"] == "create":
        return create(request, workdir)
    raw = source_bytes(workdir, request["source"], SUFFIX)
    if SUFFIX != ".pdf":
        check_office_archive(raw)
    report = inspect(raw)
    report["source_sha256"] = hashlib.sha256(raw).hexdigest()
    report["operation"] = "inspect"
    result = write_output(workdir, "inspection.json", encode_json(report))
    return {**result, "operation": "inspect", "scope": report["scope"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = load_request(args.input)
        workdir = args.workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        if (workdir / "result.json").exists():
            raise FileExistsError("Workdir already contains result.json; choose a fresh output directory")
        result = run(request, workdir)
        write_output(workdir, "result.json", encode_json(result))
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        sys.stderr.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False) + "\n")
        return 2
    return 0


from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SUFFIX = ".pdf"


def create(request: JsonObject, workdir: Path) -> JsonObject:
    font = "Helvetica"
    if request.get("font_mode", "latin") == "cjk":
        font = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = font
        if font == "STSong-Light":
            style.wordWrap = "CJK"

    def text(value: str) -> str:
        if font == "Helvetica":
            try:
                value.encode("cp1252")
            except UnicodeEncodeError:
                raise ValueError("Latin font cannot encode this text; use font_mode=cjk for supported Chinese text") from None
        elif any(ord(character) > 0xFFFF for character in value):
            raise ValueError("The bundled PDF font mode does not support supplementary Unicode characters")
        return escape(value).replace("\n", "<br/>")

    story = [Paragraph(text(request["title"]), styles["Title"]), Spacer(1, 12)]
    cells = 0
    for block in request["blocks"]:
        if block["type"] == "heading":
            story.append(Paragraph(text(block["text"]), styles[f"Heading{block['level']}"]))
        elif block["type"] == "paragraph":
            story.append(Paragraph(text(block["text"]), styles["BodyText"]))
        elif block["type"] == "bullets":
            for item in block["items"]:
                story.append(Paragraph(text(item), styles["BodyText"], bulletText="•"))
        else:
            rows = [block["columns"], *block["rows"]]
            if any(len(row) != len(block["columns"]) for row in rows):
                raise ValueError("Every table row must match its column count")
            cells += sum(len(row) for row in rows)
            if cells > 4000:
                raise ValueError("Document exceeds the 4,000 table-cell limit")
            rendered = [[Paragraph(text(value), styles["BodyText"]) for value in row] for row in rows]
            table = Table(rendered, colWidths=[468 / len(block["columns"])] * len(block["columns"]), repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0F8")),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(table)
        story.append(Spacer(1, 10))
    stream = io.BytesIO()
    SimpleDocTemplate(stream, title=request["title"], author="Bowra").build(story)
    raw = stream.getvalue()
    pages = len(PdfReader(io.BytesIO(raw)).pages)
    return {**write_output(workdir, "report.pdf", raw), "operation": "create", "pages": pages}


def inspect(raw: bytes) -> JsonObject:
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF input is unsupported")
    if len(reader.pages) > 100:
        raise ValueError("PDF exceeds the 100-page inspection limit")
    pages: list[JsonObject] = []
    characters = 0
    for index, page in enumerate(reader.pages, start=1):
        value = page.extract_text() or ""
        characters += len(value)
        if characters > 200_000:
            raise ValueError("PDF exceeds the 200,000-character inspection limit")
        pages.append({"page": index, "text": value, "no_text": not value.strip()})
    return {
        "scope": "embedded page text; not OCR or a layout inventory",
        "pages": pages,
        "limitations": ["Text order and whitespace can differ from visual order; image-only pages need a separate OCR workflow."],
    }


if __name__ == "__main__":
    raise SystemExit(main())
