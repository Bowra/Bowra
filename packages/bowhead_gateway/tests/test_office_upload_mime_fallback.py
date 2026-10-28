"""Accept Office uploads whose minimal runtime reports application/octet-stream.

The gateway recognizes supported OOXML extensions when system MIME tables
are absent, preserving the same file-type admission rules.
"""

from __future__ import annotations

import pytest
from bowhead_core.documents.package_io import OFFICE_MIMES
from bowhead_gateway.routes.files import ALLOWED_MIME, resolve_effective_upload_mime

_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("declared", "filename", "expected"),
    [
        ("application/octet-stream", "ONC_2026Q2_QBR_editable.pptx", _PPTX),
        ("application/octet-stream", "report.docx", _DOCX),
        ("application/octet-stream", "model.xlsx", _XLSX),
        (_PPTX, "deck.pptx", _PPTX),
        ("application/octet-stream", "deck.potx", OFFICE_MIMES[".potx"]),
        ("application/octet-stream", "letter.dotx", OFFICE_MIMES[".dotx"]),
    ],
)
def test_office_suffix_fallback_when_client_has_no_mime_table(monkeypatch, declared, filename, expected) -> None:
    got = resolve_effective_upload_mime(declared, filename)
    assert got == expected
    if expected is not None:
        assert got in ALLOWED_MIME or got in OFFICE_MIMES.values()
