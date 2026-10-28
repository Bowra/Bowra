"""file-management-v2 P0-6: /files 管理面路由级测试。

补 discovery §3.8 的盲区——列表/软删/恢复/usages/分类此前无路由级覆盖
（upload/presign/inline 已有）。断言 payload 而非只断 200
（feedback: smoke 看 payload 不只 event 数）。

Fixture 模式沿 test_files_inline_disposition.py（dev auth_mode +
X-Bowhead-User-Id header）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "usr_owner"
_OTHER = "usr_other"
_H = {"X-Bowhead-User-Id": _OWNER}


@pytest.fixture
async def app_client(tmp_path: Path):
    db_path = tmp_path / "a.db"
    # Production wiring (main.py:365/:456): session_store and artifact_store share the same
    # DB file; usage_count's turn_attachments JOIN requires the session schema to exist.
    session_store = SessionStore(db_path)
    await session_store.initialize()
    backend = LocalBackend(root_dir=tmp_path / "files")
    store = ArtifactStore(backend=backend, db_path=db_path)
    await store.initialize()
    app = FastAPI()
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    client = TestClient(app)
    return client, store


async def _put(
    store: ArtifactStore,
    *,
    filename: str = "a.csv",
    mime: str = "text/csv",
    content: bytes = b"x,y\n1,2\n",
    source: str = "upload",
    created_by: str = _OWNER,
    kind: str | None = None,
):
    return await store.put_bytes(
        content=content,
        filename=filename,
        mime=mime,
        source=source,
        created_by=created_by,
        kind=kind,
    )


# ── GET / ────────────────────────────────────────────────────


async def test_list_default_source_is_upload_only(app_client) -> None:
    client, store = app_client
    await _put(store, filename="mine.csv", source="upload")
    await _put(store, filename="rows.csv", source="bi_fetch_rows")
    r = client.get("/api/v1/files/", headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["filename"] == "mine.csv"
    # Payload contract fields (frontend FileDTO consumer).
    it = body["items"][0]
    for key in (
        "file_id",
        "mime_family",
        "origin",
        "kind",
        "size",
        "usage_count",
        "kb_promotable",
        "url",
    ):
        assert key in it, f"FileListItemDTO 缺字段 {key}"
    assert it["origin"] == "upload"
    assert body["size_total_bytes"] > 0


async def test_list_source_all_returns_everything(app_client) -> None:
    client, store = app_client
    await _put(store, filename="mine.csv", source="upload")
    await _put(store, filename="rows.csv", source="bi_fetch_rows")
    r = client.get("/api/v1/files/?source=all", headers=_H)
    assert r.status_code == 200
    assert r.json()["total"] == 2


async def test_list_filters_mime_family_and_q(app_client) -> None:
    client, store = app_client
    await _put(store, filename="report.pdf", mime="application/pdf", content=b"%PDF")
    art = await _put(store, filename="data.csv")
    # mime_family
    r = client.get("/api/v1/files/?mime_family=pdf", headers=_H)
    assert [i["filename"] for i in r.json()["items"]] == ["report.pdf"]
    # q fuzzy filename match.
    r = client.get("/api/v1/files/?q=data", headers=_H)
    assert [i["filename"] for i in r.json()["items"]] == ["data.csv"]
    # q exact artifact ID match.
    r = client.get(f"/api/v1/files/?q={art.id}", headers=_H)
    assert [i["file_id"] for i in r.json()["items"]] == [art.id]


async def test_list_exposes_backend_kb_promotion_capability(app_client) -> None:
    client, store = app_client
    await _put(
        store,
        filename="report.pdf",
        mime="application/pdf",
        content=b"%PDF",
        kind="deliverable",
    )
    await _put(
        store,
        filename="manifest.json",
        mime="application/json",
        content=b"{}",
        kind="deliverable",
    )

    body = client.get("/api/v1/files/?source=all", headers=_H).json()
    by_name = {item["filename"]: item for item in body["items"]}
    assert by_name["report.pdf"]["kb_promotable"] is True
    assert by_name["manifest.json"]["kb_promotable"] is False


async def test_list_sort_size_and_pagination(app_client) -> None:
    client, store = app_client
    await _put(store, filename="small.csv", content=b"1")
    await _put(store, filename="big.csv", content=b"0" * 4096)
    r = client.get("/api/v1/files/?sort=size_desc", headers=_H)
    names = [i["filename"] for i in r.json()["items"]]
    assert names == ["big.csv", "small.csv"]
    r = client.get("/api/v1/files/?sort=size_desc&limit=1&offset=1", headers=_H)
    body = r.json()
    assert body["total"] == 2  # total counts matches, not page size.
    assert [i["filename"] for i in body["items"]] == ["small.csv"]


async def test_list_scoped_to_owner(app_client) -> None:
    client, store = app_client
    await _put(store, filename="mine.csv")
    await _put(store, filename="theirs.csv", created_by=_OTHER)
    r = client.get("/api/v1/files/", headers=_H)
    assert [i["filename"] for i in r.json()["items"]] == ["mine.csv"]


# ── DELETE / restore ─────────────────────────────────────────


async def test_soft_delete_then_trash_view_then_restore(app_client) -> None:
    client, store = app_client
    art = await _put(store)
    r = client.delete(f"/api/v1/files/{art.id}", headers=_H)
    assert r.status_code == 204
    # Hidden from the default view.
    assert client.get("/api/v1/files/", headers=_H).json()["total"] == 0
    # include_deleted=1 = deleted only (trash semantics, discovery §1.5).
    body = client.get("/api/v1/files/?include_deleted=1", headers=_H).json()
    assert body["total"] == 1
    assert body["items"][0]["deleted_at"] is not None
    # restore → back to default view.
    r = client.post(f"/api/v1/files/{art.id}/restore", headers=_H)
    assert r.status_code == 200
    assert r.json()["deleted_at"] is None
    assert client.get("/api/v1/files/", headers=_H).json()["total"] == 1


async def test_delete_not_owner_and_double_delete_404(app_client) -> None:
    client, store = app_client
    art = await _put(store, created_by=_OTHER)
    r = client.delete(f"/api/v1/files/{art.id}", headers=_H)
    assert r.status_code == 404  # Silent 404 hides existence.
    mine = await _put(store)
    assert client.delete(f"/api/v1/files/{mine.id}", headers=_H).status_code == 204
    assert client.delete(f"/api/v1/files/{mine.id}", headers=_H).status_code == 404


async def test_restore_not_deleted_404(app_client) -> None:
    client, store = app_client
    art = await _put(store)
    r = client.post(f"/api/v1/files/{art.id}/restore", headers=_H)
    assert r.status_code == 404


# ── usages ───────────────────────────────────────────────────


async def test_usages_empty_and_authz(app_client) -> None:
    client, store = app_client
    art = await _put(store)
    r = client.get(f"/api/v1/files/{art.id}/usages", headers=_H)
    assert r.status_code == 200
    assert r.json()["usages"] == []
    theirs = await _put(store, created_by=_OTHER)
    assert client.get(f"/api/v1/files/{theirs.id}/usages", headers=_H).status_code == 404
    assert client.get("/api/v1/files/art_nonexist/usages", headers=_H).status_code == 404


# ── category ─────────────────────────────────────────────────


async def test_set_category_valid_invalid_authz(app_client) -> None:
    client, store = app_client
    art = await _put(store)
    r = client.post(f"/api/v1/files/{art.id}/category", json={"category": "dataset"}, headers=_H)
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "dataset"
    assert body["category_source"] == "user"
    assert (await store.get(art.id)).category == "dataset"
    for bad in ("不存在的类", "Dataset", ""):
        r = client.post(f"/api/v1/files/{art.id}/category", json={"category": bad}, headers=_H)
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"]["code"] == "invalid_file_category"
    assert (await store.get(art.id)).category == "dataset"
    theirs = await _put(store, created_by=_OTHER)
    r = client.post(f"/api/v1/files/{theirs.id}/category", json={"category": "dataset"}, headers=_H)
    assert r.status_code == 404


async def test_set_category_accepts_legacy_chinese_label_and_stores_code(app_client) -> None:
    """过渡期兼容：旧前端仍发中文类别名，接口归一化成代码值再落库、再返回。"""
    client, store = app_client
    art = await _put(store)
    r = client.post(f"/api/v1/files/{art.id}/category", json={"category": " 会议纪要 "}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "meeting_notes"
    assert (await store.get(art.id)).category == "meeting_notes"


async def test_preview_failure_returns_code_without_exception_prose(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = app_client
    art = await _put(store)

    async def _fail_read(_art):
        raise RuntimeError("RAW_STORAGE_PATH_AND_TOKEN")

    monkeypatch.setattr(store, "open_read", _fail_read)
    r = client.get(f"/api/v1/files/{art.id}/preview", headers=_H)
    assert r.status_code == 200
    assert r.json()["error"] is None
    assert r.json()["error_code"] == "file_preview_failed"
    assert "RAW_STORAGE_PATH_AND_TOKEN" not in r.text


async def test_text_preview_keeps_shared_bounded_extraction_instead_of_recropping(
    app_client,
) -> None:
    client, store = app_client
    content = ("0123456789" * 400).encode()
    art = await _put(
        store,
        filename="long.txt",
        mime="text/plain",
        content=content,
    )

    r = client.get(f"/api/v1/files/{art.id}/preview?mode=text", headers=_H)

    assert r.status_code == 200
    assert r.json()["text"] == content.decode()
    assert len(r.json()["text"]) > 1500
    assert r.json()["truncated"] is False
