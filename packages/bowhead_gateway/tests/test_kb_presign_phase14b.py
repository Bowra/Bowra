"""Phase 1.4b UT — KB single-doc presign + confirm endpoints.

Bundle presign + confirm land in Phase 1.4c.

Covers tech-design §5.1b + §5.3b:
- Scope ACL at presign AND at confirm (membership-changed defense)
- doc_type cross-workspace leak guard
- Atomic pending-cap (BEGIN IMMEDIATE)
- CAS confirm with head() error → 503 + rollback
- MIME re-validation at confirm
- Idempotent replay when parse_status already past 'awaiting_upload'
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
from bowhead_core.artifact_store import TEAM_SCOPE_WORKSPACE_ID, LocalBackend, ObjectHead, PresignPostResult
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_presign
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bowhead_core import ArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

# ── fakes ─────────────────────────────────────────────────────────


@dataclass
class _FakeBackend:
    name: str = "local"
    presign_result: PresignPostResult | None = None
    head_result: ObjectHead | None = None
    head_raises: Exception | None = None

    async def presign_post(self, *, aid, filename, mime, size_bytes, expires_in=3600):
        if self.presign_result is None:
            return None
        return PresignPostResult(
            url=self.presign_result.url,
            fields=dict(self.presign_result.fields),
            path=f"/tmp/kbp/{aid}_{filename}",
            expires_at=self.presign_result.expires_at,
        )

    async def head(self, path: str):
        if self.head_raises is not None:
            raise self.head_raises
        return self.head_result


class _FakeUserStore:
    def __init__(self) -> None:
        self.allowed = True

    async def rate_limit_check(self, bucket: str, *, limit: int, window_s: int) -> bool:
        return self.allowed


class _FakeDocTypeStore:
    def __init__(self) -> None:
        self.doc_types: dict[str, Any] = {}

    async def get(self, doc_type_id: str) -> Any:
        return self.doc_types.get(doc_type_id)


class _FakeWorker:
    def __init__(self) -> None:
        self.called_with: list[str] = []

    async def parse_and_index(self, file_id: str) -> None:
        self.called_with.append(file_id)


def _make_user(uid: str = "u_alice", team: str = "team_test", email: str = "alice@test.com") -> User:
    return User(id=uid, email=email, team_id=team, tier="ultra", email_verified=True)


# ── app fixture ───────────────────────────────────────────────────


@pytest.fixture
async def app(initialize_migrated_kb_files_database, tmp_path: Path, monkeypatch) -> FastAPI:
    # Ensure _resolve_role returns 'user' (not 'team_admin') so can_write_to_scope
    # is governed by scope/membership, not by admin override.
    monkeypatch.delenv("BOWHEAD_AUTO_ADMIN_DOMAINS", raising=False)

    a = FastAPI()
    a.include_router(kb_presign.router)

    artifact_store = ArtifactStore(
        db_path=str(tmp_path / "kbp_art.db"),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await artifact_store.initialize()
    a.state.artifact_store = artifact_store

    initialize_migrated_kb_files_database(tmp_path / "kbp_files.db")
    files_store = KbFilesStore(tmp_path / "kbp_files.db")
    await files_store.initialize()
    a.state.kb_files_store = files_store

    a.state.artifact_backend = _FakeBackend(
        presign_result=PresignPostResult(
            url="http://test/kbp/local-receiver/aid",
            fields={"Content-Type": "application/pdf", "Authorization": "Bearer XYZ"},
            path="placeholder",
            expires_at=datetime.now(tz=UTC) + timedelta(seconds=3600),
        ),
        head_result=ObjectHead(size=99999, etag='"k"', mime="application/pdf"),
    )
    a.state.user_store = _FakeUserStore()
    a.state.kb_doc_type_store = _FakeDocTypeStore()
    a.state.kb_worker = _FakeWorker()
    return a


@pytest.fixture
def client(app: FastAPI, stub_current_user) -> TestClient:
    """Build TestClient AFTER stub_current_user wires the dependency override."""
    return TestClient(app)


@pytest.fixture
def stub_current_user(app: FastAPI) -> dict:
    """Override Depends(_current_user) via FastAPI's dependency_overrides.

    Tests mutate `state["user"]` between requests to swap identity
    (e.g., simulate ACL revoke between presign and confirm).
    """
    from bowhead_gateway.routes import kb_presign as kp

    state = {"user": _make_user()}

    async def _fake_current_user() -> User:
        return state["user"]

    app.dependency_overrides[kp._current_user] = _fake_current_user
    return state


# ── presign happy + size/mime gates ───────────────────────────────


def test_kb_presign_happy_returns_envelope(client: TestClient, app: FastAPI) -> None:
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "report.pdf",
            "mime": "application/pdf",
            "size_bytes": 500_000,
            "scope": "personal",
            "scope_id": "u_alice",
        },
        headers={"X-Workspace-Id": "ws_42"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"].startswith("kbf_")
    assert body["artifact_id"].startswith("art_")
    assert body["upload"]["url"].startswith("http://")
    assert body["upload"]["method"] == "POST"
    assert body["confirm_url"] == f"/api/v1/kb/presign/{body['file_id']}/confirm"

    # kb_files row exists with parse_status='awaiting_upload'
    files_store: KbFilesStore = app.state.kb_files_store

    async def _check():
        async with aiosqlite.connect(files_store._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT parse_status, artifact_id FROM kb_files WHERE file_id=?",
                (body["file_id"],),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["parse_status"] == "awaiting_upload"
        assert row["artifact_id"] == body["artifact_id"]

    asyncio.run(_check())


def test_kb_presign_rejects_oversize_413(client: TestClient) -> None:
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "huge.pdf",
            "mime": "application/pdf",
            "size_bytes": 3 * 1024**3,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 413


def test_kb_presign_rejects_unsupported_mime_415(client: TestClient) -> None:
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "x.exe",
            "mime": "application/x-msdownload",
            "size_bytes": 100,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 415


def test_kb_presign_rejects_text_file_with_unsupported_extension(client: TestClient) -> None:
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "notes.bin",
            "mime": "text/plain",
            "size_bytes": 100,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 415
    assert r.json()["detail"]["error"]["code"] == "kb_file_type_unsupported"


def test_kb_presign_403_on_scope_spoof(client: TestClient, stub_current_user) -> None:
    """User u_alice presigns scope=personal scope_id=u_bob (spoof) → 403."""
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "x.pdf",
            "mime": "application/pdf",
            "size_bytes": 100,
            "scope": "personal",
            "scope_id": "u_bob",  # not me
        },
    )
    assert r.status_code == 403


def test_kb_presign_403_on_doc_type_cross_ws_leak(client: TestClient, app: FastAPI) -> None:
    """doc_type belongs to ws_99 but caller's workspace is ws_42 → 403 leak guard."""
    dt = MagicMock()
    dt.workspace_id = "ws_99"
    app.state.kb_doc_type_store.doc_types["dt_other"] = dt
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "x.pdf",
            "mime": "application/pdf",
            "size_bytes": 100,
            "scope": "personal",
            "scope_id": "u_alice",
            "doc_type_id": "dt_other",
        },
        headers={"X-Workspace-Id": "ws_42"},
    )
    assert r.status_code == 403


