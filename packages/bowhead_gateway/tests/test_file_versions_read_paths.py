"""Readers that follow a file's newest version (file-versions design R2, R3, R6, Q7).

Chat cards keep the version they delivered and resolve the newest one at projection time,
so live delivery, history reads and replay agree. The owner's Results list every live
version of a file; the knowledge base and the Files page say when an entry is on an older one.
"""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.artifact_store import ArtifactStore, LocalBackend, ResultsArtifactEntry
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_gateway.routes import results as results_routes
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.source_access_projection import project_event_payloads_for_viewer, project_live_event_payload_for_viewer
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bowhead_core import migrations

pytestmark = pytest.mark.integration


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
    await sessions.ensure_session(session_id="s_v", workspace_id="ws_v", user_id="u_owner")
    for turn in ("t1", "t2", "t3"):
        await sessions.record_turn(turn_id=turn, session_id="s_v", user_text="Fix the report charts")
        await sessions.mark_completed(turn_id=turn, intent=None, final_answer="done", tokens_input=0, tokens_output=0, cost_usd=0.0)
        await sessions.finalize_turn(session_id="s_v", current_turn_id=turn, next_turn_id=None)
    return store, sessions


async def _put(store: ArtifactStore, turn: str, target: str | None = None, **overrides):
    fields = {
        "content": f"<html>{turn}</html>".encode(),
        "filename": "report.html",
        "mime": "text/html",
        "created_by": "u_owner",
        "source": "python_sandbox",
        "kind": "deliverable",
        "created_by_turn": turn,
        "session_id": "s_v",
        "workspace_id": "ws_v",
        **overrides,
    }
    return await store.put_bytes(version_of_target=target, **fields)


def _card(artifact) -> dict:
    return {"kind": "file", "id": artifact.id, "filename": artifact.filename, "download_url": f"/api/v1/files/{artifact.id}"}


async def test_a_delivered_card_points_at_the_newest_version_live_and_on_replay(stores, monkeypatch):
    store, _ = stores
    first = await _put(store, "t1")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_store=store)))
    final = {"inline_artifacts": [_card(first)]}
    [delivered] = await project_event_payloads_for_viewer(request, "t1", [("lead.final.payload", final)])
    assert "latest" not in delivered["inline_artifacts"][0], "A card for the newest version is unchanged"

    second = await _put(store, "t2", first.id)
    [replayed] = await project_event_payloads_for_viewer(request, "t1", [("lead.final.payload", final)])
    card = replayed["inline_artifacts"][0]
    assert card["id"] == first.id and "pruned" not in card
    assert card["latest"] == {
        "id": second.id,
        "version_no": 2,
        "filename": "report.html",
        "size_bytes": second.size,
        "download_url": f"/api/v1/files/{second.id}",
    }
    live = await project_live_event_payload_for_viewer(request, "t1", "lead.final.payload", final)
    assert live["inline_artifacts"] == replayed["inline_artifacts"]
    assert "latest" not in final["inline_artifacts"][0], "The stored payload keeps the delivered version"

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-11-13T00:00:00Z' WHERE id=?", (first.id,))
        await db.commit()
    [pruned] = await project_event_payloads_for_viewer(request, "t1", [("lead.final.payload", final)])
    assert pruned["inline_artifacts"][0]["pruned"] is True and pruned["inline_artifacts"][0]["latest"]["id"] == second.id

    async def broken(refs):
        raise RuntimeError("lookup down")

    monkeypatch.setattr(store, "newer_versions", broken)
    [kept] = await project_event_payloads_for_viewer(request, "t1", [("lead.final.payload", final)])
    assert kept["inline_artifacts"] == final["inline_artifacts"], "A failed lookup keeps the delivered cards"


async def test_an_image_card_carries_the_newest_preview_url(stores):
    store, _ = stores
    first = await _put(store, "t1", filename="chart.png", mime="image/png", content=b"png-1")
    second = await _put(store, "t2", first.id, filename="chart.png", mime="image/png", content=b"png-2")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_store=store)))
    image = {"kind": "image", "id": first.id, "url": f"/api/v1/files/{first.id}?disposition=inline", "download_url": f"/api/v1/files/{first.id}"}
    [replayed] = await project_event_payloads_for_viewer(request, "t1", [("lead.final.payload", {"inline_artifacts": [image]})])
    assert replayed["inline_artifacts"][0]["latest"]["url"] == f"/api/v1/files/{second.id}?disposition=inline"


