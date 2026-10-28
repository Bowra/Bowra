"""知识库引用的对话投影：按看的人的知识库读权限出态与字段（知识库回答引用技术设计 §3）。"""

from __future__ import annotations

from types import SimpleNamespace

import bowhead_gateway.kb_citation_access as kb_access
import pytest
from bowhead_core.kb_citations import kb_citation_identity
from bowhead_core.source_access_projection import SOURCE_ALIAS_PREFIX
from bowhead_gateway.source_access_projection import project_event_payloads_for_viewer

CHUNK = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"


class _SessionStore:
    async def get_turn(self, turn_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=turn_id, session_id="ses_1")

    async def get_session(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=session_id, workspace_id="ws_chat")

    async def list_turns(self, session_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="turn_1")]


class _CanvasStore:
    async def get_canvas_by_session(self, session_id: str) -> None:
        return None


class _FilesStore:
    def __init__(self, files: dict[str, SimpleNamespace]) -> None:
        self.files = files

    async def get(self, file_id: str) -> SimpleNamespace | None:
        return self.files.get(file_id)


class _AccessStore:
    def __init__(self, denied: set[str]) -> None:
        self.denied = denied

    async def explain_decision(self, ctx: object, kind: str, resource_id: str, permission: str) -> dict[str, object]:
        return {"allowed": resource_id not in self.denied}


def _file(file_id: str, *, scope: str, scope_id: str, uploaded_by: str = "u_owner", deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        file_id=file_id,
        filename=f"{file_id}-当前名.pdf",
        scope=scope,
        scope_id=scope_id,
        uploaded_by=uploaded_by,
        soft_deleted_at="2026-09-01" if deleted else None,
    )


def _request(files: dict[str, SimpleNamespace], *, user: SimpleNamespace | None, access_store: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore(),
                artifact_store=None,
                artifact_fetch_secret="root-secret",
                kb_files_store=_FilesStore(files),
                access_control_store=access_store,
            ),
        ),
        state=SimpleNamespace(bowhead_user=user),
    )


def _viewer(user_id: str = "u_viewer", team_id: str = "team_1") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, team_id=team_id)


@pytest.fixture(autouse=True, params=["1", "0"], ids=["v2_on", "v2_off"])
def _flags_and_membership(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    # KB fragments do not depend on V2: both settings must produce identical results (dependency removed on KB answer citation release).
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", request.param)
    member_of = {"ws_kb"}

    async def fake_build(request: object, *, user: object, workspace_id: str, allow_missing_workspace: bool, operation: object) -> object:
        if workspace_id and workspace_id not in member_of:
            raise PermissionError("not a member")
        return SimpleNamespace(workspace_id=workspace_id)

    monkeypatch.setattr(kb_access, "build_access_context_from_request", fake_build)
    monkeypatch.setattr(kb_access, "access_context_to_dict", lambda ctx: {"workspace_id": getattr(ctx, "workspace_id", "")})


async def _project(request: SimpleNamespace, file_id: str) -> dict[str, object]:
    event = (
        "turn.citation_registered",
        {"citation_seq": 1, "artifact_id": kb_citation_identity(file_id, CHUNK), "label": "登记时的旧名.pdf", "kind": "kb_chunk"},
    )
    return (await project_event_payloads_for_viewer(request, "turn_1", [event]))[0]


def _assert_nothing_released(row: dict[str, object], file_id: str) -> None:
    text = repr(row)
    for secret in (file_id, CHUNK, "当前名", "旧名"):
        assert secret not in text
    assert "label" not in row and "safe_label" not in row


@pytest.mark.anyio
async def test_space_member_sees_file_location_even_from_another_space() -> None:
    """会话在 ws_chat、文件在 ws_kb：按文件所在空间的成员资格判，而不是看的人当前所在空间。"""
    request = _request({"kbf_1": _file("kbf_1", scope="space", scope_id="ws_kb")}, user=_viewer())
    row = await _project(request, "kbf_1")
    assert row["access_state"] == "linked_accessible"
    assert row["label"] == row["safe_label"] == "kbf_1-当前名.pdf"
    assert (row["kb_file_id"], row["kb_chunk_id"], row["kb_scope"], row["kb_space_id"]) == ("kbf_1", CHUNK, "space", "ws_kb")
    assert str(row["artifact_id"]).startswith(SOURCE_ALIAS_PREFIX)


@pytest.mark.anyio
async def test_non_member_is_restricted_and_gets_nothing() -> None:
    request = _request({"kbf_2": _file("kbf_2", scope="space", scope_id="ws_secret")}, user=_viewer())
    row = await _project(request, "kbf_2")
    assert row["access_state"] == "linked_restricted"
    _assert_nothing_released(row, "kbf_2")


@pytest.mark.anyio
async def test_personal_file_only_for_its_uploader() -> None:
    files = {"kbf_p": _file("kbf_p", scope="personal", scope_id="u_owner", uploaded_by="u_owner")}
    mine = await _project(_request(files, user=_viewer("u_owner")), "kbf_p")
    assert mine["access_state"] == "linked_accessible"
    assert mine["kb_scope"] == "personal" and "kb_space_id" not in mine
    theirs = await _project(_request(files, user=_viewer("u_other")), "kbf_p")
    assert theirs["access_state"] == "linked_restricted"
    _assert_nothing_released(theirs, "kbf_p")


@pytest.mark.anyio
async def test_team_file_by_team() -> None:
    files = {"kbf_t": _file("kbf_t", scope="team", scope_id="team_1")}
    assert (await _project(_request(files, user=_viewer(team_id="team_1")), "kbf_t"))["access_state"] == "linked_accessible"
    other = await _project(_request(files, user=_viewer(team_id="team_2")), "kbf_t")
    assert other["access_state"] == "linked_restricted"


@pytest.mark.anyio
async def test_deleted_file_is_missing() -> None:
    request = _request({"kbf_d": _file("kbf_d", scope="space", scope_id="ws_kb", deleted=True)}, user=_viewer())
    row = await _project(request, "kbf_d")
    assert row["access_state"] == "linked_missing"
    _assert_nothing_released(row, "kbf_d")


@pytest.mark.anyio
async def test_no_viewer_on_request_is_restricted() -> None:
    request = _request({"kbf_1": _file("kbf_1", scope="space", scope_id="ws_kb")}, user=None)
    row = await _project(request, "kbf_1")
    assert row["access_state"] == "linked_restricted"
    _assert_nothing_released(row, "kbf_1")


@pytest.mark.anyio
async def test_denied_file_grant_restricts_only_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"kbf_1": _file("kbf_1", scope="space", scope_id="ws_kb")}
    request = _request(files, user=_viewer(), access_store=_AccessStore(denied={"kbf_1"}))
    assert (await _project(request, "kbf_1"))["access_state"] == "linked_accessible"  # Shadow mode: record, do not deny (same as KB routes).
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    row = await _project(request, "kbf_1")
    assert row["access_state"] == "linked_restricted"
    _assert_nothing_released(row, "kbf_1")


