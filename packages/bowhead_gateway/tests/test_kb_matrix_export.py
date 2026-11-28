"""Document matrix step 1 (S5): Excel export of a matrix.

Covers the workbook built from the snapshot's per-viewer projection: typed answers and fills, one Sources row
per cell with its status, the About sheet's counts, rows the caller cannot read exported without values, text
that looks like a formula kept as text, long text cut with a marker, sheet names and headers in the interface
language, the download header, who may export, and the audit line.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import pytest
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_export import FILL_EDITED, FILL_VERIFIED, MAX_CELL_TEXT_CHARS, export_filename
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_gateway.routes import kb_matrix
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from test_kb_matrix_authz import TEAM, U_MEMBER, U_OUTSIDER, U_VIEWER, WS_A, WS_B, _as, _h, _MembershipStore

if TYPE_CHECKING:
    from pathlib import Path

MATRIX_NAME = "供应商/合同 2026"
FORMULA = '=HYPERLINK("http://evil.example","click")'


@pytest.fixture(autouse=True)
def _single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


@pytest.fixture
async def env(initialize_migrated_kb_files_database, initialize_migrated_kb_matrix_database, tmp_path: Path, monkeypatch):
    initialize_migrated_kb_matrix_database(tmp_path / "kb_matrix.db")
    store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    app = FastAPI()
    app.include_router(kb_matrix.router)
    app.state.kb_matrix_store = store
    app.state.kb_files_store = files
    app.state.user_store = _MembershipStore()
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(kb_matrix, "emit_audit", lambda event, payload: audits.append((event, payload)))

    async def add(name: str, ws: str) -> str:
        return await files.create(
            artifact_id=name,
            filename=name,
            mime_type="text/markdown",
            size_bytes=1,
            scope="workspace",
            scope_id=ws,
            workspace_id=ws,
            team_id=TEAM,
            uploaded_by=U_MEMBER,
        )

    a0, a1, a2 = await add("合同A.md", WS_A), await add("=cmd|' /C calc'!A0.md", WS_A), await add("c.md", WS_A)
    # A row the caller cannot read: another Space's file kept in the matrix.
    hidden = await add("secret.md", WS_B)
    matrix = await store.create_matrix(
        name=MATRIX_NAME,
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": f, "char_count": 1} for f in (a0, a1, a2, hidden)],
        columns=[
            {"name": "Amount", "type": "currency", "question": "How much?"},
            {"name": "Signed", "type": "date", "question": "When signed?"},
            {"name": "Renews", "type": "boolean", "question": "Auto renewal?"},
            {"name": "Party", "type": "question", "question": "Who is the supplier?"},
            {"name": "Tags", "type": "multi_select", "question": "Which tags?", "options": ["IT", "HR"]},
        ],
    )
    amount, signed, renews, party, tags = (c["column_id"] for c in matrix.columns)
    by_name = {c["name"]: c for c in matrix.columns}

    async def land(file_id: str, col: str, normalized: dict[str, Any] | None, **variant: Any) -> None:
        await store.enqueue_cells(
            matrix.matrix_id, [(file_id, col)], run_id="mxrun_t", workspace_id=WS_A, team_id=TEAM, column_versions={col: 1}, file_versions={}
        )
        primary = {"raw": None, "normalized": normalized, "is_primary": True, "extracted_by": "tier1", "confidence": 0.9, **variant}
        status = "needs_review" if normalized and "raw_string" in normalized else "done"
        assert await store.finalize_cell(
            matrix.matrix_id, file_id, col, status=status, variants=[primary], prompt_version=1, file_version=1, run_id="mxrun_t"
        )

    await land(a0, amount, {"amount": "286000", "currency": None}, source_excerpt="每年人民币贰拾捌万陆仟元整", page=5, chunk_id="chk_1")
    await land(a0, signed, {"value": "2026-03-01"})
    await land(a0, renews, {"flag": True})
    await land(a0, party, {"value": "Acme 有限公司"}, confidence=0.55)
    await land(a0, tags, {"choices": ["IT", "HR"]})
    await store.set_cell_verified(matrix.matrix_id, a0, amount, workspace_id=WS_A, verified=True, actor_user_id=U_MEMBER)
    # Row a1: a hand-edited answer that looks like a formula, a type mismatch, a failure, a date that is not a date.
    await land(a1, party, {"value": "AI"})
    await store.edit_cell_value(
        matrix.matrix_id, a1, party, value=FORMULA, column=by_name["Party"], workspace_id=WS_A, team_id=TEAM, actor_user_id=U_MEMBER
    )
    await land(a1, amount, {"raw_string": "约 100 万"})
    await store.enqueue_cells(
        matrix.matrix_id, [(a1, renews)], run_id="mxrun_f", workspace_id=WS_A, team_id=TEAM, column_versions={renews: 1}, file_versions={}
    )
    await store.fail_cells(matrix.matrix_id, [(a1, renews)], fail_reason="timeout")
    await land(a1, signed, {"value": "next spring"})
    # Row a2: one cell queued, the rest never run.
    await store.enqueue_cells(
        matrix.matrix_id, [(a2, amount)], run_id="mxrun_q", workspace_id=WS_A, team_id=TEAM, column_versions={amount: 1}, file_versions={}
    )
    # The hidden row has values the caller must never receive.
    await land(hidden, party, {"value": "secret supplier"}, source_excerpt="secret excerpt", page=2)
    return {
        "client": TestClient(app, raise_server_exceptions=False),
        "app": app,
        "store": store,
        "matrix": matrix,
        "rows": (a0, a1, a2),
        "hidden": hidden,
        "cols": {"amount": amount, "signed": signed, "renews": renews, "party": party, "tags": tags},
        "by_name": by_name,
        "audits": audits,
    }


def _export(env: dict[str, Any], **kwargs: Any) -> Any:
    headers = {**_h(WS_A), **kwargs.pop("headers", {})}
    return env["client"].get(f"/api/v1/kb/matrices/{env['matrix'].matrix_id}/export.xlsx", headers=headers, **kwargs)


def _workbook(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return load_workbook(io.BytesIO(response.content))


def _values(ws: Any) -> list[list[Any]]:
    return [[c.value for c in row] for row in ws.iter_rows()]


async def test_export_writes_typed_answers_fills_sources_and_about(env) -> None:
    _as(env["app"], U_MEMBER)
    r = _export(env, headers={"Accept-Language": "en-US"})
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert r.headers["cache-control"] == "no-store, private"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=UTF-8''")
    assert unquote(disposition.split("''", 1)[1]) == "供应商_合同 2026.xlsx"
    wb = _workbook(r)
    assert wb.sheetnames == ["Answers", "Sources", "About"]

    answers = wb["Answers"]
    rows = list(answers.iter_rows())
    assert [c.value for c in rows[0]] == ["File", "Amount", "Signed", "Renews", "Party", "Tags"]
    a0 = rows[1]
    assert [c.value for c in a0] == ["合同A.md", 286000, datetime(2026, 3, 1), True, "Acme 有限公司", "IT, HR"]
    assert (a0[1].data_type, a0[2].data_type, a0[3].data_type) == ("n", "d", "b")
    assert a0[2].number_format == "yyyy-mm-dd"
    assert a0[1].fill.fgColor.rgb.endswith(FILL_VERIFIED)
    assert a0[4].fill.fill_type is None
    a1 = rows[2]
    # The edited answer is yellow and stays text; a type mismatch and a date that does not parse stay text.
    assert [c.value for c in a1] == ["=cmd|' /C calc'!A0.md", "约 100 万", "next spring", None, FORMULA, None]
    assert a1[4].fill.fgColor.rgb.endswith(FILL_EDITED)
    assert [c.value for c in rows[3]] == ["c.md", None, None, None, None, None]
    # The unreadable row keeps its place with the marker and no values.
    assert [c.value for c in rows[4]] == ["(No access)", None, None, None, None, None]

    sources = _values(wb["Sources"])
    assert sources[0] == ["File", "Column", "Question", "Answer", "Status", "Confidence", "Location", "Source excerpt"]
    assert len(sources) == 1 + 4 * 5
    by_cell = {(row[0], row[1]): row for row in sources[1:]}
    assert by_cell[("合同A.md", "Amount")] == [
        "合同A.md",
        "Amount",
        "How much?",
        286000,
        "Verified",
        0.9,
        "Page 5",
        "每年人民币贰拾捌万陆仟元整",
    ]
    assert by_cell[("合同A.md", "Party")][4:6] == ["Not verified", 0.55]
    # A person's answer has no AI confidence.
    assert by_cell[("=cmd|' /C calc'!A0.md", "Party")][3:6] == [FORMULA, "Edited", None]
    assert by_cell[("=cmd|' /C calc'!A0.md", "Amount")][3:5] == ["约 100 万", "Type mismatch"]
    assert by_cell[("=cmd|' /C calc'!A0.md", "Renews")][3:5] == [None, "Failed"]
    assert by_cell[("c.md", "Amount")][3:5] == [None, "Processing"]
    assert by_cell[("c.md", "Tags")][3:5] == [None, "Not run"]
    hidden_rows = [row for row in sources[1:] if row[0] == "(No access)"]
    assert len(hidden_rows) == 5
    assert all(row[3:] == [None, "No access", None, None, None] for row in hidden_rows)
    assert wb["Sources"]["F2"].number_format == "0%"

    about = {row[0]: row[1] for row in _values(wb["About"]) if row and row[0]}
    assert about["Matrix"] == MATRIX_NAME
    assert about["Exported by"] == "u_member"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", about["Exported at"])
    assert (about["Files"], about["Question columns"], about["Hidden cells"]) == (4, 5, 5)
    assert about["Verified"].startswith("A person checked")
    assert about["No access"].startswith("You can't view this file")

    # Nothing of the unreadable row reaches the file, wherever it would be written.
    raw = b"".join(zipfile.ZipFile(io.BytesIO(r.content)).read(name) for name in zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert b"secret" not in raw

    assert env["audits"] == [
        ("kb.matrix_exported", {"matrix_id": env["matrix"].matrix_id, "files": 4, "columns": 5, "hidden_cells": 5, "language": "en-US"})
    ]


async def test_text_that_looks_like_a_formula_stays_text(env) -> None:
    _as(env["app"], U_MEMBER)
    r = _export(env)
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    # No sheet holds a formula: every text cell is an inline string.
    for name in archive.namelist():
        if name.startswith("xl/worksheets/"):
            assert b"<f>" not in archive.read(name) and b"<f " not in archive.read(name)
    wb = _workbook(r)
    formula_cells = [c for ws in wb for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert {c.value for c in formula_cells} == {FORMULA, "=cmd|' /C calc'!A0.md"}
    assert all(c.data_type == "s" for c in formula_cells)


async def test_long_text_is_cut_with_a_marker_and_illegal_characters_dropped(env) -> None:
    store, m, a2 = env["store"], env["matrix"], env["rows"][2]
    long_value = "条" * 40_000
    await store.edit_cell_value(
        m.matrix_id, a2, env["cols"]["party"], value=long_value, column=env["by_name"]["Party"], workspace_id=WS_A, team_id=TEAM
    )
    await store.edit_cell_value(
        m.matrix_id, a2, env["cols"]["tags"], value="bell\x07 here", column=env["by_name"]["Party"], workspace_id=WS_A, team_id=TEAM
    )
    _as(env["app"], U_MEMBER)
    answers = list(_workbook(_export(env, headers={"Accept-Language": "fr-FR"}))["Réponses"].iter_rows(values_only=True))
    cut = answers[3][4]
    assert len(cut) == MAX_CELL_TEXT_CHARS
    assert cut.endswith("… (tronqué)") and cut.startswith("条" * 100)
    assert answers[3][5] == "bell here"


@pytest.mark.parametrize(
    ("request_kwargs", "sheets", "file_header", "hidden_label"),
    [
        ({"headers": {"Accept-Language": "zh-CN,zh;q=0.9"}}, ["答案", "出处", "说明"], "文件", "（无权查看）"),
        ({"headers": {"Accept-Language": "fr-FR"}, "cookies": {"NEXT_LOCALE": "de-DE"}}, ["Antworten", "Quellen", "Info"], "Datei", "(Kein Zugriff)"),
        ({"headers": {"Accept-Language": "fr"}}, ["Réponses", "Sources", "À propos"], "Fichier", "(Aucun accès)"),
        ({}, ["Answers", "Sources", "About"], "File", "(No access)"),
    ],
)
async def test_sheets_and_headers_follow_the_interface_language(env, request_kwargs, sheets, file_header, hidden_label) -> None:
    _as(env["app"], U_MEMBER)
    cookies = request_kwargs.get("cookies")
    if cookies:
        env["client"].cookies.update(cookies)
    wb = _workbook(_export(env, headers=request_kwargs.get("headers", {})))
    env["client"].cookies.clear()
    assert wb.sheetnames == sheets
    answers = list(wb[sheets[0]].iter_rows(values_only=True))
    assert answers[0][0] == file_header
    assert answers[4][0] == hidden_label


async def test_viewers_may_export_and_other_spaces_may_not(env) -> None:
    _as(env["app"], U_VIEWER)
    assert _export(env).status_code == 200
    _as(env["app"], U_OUTSIDER)
    r = _export(env)
    assert r.status_code == 403 and r.json()["detail"]["error"]["code"] == "workspace_access_denied"
    # A member of another Space probing this matrix id gets the same answer as for a missing matrix.
    r = env["client"].get(f"/api/v1/kb/matrices/{env['matrix'].matrix_id}/export.xlsx", headers=_h(WS_B))
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "matrix_not_found"
    r = env["client"].get("/api/v1/kb/matrices/mx_missing/export.xlsx", headers=_h(WS_B))
    assert r.status_code == 404 and r.json()["detail"]["error"]["code"] == "matrix_not_found"
    assert [event for event, _ in env["audits"]] == ["kb.matrix_exported"]


def test_export_filename_replaces_characters_file_systems_refuse() -> None:
    assert export_filename("Q3: 合同/续约?") == "Q3_ 合同_续约_.xlsx"
    assert export_filename('  ..\x00"<>  ') == "_.xlsx"
    assert export_filename("...") == "matrix.xlsx"
    assert export_filename("x" * 300) == "x" * 120 + ".xlsx"
