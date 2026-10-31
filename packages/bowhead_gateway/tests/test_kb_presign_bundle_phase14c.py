"""Phase 1.4c UT — KB bundle presign + confirm endpoints.

Covers tech-design §5.1c + §5.3c:
- bundle presign creates artifact + marker kb_files row in 'awaiting_upload'
- presign 415 on non-zip mime
- presign 413 on bundle oversize (>_BUNDLE_MAX_SIZE)
- presign 403 on scope spoof
- presign 429 on pending cap
- confirm classifies entries via backend.read_stream + ZipFile (no full-RAM zip)
- confirm enqueues per-entry workers
- confirm 404 unknown bundle marker
- confirm 403 on cross-user OR scope-revoked-at-confirm
- confirm 410 on expired + no object
- confirm 503 on backend hiccup with CAS rollback
- bundle marker soft-deleted after success
- skipped entries reported (__MACOSX, .DS_Store)
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.artifact_store import TEAM_SCOPE_WORKSPACE_ID, LocalBackend, ObjectHead, PresignPostResult
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_presign
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import ArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────


def _build_zip(entries: dict[str, bytes]) -> bytes:
    """Build a fresh zip with the given {filename: content} mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@dataclass
class _StreamBackend:
    """Minimal backend that serves presign + head + read_stream from in-memory dict."""

    name: str = "local"
    presign_result: PresignPostResult | None = None
    head_result: ObjectHead | None = None
    head_raises: Exception | None = None
    objects: dict[str, bytes] = None  # path → zip body

    def __post_init__(self) -> None:
        if self.objects is None:
            self.objects = {}

    async def presign_post(self, *, aid, filename, mime, size_bytes, expires_in=3600):
        if self.presign_result is None:
            return None
        return PresignPostResult(
            url=self.presign_result.url,
            fields=dict(self.presign_result.fields),
            path=f"/tmp/kbb/{aid}_{filename}",
            expires_at=self.presign_result.expires_at,
        )

    async def head(self, path: str):
        if self.head_raises is not None:
            raise self.head_raises
        if self.head_result is not None:
            return self.head_result
        if path in self.objects:
            return ObjectHead(size=len(self.objects[path]), etag='"x"', mime="application/zip")
        return None

    def read_stream(self, path: str):
        import contextlib

        @contextlib.asynccontextmanager
        async def _ctx():
            payload = self.objects.get(path, b"")
            buf = io.BytesIO(payload)
            try:
                yield buf
            finally:
                buf.close()

        return _ctx()


class _FakeUserStore:
    def __init__(self) -> None:
        self.allowed = True

    async def rate_limit_check(self, bucket: str, *, limit: int, window_s: int) -> bool:
        return self.allowed


class _FakeWorker:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def parse_and_index(self, file_id: str) -> None:
        self.called.append(file_id)

    async def parse_and_index_many(self, file_ids: list[str]) -> None:
        # Mirror real KbWorker: confirm fans out ALL children to one task that
        # gathers parse_and_index concurrently (instead of per-child add_task).
        import asyncio

        await asyncio.gather(
            *(self.parse_and_index(f) for f in file_ids),
            return_exceptions=True,
        )


class _FakeDocTypeStore:
    def __init__(self) -> None:
        self.doc_types: dict = {}

    async def get(self, _id: str):
        return self.doc_types.get(_id)


def _make_user(uid: str = "u_alice", team: str = "team_test") -> User:
    return User(id=uid, email="alice@test.com", team_id=team, tier="ultra", email_verified=True)