@pytest.fixture
def versions_client(stores, monkeypatch):
    store, sessions = stores
    app = FastAPI()
    app.include_router(results_routes.owner_router, prefix="/api/v1")
    app.state.artifact_store = store
    app.state.session_store = sessions
    viewer = SimpleNamespace(id="u_owner", team_id="team_v")
    app.dependency_overrides[_current_user] = lambda: viewer
    access = {"denied": False}

    async def require_access(request, *, user_id, workspace_id, operation):
        if access["denied"]:
            raise HTTPException(status_code=403)

    from bowhead_gateway.routes import sessions as session_routes

    monkeypatch.setattr(session_routes, "_require_workspace_access", require_access)
    return TestClient(app), viewer, access


async def test_the_versions_list_follows_the_owner_results_fences(stores, versions_client):
    store, _ = stores
    client, viewer, access = versions_client
    first = await _put(store, "t1")
    second = await _put(store, "t2", first.id)
    third = await _put(store, "t3", second.id)
    other = await _put(store, "t1", filename="other.html")

    response = client.post("/api/v1/sessions/s_v/results/versions", json={"artifact_id": first.id})
    assert response.status_code == 200, response.text
    rows = response.json()["versions"]
    assert [(row["artifact_id"], row["version_no"], row["latest"]) for row in rows] == [
        (third.id, 3, True),
        (second.id, 2, False),
        (first.id, 1, False),
    ]
    single = client.post("/api/v1/sessions/s_v/results/versions", json={"artifact_id": other.id}).json()["versions"]
    assert [row["artifact_id"] for row in single] == [other.id]

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-11-13T00:00:00Z' WHERE id=?", (second.id,))
        await db.commit()
    listed = client.post("/api/v1/sessions/s_v/results/versions", json={"artifact_id": third.id}).json()["versions"]
    assert [row["artifact_id"] for row in listed] == [third.id, first.id], "Deleted versions are not listed"

    viewer.id = "u_intruder"
    assert client.post("/api/v1/sessions/s_v/results/versions", json={"artifact_id": first.id}).status_code == 404
    viewer.id = "u_owner"
    access["denied"] = True
    assert client.post("/api/v1/sessions/s_v/results/versions", json={"artifact_id": first.id}).status_code == 404
    access["denied"] = False
    assert client.post("/api/v1/sessions/s_v/results/versions", json={"artifact_id": "art_missing"}).status_code == 404


async def test_only_the_owner_sees_the_version_history_action(stores):
    from bowhead_core.results_report_relations import ReportRelations
    from bowhead_gateway.results_query_service import _position_artifacts

    store, _ = stores
    first = await _put(store, "t1")
    second = await store.get((await _put(store, "t2", first.id)).id)
    entry = ResultsArtifactEntry(artifact=second, sort_tuple=(0, 0, 0, 1, 0, second.created_at, f"artifact:{first.id}"), position=0, lineage_count=2)
    relations = ReportRelations(relations=(), projection_revision="r0")
    [owner] = _position_artifacts((entry,), [], "", writable=True, report_only=False, report_relations=relations)
    [reader] = _position_artifacts((entry,), [], "", writable=False, report_only=True, report_relations=relations)
    assert owner.lineage == (2, 2) and "view_history" in owner.capabilities
    assert "view_history" not in reader.capabilities


def _kb_row(artifact_id: str, *, file_id: str = "kbf_1"):
    from bowhead_core.kb.schemas import KbFileDTO

    return KbFileDTO(
        file_id=file_id,
        artifact_id=artifact_id,
        filename="report.html",
        mime_type="text/html",
        size_bytes=10,
        scope="team",
        scope_id="team_v",
        workspace_id="",
        team_id="team_v",
        uploaded_by="u_owner",
        uploaded_at="2026-11-13T00:00:00Z",
    )


