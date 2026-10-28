"""Phase 1.4a UT — chat-attachment presign + confirm endpoints.

Covers tech-design §5.1a + §5.3a:
- presign rate limit (429) + pending cap (429) + MIME whitelist (415) + size cap (413)
- presign negotiates bounded multipart when the backend cannot issue an envelope
- confirm CAS race + idempotent_replay + cross-user 403 + 404 + 415 mime swap + 410 expired
- session pinning on confirm
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_store import LocalBackend, ObjectHead, PresignPostResult
from bowhead_gateway.routes import files_presign
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import ArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

# ── fixtures ──────────────────────────────────────────────────────


@dataclass
class _FakeBackend:
    """Stand-in that supports presign_post + head per test needs."""

    name: str = "local"
    presign_result: PresignPostResult | None = None
    head_result: ObjectHead | None = None
    head_raises: Exception | None = None

    async def presign_post(self, *, aid, filename, mime, size_bytes, expires_in=3600):
        if self.presign_result is None:
            return None
        # rebuild with the aid the caller passed (path embeds it)
        return PresignPostResult(
            url=self.presign_result.url,
            fields=dict(self.presign_result.fields),
            path=f"/tmp/{aid}_{filename}",
            expires_at=self.presign_result.expires_at,
        )

    async def head(self, path: str):
        if self.head_raises is not None:
            raise self.head_raises
        return self.head_result


class _FakeUserStore:
    """rate_limit_check stub — controlled per-test."""

    def __init__(self) -> None:
        self.allowed = True
        self.calls: list[tuple[str, int, int]] = []

    async def rate_limit_check(self, bucket: str, *, limit: int, window_s: int) -> bool:
        self.calls.append((bucket, limit, window_s))
        return self.allowed


@pytest.fixture
async def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(files_presign.router)
    # Real ArtifactStore (initialized for status/expires_at columns) + fake backend
    store = ArtifactStore(
        db_path=str(tmp_path / "p14a.db"),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await store.initialize()
    a.state.artifact_store = store
    a.state.artifact_backend = _FakeBackend(
        presign_result=PresignPostResult(
            url="http://test/p14a/local-receiver/aid",
            fields={"Content-Type": "image/png", "Authorization": "Bearer XYZ"},
            path="placeholder",  # overridden per-call
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=3600),
        ),
        head_result=ObjectHead(size=12345, etag='"abc"', mime="image/png"),
    )
    a.state.user_store = _FakeUserStore()
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# Patch `_user_id` so we don't have to spin up the full auth pipeline.
@pytest.fixture(autouse=True)
def stub_user_id(monkeypatch) -> None:
    from bowhead_gateway.routes import files_presign as fp

    monkeypatch.setattr(fp, "_user_id", lambda req: "u_test")


# ── presign happy + validation ────────────────────────────────────


def test_files_presign_happy_returns_envelope(client: TestClient, app: FastAPI) -> None:
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "screenshot.png", "mime": "image/png", "size_bytes": 100_000},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"].startswith("art_") and len(body["file_id"]) == 16
    assert body["artifact_id"] == body["file_id"]
    assert body["upload"]["url"].startswith("http://test/")
    assert body["upload"]["fields"]["Content-Type"] == "image/png"
    assert body["upload"]["method"] == "POST"
    assert body["confirm_url"] == f"/api/v1/files/presign/{body['file_id']}/confirm"
    # DB row exists with status=pending_upload
    store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT status, created_by, mime, size FROM artifacts WHERE id=?",
                (body["file_id"],),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "pending_upload"
        assert row["created_by"] == "u_test"
        assert row["mime"] == "image/png"
        assert row["size"] == 100_000

    asyncio.run(_check())


def test_files_presign_mime_ext_fallback_md_to_markdown(client: TestClient) -> None:
    """Browser may send `.md` as application/octet-stream → normalize to text/markdown."""
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "doc.md", "mime": "application/octet-stream", "size_bytes": 1000},
    )
    assert r.status_code == 200, r.text


def test_files_presign_rejects_text_mime_with_wrong_extension(client: TestClient) -> None:
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "page.html", "mime": "text/plain", "size_bytes": 100_000},
    )
    assert r.status_code == 415


def test_files_presign_rejects_unsupported_mime(client: TestClient) -> None:
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "x.exe", "mime": "application/x-msdownload", "size_bytes": 100},
    )
    assert r.status_code == 415


def test_files_presign_persists_space_attribution_from_header(client: TestClient, app: FastAPI) -> None:
    """★ 空间隔离：请求带了空间头时，`files_presign` 手上的 `workspace_id` 必须**真的**落进列里。

    行为断言（查库真值），不是源码文本断言——同族突变「把写入值改成 `None`」在两条源码级
    守卫下都是活的（2026-09-18 在 `kb_presign` 上突变矩阵实测，9/9 里守卫全绿）。
    顺带钉住列与**梯子①**（`artifact_upload_bindings`）一致：两者各说各话时，
    「先读列、为 NULL 再回落推导」会在两档 flag 下给出不同答案。

    ⚠ **已知缺口，本用例故意不钉**：没有空间上下文时（既无 `session_id` 也无空间头），
    `_atomic_insert_pending` 的 `workspace_id or None` 落 **NULL** ⇒ 打戳并翻 fail-close 之后这类产物对
    **所有人**（含作者）403；而前端 `api.ts` 的 `issueFilesPresign` **根本不发空间头**。
    （设计文档 `artifact-workspace-backfill.md` 的写入点表原先写着这条路「手上就有权威值、直接落」，
    与实现不符——2026-09-21 已更正。）该缺口应当落成什么值**尚未拍板**，
    所以这里只钉「有空间上下文时必须落真值」这条无争议的不变量，
    **不把 NULL 钉成「对的」**（钉了等于把缺陷固化成契约）。
    """
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "screenshot.png", "mime": "image/png", "size_bytes": 1000},
        headers={"X-Space-Id": "ws_42"},
    )
    assert r.status_code == 200, r.text
    aid = r.json()["artifact_id"]

    store: ArtifactStore = app.state.artifact_store

    async def _read():
        async with aiosqlite.connect(store.db_path) as db:
            cur = await db.execute("SELECT workspace_id FROM artifacts WHERE id=?", (aid,))
            art = await cur.fetchone()
            cur = await db.execute(
                "SELECT workspace_id FROM artifact_upload_bindings WHERE artifact_id=?",
                (aid,),
            )
            binding = await cur.fetchone()
        return art, binding

    art, binding = asyncio.run(_read())

    # Positive control: artifact really persisted (otherwise the next assertion only proves no row found).
    assert art is not None, "产物没落进 artifacts —— 这条用例证明不了任何事"
    assert art[0] == "ws_42", f"列里的归属写错：实得 {art[0]!r}，应为 'ws_42'（留 NULL 会在切点后被 fail-close 拦掉）"
    assert binding is not None and binding[0] == "ws_42", f"梯子① 的 binding 与列不一致：{binding!r}"


def test_files_presign_persists_space_attribution_from_session(client: TestClient, app: FastAPI) -> None:
    """★★ **生产真正走的是这条**：`_require_upload_space` 的 **session 分支**。

    2026-09-19 review 逮到的缺口：上面那条用的是**空间头**，而前端 `api.ts` 的
    `issueFilesPresign` **只发 `Content-Type`**、从不发空间头——它发的是 `session_id`。
    于是 `files_presign` 的 `_require_upload_space` 走 session 分支，归属取自 **`session.workspace_id`**，
    请求头那条路只有 API / agent 客户端才走。
    ⇒ 只测带头那档 = 测了一条 shipped FE **从不走**的分支（又一例「自己的夹具只覆盖顺利路径」）。

    两条一起留着才是完整的：带头那条守 API 客户端，这条守浏览器。
    """
    from bowhead_core.session_store import SessionStore

    store: ArtifactStore = app.state.artifact_store
    sessions = SessionStore(store.db_path)

    async def _make_session():
        await sessions.initialize()
        # user_id must match the caller from `stub_user_id`; otherwise it exercises the 403 branch.
        return await sessions.ensure_session(session_id=None, workspace_id="ws_from_session", user_id="u_test")

    session = asyncio.run(_make_session())
    app.state.session_store = sessions

    r = client.post(
        "/api/v1/files/presign",
        json={
            "filename": "shot.png",
            "mime": "image/png",
            "size_bytes": 1000,
            "session_id": session.id,
        },
    )
    assert r.status_code == 200, r.text
    aid = r.json()["artifact_id"]

    async def _read():
        async with aiosqlite.connect(store.db_path) as db:
            cur = await db.execute("SELECT workspace_id FROM artifacts WHERE id=?", (aid,))
            return await cur.fetchone()

    art = asyncio.run(_read())

    assert art is not None, "产物没落进 artifacts —— 这条用例证明不了任何事"
    assert art[0] == "ws_from_session", f"session 分支的归属写错：实得 {art[0]!r}，应为 'ws_from_session'"


# ── Attribution metrics (persisted in metrics_event_log) ────────────────────
#
# `files_presign` reason classifies by **persisted results** (see `reason_for_resolved_attribution`):
# each branch also asserts the actual `artifacts.workspace_id` value, tying reason to result
# rather than trusting reason alone; 2026-09-21 B review used three probes to show intent-based classification is wrong here:
# ① Empty session space⇒persist NULL, but old policy recorded `session` (undercounting `none`);
# ② Space header and session both present⇒session branch **ignores** header, persists session's space, but old policy recorded `space`;
# ③ Only legacy `X-Workspace-Id` header⇒persist `ws_legacy`, correctly record `space` (this fallback was untested).
# Drive the real route: `with client:` + `attribution_metrics.drain(client)` deterministically await background-task writes.

_METRICS_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"

_PRESIGN_BODY = {"filename": "shot.png", "mime": "image/png", "size_bytes": 1000}


def _persisted_workspace(app: FastAPI, artifact_id: str) -> str | None:
    async def _read() -> str | None:
        async with aiosqlite.connect(app.state.artifact_store.db_path) as db:
            cur = await db.execute("SELECT workspace_id FROM artifacts WHERE id=?", (artifact_id,))
            row = await cur.fetchone()
        assert row is not None, "产物没落进 artifacts —— 这条用例证明不了任何事"
        return row[0]

    return asyncio.run(_read())


def _attach_session(app: FastAPI, *, workspace_id: str) -> str:
    """建一个属于调用者（`u_test`）的 session 并挂上 session_store，返回 session id。"""
    from bowhead_core.session_store import SessionStore

    sessions = SessionStore(app.state.artifact_store.db_path)

    async def _make():
        await sessions.initialize()
        return await sessions.ensure_session(session_id=None, workspace_id=workspace_id, user_id="u_test")

    session = asyncio.run(_make())
    app.state.session_store = sessions
    return session.id


@pytest.mark.integration
@pytest.mark.parametrize(
    ("leg", "expected_reason", "expected_persisted"),
    [
        ("header", "space", "ws_42"),
        ("legacy_header", "space", "ws_legacy"),  # ★ Only legacy X-Workspace-Id: fallback previously untested.
        ("session", "session", "ws_from_session"),
        ("session_with_empty_workspace", "none", None),  # ★ Persist NULL⇒record none (old policy: session).
        ("header_and_session", "session", "ws_from_session"),  # ★ Session ignores header (old policy: space).
        # ★ B/A review: header and session point to the **same** space; session still ignores the header, so must record session.
        # Guessing by 'persisted value equals header' would record space (previous cases all used different IDs, missing this).
        ("header_equal_to_session_space", "session", "ws_from_session"),
        # ★ Conversely: session_store wired, but request **omits** session_id⇒header branch, recording space.
        # Checking only whether session_store exists, ignoring session_id, would record session here.
        ("header_with_session_store_but_no_session_id", "space", "ws_42"),
        ("neither", "none", None),
    ],
)
def test_files_presign_metric_reason_follows_the_persisted_value(
    client: TestClient,
    app: FastAPI,
    attribution_metrics,
    monkeypatch: pytest.MonkeyPatch,
    leg: str,
    expected_reason: str,
    expected_persisted: str | None,
) -> None:
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)

    headers: dict[str, str] = {}
    body = dict(_PRESIGN_BODY)
    if leg in ("header", "header_and_session", "header_with_session_store_but_no_session_id"):
        headers["X-Space-Id"] = "ws_42"
    if leg == "header_with_session_store_but_no_session_id":
        _attach_session(app, workspace_id="ws_from_session")  # session_store wired, but no session_id in body.
    if leg == "legacy_header":
        headers["X-Workspace-Id"] = "ws_legacy"
    if leg == "header_equal_to_session_space":
        headers["X-Space-Id"] = "ws_from_session"
    if leg in ("session", "header_and_session", "header_equal_to_session_space"):
        body["session_id"] = _attach_session(app, workspace_id="ws_from_session")
    if leg == "session_with_empty_workspace":
        body["session_id"] = _attach_session(app, workspace_id="")

    with client:
        r = client.post("/api/v1/files/presign", json=body, headers=headers)
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)

    # First pin the actual persisted value (prerequisite), then assert reason agrees.
    assert _persisted_workspace(app, r.json()["artifact_id"]) == expected_persisted, f"前置不成立：{leg} 腿的落值不是预期"
    assert attribution_metrics.events() == [{"route": "files_presign", "reason": expected_reason}]
    assert attribution_metrics.user_ids() == [None]


@pytest.mark.integration
def test_files_presign_metric_flag_off_control_writes_nothing(client: TestClient, app: FastAPI, attribution_metrics) -> None:
    """★ 对照：同一请求，flag 关着必须 0 行（drain 之后再读）。"""
    attribution_metrics.attach(app)
    with client:
        r = client.post("/api/v1/files/presign", json=_PRESIGN_BODY, headers={"X-Space-Id": "ws_42"})
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_files_presign_metric_rejected_requests_are_not_counted(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected requests and multipart negotiation must not count as admitted uploads."""
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    store: ArtifactStore = app.state.artifact_store

    async def _seed_pending() -> None:
        async with aiosqlite.connect(store.db_path) as db:
            for i in range(20):
                await db.execute(
                    "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                    "canvas_id, created_by, created_at, status, expires_at) "
                    "VALUES (?, ?, ?, ?, 'upload', ?, NULL, 'u_test', ?, 'pending_upload', ?)",
                    (
                        f"art_pen{i:09d}",
                        f"f{i}.bin",
                        "application/octet-stream",
                        100,
                        f"/tmp/{i}",
                        "2026-05-27T00:00:00+00:00",
                        "2026-05-27T23:00:00+00:00",
                    ),
                )
            await db.commit()

    with client:
        unsupported = client.post("/api/v1/files/presign", json={"filename": "x.exe", "mime": "application/x-msdownload", "size_bytes": 100})
        assert unsupported.status_code == 415, unsupported.text
        oversize = client.post("/api/v1/files/presign", json={"filename": "huge.bin", "mime": "application/octet-stream", "size_bytes": 3 * 1024**3})
        assert oversize.status_code == 413, oversize.text

        app.state.user_store.allowed = False
        rate_limited = client.post("/api/v1/files/presign", json=_PRESIGN_BODY)
        assert rate_limited.status_code == 429, rate_limited.text
        app.state.user_store.allowed = True

        backend = app.state.artifact_backend
        app.state.artifact_backend = _FakeBackend(presign_result=None)
        unavailable = client.post("/api/v1/files/presign", json=_PRESIGN_BODY)
        assert unavailable.status_code == 409, unavailable.text
        app.state.artifact_backend = backend

        asyncio.run(_seed_pending())
        capped = client.post("/api/v1/files/presign", json=_PRESIGN_BODY)
        assert capped.status_code == 429, capped.text
        assert capped.json()["detail"]["error"]["code"] == "file_presign_pending_limit"

        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.parametrize("size,expected", [(100, 409), (100 * 1024 * 1024, 409), (100 * 1024 * 1024 + 1, 413)])
