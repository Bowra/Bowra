"""Document matrix step 1 (S9): usage events recorded by the matrix routes.

Each route that creates, opens, runs, exports or verifies records one ``kb.matrix.*`` event in the real
``MetricsLog``, with the actor as ``user_id`` and ``{team_id, workspace_id, matrix_id, origin}``. Covers
the first-load-only rule of ``opened``, requests that change nothing or fail recording nothing, a failing
log never failing the request, and attributes that carry no content.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.extracted_record_store import KbExtractedRecordStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.kb.matrix_engine import MatrixRunConflict
from bowhead_core.kb.matrix_store import KbMatrixStore
from bowhead_core.metrics_log import MetricsLog
from bowhead_gateway.routes import kb_matrix
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_kb_matrix_authz import TEAM, U_MEMBER, U_VIEWER, WS_A, _as, _FakeKbStore, _h, _MembershipStore

if TYPE_CHECKING:
    from pathlib import Path

MATRIX_NAME = "Supplier contracts 2026"
FILE_NAMES = ("acme-master-agreement.md", "globex-renewal.md")
COLUMN_NAME = "Termination notice"
QUESTION = "How many days of notice does termination need?"
ANSWER = "ninety calendar days"
CONTENT = (MATRIX_NAME, *FILE_NAMES, COLUMN_NAME, QUESTION, ANSWER)


class _StubEngine:
    """Starts nothing: the routes only need the run it returns."""

    async def start_run(self, matrix: Any, scope: dict[str, Any], *, initiated_by: str) -> Any:
        if scope.get("column_id") == "col_busy":
            raise MatrixRunConflict("matrix_run_active")
        cells = 1 if scope["kind"] == "single_cell" else len(scope.get("file_ids") or matrix.files) * len(matrix.columns)
        return SimpleNamespace(run_id="mxrun_stub", cells_total=cells, skipped_edited=0, credits_estimated=0)


class _BrokenLog:
    async def track(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("metrics store unavailable")


@pytest.fixture(autouse=True)
def _single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


@pytest.fixture
async def env(initialize_migrated_kb_files_database, initialize_migrated_kb_matrix_database, tmp_path: Path):
    initialize_migrated_kb_matrix_database(tmp_path / "kb_matrix.db")
    store = KbMatrixStore(tmp_path / "kb_matrix.db")
    await store.initialize()
    initialize_migrated_kb_files_database(tmp_path / "files.db")
    files = KbFilesStore(tmp_path / "files.db")
    await files.initialize()
    metrics = MetricsLog(tmp_path / "metrics.db")
    await metrics.initialize()
    app = FastAPI()
    app.include_router(kb_matrix.router)
    app.state.kb_matrix_store = store
    app.state.kb_files_store = files
    app.state.kb_store = _FakeKbStore()
    app.state.kb_matrix_engine = _StubEngine()
    app.state.user_store = _MembershipStore()
    app.state.metrics_log = metrics
    rows = [
        await files.create(
            artifact_id=name,
            filename=name,
            mime_type="text/markdown",
            size_bytes=1,
            scope="workspace",
            scope_id=WS_A,
            workspace_id=WS_A,
            team_id=TEAM,
            uploaded_by=U_MEMBER,
        )
        for name in FILE_NAMES
    ]
    matrix = await store.create_matrix(
        name=MATRIX_NAME,
        workspace_id=WS_A,
        team_id=TEAM,
        created_by=U_MEMBER,
        files=[{"file_id": f, "char_count": 1} for f in rows],
        columns=[{"name": COLUMN_NAME, "type": "question", "question": QUESTION}],
    )
    column = matrix.columns[0]["column_id"]
    for file_id in rows:
        await store.enqueue_cells(
            matrix.matrix_id, [(file_id, column)], run_id="mxrun_t", workspace_id=WS_A, team_id=TEAM, column_versions={column: 1}, file_versions={}
        )
        variants = [{"raw": None, "normalized": {"value": ANSWER}, "is_primary": True, "extracted_by": "tier1", "source_excerpt": ANSWER}]
        assert await store.finalize_cell(
            matrix.matrix_id, file_id, column, status="done", variants=variants, prompt_version=1, file_version=1, run_id="mxrun_t"
        )
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        app=app,
        metrics=metrics,
        matrix_id=matrix.matrix_id,
        rows=rows,
        column=column,
        base=f"/api/v1/kb/matrices/{matrix.matrix_id}",
    )


def _place(matrix_id: str) -> dict[str, Any]:
    return {"team_id": TEAM, "workspace_id": WS_A, "matrix_id": matrix_id, "origin": "knowledge"}


async def _events(env: SimpleNamespace) -> list[tuple[str, str | None, dict[str, Any]]]:
    """Every recorded event in write order, checked for content on the way out."""
    async with (
        aiosqlite.connect(env.metrics._db_path) as db,
        db.execute("SELECT name, user_id, attrs_json FROM metrics_event_log ORDER BY rowid") as cur,
    ):
        rows = await cur.fetchall()
    for _name, _user, attrs_json in rows:
        assert not [text for text in CONTENT if text in attrs_json], attrs_json
    return [(name, user_id, json.loads(attrs_json)) for name, user_id, attrs_json in rows]


async def test_creating_a_matrix_records_created_once(env: SimpleNamespace) -> None:
    _as(env.app, U_MEMBER)
    body = {"workspace_id": WS_A, "name": MATRIX_NAME, "file_ids": env.rows, "columns": [{"name": COLUMN_NAME, "question": QUESTION}]}
    created = env.client.post("/api/v1/kb/matrices", json=body, headers=_h(WS_A))
    refused = env.client.post("/api/v1/kb/matrices", json={**body, "file_ids": ["file_missing"]}, headers=_h(WS_A))

    assert created.status_code == 200, created.text
    assert refused.status_code == 404, refused.text
    assert await _events(env) == [("kb.matrix.created", U_MEMBER, _place(created.json()["matrix_id"]))]


async def test_saving_a_template_as_a_matrix_records_created(env: SimpleNamespace, tmp_path: Path) -> None:
    doc_types = KbDocTypeStore(tmp_path / "dt.db")
    await doc_types.initialize()
    records = KbExtractedRecordStore(tmp_path / "rec.db")
    await records.initialize()
    env.app.state.kb_doc_type_store = doc_types
    env.app.state.kb_extracted_record_store = records
    doc_type = await doc_types.create(
        workspace_id=WS_A, name=MATRIX_NAME, created_by=U_MEMBER, fields=[ExtractField(name=COLUMN_NAME, type="string")]
    )
    await records.create(workspace_id=WS_A, doc_type_id=doc_type, file_id=env.rows[0], fields={COLUMN_NAME: {"value": ANSWER, "confidence": 0.9}})
    _as(env.app, U_MEMBER)

    r = env.client.post("/api/v1/kb/matrices/from_template", json={"workspace_id": WS_A, "doc_type_id": doc_type}, headers=_h(WS_A))

    assert r.status_code == 200, r.text
    assert await _events(env) == [("kb.matrix.created", U_MEMBER, _place(r.json()["matrix_id"]))]


async def test_opened_counts_only_the_first_load_of_a_page(env: SimpleNamespace) -> None:
    _as(env.app, U_VIEWER)
    url, headers = f"{env.base}/snapshot", _h(WS_A)

    first = env.client.get(url, params={"opened": "true"}, headers=headers)
    assert first.status_code == 200, first.text
    seq, digest = first.json()["matrix"]["seq"], first.json()["projection_digest"]
    # A later full reload, an idle poll and a run poll's delta never count, even when a client sends the flag again.
    reload = env.client.get(url, headers=headers)
    idle_poll = env.client.get(url, params={"since_seq": seq, "opened": "true"}, headers=headers)
    delta_poll = env.client.get(url, params={"since_seq": seq, "digest": digest, "opened": "true"}, headers=headers)

    assert [r.json()["kind"] for r in (first, reload, idle_poll, delta_poll)] == ["full", "full", "full", "delta"]
    assert await _events(env) == [("kb.matrix.opened", U_VIEWER, _place(env.matrix_id))]


async def test_starting_runs_records_scope_kind_cells_and_trial(env: SimpleNamespace) -> None:
    _as(env.app, U_MEMBER)
    whole = env.client.post(f"{env.base}/runs", json={"kind": "all_pending"}, headers=_h(WS_A))
    trial = env.client.post(f"{env.base}/runs", json={"kind": "force_column", "column_id": env.column, "file_ids": env.rows[:1]}, headers=_h(WS_A))
    cell = env.client.post(f"{env.base}/cells/{env.rows[0]}/{env.column}/rerun", headers=_h(WS_A))
    busy = env.client.post(f"{env.base}/runs", json={"kind": "column", "column_id": "col_busy"}, headers=_h(WS_A))

    assert [r.status_code for r in (whole, trial, cell, busy)] == [202, 202, 200, 409]
    assert await _events(env) == [
        ("kb.matrix.run_started", U_MEMBER, {**_place(env.matrix_id), "scope_kind": "all_pending", "cells": 2, "trial": False}),
        ("kb.matrix.run_started", U_MEMBER, {**_place(env.matrix_id), "scope_kind": "force_column", "cells": 1, "trial": True}),
        ("kb.matrix.run_started", U_MEMBER, {**_place(env.matrix_id), "scope_kind": "single_cell", "cells": 1, "trial": False}),
    ]


async def test_exporting_records_exported_once(env: SimpleNamespace) -> None:
    _as(env.app, U_VIEWER)

    r = env.client.get(f"{env.base}/export.xlsx", headers=_h(WS_A))

    assert r.status_code == 200, r.text
    assert await _events(env) == [("kb.matrix.exported", U_VIEWER, _place(env.matrix_id))]


async def test_verifying_records_count_and_bulk_or_single(env: SimpleNamespace) -> None:
    _as(env.app, U_MEMBER)
    cell_url = f"{env.base}/cells/{env.rows[0]}/{env.column}"
    verify = {"op": "set_verified", "verified": True}
    single = env.client.patch(cell_url, json=verify, headers=_h(WS_A))
    repeated = env.client.patch(cell_url, json=verify, headers=_h(WS_A))  # Already verified: nothing changes.
    undone = env.client.patch(cell_url, json={**verify, "verified": False}, headers=_h(WS_A))
    bulk = env.client.post(f"{env.base}/cells/verify", json={"column_id": env.column, "verified": True}, headers=_h(WS_A))
    bulk_again = env.client.post(f"{env.base}/cells/verify", json={"column_id": env.column, "verified": True}, headers=_h(WS_A))
    bulk_undone = env.client.post(f"{env.base}/cells/verify", json={"verified": False}, headers=_h(WS_A))

    assert all(r.status_code == 200 for r in (single, repeated, undone, bulk, bulk_again, bulk_undone))
    assert [bulk.json()["changed"], bulk_again.json()["changed"], bulk_undone.json()["changed"]] == [2, 0, 2]
    assert await _events(env) == [
        ("kb.matrix.cells_verified", U_MEMBER, {**_place(env.matrix_id), "count": 1, "bulk": False}),
        ("kb.matrix.cells_verified", U_MEMBER, {**_place(env.matrix_id), "count": 2, "bulk": True}),
    ]


async def test_a_failing_metrics_log_never_fails_the_request(env: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    env.app.state.metrics_log = _BrokenLog()
    _as(env.app, U_MEMBER)
    headers = _h(WS_A)

    with caplog.at_level(logging.WARNING, logger="bowhead_core.kb.matrix_usage"):
        responses = [
            env.client.post("/api/v1/kb/matrices", json={"workspace_id": WS_A, "name": "n", "file_ids": env.rows[:1]}, headers=headers),
            env.client.get(f"{env.base}/snapshot", params={"opened": "true"}, headers=headers),
            env.client.post(f"{env.base}/runs", json={"kind": "all_pending"}, headers=headers),
            env.client.post(f"{env.base}/cells/{env.rows[0]}/{env.column}/rerun", headers=headers),
            env.client.get(f"{env.base}/export.xlsx", headers=headers),
            env.client.patch(f"{env.base}/cells/{env.rows[0]}/{env.column}", json={"op": "set_verified", "verified": True}, headers=headers),
            env.client.post(f"{env.base}/cells/verify", json={"verified": True}, headers=headers),
        ]

    assert [r.status_code for r in responses] == [200, 200, 202, 200, 200, 200, 200]
    failed = [r.getMessage() for r in caplog.records if "kb_matrix.usage_failed" in r.getMessage()]
    assert len(failed) == len(responses)
    assert all("metrics store unavailable" in message for message in failed)


async def test_events_hold_no_content(env: SimpleNamespace) -> None:
    """Names, questions, answers and excerpts never reach the log: only identifiers, counts and kinds."""
    _as(env.app, U_MEMBER)
    headers = _h(WS_A)
    env.client.post("/api/v1/kb/matrices", json={"workspace_id": WS_A, "name": MATRIX_NAME, "file_ids": env.rows}, headers=headers)
    env.client.get(f"{env.base}/snapshot", params={"opened": "true"}, headers=headers)
    env.client.post(f"{env.base}/runs", json={"kind": "all_pending"}, headers=headers)
    env.client.get(f"{env.base}/export.xlsx", headers=headers)
    env.client.post(f"{env.base}/cells/verify", json={"verified": True}, headers=headers)

    events = await _events(env)  # Also checks the raw attributes for every content string.

    assert [name for name, _user, _attrs in events] == [
        "kb.matrix.created",
        "kb.matrix.opened",
        "kb.matrix.run_started",
        "kb.matrix.exported",
        "kb.matrix.cells_verified",
    ]
    allowed = {"team_id", "workspace_id", "matrix_id", "origin", "scope_kind", "cells", "trial", "count", "bulk"}
    assert all(set(attrs) <= allowed and user_id == U_MEMBER for _name, user_id, attrs in events)
