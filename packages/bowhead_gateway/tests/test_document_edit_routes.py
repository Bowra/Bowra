"""In-place editing of a Word result through the owner Results routes (docx inline edit design §7).

Mirrors the deck editor's route tests: the switch, the owner fences, the version write and its
refusal when the base is no longer the latest, refusals by edit and by document, an upload saved
as a new file, and the session rule that keeps a person's version listed for the Lead.
"""

from __future__ import annotations

import io
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import DOCUMENT_USER_EDIT_PRODUCER, ArtifactStore, LocalBackend
from bowhead_core.delivered_files import render_delivered_files_block
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_gateway.routes import results as results_routes
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
INVENTORY = "/api/v1/sessions/s_e/results/document-edit/inventory"
EDITS = "/api/v1/sessions/s_e/results/document-edits"


def _document(first: str = "一季度销售额 2,905 万元。", *, track_changes: bool = False) -> bytes:
    from docx import Document
    from docx.oxml import OxmlElement

    document = Document()
    document.add_heading("季度复盘", 1)
    paragraph = document.add_paragraph()
    paragraph.add_run(first)
    document.add_paragraph("二季度目标见附表。")
    if track_changes:
        document.settings.element.append(OxmlElement("w:trackRevisions"))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture
async def stores(tmp_path):
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "artifacts"), db_path=tmp_path / "files.db")
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{store.db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, SOURCE_REVISION)
    await store.initialize()
    contract = import_module("bowhead_core.migrations.versions.0228_artifact_versions")
    async with aiosqlite.connect(store.db_path) as db:
        for column, declaration in contract.COLUMNS:
            await db.execute(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        for statement in (contract.LINEAGE_INDEX, contract.VERSION_OF_UNIQUE, contract.VERSION_IMMUTABLE):
            await db.execute(statement)
        await db.commit()
    sessions = SessionStore(store.db_path)
    await sessions.initialize()
    await sessions.ensure_session(session_id="s_e", workspace_id="ws_e", user_id="u_owner")
    for turn in ("t1", "t2"):
        await sessions.record_turn(turn_id=turn, session_id="s_e", user_text="把总结页改成三条要点，并加一页数据来源说明")
        await sessions.mark_completed(turn_id=turn, intent=None, final_answer="done", tokens_input=0, tokens_output=0, cost_usd=0.0)
        await sessions.finalize_turn(session_id="s_e", current_turn_id=turn, next_turn_id=None)
    return store, sessions


@pytest.fixture
def client(stores, monkeypatch):
    monkeypatch.setenv("FEATURE_DOCX_INLINE_EDIT", "1")
    store, sessions = stores
    app = FastAPI()
    app.include_router(results_routes.owner_router, prefix="/api/v1")
    app.state.artifact_store = store
    app.state.session_store = sessions
    viewer = SimpleNamespace(id="u_owner", team_id="team_e")
    app.dependency_overrides[_current_user] = lambda: viewer
    access = {"denied": False, "operations": []}

    async def require_access(request, *, user_id, workspace_id, operation):
        access["operations"].append(operation)
        if access["denied"]:
            raise HTTPException(status_code=403)

    from bowhead_gateway.routes import sessions as session_routes

    monkeypatch.setattr(session_routes, "_require_workspace_access", require_access)
    pruned: list[str] = []
    from bowhead_gateway import file_versions_pruning

    monkeypatch.setattr(file_versions_pruning, "schedule_file_version_prune", lambda app, root: pruned.append(root))
    return TestClient(app), access, pruned


async def _agent_document(store: ArtifactStore, turn: str = "t1", target: str | None = None, first: str = "一季度销售额 2,905 万元。", **options):
    return await store.put_bytes(
        content=_document(first, **options),
        filename="季度复盘.docx",
        mime=DOCX,
        created_by="u_owner",
        source="python_sandbox",
        kind="deliverable",
        created_by_turn=turn,
        session_id="s_e",
        workspace_id="ws_e",
        version_of_target=target,
    )


def _edit(inventory: dict, before: str, after: str) -> dict:
    paragraph = next(item for item in inventory["paragraphs"] if item["text"].startswith(before[:4]))
    return {"key": paragraph["key"], "expect": before, "text": after}


async def test_the_switch_closes_both_routes(stores, client, monkeypatch):
    store, _ = stores
    http, _, _ = client
    document = await _agent_document(store)
    monkeypatch.setenv("FEATURE_DOCX_INLINE_EDIT", "0")
    for path, body in (
        (INVENTORY, {"artifact_id": document.id}),
        (EDITS, {"artifact_id": document.id, "edits": [{"key": "k", "expect": "", "text": ""}]}),
    ):
        response = http.post(path, json=body)
        assert response.status_code == 404 and response.json()["detail"]["error"]["code"] == "document_edit_disabled"


async def test_a_saved_edit_is_the_next_version_and_stays_listed_for_the_lead(stores, client):
    store, _ = stores
    http, access, pruned = client
    document = await _agent_document(store)
    inventory = http.post(INVENTORY, json={"artifact_id": document.id}).json()
    assert inventory["edit_mode"] == "version" and inventory["version_no"] == 1
    assert inventory["document"]["refusal"] is None and access["operations"][-1] == "write"
    response = http.post(
        EDITS, json={"artifact_id": document.id, "edits": [_edit(inventory, "一季度销售额 2,905 万元。", "一季度销售额 3,105 万元。")]}
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["version_no"] == 2 and saved["new_file"] is False and saved["filename"] == "季度复盘.docx"
    assert saved["changes"][0]["after"] == "一季度销售额 3,105 万元。"
    row = await store.get(saved["artifact_id"])
    assert row.version_of == document.id and row.created_by_turn is None
    assert row.producer_kind == DOCUMENT_USER_EDIT_PRODUCER and row.source == "document_edit" and row.kind == "deliverable"
    assert pruned == [document.id]
    listed = await store.list_session_deliverables(session_id="s_e", user_id="u_owner")
    assert [item.id for item in listed] == [saved["artifact_id"]], "The person's version is placed in its session by the pin"
    assert "version=2 edited_by=user" in render_delivered_files_block(listed), "The Lead learns the person saved it"
    reread = http.post(INVENTORY, json={"artifact_id": saved["artifact_id"]}).json()
    assert any(item["text"] == "一季度销售额 3,105 万元。" for item in reread["paragraphs"])


async def test_an_older_base_is_refused_with_who_saved_the_newer_version(stores, client):
    store, _ = stores
    http, _, _ = client
    document = await _agent_document(store)
    stale = http.post(INVENTORY, json={"artifact_id": document.id}).json()
    newer = await _agent_document(store, "t2", document.id, first="AI 改过的段落")
    response = http.post(EDITS, json={"artifact_id": document.id, "edits": [_edit(stale, "一季度销售额 2,905 万元。", "人改的段落")]})
    assert response.status_code == 409
    latest = response.json()["detail"]["error"]["latest"]
    assert latest["artifact_id"] == newer.id and latest["version_no"] == 2 and latest["produced_by"] == "agent"


async def test_refusals_name_the_edit_or_the_document(stores, client):
    store, _ = stores
    http, _, _ = client
    document = await _agent_document(store)
    inventory = http.post(INVENTORY, json={"artifact_id": document.id}).json()
    changed = http.post(
        EDITS, json={"artifact_id": document.id, "edits": [{**_edit(inventory, "一季度销售额 2,905 万元。", "新"), "expect": "不是这段"}]}
    )
    assert changed.status_code == 422
    error = changed.json()["detail"]["error"]
    assert error["code"] == "edit_refused" and error["edits"][0]["index"] == 0 and error["edits"][0]["code"] == "text_changed"
    same = http.post(EDITS, json={"artifact_id": document.id, "edits": [_edit(inventory, "一季度销售额 2,905 万元。", "一季度销售额 2,905 万元。")]})
    assert same.status_code == 422 and same.json()["detail"]["error"]["code"] == "no_changes"

    tracked = await _agent_document(store, "t2", track_changes=True)
    tracked_inventory = http.post(INVENTORY, json={"artifact_id": tracked.id}).json()
    assert tracked_inventory["document"]["refusal"] == "track_changes_on", "The editor can say why before any edit"
    refused = http.post(EDITS, json={"artifact_id": tracked.id, "edits": [_edit(tracked_inventory, "一季度销售额 2,905 万元。", "改")]})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error"] == {"code": "document_not_editable", "reason": "track_changes_on"}
    assert len(await store.list_result_versions(session_id="s_e", user_id="u_owner", artifact_id=document.id)) == 1


async def test_an_upload_is_saved_as_a_new_file_and_offered_when_reopened(stores, client):
    store, _ = stores
    http, _, pruned = client
    upload = await store.put_bytes(
        content=_document("上传的合同条款"),
        filename="合同.docx",
        mime=DOCX,
        created_by="u_owner",
        source="upload",
        kind="user_upload",
        session_id="s_e",
        workspace_id="ws_e",
    )
    english = http.post(INVENTORY, json={"artifact_id": upload.id}, headers={"Accept-Language": "en-US"}).json()
    assert english["new_filename"] == "合同 (edited).docx"
    chinese = {"Accept-Language": "zh-CN"}
    inventory = http.post(INVENTORY, json={"artifact_id": upload.id}, headers=chinese).json()
    assert inventory["edit_mode"] == "new_file" and inventory["edited_copies"] == [] and inventory["new_filename"] == "合同（已修改）.docx"
    first = http.post(
        EDITS, json={"artifact_id": upload.id, "edits": [_edit(inventory, "上传的合同条款", "修改后的合同条款")]}, headers=chinese
    ).json()
    assert first["new_file"] is True and first["filename"] == "合同（已修改）.docx" and first["version_no"] == 1
    created = await store.get(first["artifact_id"])
    assert created.derived_from == upload.id and created.version_of is None and pruned == []
    reopened = http.post(INVENTORY, json={"artifact_id": upload.id}).json()
    assert reopened["edited_copies"] == [{"artifact_id": created.id, "filename": "合同（已修改）.docx", "version_no": 1, "made_by": "you"}]


async def test_the_owner_fences_and_file_type(stores, client):
    store, sessions = stores
    http, access, _ = client
    document = await _agent_document(store)
    await sessions.ensure_session(session_id="s_other", workspace_id="ws_e", user_id="u_other")
    assert http.post("/api/v1/sessions/s_other/results/document-edit/inventory", json={"artifact_id": document.id}).status_code == 404
    assert http.post(INVENTORY, json={"artifact_id": "art_missing"}).status_code == 404
    deck = await store.put_bytes(
        content=b"PK deck",
        filename="deck.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        created_by="u_owner",
        source="python_sandbox",
        kind="deliverable",
        created_by_turn="t1",
        session_id="s_e",
        workspace_id="ws_e",
    )
    response = http.post(INVENTORY, json={"artifact_id": deck.id})
    assert response.status_code == 404 and response.json()["detail"]["error"]["code"] == "document_not_editable"
    access["denied"] = True
    assert http.post(INVENTORY, json={"artifact_id": document.id}).status_code == 404


def test_results_offer_word_editing_only_for_owned_word_files():
    from bowhead_gateway.results_query_service import _document_editable

    fields = {"mime": DOCX, "filename": "review.docx"}
    document = SimpleNamespace(kind="deliverable", producer_kind="python_sandbox", source="python_sandbox", **fields)
    upload = SimpleNamespace(kind="user_upload", producer_kind="upload", source="upload", **fields)
    template = SimpleNamespace(kind="deliverable", producer_kind="template_document_v1", source="x", **fields)
    pdf = SimpleNamespace(kind="deliverable", producer_kind="python_sandbox", source="python_sandbox", mime="application/pdf", filename="r.pdf")
    assert _document_editable(document) and _document_editable(upload)
    assert not _document_editable(template) and not _document_editable(pdf)