def test_files_presign_negotiates_only_within_multipart_limit(client: TestClient, app: FastAPI, size, expected) -> None:
    app.state.artifact_backend = _FakeBackend(presign_result=None)
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "x.png", "mime": "image/png", "size_bytes": size},
    )
    assert r.status_code == expected
    assert r.json()["detail"]["error"]["code"] == ("file_multipart_required" if expected == 409 else "file_too_large")


def test_files_presign_429_on_rate_limit(client: TestClient, app: FastAPI) -> None:
    app.state.user_store.allowed = False
    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "x.png", "mime": "image/png", "size_bytes": 100},
    )
    assert r.status_code == 429


def test_files_presign_429_on_pending_cap(client: TestClient, app: FastAPI) -> None:
    """Insert 20 fake pending rows → 21st presign call returns 429."""
    store: ArtifactStore = app.state.artifact_store

    async def _seed_pending():
        async with aiosqlite.connect(store.db_path) as db:
            for i in range(20):
                await db.execute(
                    "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                    "canvas_id, created_by, created_at, status, expires_at) "
                    "VALUES (?, ?, ?, ?, 'upload', ?, NULL, 'u_test', ?, 'pending_upload', ?)",
                    (
                        f"art_pen{i:09d}",
                        f"f{i}.bin",
                        "application/octet-stream",
                        100,
                        f"/tmp/{i}",
                        "2026-05-27T00:00:00+00:00",
                        "2026-05-27T23:00:00+00:00",
                    ),
                )
            await db.commit()

    asyncio.run(_seed_pending())

    r = client.post(
        "/api/v1/files/presign",
        json={"filename": "x.png", "mime": "image/png", "size_bytes": 100},
    )
    assert r.status_code == 429
    assert r.json()["detail"]["error"]["code"] == "file_presign_pending_limit"


