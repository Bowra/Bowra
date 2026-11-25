"""In-place editing of a PowerPoint result through the owner Results routes (pptx-inline-edit design §6).

Covers the switch, the owner fences, the version write and its refusal when the base is no
longer the latest, the facts the conflict message needs, an upload saved as a new file, and
the session rule that keeps a person's version revisable and listed for the Lead (§6.5).
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
from bowhead_core.artifact_store import PRESENTATION_USER_EDIT_PRODUCER, ArtifactStore, LocalBackend
from bowhead_core.delivered_files import render_delivered_files_block
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_gateway.routes import results as results_routes
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _deck(title: str = "2025 年 Q1 季度销售表现") -> bytes:
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Emu(457200), Emu(228600), Emu(6400800), Emu(600000))
    box.name = "标题"
    box.text_frame.text = title
    buffer = io.BytesIO()
    prs.save(buffer)
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
    store, sessions = stores
    monkeypatch.setenv("FEATURE_PPTX_INLINE_EDIT", "1")
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


async def _agent_deck(store: ArtifactStore, turn: str = "t1", target: str | None = None, title: str = "2025 年 Q1 季度销售表现"):
    return await store.put_bytes(
        content=_deck(title),
        filename="季度复盘.pptx",
        mime=PPTX,
        created_by="u_owner",
        source="python_sandbox",
        kind="deliverable",
        created_by_turn=turn,
        session_id="s_e",
        workspace_id="ws_e",
        version_of_target=target,
    )


def _title_edit(inventory: dict, before: str, after: str) -> dict:
    element = inventory["slides"][0]["elements"][0]
    return {"kind": "text", "key": element["key"], "paragraph": 0, "expect": before, "text": after}


async def test_the_switch_hides_both_routes(stores, client, monkeypatch):
    store, _ = stores
    http, _, _ = client
    deck = await _agent_deck(store)
    monkeypatch.setenv("FEATURE_PPTX_INLINE_EDIT", "0")
    response = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": deck.id})
    assert response.status_code == 404 and response.json()["detail"]["error"]["code"] == "presentation_edit_unavailable"
    assert http.post("/api/v1/sessions/s_e/results/presentation-edits", json={"artifact_id": deck.id, "edits": [{}]}).status_code == 404


async def test_a_saved_edit_is_the_next_version_and_stays_revisable_and_listed(stores, client):
    store, _ = stores
    http, access, pruned = client
    deck = await _agent_deck(store)
    inventory = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": deck.id}).json()
    assert inventory["edit_mode"] == "version" and inventory["version_no"] == 1
    assert access["operations"][-1] == "write"
    response = http.post(
        "/api/v1/sessions/s_e/results/presentation-edits",
        json={"artifact_id": deck.id, "edits": [_title_edit(inventory, "2025 年 Q1 季度销售表现", "2026 年 Q1 季度销售表现")]},
    )
    assert response.status_code == 201
    saved = response.json()
    assert saved["version_no"] == 2 and saved["new_file"] is False and saved["filename"] == "季度复盘.pptx"
    assert saved["changes"][0]["after"] == "2026 年 Q1 季度销售表现"
    row = await store.get(saved["artifact_id"])
    assert row.version_of == deck.id and row.created_by_turn is None and row.producer_kind == PRESENTATION_USER_EDIT_PRODUCER
    assert pruned == [deck.id]

    listed = await store.list_session_deliverables(session_id="s_e", user_id="u_owner")
    assert [item.id for item in listed] == [saved["artifact_id"]], "The Lead still sees the file, at the person's version"
    block = render_delivered_files_block(listed)
    assert f"artifact_id={saved['artifact_id']}" in block and "version=2 edited_by=user" in block, "The Lead learns the person saved it"
    target = await store.check_version_target(saved["artifact_id"], user_id="u_owner", session_id="s_e", workspace_id="ws_e", mime=PPTX)
    assert target.id == saved["artifact_id"], "The agent can revise the person's version"
    agent_next = await _agent_deck(store, "t2", saved["artifact_id"], title="2026 年 Q1 季度销售表现（AI）")
    assert agent_next.version_no == 3
    relisted = render_delivered_files_block(await store.list_session_deliverables(session_id="s_e", user_id="u_owner"))
    assert "version=3" in relisted and "version=3 edited_by=user" not in relisted, "An agent version is not marked as the person's"


async def test_an_older_base_is_refused_with_who_saved_the_newer_version(stores, client):
    store, _ = stores
    http, _, _ = client
    deck = await _agent_deck(store)
    stale = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": deck.id}).json()
    newer = await _agent_deck(store, "t2", deck.id, title="AI 改过的标题")
    response = http.post(
        "/api/v1/sessions/s_e/results/presentation-edits",
        json={"artifact_id": deck.id, "edits": [_title_edit(stale, "2025 年 Q1 季度销售表现", "人改的标题")]},
    )
    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == "version_target_not_latest"
    assert error["latest"]["artifact_id"] == newer.id and error["latest"]["version_no"] == 2
    assert error["latest"]["produced_by"] == "agent" and error["latest"]["request_excerpt"].startswith("把总结页改成三条要点")
    assert (await store.get(newer.id)).version_no == 2 and len(
        await store.list_result_versions(session_id="s_e", user_id="u_owner", artifact_id=newer.id)
    ) == 2


async def test_an_edit_on_changed_text_is_refused_by_index(stores, client):
    store, _ = stores
    http, _, _ = client
    deck = await _agent_deck(store)
    inventory = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": deck.id}).json()
    response = http.post(
        "/api/v1/sessions/s_e/results/presentation-edits",
        json={"artifact_id": deck.id, "edits": [_title_edit(inventory, "不是这段", "新标题")]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == {"code": "edit_refused", "edits": [{"index": 0, "code": "target_changed"}]}
    assert len(await store.list_result_versions(session_id="s_e", user_id="u_owner", artifact_id=deck.id)) == 1


async def test_an_upload_is_saved_as_a_new_file_that_later_edits_version(stores, client):
    store, _ = stores
    http, _, pruned = client
    upload = await store.put_bytes(
        content=_deck("上传的模板"),
        filename="客户模板.pptx",
        mime=PPTX,
        created_by="u_owner",
        source="upload",
        kind="user_upload",
        session_id="s_e",
        workspace_id="ws_e",
    )
    english = http.post(
        "/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": upload.id}, headers={"Accept-Language": "en-US"}
    ).json()
    assert english["new_filename"] == "客户模板 (edited).pptx", "The new file's name follows the interface language"
    chinese = {"Accept-Language": "zh-CN"}
    inventory = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": upload.id}, headers=chinese).json()
    assert inventory["edit_mode"] == "new_file" and inventory["edited_copies"] == []
    assert inventory["new_filename"] == "客户模板（已修改）.pptx"
    first = http.post(
        "/api/v1/sessions/s_e/results/presentation-edits",
        json={"artifact_id": upload.id, "edits": [_title_edit(inventory, "上传的模板", "填好的模板")]},
        headers=chinese,
    ).json()
    assert first["new_file"] is True and first["filename"] == "客户模板（已修改）.pptx" and first["version_no"] == 1
    created = await store.get(first["artifact_id"])
    assert created.derived_from == upload.id and created.version_of is None and pruned == []
    assert (await store.get(upload.id)).size == upload.size, "The upload keeps its bytes"

    again = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": first["artifact_id"]}).json()
    assert again["edit_mode"] == "version"
    second = http.post(
        "/api/v1/sessions/s_e/results/presentation-edits",
        json={"artifact_id": first["artifact_id"], "edits": [_title_edit(again, "填好的模板", "再改一次")]},
    ).json()
    assert second["version_no"] == 2 and second["new_file"] is False

    reopened = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": upload.id}).json()
    assert reopened["edited_copies"] == [{"artifact_id": second["artifact_id"], "filename": "客户模板（已修改）.pptx", "version_no": 2}], (
        "Reopening the upload offers the newest version of the file already made from it"
    )


async def test_the_owner_fences_hide_other_sessions_and_denied_spaces(stores, client):
    store, sessions = stores
    http, access, _ = client
    deck = await _agent_deck(store)
    await sessions.ensure_session(session_id="s_other", workspace_id="ws_e", user_id="u_other")
    assert http.post("/api/v1/sessions/s_other/results/presentation-edit/inventory", json={"artifact_id": deck.id}).status_code == 404
    assert http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": "art_missing"}).status_code == 404
    html = await store.put_bytes(
        content=b"<html></html>",
        filename="r.html",
        mime="text/html",
        created_by="u_owner",
        source="python_sandbox",
        kind="deliverable",
        created_by_turn="t1",
        session_id="s_e",
        workspace_id="ws_e",
    )
    response = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": html.id})
    assert response.status_code == 404 and response.json()["detail"]["error"]["code"] == "presentation_not_editable"
    access["denied"] = True
    assert http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": deck.id}).status_code == 404


def test_results_offer_editing_only_for_owned_decks_when_switched_on(monkeypatch):
    from bowhead_gateway.results_query_service import _presentation_editable

    fields = {"mime": PPTX, "filename": "deck.pptx"}
    deck = SimpleNamespace(kind="deliverable", producer_kind="python_sandbox", source="python_sandbox", **fields)
    upload = SimpleNamespace(kind="user_upload", producer_kind="upload", source="upload", **fields)
    template = SimpleNamespace(kind="deliverable", producer_kind="template_document_v1", source="x", **fields)
    pdf = SimpleNamespace(kind="deliverable", producer_kind="python_sandbox", source="python_sandbox", mime="application/pdf", filename="deck.pdf")
    monkeypatch.setenv("FEATURE_PPTX_INLINE_EDIT", "1")
    assert _presentation_editable(deck) and _presentation_editable(upload)
    assert not _presentation_editable(template) and not _presentation_editable(pdf)
    monkeypatch.setenv("FEATURE_PPTX_INLINE_EDIT", "0")
    assert not _presentation_editable(deck)


async def test_a_running_turn_holds_pruning_of_a_file_only_people_saved(stores, client):
    """A Turn lists the session's files when it starts; pruning one under it fails the Turn (116 N12).

    A file made from an edited upload has no Turn in any version, so its session comes from its pin.
    """
    store, sessions = stores
    http, _, _ = client
    upload = await store.put_bytes(
        content=_deck("v0"),
        filename="客户模板.pptx",
        mime=PPTX,
        created_by="u_owner",
        source="upload",
        kind="user_upload",
        session_id="s_e",
        workspace_id="ws_e",
    )
    target, text = upload.id, "v0"
    for step in range(1, 5):
        inventory = http.post("/api/v1/sessions/s_e/results/presentation-edit/inventory", json={"artifact_id": target}).json()
        saved = http.post(
            "/api/v1/sessions/s_e/results/presentation-edits",
            json={"artifact_id": target, "edits": [_title_edit(inventory, text, f"v{step}")]},
        ).json()
        target, text = saved["artifact_id"], f"v{step}"
        if step == 1:
            root = target
    assert saved["version_no"] == 4
    assert not await store.version_prune_deferred(root)
    await sessions.record_turn(turn_id="t_run", session_id="s_e", user_text="继续改")
    assert await store.version_prune_deferred(root), "The running Turn of the file's session holds pruning"
    await sessions.mark_completed(turn_id="t_run", intent=None, final_answer="done", tokens_input=0, tokens_output=0, cost_usd=0.0)
    assert not await store.version_prune_deferred(root)