def test_kb_presign_429_pending_cap(client: TestClient, app: FastAPI) -> None:
    """20 stale pending artifacts → 21st presign returns 429 (atomic BEGIN IMMEDIATE)."""
    artifact_store: ArtifactStore = app.state.artifact_store

    async def _seed():
        async with aiosqlite.connect(artifact_store.db_path) as db:
            for i in range(20):
                await db.execute(
                    "INSERT INTO artifacts(id, filename, mime, size, source, path, "
                    "canvas_id, created_by, created_at, status, expires_at) "
                    "VALUES (?, ?, ?, ?, 'kb_upload', ?, NULL, 'u_alice', ?, 'pending_upload', ?)",
                    (f"art_pen{i:09d}", f"f{i}.pdf", "application/pdf", 100, f"/tmp/{i}", "2026-05-27T00:00:00+00:00", "2026-05-27T23:00:00+00:00"),
                )
            await db.commit()

    asyncio.run(_seed())
    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "x.pdf",
            "mime": "application/pdf",
            "size_bytes": 100,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert r.status_code == 429


# ── Artifact space attribution (space isolation batch 4 ①) ──────────────────


@pytest.mark.parametrize(
    ("scope", "scope_id", "expected_scope", "expected_workspace"),
    [
        ("team", "team_test", "team", TEAM_SCOPE_WORKSPACE_ID),
        ("space", "ws_42", "space", "ws_42"),
    ],
    ids=["team_scope_sentinel", "space_scope_real_id"],
)
def test_kb_presign_persists_artifact_workspace_attribution(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    scope_id: str,
    expected_scope: str,
    expected_workspace: str,
) -> None:
    """★ 空间隔离批4 ①：单文件 presign 必须把归属**真的写进库** —— 团队档落哨兵 `__team__`、空间档落真实 `ws_*`。

    这是**行为断言**（查库里的真值），不是源码文本断言。后者已被证明够不着：2026-09-17 批4 的
    M1 突变实测，把写入值改成 `None` 之后 `test_every_artifact_writer_decides_about_workspace_id`
    **仍然全绿**（`TEAM_SCOPE_WORKSPACE_ID` 还留在 import 行上）；按调用点清点的那条
    `test_every_put_bytes_call_site_supplies_an_attribution_input` 同样够不着——它只看关键字
    **在不在**，不看写下去的值。当时 `brand_assets` 补了查库用例，`kb_presign` 没补，这条补的就是它。
    [[feedback_gate_that_cannot_parse_must_not_pass]]

    ★ 两档一起参数化是**判别对照**，不是凑数：只断言团队档的话，「无条件写哨兵」那种突变照样绿；
      只断言空间档的话，「砍掉 `or TEAM_SCOPE_WORKSPACE_ID` 兜底」（团队档退回空串）照样绿。

    ⚠ **夹具保真度**（本文件的老毛病，写在这里省得下一个人重查）：`_FakeUserStore` 不带 RBAC 方法
      ⇒ `has_workspace_access_source()` 为 False ⇒ `_make_scope_context_checked` 直接返回**请求头推出的**
      ctx，不经 `build_access_context`。归属表达式在两条路上逐字相同（后者只校验成员资格、
      **不改写** `workspace_id`），所以这里的断言仍然成立；但**授权**那一面不由本用例覆盖，
      它归 `test_kb_presign_403_on_scope_spoof`。

    留 NULL 的后果很具体：批4 把「NULL 且创建于切点之后」翻成 fail-close，而 fail-close 是按
    **产物**判的 ⇒ 切点后经 KB 上传的文件会对**所有人**关门，包括上传者本人。
    """
    if scope == "team":
        # ★ Elevate only team: `KbPermission.can_write_to_scope` checks role only in the team branch;
        # space is role-blind. Enabling it for space would needlessly elevate the caller to team_admin,
        # **masking** regressions such as space writes starting to require admin (fixture deliberately delenv to preserve this default).
        monkeypatch.setenv("BOWHEAD_AUTO_ADMIN_DOMAINS", "test.com")

    r = client.post(
        "/api/v1/kb/presign",
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "report.pdf",
            "mime": "application/pdf",
            "size_bytes": 1000,
            "scope": scope,
            "scope_id": scope_id,
        },
        # Use **canonical** spelling: `_make_scope_context` reads `X-Space-Id` first; `X-Workspace-Id` is legacy-client fallback.
        # Other cases in this file use fallback, covering both spellings.
        headers={"X-Space-Id": "ws_42"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    artifact_store: ArtifactStore = app.state.artifact_store
    files_store: KbFilesStore = app.state.kb_files_store

    async def _read():
        async with aiosqlite.connect(artifact_store.db_path) as db:
            cur = await db.execute("SELECT workspace_id FROM artifacts WHERE id=?", (body["artifact_id"],))
            art = await cur.fetchone()
        async with aiosqlite.connect(files_store._db_path) as db:
            cur = await db.execute("SELECT scope FROM kb_files WHERE file_id=?", (body["file_id"],))
            kbf = await cur.fetchone()
        return art, kbf

    art, kbf = asyncio.run(_read())

    # Positive control: artifact really persisted. Without this, the next assertion might only prove no row found.
    assert art is not None, "产物没落进 artifacts —— 这条用例证明不了任何事"
    # Prerequisite: this upload **actually** used the tested scope; otherwise we are not asserting the team sentinel.
    assert kbf is not None and kbf[0] == expected_scope, f"前置不成立：scope={kbf and kbf[0]!r}"
    assert art[0] == expected_workspace, f"归属写错：实得 {art[0]!r}，应为 {expected_workspace!r}（留 NULL 会在切点后被 fail-close 拦掉）"


# ── Attribution metrics (persisted in metrics_event_log) ────────────────────
#
# Drive the real route: `with client:` + `attribution_metrics.drain(client)` deterministically await background-task writes
# (metrics run outside the request path; see conftest's `AttributionMetricsProbe` explanation).

_METRICS_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"

_PRESIGN_BODY = {"knowledge_intent": "add_to_knowledge", "filename": "report.pdf", "mime": "application/pdf", "size_bytes": 1000}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("scope", "scope_id", "with_space_header", "expected_reason"),
    [
        ("team", "team_test", True, "team"),
        ("space", "ws_42", True, "space"),
        # ★ Why these metrics exist: personal scope without a request space ⇒ attribution unresolved ⇒ `none`.
        ("personal", "u_alice", False, "none"),
        # ★ Pin personal + space header ⇒ space; wrongly recording none with a personal header previously survived the whole suite.
        ("personal", "u_alice", True, "space"),
    ],
    ids=["team", "space", "personal_without_space_header", "personal_with_space_header"],
)
def test_kb_presign_single_emits_one_attribution_metric(
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
            "/api/v1/kb/presign",
            json={**_PRESIGN_BODY, "scope": scope, "scope_id": scope_id},
            headers={"X-Space-Id": "ws_42"} if with_space_header else {},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)

    assert attribution_metrics.events() == [{"route": "kb_presign_single", "reason": expected_reason, "scope": scope}]
    assert attribution_metrics.user_ids() == [None]