# ── confirm ───────────────────────────────────────────────────────


def _do_presign(client: TestClient, **overrides) -> dict:
    payload = {"filename": "screenshot.png", "mime": "image/png", "size_bytes": 12345}
    payload.update(overrides)
    r = client.post("/api/v1/files/presign", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_confirm_happy_path(client: TestClient, app: FastAPI) -> None:
    pres = _do_presign(client)
    aid = pres["file_id"]
    r = client.post(pres["confirm_url"], json={"uploaded_size": 12345})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == aid
    assert body["status"] == "ready"
    assert body["idempotent_replay"] is False
    assert body["size"] == 12345
    # DB row flipped to ready
    store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status, size FROM artifacts WHERE id=?", (aid,))
            row = await cur.fetchone()
        assert row["status"] == "ready"
        assert row["size"] == 12345

    asyncio.run(_check())


def test_confirm_404_on_unknown_file_id(client: TestClient) -> None:
    r = client.post(
        "/api/v1/files/presign/art_nonexistent_/confirm",
        json={},
    )
    assert r.status_code == 404


def test_confirm_cross_user_answers_like_missing(client: TestClient, app: FastAPI, monkeypatch) -> None:
    pres = _do_presign(client)
    pres["file_id"]
    # Swap user_id to a different identity for the confirm call
    from bowhead_gateway.routes import files_presign as fp

    monkeypatch.setattr(fp, "_user_id", lambda req: "u_attacker")
    r = client.post(pres["confirm_url"], json={})
    missing = client.post("/api/v1/files/presign/art_missing/confirm", json={})
    assert r.status_code == 404
    assert (r.status_code, r.content) == (missing.status_code, missing.content)


def test_confirm_idempotent_replay_on_already_ready(client: TestClient, app: FastAPI) -> None:
    pres = _do_presign(client)
    aid = pres["file_id"]
    # First confirm
    r1 = client.post(pres["confirm_url"], json={})
    assert r1.status_code == 200
    assert r1.json()["idempotent_replay"] is False
    # Second confirm with same token
    r2 = client.post(pres["confirm_url"], json={})
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["file_id"] == aid


def test_confirm_404_when_object_missing_from_backend(client: TestClient, app: FastAPI) -> None:
    """head() returns None → 404, status rolled back to pending_upload for retry."""
    pres = _do_presign(client)
    aid = pres["file_id"]
    app.state.artifact_backend.head_result = None
    r = client.post(pres["confirm_url"], json={})
    assert r.status_code == 404
    # Status rolled back
    store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status FROM artifacts WHERE id=?", (aid,))
            row = await cur.fetchone()
        assert row["status"] == "pending_upload", "must roll back so retry works"

    asyncio.run(_check())


def test_confirm_415_on_mime_mismatch(client: TestClient, app: FastAPI) -> None:
    """object store reports a different MIME than presign claimed → 415."""
    pres = _do_presign(client, mime="image/png")
    app.state.artifact_backend.head_result = ObjectHead(
        size=12345,
        etag='"x"',
        mime="text/html",  # mime swap attempt
    )
    r = client.post(pres["confirm_url"], json={})
    assert r.status_code == 415
    assert r.json()["detail"]["error"]["code"] == "file_type_unsupported"


def test_confirm_410_on_expired_with_no_object(client: TestClient, app: FastAPI) -> None:
    """Row expires before browser uploaded → 410."""
    pres = _do_presign(client)
    aid = pres["file_id"]
    # Force-expire the row
    store: ArtifactStore = app.state.artifact_store
    past = (datetime.now(tz=UTC) - timedelta(seconds=10)).isoformat(timespec="seconds")
    app.state.artifact_backend.head_result = None  # no object yet

    async def _expire():
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE artifacts SET expires_at=? WHERE id=?", (past, aid))
            await db.commit()

    asyncio.run(_expire())

    r = client.post(pres["confirm_url"], json={})
    assert r.status_code == 410


def test_presign_oversize_returns_413_per_spec(client: TestClient) -> None:
    """Phase 1.4a review P1-2: tech-design §5.5 contracts oversize → 413.
    Previously Pydantic's `le=` constraint returned 422 (generic validation).
    """
    r = client.post(
        "/api/v1/files/presign",
        json={
            "filename": "huge.bin",
            "mime": "application/octet-stream",
            "size_bytes": 3 * 1024**3,  # 3 GB, above 2 GB cap
        },
    )
    assert r.status_code == 413
    assert r.json()["detail"]["error"]["code"] == "file_too_large"


def test_presign_filename_sanitized_in_db(client: TestClient, app: FastAPI) -> None:
    """Phase 1.4a review P1-5: control chars / path separators in filename
    must be stripped before INSERT into artifacts.filename to avoid log
    injection and align with the backend path-sanitization.
    """
    r = client.post(
        "/api/v1/files/presign",
        json={
            "filename": "evil\nmulti\x00line.png",
            "mime": "image/png",
            "size_bytes": 1000,
        },
    )
    assert r.status_code == 200, r.text
    aid = r.json()["file_id"]
    store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT filename FROM artifacts WHERE id=?", (aid,))
            row = await cur.fetchone()
        # _safe_filename replaces [/\\\x00-\x1f\x7f]+ with _
        assert row is not None
        assert "\n" not in row["filename"]
        assert "\x00" not in row["filename"]

    asyncio.run(_check())


def test_presign_accepts_uppercase_hex_checksum(client: TestClient) -> None:
    """Phase 1.4a review P1-6: SHA-256 hex is conventionally case-insensitive.
    Previously the regex `^[a-f0-9]{64}$` rejected uppercase.
    """
    upper = "A" * 64
    r = client.post(
        "/api/v1/files/presign",
        json={
            "filename": "x.png",
            "mime": "image/png",
            "size_bytes": 1000,
            "checksum_sha256": upper,
        },
    )
    assert r.status_code == 200, r.text


def test_confirm_409_when_status_is_confirming(client: TestClient, app: FastAPI) -> None:
    """Phase 1.4a review P1-4: row stuck in 'confirming' (e.g. concurrent
    in-flight) → 409 with retry hint, NOT idempotent_replay, NOT 404.
    """
    pres = _do_presign(client)
    aid = pres["file_id"]
    store: ArtifactStore = app.state.artifact_store

    async def _force_confirming():
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute("UPDATE artifacts SET status='confirming' WHERE id=?", (aid,))
            await db.commit()

    asyncio.run(_force_confirming())

    r = client.post(pres["confirm_url"], json={})
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "file_confirm_in_progress"


def test_confirm_503_on_backend_head_error_rolls_back_cas(client: TestClient, app: FastAPI) -> None:
    """Phase 1.4a review P0-1: backend.head() raise must NOT leave row stuck
    in 'confirming'. Roll back to 'pending_upload' + return 503 (retryable).
    """
    pres = _do_presign(client)
    aid = pres["file_id"]
    app.state.artifact_backend.head_raises = RuntimeError("simulated backend hiccup")
    r = client.post(pres["confirm_url"], json={})
    assert r.status_code == 503

    # Row rolled back to pending_upload (NOT stuck in 'confirming')
    store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status FROM artifacts WHERE id=?", (aid,))
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "pending_upload", "must roll back so retry can succeed"

    asyncio.run(_check())


def test_confirm_pins_session_when_provided(client: TestClient, app: FastAPI) -> None:
    """If session_id supplied, confirm INSERTs into session_attachments."""
    pres = _do_presign(client)
    aid = pres["file_id"]
    store: ArtifactStore = app.state.artifact_store
    session_id = _attach_session(app, workspace_id="ws_42")
    r = client.post(pres["confirm_url"], json={"session_id": session_id, "uploaded_size": 12345})
    assert r.status_code == 200

    async def _check():
        async with aiosqlite.connect(store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT session_id, artifact_id FROM session_attachments WHERE artifact_id=?",
                (aid,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["session_id"] == session_id

    asyncio.run(_check())


def test_presign_rejects_session_when_ownership_cannot_be_checked(client: TestClient) -> None:
    response = client.post(
        "/api/v1/files/presign",
        headers={"X-Space-Id": "ws_42"},
        json={
            "filename": "image.png",
            "mime": "image/png",
            "size_bytes": 10,
            "session_id": "s_unknown",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "file_upload_session_denied"
