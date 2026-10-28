"""RW-U-012: real catalog routes must agree on export-copy trust."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_export import init_export_jobs
from bowhead_core.export_copy_contract import (
    CURRENT_MANIFEST_PRODUCER,
    TRUST_COPY_CONTRACT,
    evict_export_copy_manifest,
    output_producer_for,
)
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_gateway.error_handler import register_error_handlers
from bowhead_gateway.routes import files, results, sessions
from cryptography.fernet import Fernet
from fastapi import FastAPI

from bowhead_core import CanvasStore, SessionStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    db = str(tmp_path / "catalog.db")
    session_store = SessionStore(db)
    canvas_store = CanvasStore(db)
    artifact_store = ArtifactStore(db_path=db, backend=LocalBackend(root_dir=tmp_path / "blobs"))
    for store in (session_store, TurnLogStore(db_path=db), canvas_store, artifact_store):
        await store.initialize()
    await init_export_jobs(db)
    session = await session_store.ensure_session(session_id="s_trust", workspace_id="ws_trust", user_id="owner")
    canvas = await canvas_store.ensure_canvas(session_id=session.id, workspace_id=session.workspace_id, user_id=session.user_id)
    await canvas_store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "经营报告"})
    app = FastAPI()
    register_error_handlers(app)
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user="owner")
    app.state.session_store = session_store
    app.state.canvas_store = canvas_store
    app.state.artifact_store = artifact_store
    app.include_router(files.router, prefix="/api/v1/files")
    app.include_router(sessions.router, prefix="/api/v1/sessions")
    app.include_router(results.router, prefix="/api/v1/results")
    app.include_router(results.owner_router, prefix="/api/v1")
    app.dependency_overrides[results._current_user] = lambda: SimpleNamespace(id="owner", team_id="team")
    evict_export_copy_manifest()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, artifacts=artifact_store, canvases=canvas_store, canvas=canvas, session=session)
    evict_export_copy_manifest()


async def _export(env, *, fmt="pdf", current=False, source="canvas_export", canvas_id=None, name="report"):
    mime = {
        "pdf": "application/pdf",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[fmt]
    canvas_id = canvas_id or env.canvas.id
    manifest = None
    job_id = f"exp_{name}_{fmt}"
    if current:
        manifest = await env.artifacts.put_bytes(
            content=json.dumps({"trust_copy_contract": TRUST_COPY_CONTRACT, "job_id": job_id, "format": fmt}).encode(),
            filename=f"{name}.manifest.json",
            mime="application/json",
            source=source,
            created_by="owner",
            canvas_id=canvas_id,
            session_id=env.session.id,
            producer_kind=CURRENT_MANIFEST_PRODUCER,
        )
    artifact = await env.artifacts.put_bytes(
        content=b"export fixture",
        filename=f"{name}.{fmt}",
        mime=mime,
        source=source,
        created_by="owner",
        canvas_id=canvas_id,
        session_id=env.session.id,
        producer_kind=output_producer_for(engine="legacy" if fmt == "docx" else "durable", fmt=fmt) if current else None,
        manifest_artifact_id=manifest.id if manifest else None,
    )
    if current:
        async with open_shared_ctx(env.artifacts.db_path) as db:
            await db.execute(
                "INSERT INTO canvas_export_jobs "
                "(id,canvas_id,fmt,status,contract_version,artifact_id,manifest_artifact_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, canvas_id, fmt, "done", 1 if fmt == "docx" else 2, artifact.id, manifest.id, artifact.created_at, artifact.created_at),
            )
            await db.commit()
    return artifact


async def _query(env, **body):
    return await env.client.post(f"/api/v1/sessions/{env.session.id}/results/query", json={"surface": "explorer", **body})


@pytest.mark.parametrize("surface", ["explorer", "quick", "locate"])
async def test_default_catalog_agrees_with_files_and_session_for_legacy_export(env, surface):
    artifact = await _export(env)
    capabilities = await env.client.get("/api/v1/results/capabilities")
    assert capabilities.json()["complete_list"] is True
    file_list = await env.client.get("/api/v1/files/?source=all")
    session_list = await env.client.get(f"/api/v1/sessions/{env.session.id}/artifacts")
    for response in (file_list, session_list):
        assert response.status_code == 200, response.text
    expected = "export_copy_legacy_regenerable"
    assert next(x for x in file_list.json()["items"] if x["file_id"] == artifact.id)["trust"] == expected
    assert next(x for x in session_list.json()["items"] if x["id"] == artifact.id)["trust"] == expected
    extra = {"anchor_key": f"artifact:{artifact.id}"} if surface == "locate" else {}
    response = await _query(env, surface=surface, **extra)
    assert response.status_code == 200, response.text
    item = next(x for x in response.json()["items"] if x["key"] == f"artifact:{artifact.id}")
    assert item["artifact_metadata"]["trust"] == expected


@pytest.mark.parametrize("fmt", ["pdf", "pptx", "xlsx", "docx"])
@pytest.mark.parametrize("source", ["canvas_export", "canvas_export_clone"])
async def test_current_export_contract_is_shared_for_all_supported_formats(env, fmt, source):
    artifact = await _export(env, fmt=fmt, source=source, current=True)
    file_list = await env.client.get("/api/v1/files/?source=all")
    session_list = await env.client.get(f"/api/v1/sessions/{env.session.id}/artifacts")
    catalog = await _query(env)
    for response in (file_list, session_list, catalog):
        assert response.status_code == 200, response.text
    assert next(x for x in file_list.json()["items"] if x["file_id"] == artifact.id)["trust"] == "export_copy_current"
    assert next(x for x in session_list.json()["items"] if x["id"] == artifact.id)["trust"] == "export_copy_current"
    assert next(x for x in catalog.json()["items"] if x["key"] == f"artifact:{artifact.id}")["artifact_metadata"]["trust"] == "export_copy_current"


@pytest.mark.parametrize("state", ["missing", "archived", "empty", "quarantined"])
async def test_results_conservatively_classifies_legacy_source_state(env, state):
    source = await env.canvases.ensure_canvas(session_id=f"s_{state}", workspace_id="ws_trust", user_id="owner")
    if state != "empty":
        section = await env.canvases.append_section(canvas_id=source.id, kind="h1", payload={"text": "Source"})
        if state == "quarantined":
            await env.canvases.set_quarantine(section_id=section.id, quarantined=True, reason="review")
    if state == "archived":
        await env.canvases.set_status(source.id, "archived")
    artifact = await _export(env, canvas_id="cv_missing" if state == "missing" else source.id)
    response = await _query(env)
    assert response.status_code == 200, response.text
    expected = {
        "missing": "export_copy_legacy_source_missing",
        "quarantined": "export_copy_legacy_source_quarantined",
        "empty": "export_copy_legacy_source_not_exportable",
        "archived": "export_copy_legacy_source_not_exportable",
    }[state]
    assert next(x for x in response.json()["items"] if x["key"] == f"artifact:{artifact.id}")["artifact_metadata"]["trust"] == expected


async def test_unreadable_manifest_does_not_reinstate_storage_trusted(env, monkeypatch):
    artifact = await _export(env, current=True)

    async def unavailable(_artifact):
        raise OSError("fixture blob unavailable")

    monkeypatch.setattr(env.artifacts, "open_read", unavailable)
    response = await _query(env)
    assert response.status_code == 200, response.text
    assert (
        next(x for x in response.json()["items"] if x["key"] == f"artifact:{artifact.id}")["artifact_metadata"]["trust"]
        == "export_copy_legacy_regenerable"
    )


async def test_long_display_title_cannot_bypass_export_classification(env):
    artifact = await _export(env, name="long" * 130)
    response = await _query(env)
    assert response.status_code == 200, response.text
    item = next(x for x in response.json()["items"] if x["key"] == f"artifact:{artifact.id}")
    assert len(item["title"]) == 500
    assert item["artifact_metadata"]["trust"] == "export_copy_legacy_regenerable"


@pytest.mark.parametrize("trust", ["trusted", "unverified"])
async def test_ordinary_artifact_keeps_trust_without_proof_or_metadata_batch(env, monkeypatch, trust):
    artifact = await env.artifacts.put_bytes(
        content=b"input", filename="input.pdf", mime="application/pdf", source="upload", created_by="owner", session_id=env.session.id, trust=trust
    )

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Ordinary files must not read export proof or batch metadata")

    monkeypatch.setattr(env.artifacts, "get_many", forbidden)
    monkeypatch.setattr(env.artifacts, "open_read", forbidden)
    response = await _query(env)
    assert response.status_code == 200, response.text
    item = next(x for x in response.json()["items"] if x["key"] == f"artifact:{artifact.id}")
    assert item["artifact_metadata"]["trust"] == trust
    assert item["status"] == ("unverified" if trust == "unverified" else "ready")


@pytest.mark.parametrize("surface", ["explorer", "quick", "locate"])
async def test_proof_reads_are_bounded_to_final_page_including_quick_priority_and_locate(env, monkeypatch, surface):
    outputs = [await _export(env, current=True, name=f"delivery_{index}") for index in range(14)]
    target = outputs[0]
    blob_reads = []
    metadata_reads = []
    original_read = env.artifacts.open_read
    original_many = env.artifacts.get_many

    async def read(artifact):
        blob_reads.append(artifact.id)
        return await original_read(artifact)

    async def many(ids, **kwargs):
        metadata_reads.append(list(ids))
        return await original_many(ids, **kwargs)

    monkeypatch.setattr(env.artifacts, "open_read", read)
    monkeypatch.setattr(env.artifacts, "get_many", many)
    body = {"surface": surface, "query": "delivery"}
    if surface == "quick":
        body.update(current_key=f"artifact:{target.id}", recent_keys=[f"artifact:{outputs[1].id}", "artifact:art_invisible"])
    else:
        body["page_size"] = 2
    if surface == "locate":
        body["anchor_key"] = f"artifact:{target.id}"
    response = await _query(env, **body)
    assert response.status_code == 200, response.text
    data = response.json()
    selected = {x["route"]["artifact_id"] for x in data["items"]}
    assert len(selected) == (8 if surface == "quick" else 2)
    if surface != "explorer":
        assert target.id in selected
    assert set(metadata_reads[0]) == selected
    assert set(blob_reads) == {x.manifest_artifact_id for x in outputs if x.id in selected}
    assert all(x["artifact_metadata"]["trust"] == "export_copy_current" for x in data["items"])

    if surface == "explorer":
        blob_reads.clear()
        metadata_reads.clear()
        page_two = await _query(env, **body, cursor=data["next_cursor"], snapshot_token=data["snapshot_token"])
        assert page_two.status_code == 200, page_two.text
        next_items = page_two.json()["items"]
        next_ids = {x["route"]["artifact_id"] for x in next_items}
        assert len(next_ids) == 2 and selected.isdisjoint(next_ids)
        assert [x["position"] for x in next_items] == [3, 4]
        assert set(metadata_reads[0]) == next_ids
        assert set(blob_reads) == {x.manifest_artifact_id for x in outputs if x.id in next_ids}
        assert all(x["artifact_metadata"]["trust"] == "export_copy_current" for x in next_items)

    # changes carries no items, and must not perform proof/metadata reads.
    blob_reads.clear()
    metadata_reads.clear()
    changes = await _query(env, surface="changes", query="delivery", snapshot_token=data["snapshot_token"])
    assert changes.status_code == 200, changes.text
    assert changes.json()["items"] == []
    assert blob_reads == metadata_reads == []


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        ("missing", 409, "results_snapshot_stale"),
        ("io", 503, "results_read_model_unavailable"),
    ],
)
async def test_selected_metadata_failure_returns_finite_error_not_trusted(env, monkeypatch, failure, status, code):
    await _export(env)

    async def broken(_ids):
        if failure == "io":
            raise OSError("internal metadata failure")
        return []

    monkeypatch.setattr(env.artifacts, "get_many", broken)
    response = await _query(env)
    assert response.status_code == status
    assert response.json() == {"detail": {"error": {"code": code}}}


async def test_legacy_page_shares_source_reads_and_leaves_sidecar_trust_unchanged(env, monkeypatch):
    outputs = [await _export(env, name=f"legacy_{index}") for index in range(4)]
    sidecar = await env.artifacts.put_bytes(
        content=b"{}",
        filename="notes.json",
        mime="application/json",
        source="canvas_export",
        created_by="owner",
        session_id=env.session.id,
        canvas_id=env.canvas.id,
    )
    calls = []
    original = env.canvases.list_export_projection

    async def projection(canvas_id):
        calls.append(canvas_id)
        return await original(canvas_id)

    async def no_blobs(*_args, **_kwargs):
        pytest.fail("Legacy exports without jobs and non-output sidecars have no proof blobs")

    monkeypatch.setattr(env.canvases, "list_export_projection", projection)
    monkeypatch.setattr(env.artifacts, "open_read", no_blobs)
    response = await _query(env)
    assert response.status_code == 200, response.text
    trust = {x["route"]["artifact_id"]: x["artifact_metadata"]["trust"] for x in response.json()["items"] if x["object_type"] == "artifact"}
    assert trust[sidecar.id] == "trusted"
    assert all(trust[x.id] == "export_copy_legacy_regenerable" for x in outputs)
    assert calls == [env.canvas.id]