@pytest.mark.integration
def test_kb_presign_single_flag_off_control_writes_no_metric(client: TestClient, app: FastAPI, attribution_metrics) -> None:
    """★ 对照：同一请求，flag 关着必须 0 行（drain 之后再读，所以「0」只可能是没排过任务）。"""
    attribution_metrics.attach(app)
    with client:
        r = client.post(
            "/api/v1/kb/presign",
            json={**_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"},
            headers={"X-Space-Id": "ws_42"},
        )
        assert r.status_code == 200, r.text
        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_kb_presign_single_rejected_requests_are_not_counted(
    client: TestClient, app: FastAPI, attribution_metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 被拒的请求（413 / 415 / 403 / **429**）都不计数——打点在**成功之后**。

    429（pending 上限）是最有区分度的一条：旧位置在 pending 检查**之前**，它会被计进去；
    而被拒请求正是会灌水 `none` 桶（本打点要量的那个）的来源。
    每个请求都先断言状态码，前置不成立时本用例要报出来而不是空过。
    """
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
                    (f"art_pen{i:09d}", f"f{i}.pdf", "application/pdf", 100, f"/tmp/{i}", "2026-05-27T00:00:00+00:00", "2026-05-27T23:00:00+00:00"),
                )
            await db.commit()

    personal = {"scope": "personal", "scope_id": "u_alice"}
    with client:
        oversize = client.post("/api/v1/kb/presign", json={**_PRESIGN_BODY, "size_bytes": 3 * 1024**3, **personal})
        assert oversize.status_code == 413, oversize.text
        unsupported = client.post(
            "/api/v1/kb/presign",
            json={**_PRESIGN_BODY, "knowledge_intent": "add_to_knowledge", "filename": "x.exe", "mime": "application/x-msdownload", **personal},
        )
        assert unsupported.status_code == 415, unsupported.text
        spoofed = client.post("/api/v1/kb/presign", json={**_PRESIGN_BODY, "scope": "personal", "scope_id": "u_bob"})
        assert spoofed.status_code == 403, spoofed.text

        asyncio.run(_seed_pending())
        capped = client.post("/api/v1/kb/presign", json={**_PRESIGN_BODY, **personal})
        assert capped.status_code == 429, capped.text

        attribution_metrics.drain(client)
    assert attribution_metrics.events() == []


@pytest.mark.integration
def test_kb_presign_single_failure_after_the_decision_is_not_counted(
    app: FastAPI, stub_current_user, attribution_metrics, fail_audit_event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ B 评审：「成功之后才计数」此前只被**决策之前**的拒绝保护（413/415/403/429 都发生在决策点之前或原子插入处）。
    这里让请求在**紧贴打点之前的最后一步**失败（`kb_presign.issued` 审计抛错 ⇒ 500，此时产物与 kb_files 行都已落库）：
    打点若被挪到比这更早的任何位置，这次失败也会被计进去。放行后再发一次（正对照：成功的请求**会**被计一行）。
    """
    monkeypatch.setenv(_METRICS_FLAG, "1")
    attribution_metrics.attach(app)
    request = {"json": {**_PRESIGN_BODY, "scope": "space", "scope_id": "ws_42"}, "headers": {"X-Space-Id": "ws_42"}}

    with TestClient(app, raise_server_exceptions=False) as strict:
        restore = fail_audit_event(kb_presign, "kb_presign.issued")
        failed = strict.post("/api/v1/kb/presign", **request)
        assert failed.status_code == 500, failed.text  # Prerequisite: actually failed after the decision.
        attribution_metrics.drain(strict)
        assert attribution_metrics.events() == [], "失败的那次不得计数"

        restore()
        ok = strict.post("/api/v1/kb/presign", **request)
        assert ok.status_code == 200, ok.text
        attribution_metrics.drain(strict)
    assert attribution_metrics.events() == [{"route": "kb_presign_single", "reason": "space", "scope": "space"}]


# ── confirm ───────────────────────────────────────────────────────


def _do_kb_presign(client: TestClient, **overrides) -> dict:
    payload = {
        "knowledge_intent": "add_to_knowledge",
        "filename": "report.pdf",
        "mime": "application/pdf",
        "size_bytes": 500_000,
        "scope": "personal",
        "scope_id": "u_alice",
    }
    payload.update(overrides)
    r = client.post("/api/v1/kb/presign", json=payload, headers={"X-Workspace-Id": "ws_42"})
    assert r.status_code == 200, r.text
    return r.json()


def test_kb_confirm_happy_enqueues_worker(client: TestClient, app: FastAPI) -> None:
    pres = _do_kb_presign(client)
    file_id = pres["file_id"]
    r = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={"uploaded_size": 99999})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == file_id
    assert body["parse_status"] in ("queued", "pending", "parsing")  # worker may have started

    # Worker enqueued
    worker = app.state.kb_worker
    assert file_id in worker.called_with

    # kb_files row flipped to queued/pending
    files_store: KbFilesStore = app.state.kb_files_store

    async def _check():
        async with aiosqlite.connect(files_store._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT parse_status FROM kb_files WHERE file_id=?",
                (file_id,),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row["parse_status"] != "awaiting_upload"

    asyncio.run(_check())


def test_kb_confirm_404_on_unknown_file_id(client: TestClient) -> None:
    r = client.post("/api/v1/kb/presign/kbf_doesnotex/confirm", json={})
    assert r.status_code == 404


def test_kb_confirm_cross_user_answers_like_missing(client: TestClient, app: FastAPI, stub_current_user) -> None:
    pres = _do_kb_presign(client)
    file_id = pres["file_id"]
    # Swap user mid-flight
    stub_current_user["user"] = _make_user(uid="u_attacker", email="attacker@evil.com")
    r = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={})
    missing = client.post("/api/v1/kb/presign/kbf_doesnotex/confirm", json={})
    assert r.status_code == 404
    assert (r.status_code, r.content) == (missing.status_code, missing.content)


def test_kb_confirm_415_on_mime_mismatch(client: TestClient, app: FastAPI) -> None:
    """Object store reports text/html when presign was for application/pdf → 415."""
    pres = _do_kb_presign(client)
    file_id = pres["file_id"]
    app.state.artifact_backend.head_result = ObjectHead(
        size=99999,
        etag='"x"',
        mime="text/html",
    )
    r = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={})
    assert r.status_code == 415


