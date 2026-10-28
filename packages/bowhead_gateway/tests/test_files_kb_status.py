"""知识库体验整治 FR7：`/files` 列表带出知识库状态。

真 `ArtifactStore` + 真 `KbFilesStore`（迁移基线）+ 真 files 路由。断言：
  1. 加入知识库的文件带出 KB file_id / 范围 / 状态字段（与 KbFileDTO 同名同义）
  2. 一页只查一次 KB 库（不是 N+1）
  3. 调用方按 KB 读权限看不到的行：`in_kb` 照旧为 True，但不透露状态
  4. 空间行按**行自己的空间**判可见（文件页请求不带空间头）：是成员 → 带出状态与空间 id；
     不是成员 → 不透露；建上下文的次数只随页内不同空间数增长，不随行数增长
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_TEAM = "team_t"
_WS = "ws_t"
_WS_OTHER = "ws_other"
_WS_CLOSED = "ws_closed"  # Same-team space, but alice is not a member.
_UID = "usr_alice"
_OTHER = "usr_bob"


def _h(user: str = _UID, space: str | None = _WS) -> dict[str, str]:
    h = {"X-Bowhead-User-Id": user}
    if space:
        h["X-Space-Id"] = space
    return h


class _UserStore:
    """三个空间都属本团队；alice 与 bob 是前两个的成员，ws_closed 只有 bob。"""

    async def get_user_team_id(self, user_id: str) -> str | None:
        return _TEAM if user_id in (_UID, _OTHER) else None

    async def get_workspace(self, workspace_id: str) -> dict[str, str] | None:
        if workspace_id not in (_WS, _WS_OTHER, _WS_CLOSED):
            return None
        return {"id": workspace_id, "team_id": _TEAM, "status": "active"}

    async def get_workspace_team_id(self, workspace_id: str) -> str | None:
        return _TEAM if workspace_id in (_WS, _WS_OTHER, _WS_CLOSED) else None

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        if workspace_id == _WS_CLOSED:
            return "member" if user_id == _OTHER else None
        return "member" if workspace_id in (_WS, _WS_OTHER) and user_id in (_UID, _OTHER) else None

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "member" if team_id == _TEAM else None


class _CountingKbStore(KbFilesStore):
    def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        super().__init__(*a, **kw)
        self.batch_calls: list[list[str]] = []

    async def active_for_artifacts(self, artifact_ids: list[str]):  # noqa: ANN201
        self.batch_calls.append(list(artifact_ids))
        return await super().active_for_artifacts(artifact_ids)


@pytest.fixture
async def env(initialize_migrated_kb_files_database, tmp_path: Path):
    db = tmp_path / "a.db"
    await SessionStore(db).initialize()
    artifacts = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "blob"), db_path=db)
    await artifacts.initialize()
    initialize_migrated_kb_files_database(tmp_path / "kb.db")
    kb = _CountingKbStore(tmp_path / "kb.db")
    await kb.initialize()
    app = FastAPI()
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = artifacts
    app.state.kb_files_store = kb
    app.state.user_store = _UserStore()
    app.state.settings = SimpleNamespace(dev_default_user=_UID, auth_mode="dev")
    return TestClient(app), artifacts, kb


async def _art(store: ArtifactStore, name: str, owner: str = _UID):
    return await store.put_bytes(
        content=b"%PDF-1.4 fake", filename=name, mime="application/pdf", source="upload", created_by=owner, kind="user_upload"
    )


async def _kb_row(kb: KbFilesStore, art, *, scope: str, scope_id: str, by: str = _UID, ws: str = "") -> str:  # noqa: ANN001
    return await kb.create(
        artifact_id=art.id,
        filename=art.filename,
        mime_type=art.mime,
        size_bytes=art.size,
        scope=scope,
        scope_id=scope_id,
        workspace_id=ws,
        team_id=_TEAM,
        uploaded_by=by,
    )


def _items(client: TestClient, headers: dict[str, str]) -> dict[str, dict]:
    r = client.get("/api/v1/files/?source=all", headers=headers)
    assert r.status_code == 200, r.text
    return {i["file_id"]: i for i in r.json()["items"]}


async def test_list_carries_kb_status_for_promoted_file_in_one_batch(env) -> None:
    client, artifacts, kb = env
    ready = await _art(artifacts, "ready.pdf")
    broken = await _art(artifacts, "broken.pdf")
    plain = await _art(artifacts, "plain.pdf")
    ready_fid = await _kb_row(kb, ready, scope="personal", scope_id=_UID)
    broken_fid = await _kb_row(kb, broken, scope="space", scope_id=_WS, ws=_WS)
    await kb.update(ready_fid, parse_status="done")
    await kb.mark_parse_failed(broken_fid, "unsupported format: foo")

    by_id = _items(client, _h())

    assert by_id[ready.id]["in_kb"] is True
    assert by_id[ready.id]["kb"] == {
        "file_id": ready_fid,
        "scope": "personal",
        "workspace_id": None,
        "parse_status": "done",
        "parse_progress_pct": None,
        "parse_stuck": False,
        "parse_error_code": None,
    }
    assert by_id[broken.id]["kb"]["scope"] == "space"
    assert by_id[broken.id]["kb"]["parse_status"] == "failed"
    assert by_id[broken.id]["kb"]["parse_error_code"] is not None
    # Not in KB: both old and new fields indicate absence.
    assert by_id[plain.id]["in_kb"] is False and by_id[plain.id]["kb"] is None
    # One KB query covering the whole page.
    assert len(kb.batch_calls) == 1
    assert set(kb.batch_calls[0]) == {ready.id, broken.id, plain.id}


async def test_space_row_visible_to_member_without_a_space_header(env) -> None:
    """文件页请求不带空间头：空间行按它自己的空间判成员资格，是成员就带出状态和空间 id。"""
    client, artifacts, kb = env
    art = await _art(artifacts, "contract.pdf")
    fid = await _kb_row(kb, art, scope="space", scope_id=_WS_OTHER, ws=_WS_OTHER)

    item = _items(client, _h(space=None))[art.id]
    assert item["kb"] is not None and item["kb"]["file_id"] == fid
    assert item["kb"]["scope"] == "space" and item["kb"]["workspace_id"] == _WS_OTHER
    # Independent of the space named by the request header: another space's header makes no difference.
    assert _items(client, _h(space=_WS))[art.id]["kb"]["workspace_id"] == _WS_OTHER


async def test_space_row_of_a_non_member_space_keeps_in_kb_but_hides_status(env) -> None:
    client, artifacts, kb = env
    art = await _art(artifacts, "closed.pdf")
    await _kb_row(kb, art, scope="space", scope_id=_WS_CLOSED, ws=_WS_CLOSED)

    hidden = _items(client, _h(space=None))[art.id]
    assert hidden["in_kb"] is True, "in_kb 语义不变：删除防护依赖它"
    assert hidden["kb"] is None, "不是那个空间的成员，不能透露它的状态、file_id 与空间"

    # Positive control: same row visible to space member bob (if he can also see the file); None above comes from membership checks.
    bob_art = await _art(artifacts, "closed-bob.pdf", owner=_OTHER)
    await _kb_row(kb, bob_art, scope="space", scope_id=_WS_CLOSED, by=_OTHER, ws=_WS_CLOSED)
    assert _items(client, _h(user=_OTHER, space=None))[bob_art.id]["kb"]["workspace_id"] == _WS_CLOSED


async def test_access_context_builds_scale_with_distinct_spaces_not_rows(env, monkeypatch) -> None:
    client, artifacts, kb = env
    real = files_routes.build_access_context
    calls: list[str] = []

    async def counting(**kw):  # noqa: ANN003, ANN202
        calls.append(kw["workspace_id"])
        return await real(**kw)

    monkeypatch.setattr(files_routes, "build_access_context", counting)
    for i in range(6):
        art = await _art(artifacts, f"s{i}.pdf")
        ws = _WS if i % 2 else _WS_OTHER
        await _kb_row(kb, art, scope="space", scope_id=ws, ws=ws)
    for i in range(3):
        art = await _art(artifacts, f"p{i}.pdf")
        await _kb_row(kb, art, scope="personal", scope_id=_UID)

    calls.clear()
    items = _items(client, _h(space=None))
    assert sum(1 for i in items.values() if i["kb"]) == 9
    kb_builds = [ws for ws in calls if ws]
    # 9 rows, 2 spaces: build context once per space (plus once without a space for personal/team rows).
    assert sorted(kb_builds) == sorted([_WS, _WS_OTHER])
    assert calls.count("") == 1

    # Negative control: add 4 files in the same two spaces; context build count stays unchanged.
    for i in range(4):
        art = await _art(artifacts, f"t{i}.pdf")
        await _kb_row(kb, art, scope="space", scope_id=_WS, ws=_WS)
    calls.clear()
    _items(client, _h(space=None))
    assert sorted(ws for ws in calls if ws) == sorted([_WS, _WS_OTHER])


async def test_someone_elses_personal_kb_row_is_not_revealed(env) -> None:
    client, artifacts, kb = env
    art = await _art(artifacts, "shared-with-me.pdf")
    # bob placed the same artifact in his own personal KB.
    await _kb_row(kb, art, scope="personal", scope_id=_OTHER, by=_OTHER)
    assert _items(client, _h())[art.id]["kb"] is None
    # Positive control: alice's own personal row is visible.
    own = await _kb_row(kb, art, scope="personal", scope_id=_UID)
    assert _items(client, _h())[art.id]["kb"]["file_id"] == own


async def test_forged_space_header_cannot_read_status(env) -> None:
    """X-Space-Id 须经成员校验：填一个不是成员的空间，列表被拒，不会带出那个空间的状态。"""
    client, artifacts, kb = env
    art = await _art(artifacts, "x.pdf")
    await _kb_row(kb, art, scope="space", scope_id="ws_foreign", ws="ws_foreign")
    r = client.get("/api/v1/files/?source=all", headers=_h(space="ws_foreign"))
    assert r.status_code in (403, 404)