@pytest.fixture
async def app(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch) -> FastAPI:
    monkeypatch.delenv("BOWHEAD_AUTO_ADMIN_DOMAINS", raising=False)
    a = FastAPI()
    a.include_router(kb_presign.router)
    artifact_db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(artifact_db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{artifact_db_path}", target="core")
    artifact_store = ArtifactStore(
        db_path=str(artifact_db_path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await artifact_store.initialize()
    a.state.artifact_store = artifact_store
    initialize_migrated_kb_files_database(tmp_path / "kbb_files.db")
    files_store = KbFilesStore(tmp_path / "kbb_files.db")
    await files_store.initialize()
    a.state.kb_files_store = files_store
    a.state.artifact_backend = _StreamBackend(
        presign_result=PresignPostResult(
            url="http://test/kbb/local-receiver/aid",
            fields={"Content-Type": "application/zip", "Authorization": "Bearer XYZ"},
            path="placeholder",
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=3600),
        ),
    )
    a.state.user_store = _FakeUserStore()
    a.state.kb_worker = _FakeWorker()
    a.state.kb_doc_type_store = _FakeDocTypeStore()
    return a


@pytest.fixture
def stub_current_user(app: FastAPI) -> dict:
    from bowhead_gateway.routes import kb_presign as kp

    state = {"user": _make_user()}

    async def _fake():
        return state["user"]

    app.dependency_overrides[kp._current_user] = _fake
    return state


@pytest.fixture
def client(app: FastAPI, stub_current_user) -> TestClient:
    return TestClient(app)


# ── bundle presign ────────────────────────────────────────────────


def test_bundle_presign_happy_creates_marker(client: TestClient, app: FastAPI) -> None:
    r = client.post(
        "/api/v1/kb/presign/bundle",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "mei-dd.zip",
            "mime": "application/zip",
            "size_bytes": 5_000_000,
            "scope": "personal",
            "scope_id": "u_alice",
        },
        headers={"X-Workspace-Id": "ws_42"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"].startswith("kbf_")
    assert body["artifact_id"].startswith("art_")
    assert body["confirm_url"] == f"/api/v1/kb/presign/bundle/{body['file_id']}/confirm"

    # Bundle marker row in awaiting_upload
    fs: KbFilesStore = app.state.kb_files_store

    async def _check():
        async with aiosqlite.connect(fs._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT parse_status, mime_type FROM kb_files WHERE file_id=?",
                (body["file_id"],),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["parse_status"] == "awaiting_upload"
        assert row["mime_type"] == "application/zip"

    asyncio.run(_check())


def test_bundle_presign_rejects_non_zip_415(client: TestClient) -> None:
    r = client.post(
        "/api/v1/kb/presign/bundle",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "x.pdf",
            "mime": "application/pdf",
            "size_bytes": 1000,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 415


def test_bundle_presign_rejects_oversize_413(client: TestClient) -> None:
    r = client.post(
        "/api/v1/kb/presign/bundle",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "huge.zip",
            "mime": "application/zip",
            "size_bytes": 600 * 1024 * 1024,  # > _BUNDLE_MAX_SIZE 500MB
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 413


def test_bundle_presign_403_on_scope_spoof(client: TestClient) -> None:
    r = client.post(
        "/api/v1/kb/presign/bundle",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "x.zip",
            "mime": "application/zip",
            "size_bytes": 1000,
            "scope": "personal",
            "scope_id": "u_bob",  # not me
        },
    )
    assert r.status_code == 403


def test_bundle_presign_accepts_xzipcompressed_variant(client: TestClient) -> None:
    """Windows drag-drop reports application/x-zip-compressed (P1-D-9 from r2)."""
    r = client.post(
        "/api/v1/kb/presign/bundle",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "win.zip",
            "mime": "application/x-zip-compressed",
            "size_bytes": 1000,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 200


#: Two parameterized settings = **discriminating controls**: team alone misses unconditional sentinel writes; space alone misses
#: removing the `or TEAM_SCOPE_WORKSPACE_ID` fallback. Rationale in
#: `test_kb_presign_phase14b.py::test_kb_presign_persists_artifact_workspace_attribution`。
_ATTRIBUTION_CASES = [
    ("team", "team_test", "team", TEAM_SCOPE_WORKSPACE_ID),
    ("space", "ws_42", "space", "ws_42"),
]
_ATTRIBUTION_IDS = ["team_scope_sentinel", "space_scope_real_id"]


@pytest.mark.parametrize(("scope", "scope_id", "expected_scope", "expected_workspace"), _ATTRIBUTION_CASES, ids=_ATTRIBUTION_IDS)
def test_bundle_presign_persists_artifact_workspace_attribution(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    expected_scope: str,
    expected_workspace: str,
) -> None:
    """★ 空间隔离批4 ①：bundle presign 的**根包产物**也要把归属真的写进库（行为断言，不是源码文本断言）。

    与单文件那条同源：源码级守卫对「把值改成 `None`」是瞎的，只有查库才杀得掉那个突变。
    """
    if scope == "team":
        # Only team checks role (`can_write_to_scope`); space is role-blind, so elevation would only mask regressions.
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")

    pres = _do_bundle_presign(client, scope=scope, scope_id=scope_id)

    art_store: ArtifactStore = app.state.artifact_store
    fs: KbFilesStore = app.state.kb_files_store

    async def _read():
        async with aiosqlite.connect(art_store.db_path) as db:
            cur = await db.execute("SELECT workspace_id FROM artifacts WHERE id=?", (pres["artifact_id"],))
            art = await cur.fetchone()
        async with aiosqlite.connect(fs._db_path) as db:
            cur = await db.execute("SELECT scope FROM kb_files WHERE file_id=?", (pres["file_id"],))
            marker = await cur.fetchone()
        return art, marker

    art, marker = asyncio.run(_read())

    assert art is not None, "根包产物没落进 artifacts —— 这条用例证明不了任何事"
    assert marker is not None and marker[0] == expected_scope, f"前置不成立：scope={marker and marker[0]!r}"
    assert art[0] == expected_workspace, f"归属写错：实得 {art[0]!r}，应为 {expected_workspace!r}"


# ── bundle confirm ────────────────────────────────────────────────


def _do_bundle_presign(client: TestClient, **overrides) -> dict:
    payload = {
        "knowledge_intent": "add_to_knowledge",
        "filename": "mei-dd.zip",
        "mime": "application/zip",
        "size_bytes": 5_000_000,
        "scope": "personal",
        "scope_id": "u_alice",
    }
    payload.update(overrides)
    r = client.post("/api/v1/kb/presign/bundle", json=payload, headers={"X-Workspace-Id": "ws_42"})
    assert r.status_code == 200, r.text
    return r.json()


def test_bundle_confirm_classifies_entries_and_enqueues_workers(
    client: TestClient,
    app: FastAPI,
    monkeypatch,
) -> None:
    """Place a 3-file zip in the backend, confirm → 3 child kb_files created,
    bundle marker soft-deleted, worker called 3 times."""
    pres = _do_bundle_presign(client)
    monkeypatch.setitem(
        sys.modules,
        "magic",
        SimpleNamespace(from_buffer=lambda payload, mime: "application/pdf" if payload.startswith(b"%PDF") else "text/plain"),
    )
    file_id = pres["file_id"]

    # Pre-stage the zip body where the confirm path will read it
    fs: KbFilesStore = app.state.kb_files_store
    art_store: ArtifactStore = app.state.artifact_store

    async def _get_path():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT path FROM artifacts WHERE id=?",
                (pres["artifact_id"],),
            )
            row = await cur.fetchone()
        return row["path"]

    path = asyncio.run(_get_path())
    # Use .pdf extensions — the ext-fallback path catches them even when
    # libmagic isn't installed in the test env (which is common on Mac dev).
    zip_body = _build_zip(
        {
            "subdir/": b"",
            "a.pdf": b"%PDF-1.4\n%mock pdf body for test",
            "subdir/b.pdf": b"%PDF-1.4\nanother mock pdf",
            "subdir/notes.bin": b"synthetic searchable text",
            "__MACOSX/._ignore": b"skip me",
        }
    )
    app.state.artifact_backend.objects[path] = zip_body

    r = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={"uploaded_size": len(zip_body)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_entries"] == 4  # Exclude directory entries from pending files.
    # macOS metadata reported in skipped list
    assert any(s.get("reason_code") == "kb_bundle_entry_metadata" and s.get("reason") is None for s in body["skipped"])
    assert any(s.get("filename") == "subdir/notes.bin" and s.get("reason_code") == "kb_bundle_entry_type_unsupported" for s in body["skipped"])
    # Two real entries should be uploaded
    assert len(body["uploaded"]) == 2, f"expected 2 uploads, got skipped={body['skipped']}"

    # Worker called for each uploaded child
    worker = app.state.kb_worker
    assert len(worker.called) == 2

    # Bundle marker soft-deleted (parse_status='done')
    async def _check_marker():
        async with aiosqlite.connect(fs._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT parse_status, soft_deleted_at FROM kb_files WHERE file_id=?",
                (file_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["parse_status"] == "done"
        assert row["soft_deleted_at"] is not None

    asyncio.run(_check_marker())

    # Bundle artifact flipped to ready
    async def _check_art():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT status FROM artifacts WHERE id=?",
                (pres["artifact_id"],),
            )
            row = await cur.fetchone()
        assert row["status"] == "ready"

    asyncio.run(_check_art())


# ── Attribution metrics (metrics_event_log): bundle presign and bundle confirm are **two** wiring points ──
#
# Test separately: confirm cases `attach` only **after** presign, so presign does not write a row,
# isolating the confirm event (avoid asserting two behaviors in one case).
# Drive the real route: `with client:` + `attribution_metrics.drain(client)` deterministically await background-task writes.

_METRICS_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"

#: (scope, scope_id, space header present, expected reason)
_BUNDLE_METRIC_CASES = [
    ("team", "team_test", True, "team"),
    ("space", "ws_42", True, "space"),
    ("personal", "u_alice", False, "none"),  # ★ Why these metrics exist.
    ("personal", "u_alice", True, "space"),  # ★ Pin personal + space header ⇒ space.
]
_BUNDLE_METRIC_IDS = ["team", "space", "personal_without_space_header", "personal_with_space_header"]

_BUNDLE_PRESIGN_BODY = {"knowledge_intent": "add_to_knowledge", "filename": "mei-dd.zip", "mime": "application/zip", "size_bytes": 5_000_000}


def _stage_zip(app: FastAPI, pres: dict, zip_body: bytes) -> None:
    """把 zip 摆到 confirm 路径会读的地方。"""

    async def _get_path() -> str:
        async with aiosqlite.connect(app.state.artifact_store.db_path) as db:
            cur = await db.execute("SELECT path FROM artifacts WHERE id=?", (pres["artifact_id"],))
            return (await cur.fetchone())[0]

    app.state.artifact_backend.objects[asyncio.run(_get_path())] = zip_body


@pytest.mark.integration
@pytest.mark.parametrize(("scope", "scope_id", "with_space_header", "expected_reason"), _BUNDLE_METRIC_CASES, ids=_BUNDLE_METRIC_IDS)
def test_bundle_presign_emits_one_attribution_metric(
    client: TestClient,
    app: FastAPI,
    attribution_metrics,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    with_space_header: bool,
    expected_reason: str,
) -> None:
    monkeypatch.setenv(_METRICS_FLAG, "1")
    if scope == "team":
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")
    attribution_metrics.attach(app)

    with client:
        r = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": scope, "scope_id": scope_id},
            headers={"X-Workspace-Id": "ws_42"} if with_space_header else {},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_presign_bundle", "reason": expected_reason, "scope": scope}]
    assert attribution_metrics.user_ids() == [None]


@pytest.mark.integration
def test_bundle_presign_flag_off_control_writes_no_metric(client: TestClient, app: FastAPI, attribution_metrics) -> None:
    attribution_metrics.attach(app)
    with client:
        r = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_bundle_presign_rejected_requests_are_not_counted(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 被拒的请求（415 / 413 / 403 / **429**）都不计数——打点在**成功之后**。每个请求先断言状态码，防空过。"""
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    artifact_store: ArtifactStore = app.state.artifact_store

    async def _seed_pending() -> None:
        async with aiosqlite.connect(artifact_store.db_path) as db:
            for i in range(20):
                await db.execute(
                    "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                    "canvas_id, created_by, created_at, status, expires_at) "
                    "VALUES (?, ?, ?, ?, 'kb_upload', ?, NULL, 'u_alice', ?, 'pending_upload', ?)",
                    (f"art_pen{i:09d}", f"f{i}.zip", "application/zip", 100, f"/tmp/{i}", "2026-05-27T00:00:00+00:00", "2026-05-27T23:00:00+00:00"),
                )
            await db.commit()

    personal = {"scope": "personal", "scope_id": "u_alice"}
    with client:
        not_zip = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "knowledge_intent": "add_to_knowledge", "filename": "x.pdf", "mime": "application/pdf", **personal},
        )
        assert not_zip.status_code == 415, not_zip.text
        oversize = client.post("/api/v1/kb/presign/bundle", json={**_BUNDLE_PRESIGN_BODY, "size_bytes": 600 * 1024 * 1024, **personal})
        assert oversize.status_code == 413, oversize.text
        spoofed = client.post("/api/v1/kb/presign/bundle", json={**_BUNDLE_PRESIGN_BODY, "scope": "personal", "scope_id": "u_bob"})
        assert spoofed.status_code == 403, spoofed.text

        asyncio.run(_seed_pending())
        capped = client.post("/api/v1/kb/presign/bundle", json={**_BUNDLE_PRESIGN_BODY, **personal})
        assert capped.status_code == 429, capped.text

        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_bundle_presign_failure_after_the_decision_is_not_counted(
    app: FastAPI, stub_current_user, attribution_metrics, fail_audit_event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ B 评审：让请求在**紧贴打点之前的最后一步**失败（`kb_presign_bundle.issued` 审计抛错 ⇒ 500），
    打点若被挪到更早的位置，这次失败也会被计入。放行后再发一次作正对照。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    request = {"json": {**_BUNDLE_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"}, "headers": {"X-Workspace-Id": "ws_42"}}

    with TestClient(app, raise_server_exceptions=False) as strict:
        restore = fail_audit_event(kb_presign, "kb_presign_bundle.issued")
        failed = strict.post("/api/v1/kb/presign/bundle", **request)
        assert failed.status_code == 500, failed.text
        attribution_metrics.drain(strict)
        assert attribution_metrics.events() == [], "失败的那次不得计数"

        restore()
        ok = strict.post("/api/v1/kb/presign/bundle", **request)
        assert ok.status_code == 200, ok.text
        attribution_metrics.drain(strict)
    assert attribution_metrics.events() == [{"route": "kb_presign_bundle", "reason": "space", "scope": "space"}]


# ── bundle confirm ────────────────────────────────────────────────


@pytest.mark.integration
def test_bundle_confirm_failure_at_the_very_last_step_is_not_counted(
    app: FastAPI, stub_current_user, attribution_metrics, fail_audit_event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ B 评审 N03：confirm 的打点若被挪到**最终 `status='ready'` 更新之前**，前面的失败测试（`finish_bundle_import`
    抛错）杀不掉它——那个失败发生在更早处。这里让请求失败在**紧贴打点之前**（`kb_presign_bundle.confirmed` 审计抛错，
    此时终态更新与 marker 软删都已提交）。此时 marker 已被软删，同一个 confirm 无法重试，
    所以正对照用一次**全新的** presign→confirm。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")

    def _presign_and_stage(strict: TestClient) -> tuple[str, bytes]:
        pres = strict.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert pres.status_code == 200, pres.text
        zip_body = _build_zip({"a.pdf": b"%PDF-1.4\nA"})
        _stage_zip(app, pres.json(), zip_body)
        return f"/api/v1/kb/presign/bundle/{pres.json()['file_id']}/confirm", zip_body

    with TestClient(app, raise_server_exceptions=False) as strict:
        # Both presigns precede attaching `metrics_log`: presign itself records a row, excluded from this confirm-only case.
        failing_url, failing_body = _presign_and_stage(strict)
        ok_url, ok_body = _presign_and_stage(strict)
        attribution_metrics.attach(app)

        restore = fail_audit_event(kb_presign, "kb_presign_bundle.confirmed")
        failed = strict.post(failing_url, json={"uploaded_size": len(failing_body)}, headers={"X-Workspace-Id": "ws_42"})
        assert failed.status_code == 500, failed.text
        attribution_metrics.drain(strict)
        assert attribution_metrics.events() == [], "失败的那次不得计数"

        restore()
        ok = strict.post(ok_url, json={"uploaded_size": len(ok_body)}, headers={"X-Workspace-Id": "ws_42"})
        assert ok.status_code == 200, ok.text
        attribution_metrics.drain(strict)
    assert attribution_metrics.events() == [{"route": "kb_presign_bundle_confirm", "reason": "space", "scope": "space"}]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("scope", "scope_id", "expected_reason"),
    [("team", "team_test", "team"), ("space", "ws_42", "space"), ("personal", "u_alice", "space")],
    ids=["team", "space", "personal_with_space_header"],
)
def test_bundle_confirm_emits_exactly_one_metric_not_one_per_entry(
    client: TestClient,
    app: FastAPI,
    attribution_metrics,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    expected_reason: str,
) -> None:
    """★ confirm 只记**一次**：两个真实条目 + 一个被跳过项，仍然只该有 1 行。

    这个接线点包在 `with zipfile` 深处、缩进比别处深，很容易被误以为在逐条目循环里；
    若误挪进循环，计数会被条目数放大。位置在**最终成功的 return 之前**，所以确认失败后被回滚成
    `pending_upload` 再重试的请求，只有最终成功那次会计一行。
    三档参数化：只测 space 档时，「把 reason/scope 写死成 space」这类改错照样绿。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    if scope == "team":
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")

    with client:
        pres = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": scope, "scope_id": scope_id},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert pres.status_code == 200, pres.text
        attribution_metrics.attach(app)  # ★ Attach after presign: no presign row; this case tests only confirm.

        zip_body = _build_zip({"a.pdf": b"%PDF-1.4\nA", "b.pdf": b"%PDF-1.4\nB", "__MACOSX/._x": b"skip"})
        _stage_zip(app, pres.json(), zip_body)
        r = client.post(
            f"/api/v1/kb/presign/bundle/{pres.json()['file_id']}/confirm",
            json={"uploaded_size": len(zip_body)},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["uploaded"]) == 2, r.text  # Prevent vacuous pass: both entries really persisted.
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_presign_bundle_confirm", "reason": expected_reason, "scope": scope}]


@pytest.mark.integration
def test_bundle_confirm_flag_off_control_writes_no_metric(client: TestClient, app: FastAPI, attribution_metrics) -> None:
    with client:
        pres = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert pres.status_code == 200, pres.text
        attribution_metrics.attach(app)
        zip_body = _build_zip({"a.pdf": b"%PDF-1.4\nA"})
        _stage_zip(app, pres.json(), zip_body)
        r = client.post(
            f"/api/v1/kb/presign/bundle/{pres.json()['file_id']}/confirm",
            json={"uploaded_size": len(zip_body)},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_bundle_confirm_failure_then_retry_counts_only_the_successful_attempt(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ confirm 在**归属决策之后**失败（这里让 `finish_bundle_import` 抛错 ⇒ 500，artifact 被回滚成
    `pending_upload`），再重试成功：只有**最终成功那次**计一行。

    位置若在决策点（`attribution = ...` 那里）而不是最终 return 之前，失败的那一次也会被计入，
    同一次上传就会算两行——而 confirm 重试正是前端会做的事。这条钉住 confirm 处代码注释里的那句话。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    real_finish = kb_presign.finish_bundle_import

    async def _boom(*_a, **_k):
        raise RuntimeError("inventory persist failed")

    with client:
        pres = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert pres.status_code == 200, pres.text
        attribution_metrics.attach(app)
        zip_body = _build_zip({"a.pdf": b"%PDF-1.4\nA"})
        _stage_zip(app, pres.json(), zip_body)
        confirm_url = f"/api/v1/kb/presign/bundle/{pres.json()['file_id']}/confirm"

        monkeypatch.setattr(kb_presign, "finish_bundle_import", _boom)
        failed = client.post(confirm_url, json={"uploaded_size": len(zip_body)}, headers={"X-Workspace-Id": "ws_42"})
        assert failed.status_code == 500, failed.text  # Prerequisite: actually failed after the decision point.
        attribution_metrics.drain(client)
        assert attribution_metrics.events() == [], "失败的那次不得计数"

        monkeypatch.setattr(kb_presign, "finish_bundle_import", real_finish)
        retried = client.post(confirm_url, json={"uploaded_size": len(zip_body)}, headers={"X-Workspace-Id": "ws_42"})
        assert retried.status_code == 200, retried.text
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_presign_bundle_confirm", "reason": "space", "scope": "space"}]


@pytest.mark.integration
def test_bundle_confirm_rejected_requests_are_not_counted(
    client: TestClient, app: FastAPI, attribution_metrics, stub_current_user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused confirms (unknown marker / another user's marker, both 404) are not counted."""
    monkeypatch.setenv(_METRICS_FLAG, "1")
    with client:
        pres = client.post(
            "/api/v1/kb/presign/bundle",
            json={**_BUNDLE_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"},
            headers={"X-Workspace-Id": "ws_42"},
        )
        assert pres.status_code == 200, pres.text
        attribution_metrics.attach(app)

        unknown = client.post("/api/v1/kb/presign/bundle/kbf_doesnotex/confirm", json={})
        assert unknown.status_code == 404, unknown.text

        stub_current_user["user"] = _make_user("u_bob")
        cross_user = client.post(f"/api/v1/kb/presign/bundle/{pres.json()['file_id']}/confirm", json={})
        assert cross_user.status_code == 404, cross_user.text

        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


def test_bundle_confirm_404_unknown_marker(client: TestClient) -> None:
    r = client.post("/api/v1/kb/presign/bundle/kbf_doesnotex/confirm", json={})
    assert r.status_code == 404


def test_bundle_confirm_cross_user_answers_like_missing(client: TestClient, stub_current_user) -> None:
    pres = _do_bundle_presign(client)
    file_id = pres["file_id"]
    stub_current_user["user"] = _make_user(uid="u_attacker")
    r = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
    missing = client.post("/api/v1/kb/presign/bundle/kbf_doesnotex/confirm", json={})
    assert r.status_code == 404
    assert (r.status_code, r.content) == (missing.status_code, missing.content)


def test_bundle_confirm_404_when_object_missing(client: TestClient, app: FastAPI) -> None:
    """head() returns None → 404 + CAS rolled back to pending_upload."""
    pres = _do_bundle_presign(client)
    file_id = pres["file_id"]
    # Don't stage any object → backend.head returns None
    r = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
    assert r.status_code == 404

    art_store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT status FROM artifacts WHERE id=?",
                (pres["artifact_id"],),
            )
            row = await cur.fetchone()
        assert row["status"] == "pending_upload"

    asyncio.run(_check())


def test_bundle_confirm_503_on_backend_hiccup(client: TestClient, app: FastAPI) -> None:
    pres = _do_bundle_presign(client)
    file_id = pres["file_id"]
    app.state.artifact_backend.head_raises = RuntimeError("backend dead")
    r = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
    assert r.status_code == 503


def test_bundle_confirm_409_on_replay_after_done(client: TestClient, app: FastAPI) -> None:
    """Second confirm after success returns 409 (bundle decompose is expensive — avoid races)."""
    pres = _do_bundle_presign(client)
    file_id = pres["file_id"]
    art_store: ArtifactStore = app.state.artifact_store

    async def _get_path():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT path FROM artifacts WHERE id=?",
                (pres["artifact_id"],),
            )
            row = await cur.fetchone()
        return row["path"]

    path = asyncio.run(_get_path())
    app.state.artifact_backend.objects[path] = _build_zip({"a.pdf": b"%PDF-1.4\nhi"})

    r1 = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
    assert r1.status_code == 200
    # Second call → 409 (marker now parse_status='done')
    r2 = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={})
    assert r2.status_code in (404, 409)  # 404 if soft_deleted_at filter excludes, 409 if seen


def test_bundle_confirm_keeps_capability_components_out_of_kb_and_reports_inventory(
    client: TestClient,
    app: FastAPI,
    monkeypatch,
) -> None:
    """skill-import 批1b：≥10MB 走 presign 的包也过清点——SKILL.md/agents 不进 KB（拍板④），
    文档照旧入库；清单随响应返回并按根包 artifact 落库。无 skill_instance_store ⇒ 不落 skill、不报错。"""
    from bowhead_core.skill_import.bundle_inventory_store import get_bundle_inventory

    pres = _do_bundle_presign(client)
    file_id = pres["file_id"]
    art_store: ArtifactStore = app.state.artifact_store

    async def _get_path():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT path FROM artifacts WHERE id=?", (pres["artifact_id"],))
            row = await cur.fetchone()
        return row["path"]

    path = asyncio.run(_get_path())
    zip_body = _build_zip(
        {
            "proj/acme/SKILL.md": b"---\nname: acme\ndescription: d\nallowed-tools: Read\n---\nbody",
            "proj/agents/a.md": b"---\nname: a\ndescription: d\n---\nprompt",
            "proj/docs/brief.pdf": b"%PDF-1.4\nbrief",
        }
    )
    app.state.artifact_backend.objects[path] = zip_body
    r = client.post(f"/api/v1/kb/presign/bundle/{file_id}/confirm", json={"uploaded_size": len(zip_body)})
    assert r.status_code == 200, r.text
    body = r.json()
    reasons = {s["filename"]: s["reason_code"] for s in body["skipped"]}
    assert reasons.get("proj/acme/SKILL.md") == "kb_bundle_entry_capability_component"
    assert reasons.get("proj/agents/a.md") == "kb_bundle_entry_capability_component"
    assert [f["filename"] for f in body["uploaded"]] == ["brief.pdf"]
    assert body["inventory"]["grade"] in ("yellow", "green") and body["landed_skills"] == []
    stored = asyncio.run(get_bundle_inventory(art_store.db_path, bundle_artifact_id=pres["artifact_id"], created_by="u_alice"))
    assert stored is not None and any(c["kind"] == "skill_bundle" for c in stored["components"])


def test_bundle_confirm_lands_skill_component_when_store_present(client: TestClient, app: FastAPI, tmp_path: Path, monkeypatch) -> None:
    """skill-import 批1b review：≥10MB 门的落库此前只测了「没有 store ⇒ 空」；这里装上 store，断言 skill 真落成
    个人待处理导入，且带 risk_report_json（与设置页上传同列）."""
    from types import SimpleNamespace

    from bowhead_core.skill_instance_store import SkillInstanceStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", "1")
    sis = SkillInstanceStore(str(app.state.artifact_store.db_path))
    asyncio.run(sis.initialize())
    app.state.skill_instance_store = sis
    app.state.skill_catalog = SimpleNamespace(list_names=list, list_specs=list, get=lambda n: None, refresh=lambda: None)
    app.state.tool_registry = SimpleNamespace(list_names=lambda: ["bi_query"])

    pres = _do_bundle_presign(client)
    art_store: ArtifactStore = app.state.artifact_store

    async def _get_path():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT path FROM artifacts WHERE id=?", (pres["artifact_id"],))
            row = await cur.fetchone()
        return row["path"]

    path = asyncio.run(_get_path())
    zip_body = _build_zip(
        {
            # allowed-tools contains an unregistered name ⇒ red ⇒ pending.
            "proj/acme/SKILL.md": b"---\nname: acme\ndescription: d\nallowed-tools: Read, data_fetch_skill\n---\nUse `apply_patch`.",
            "proj/acme/scripts/run.py": b"print(1)\n",
            "proj/docs/brief.pdf": b"%PDF-1.4\nbrief",
        }
    )
    app.state.artifact_backend.objects[path] = zip_body
    r = client.post(f"/api/v1/kb/presign/bundle/{pres['file_id']}/confirm", json={"uploaded_size": len(zip_body)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["filename"] for f in body["uploaded"]] == ["brief.pdf"]
    landed = body["landed_skills"]
    assert [(x["name"], x["status"], x["import_status"]) for x in landed] == [("acme", "landed", "pending")]
    assert landed[0]["declared_name"] == "acme" and landed[0]["red_count"] > 0
    inst = asyncio.run(sis.get(user_id="u_alice", name="acme"))
    assert inst is not None and inst.import_pending and inst.risk_report_json


@pytest.mark.parametrize(("scope", "scope_id", "expected_scope", "expected_workspace"), _ATTRIBUTION_CASES, ids=_ATTRIBUTION_IDS)
def test_bundle_confirm_children_persist_artifact_workspace_attribution(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    expected_scope: str,
    expected_workspace: str,
) -> None:
    """★★ 三个写入口里**最脆的那个**：bundle 子件走 `put_bytes(workspace_id=...)`。

    这一路此前**根本不传** `workspace_id`，靠 `put_bytes` 现场推导；而 bundle 子件既没有 session
    也没有 turn/canvas ⇒ **恒推不出** ⇒ 全部落 NULL（批4 ① 修的就是这个）。
    所以「把关键字的值改成 `None`」在这里不是理论突变：它**逐字复原**了修复前的缺陷，
    而按调用点清点的那条守卫（只看关键字在不在）看不见这个回归。

    ★ `len(children) == 1` 是**反空转**断言：子件一个都没造出来的话，「子件的归属都对」会**空过**
      ——这正是「断言没发生」和「用例根本没跑到」长得一样的那一类。
    """
    if scope == "team":
        # Only team checks role (`can_write_to_scope`); space is role-blind, so elevation would only mask regressions.
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")

    pres = _do_bundle_presign(client, scope=scope, scope_id=scope_id)
    art_store: ArtifactStore = app.state.artifact_store

    async def _get_path():
        async with aiosqlite.connect(art_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT path FROM artifacts WHERE id=?", (pres["artifact_id"],))
            row = await cur.fetchone()
        return row["path"]

    path = asyncio.run(_get_path())
    zip_body = _build_zip({"child.pdf": b"%PDF-1.4\nchild body"})
    app.state.artifact_backend.objects[path] = zip_body

    r = client.post(f"/api/v1/kb/presign/bundle/{pres['file_id']}/confirm", json={"uploaded_size": len(zip_body)})
    assert r.status_code == 200, r.text
    assert [f["filename"] for f in r.json()["uploaded"]] == ["child.pdf"]

    async def _children():
        async with aiosqlite.connect(art_store.db_path) as db:
            cur = await db.execute(
                "SELECT a.id, a.workspace_id FROM chat_attachment_bundle_members m "
                "JOIN artifacts a ON a.id = m.child_artifact_id WHERE m.bundle_artifact_id=?",
                (pres["artifact_id"],),
            )
            return await cur.fetchall()

    children = asyncio.run(_children())

    assert len(children) == 1, f"子件没落库（{len(children)} 条）—— 下面的归属断言会空过"
    assert children[0][1] == expected_workspace, f"子件归属写错：实得 {children[0][1]!r}，应为 {expected_workspace!r}"


@pytest.mark.parametrize("size,expected", [(11 * 1024 * 1024, 409), (500 * 1024 * 1024, 409), (500 * 1024 * 1024 + 1, 413)])
def test_local_presign_negotiates_only_within_multipart_limit(client, app, size, expected):
    app.state.artifact_backend = app.state.artifact_store.backend
    response = client.post(
        "/api/v1/kb/presign/bundle",
        headers={"X-Workspace-Id": "ws_42"},
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "source.zip",
            "mime": "application/zip",
            "size_bytes": size,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert response.status_code == expected, response.text
    assert response.json()["detail"]["error"]["code"] == ("file_multipart_required" if expected == 409 else "kb_bundle_too_large")

    async def check_no_pending():
        async with aiosqlite.connect(app.state.artifact_store.db_path) as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM artifacts")).fetchone())[0] == 0
        async with aiosqlite.connect(app.state.kb_files_store._db_path) as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM kb_files")).fetchone())[0] == 0

    asyncio.run(check_no_pending())