async def test_a_knowledge_entry_tells_only_the_owner_about_a_newer_version(stores):
    """Q7: the entry stays on the indexed version; its owner learns a newer one exists, other readers learn nothing."""
    from bowhead_gateway.routes.kb import _with_newer_versions

    store, _ = stores
    first = await _put(store, "t1")
    second = await _put(store, "t2", first.id)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifact_store=store)))
    rows = [_kb_row(first.id), _kb_row(second.id, file_id="kbf_2")]
    owner = await _with_newer_versions(request, SimpleNamespace(user_id="u_owner", team_id="team_v"), rows)
    assert owner[0].newer_version is not None and (owner[0].newer_version.artifact_id, owner[0].newer_version.version_no) == (second.id, 2)
    assert owner[1].newer_version is None
    reader = await _with_newer_versions(request, SimpleNamespace(user_id="u_reader", team_id="team_v"), rows)
    assert all(row.newer_version is None for row in reader)


async def test_the_files_page_says_the_knowledge_base_holds_an_older_version(stores, monkeypatch):
    from bowhead_gateway.dtos import FileKbStatusDTO
    from bowhead_gateway.routes import files

    store, sessions = stores
    first = await _put(store, "t1")
    second = await _put(store, "t2", first.id)
    plain = await _put(store, "t1", filename="plain.html")
    status = FileKbStatusDTO(file_id="kbf_1", scope="team", workspace_id=None, parse_status="done")

    async def overlays(request, artifact_ids):
        assert first.id in artifact_ids, "Every version's KB membership is looked up"
        return {first.id}, {first.id: status}

    monkeypatch.setattr(files, "_kb_overlays", overlays)
    app = FastAPI()
    app.include_router(files.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.session_store = sessions
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="u_owner")
    items = {item["file_id"]: item for item in TestClient(app).get("/api/v1/files/", params={"source": "all"}).json()["items"]}
    assert set(items) == {second.id, plain.id}
    entry = items[second.id]
    assert (entry["in_kb"], entry["kb_outdated"], entry["kb"]["file_id"]) == (True, True, "kbf_1")
    assert (items[plain.id]["in_kb"], items[plain.id]["kb_outdated"]) == (False, False)


async def test_pruning_fails_closed_and_keeps_knowledge_base_versions(stores):
    """B4: without a working KB check nothing is pruned; a KB-referenced version is kept."""
    from bowhead_gateway.file_versions_pruning import prune_file_versions

    store, sessions = stores
    await sessions.record_turn(turn_id="t4", session_id="s_v", user_text="again")
    await sessions.mark_completed(turn_id="t4", intent=None, final_answer="done", tokens_input=0, tokens_output=0, cost_usd=0.0)
    await sessions.finalize_turn(session_id="s_v", current_turn_id="t4", next_turn_id=None)
    v1 = await _put(store, "t1")
    v2 = await _put(store, "t2", v1.id)
    v3 = await _put(store, "t3", v2.id)
    v4 = await _put(store, "t4", v3.id)

    class _Kb:
        def __init__(self, referenced=(), broken=False):
            self.referenced, self.broken = set(referenced), broken

        async def artifact_ids_in_kb(self, ids):
            if self.broken:
                raise RuntimeError("kb unavailable")
            return self.referenced & set(ids)

    app = SimpleNamespace(state=SimpleNamespace(artifact_store=store, kb_files_store=None))
    assert await prune_file_versions(app, [v1.id]) == 0, "No KB store: nothing is pruned"
    app.state.kb_files_store = _Kb(broken=True)
    with pytest.raises(RuntimeError):
        await prune_file_versions(app, [v1.id])
    app.state.kb_files_store = _Kb(referenced={v1.id})
    assert await prune_file_versions(app, [v1.id]) == 0, "A KB-referenced version is kept"
    assert (await store.get(v1.id)) is not None
    app.state.kb_files_store = _Kb()
    assert await prune_file_versions(app) == 1
    assert await store.get(v1.id) is None and await store.get(v4.id) is not None
