"""GET /api/v1/files/{id}?disposition=inline（multimodal-v1）。

覆盖（tech-design §8）：
- png + inline → `Content-Disposition: inline` + `X-Content-Type-Options: nosniff`
  + body 逐字节 + 正确 mime
- svg + inline → 仍 attachment（XSS 白名单外）
- 无 param → 现行为（attachment 带 filename）
- owner check 在 inline 路径仍生效（跨 user 静默 404）
- MP4/WebM/Ogg 可 inline；Local FileResponse 保留标准 Range 语义
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
_MP4 = b"\x00\x00\x00\x18ftypisom" + bytes(range(64))

_OWNER = "usr_owner"


@pytest.fixture
async def app_client(tmp_path: Path):
    backend = LocalBackend(root_dir=tmp_path / "files")
    store = ArtifactStore(backend=backend, db_path=tmp_path / "a.db")
    await store.initialize()
    app = FastAPI()
    # Same mount as main.py:1846 (router itself has no prefix).
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    # dev-mode test: identity comes from the X-Bowhead-User-Id header (honored only in dev auth_mode;
    # a non-dev deployment would 401 the header — see resolve_user_id_sync).
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    client = TestClient(app)
    return client, store


async def _put(store: ArtifactStore, *, mime: str, content: bytes, filename: str, created_by: str = _OWNER):
    return await store.put_bytes(
        content=content,
        filename=filename,
        mime=mime,
        source="image_generate",
        created_by=created_by,
        session_id="s1",
        kind="deliverable",
    )


async def test_png_inline_disposition(app_client) -> None:
    client, store = app_client
    art = await _put(store, mime="image/png", content=_PNG, filename="gen_海报_1.png")
    r = client.get(
        f"/api/v1/files/{art.id}?disposition=inline",
        headers={"X-Bowhead-User-Id": _OWNER},
    )
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == _PNG


async def test_svg_inline_param_stays_attachment(app_client) -> None:
    client, store = app_client
    art = await _put(store, mime="image/svg+xml", content=_SVG, filename="x.svg")
    r = client.get(
        f"/api/v1/files/{art.id}?disposition=inline",
        headers={"X-Bowhead-User-Id": _OWNER},
    )
    assert r.status_code == 200
    disp = r.headers.get("content-disposition", "")
    assert "attachment" in disp  # Outside allowlist → existing behavior.
    assert "inline" not in disp.split(";")[0]


async def test_mp4_inline_supports_byte_ranges(app_client) -> None:
    client, store = app_client
    art = await _put(
        store,
        mime="video/mp4",
        content=_MP4,
        filename="result.mp4",
    )
    r = client.get(
        f"/api/v1/files/{art.id}?disposition=inline",
        headers={
            "X-Bowhead-User-Id": _OWNER,
            "Range": "bytes=4-11",
        },
    )
    assert r.status_code == 206
    assert r.content == _MP4[4:12]
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["content-range"] == f"bytes 4-11/{len(_MP4)}"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"].startswith("video/mp4")


async def test_no_param_unchanged_attachment(app_client) -> None:
    client, store = app_client
    art = await _put(store, mime="image/png", content=_PNG, filename="gen_x.png")
    r = client.get(f"/api/v1/files/{art.id}", headers={"X-Bowhead-User-Id": _OWNER})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.content == _PNG


async def test_inline_owner_check_cross_user_404(app_client) -> None:
    client, store = app_client
    art = await _put(store, mime="image/png", content=_PNG, filename="gen_x.png")
    r = client.get(
        f"/api/v1/files/{art.id}?disposition=inline",
        headers={"X-Bowhead-User-Id": "usr_intruder"},
    )
    assert r.status_code == 404  # Silent 404, no existence leak.


async def test_html_download_proxies_the_instance_policy_after_owner_authorization(app_client, monkeypatch) -> None:
    client, store = app_client
    monkeypatch.setenv("BOWHEAD_CANVAS_HTML_MAX_BYTES", "6291456")
    content = b"<p>Readable report</p>"
    art = await _put(store, mime="text/html; charset=utf-8", content=content, filename="report.html")
    response = client.get(f"/api/v1/files/{art.id}?disposition=inline", headers={"X-Bowhead-User-Id": _OWNER})
    assert response.status_code == 200 and response.content == content
    assert response.headers["x-bowhead-html-max-bytes"] == "6291456"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    forbidden = client.get(f"/api/v1/files/{art.id}", headers={"X-Bowhead-User-Id": "usr_intruder"})
    assert forbidden.status_code == 404
    assert "x-bowhead-html-max-bytes" not in forbidden.headers


async def test_remote_html_stream_retains_policy_without_a_presigned_redirect(app_client, monkeypatch):
    from unittest.mock import AsyncMock

    client, store = app_client
    content = b"<p>Remote report</p>"
    art = await _put(store, mime="text/html", content=content, filename="remote.html")
    art.path = "s3://private-bucket/remote.html"
    original_get = store.get

    async def get(artifact_id, **kwargs):
        return art if artifact_id == art.id else await original_get(artifact_id, **kwargs)

    async def read_range(artifact, *, start, end):
        assert artifact.path.startswith("s3://")
        yield content[start:end]

    redirect = AsyncMock(return_value="https://object-store.invalid/private-report")
    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(store, "iter_read_range", read_range)
    monkeypatch.setattr(store, "download_url", redirect)
    response = client.get(f"/api/v1/files/{art.id}", headers={"X-Bowhead-User-Id": _OWNER}, follow_redirects=False)
    assert response.status_code == 200 and response.content == content
    assert response.headers["x-bowhead-html-max-bytes"] == str(25 * 1024 * 1024)
    redirect.assert_not_called()


async def test_download_of_artifact_marked_missing_is_410(app_client) -> None:
    """对账标为 missing（原文件已丢失）⇒ 410 file_content_missing，不再发出必然失败的地址；对照：ready 时 200。"""
    client, store = app_client
    art = await _put(store, mime="image/png", content=_PNG, filename="a.png")
    assert await store.set_blob_missing([art.id], missing=True) == 1

    r = client.get(f"/api/v1/files/{art.id}", headers={"X-Bowhead-User-Id": _OWNER})
    assert r.status_code == 410
    assert "file_content_missing" in r.text

    await store.set_blob_missing([art.id], missing=False)
    assert client.get(f"/api/v1/files/{art.id}", headers={"X-Bowhead-User-Id": _OWNER}).status_code == 200