@pytest.mark.anyio
async def test_v2_off_keeps_old_shape_for_data_citations_in_same_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """V2 关时同一页里的数据引用仍走旧路（不因知识库引用顺带切到新判据）。"""
    monkeypatch.setenv("FEATURE_SOURCE_ATTRIBUTION_V2", "0")
    request = _request({"kbf_1": _file("kbf_1", scope="space", scope_id="ws_kb")}, user=_viewer())
    events = [
        ("turn.citation_registered", {"citation_seq": 1, "artifact_id": kb_citation_identity("kbf_1", CHUNK), "label": "x", "kind": "kb_chunk"}),
        ("turn.citation_registered", {"citation_seq": 2, "artifact_id": "art_theirs", "label": "别人的", "kind": "data_ref"}),
    ]
    kb_row, data_row = await project_event_payloads_for_viewer(request, "turn_1", events)
    assert kb_row["access_state"] == "linked_accessible"
    assert data_row["access_state"] == "linked_unavailable"
    assert "label" not in data_row


@pytest.mark.anyio
async def test_viewer_resolved_from_token_when_route_skipped_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """画布等旧路由不走 `_current_user`（116 1.3.53：画布详情里所有者的知识库引用被判受限）。"""
    import bowhead_gateway.routes.auth as auth_routes

    request = _request({"kbf_1": _file("kbf_1", scope="space", scope_id="ws_kb")}, user=None)

    class _Users:
        async def get_user(self, user_id: str) -> SimpleNamespace | None:
            return _viewer(user_id) if user_id == "u_viewer" else None

    request.app.state.user_store = _Users()
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _req: "u_viewer")
    row = await _project(request, "kbf_1")
    assert row["access_state"] == "linked_accessible"

    def _no_identity(_req: object) -> str:
        raise RuntimeError("auth required")

    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", _no_identity)
    row = await _project(request, "kbf_1")
    assert row["access_state"] == "linked_restricted"
    _assert_nothing_released(row, "kbf_1")


@pytest.mark.anyio
async def test_personal_file_owner_is_scope_id_not_uploader() -> None:
    """管理员把 X 的文件改到自己的个人范围：新主人可看，原上传者不可看（与知识库页同一规则）。"""
    files = {"kbf_moved": _file("kbf_moved", scope="personal", scope_id="u_admin", uploaded_by="u_x")}
    assert (await _project(_request(files, user=_viewer("u_admin")), "kbf_moved"))["access_state"] == "linked_accessible"
    row = await _project(_request(files, user=_viewer("u_x")), "kbf_moved")
    assert row["access_state"] == "linked_restricted"
    _assert_nothing_released(row, "kbf_moved")
