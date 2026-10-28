"""excel RC-1 fix — attachment deep-read hint for spreadsheets → python_sandbox.

The injector used to hint ``skill_invoke name="xlsx"`` for spreadsheet attachments,
pushing the LLM toward the knowledge-kind xlsx skill (which is now redirected).
Deep-reading a spreadsheet = load the artifact + run pandas → python_sandbox.
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.attachments_injector import _deep_read_hint, _guess_skill


def _att(mime: str, filename: str, artifact_id: str = "art_x"):
    return SimpleNamespace(mime=mime, filename=filename, artifact_id=artifact_id)


def test_spreadsheet_hint_is_python_sandbox() -> None:
    assert _guess_skill(_att("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "q3.xlsx")) == "python_sandbox"
    assert _guess_skill(_att("application/vnd.ms-excel", "old.xls")) == "python_sandbox"
    assert _guess_skill(_att("application/octet-stream", "data.xlsx")) == "python_sandbox"


def test_other_types_unchanged() -> None:
    assert _guess_skill(_att("application/pdf", "r.pdf")) == "pdf"
    assert _guess_skill(_att("application/vnd.openxmlformats-officedocument.presentationml.presentation", "d.pptx")) == "pptx"
    assert _guess_skill(_att("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "c.docx")) == "docx"


def test_deep_read_hint_python_sandbox_has_correct_shape() -> None:
    """spreadsheet hint must give python_sandbox's real shape (script+code+inputs),
    NOT a bare top-level artifact_id (which python_sandbox doesn't accept)."""
    h = _deep_read_hint(_att("application/vnd.ms-excel", "q.xlsx", artifact_id="art_42"))
    assert 'name="python_sandbox"' in h
    assert 'script="runner.py"' in h
    assert '"code"' in h
    assert '"inputs"' in h
    assert "art_42" in h


def test_deep_read_hint_pdf_docx_route_through_sandbox() -> None:
    """skill-multiruntime S3 (D4): pdf/docx/pptx deep-read now routes through
    python_sandbox. The old bare ``skill_invoke name="pdf" 传 artifact_id`` shape
    was DEAD — the builtin pdf/docx skills have many scripts (no single auto-
    select) and no ``--input`` entry, so a bare invoke errored "script required".
    The sandbox has pypdf/python-docx/python-pptx baked in → the new shape runs.
    """
    for mime, fn in (
        ("application/pdf", "r.pdf"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "c.docx"),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "d.pptx"),
    ):
        h = _deep_read_hint(_att(mime, fn, artifact_id="art_9"))
        assert 'name="python_sandbox"' in h
        assert 'script="runner.py"' in h
        assert "art_9" in h
        # No longer the dead bare-skill call.
        assert 'name="pdf"' not in h and 'name="docx"' not in h and 'name="pptx"' not in h
