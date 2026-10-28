"""Resolve OOXML MIME types explicitly when the runtime lacks an operating-system MIME table."""

from __future__ import annotations

import importlib.util
import mimetypes
from pathlib import Path
from types import ModuleType

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "catalog" / "python_sandbox" / "scripts" / "runner.py"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_mime_runner_under_test", _RUNNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ONC_2026Q2_QBR_editable.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("model.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("Deck.PPTX", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
def test_office_mime_does_not_depend_on_system_mime_table(monkeypatch, filename, expected) -> None:
    r = _runner()
    # Reproduce the slim image: the system table recognizes nothing.
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    assert r._guess_upload_mime(filename) == expected


@pytest.mark.unit
def test_non_office_still_falls_back_to_mimetypes(monkeypatch) -> None:
    r = _runner()
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: ("text/csv", None))
    assert r._guess_upload_mime("rows.csv") == "text/csv"
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_a, **_k: (None, None))
    assert r._guess_upload_mime("blob.bin") == "application/octet-stream"
    # Positive control: the patch reaches runner; the system normally recognizes .pdf, but an emptied table must fall back to octet-stream.
    assert r._guess_upload_mime("a.pdf") == "application/octet-stream"