def test_kb_confirm_503_on_backend_head_error_rolls_back(client: TestClient, app: FastAPI) -> None:
    """backend.head() raise → CAS rolled back + 503. Phase 1.4a P0-1 pattern."""
    pres = _do_kb_presign(client)
    file_id = pres["file_id"]
    aid = pres["artifact_id"]
    app.state.artifact_backend.head_raises = RuntimeError("backend hiccup")
    r = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={})
    assert r.status_code == 503

    # Artifact row rolled back to pending_upload (not stuck in 'confirming')
    artifact_store: ArtifactStore = app.state.artifact_store

    async def _check():
        async with aiosqlite.connect(artifact_store.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT status FROM artifacts WHERE id=?", (aid,))
            row = await cur.fetchone()
        assert row["status"] == "pending_upload"

    asyncio.run(_check())


def test_kb_confirm_idempotent_replay_when_already_queued(client: TestClient, app: FastAPI) -> None:
    """parse_status already past 'awaiting_upload' (e.g., 'queued') → return DTO directly."""
    pres = _do_kb_presign(client)
    file_id = pres["file_id"]
    # First confirm
    r1 = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={})
    assert r1.status_code == 200
    # Second confirm should be idempotent (return DTO, do not error)
    r2 = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={})
    assert r2.status_code == 200
    assert r2.json()["file_id"] == file_id


def test_kb_confirm_failed_source_cannot_bypass_reparse_probe(client: TestClient, app: FastAPI) -> None:
    pres = _do_kb_presign(client)
    file_id = pres["file_id"]
    asyncio.run(app.state.kb_files_store.update(file_id, parse_status="failed", parse_error="source_missing: original unavailable"))

    response = client.post(f"/api/v1/kb/presign/{file_id}/confirm", json={})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"error": {"code": "kb_source_missing"}}
    assert asyncio.run(app.state.kb_files_store.get(file_id)).parse_status == "failed"
    assert file_id not in app.state.kb_worker.called_with


@pytest.mark.parametrize("size,expected", [(11 * 1024 * 1024, 409), (200 * 1024 * 1024, 409), (200 * 1024 * 1024 + 1, 413)])
def test_local_presign_negotiates_only_within_multipart_limit(client, app, size, expected):
    app.state.artifact_backend = app.state.artifact_store.backend
    response = client.post(
        "/api/v1/kb/presign",
        headers={"X-Workspace-Id": "ws_42"},
        json={
            "knowledge_intent": "add_to_knowledge",
            "filename": "source.pdf",
            "mime": "application/pdf",
            "size_bytes": size,
            "scope": "personal",
            "scope_id": "u_alice",
        },
    )
    assert response.status_code == expected, response.text
    assert response.json()["detail"]["error"]["code"] == ("file_multipart_required" if expected == 409 else "kb_file_too_large")

    async def check_no_pending():
        async with aiosqlite.connect(app.state.artifact_store.db_path) as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM artifacts")).fetchone())[0] == 0
        async with aiosqlite.connect(app.state.kb_files_store._db_path) as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM kb_files")).fetchone())[0] == 0

    asyncio.run(check_no_pending())
