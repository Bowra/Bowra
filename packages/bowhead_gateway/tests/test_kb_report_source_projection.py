"""报告（画布）里的知识库来源按看的人的知识库读权限投影（知识库回答引用技术设计 §3 报告）。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import bowhead_gateway.kb_citation_access as kb_access
import bowhead_gateway.source_access_projection as projection
import pytest
from bowhead_core.kb_citations import kb_citation_identity
from bowhead_core.source_access_projection import SOURCE_ALIAS_PREFIX
from bowhead_gateway.routes.canvas import _viewer_safe_projector
from bowhead_gateway.source_access_projection import ViewerSafeCanvasProjector, project_event_payloads_for_viewer

CHUNK = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_CHUNK = "chunk_bbbbbbbbbbbbbbbbbbbbbbbb"
ART = "art_0123456789ab"


class _FilesStore:
    def __init__(self, files: dict[str, SimpleNamespace]) -> None:
        self.files = files
        self.calls: list[str] = []

    async def get(self, file_id: str) -> SimpleNamespace | None:
        self.calls.append(file_id)
        return self.files.get(file_id)


class _ArtifactStore:
    async def get_many(self, ids: list[str]) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=i, status="ready", created_by_turn="turn_1") for i in ids if i == ART]


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


def _file(file_id: str, *, scope: str = "space", scope_id: str = "ws_kb", uploaded_by: str = "u_owner", deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        file_id=file_id,
        filename=f"{file_id}-当前名.pdf",
        scope=scope,
        scope_id=scope_id,
        uploaded_by=uploaded_by,
        soft_deleted_at="2026-09-01" if deleted else None,
    )


def _request(files: dict[str, SimpleNamespace], *, user: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=_SessionStore(),
                canvas_store=_CanvasStore(),
                turn_log_store=None,
                artifact_store=_ArtifactStore(),
                artifact_fetch_secret="root-secret",
                kb_files_store=_FilesStore(files),
                access_control_store=None,
            ),
        ),
        state=SimpleNamespace(bowhead_user=user),
    )


def _viewer(user_id: str = "u_viewer", team_id: str = "team_1") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, team_id=team_id)


@pytest.fixture(autouse=True)
def _membership(monkeypatch: pytest.MonkeyPatch) -> None:
    member_of = {"ws_kb"}

    async def fake_build(request: object, *, user: object, workspace_id: str, allow_missing_workspace: bool, operation: object) -> object:
        if workspace_id and workspace_id not in member_of:
            raise PermissionError("not a member")
        return SimpleNamespace(workspace_id=workspace_id)

    monkeypatch.setattr(kb_access, "build_access_context_from_request", fake_build)


def _kb_source_ref(chunk_id: str, file_id: str) -> dict[str, Any]:
    # source_ref_aggregator's kb_search output shape (server turn-log aggregation).
    return {
        "id": chunk_id,
        "tool": "kb_search",
        "query": "华东区三季度",
        "turn_id": "turn_1",
        "tool_call_id": "call_kb",
        "kb_chunk_id": chunk_id,
        "kb_file_id": file_id,
        "kb_filename": "检索时的旧名.pdf",
        "kb_page": 3,
        "kb_section_title": "一、总体情况",
    }


def _report(chunk_id: str = CHUNK) -> list[dict[str, Any]]:
    return [
        {"id": "sec_n", "kind": "narrative", "markdown": f"营业额同比增长 7.3% [ref:{chunk_id}]，上海贡献最大 [ref:{ART}]。"},
        {
            "id": "sec_src",
            "kind": "sources",
            "entries": [
                # KB entry shape from sources_resolver.build_sources_list Pass 2.
                {"label": f"KB 文档片段 {chunk_id[:12]}", "tool": "kb_search", "artifact_id": chunk_id, "query": "华东区三季度"},
                {"label": "BI: 上海门店营业额", "tool": "bi_query", "artifact_id": ART, "query": "上海门店营业额"},
            ],
        },
    ]


async def _project(
    request: SimpleNamespace,
    *,
    source_refs: list[dict[str, Any]],
    sections: list[dict[str, Any]] | None = None,
) -> tuple[ViewerSafeCanvasProjector, list[dict[str, Any]], list[dict[str, Any]]]:
    sections = sections if sections is not None else _report()
    canvas = SimpleNamespace(id="cv_1", workspace_id="ws_chat", session_id="ses_1")
    projector = await _viewer_safe_projector(request, canvas, [], source_refs, additional_sections=sections)
    projected, refs = projector.project(sections, source_refs)
    return projector, projected, refs


def _entries(projected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return next(section for section in projected if section["kind"] == "sources")["entries"]


def _assert_no_kb_detail(body: str, file_id: str, chunk_id: str = CHUNK) -> None:
    for secret in (file_id, chunk_id, "当前名", "旧名", "KB 文档片段", "kb_file_id", "kb_chunk_id", "safe_url", "一、总体情况"):
        assert secret not in body, secret


@pytest.mark.anyio
async def test_readable_kb_source_carries_current_name_and_location_without_byte_exit() -> None:
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())
    data_ref = {"id": "call_1", "tool": "bi_query", "artifact_id": ART, "query": "上海"}
    _, projected, refs = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_1"), data_ref])

    kb_entry, data_entry = _entries(projected)
    assert kb_entry["access_state"] == "linked_accessible"
    assert kb_entry["generic_type"] == "document"
    assert kb_entry["safe_label"] == "kbf_1-当前名.pdf"  # Current filename, not registration/search-time name.
    assert (kb_entry["kb_file_id"], kb_entry["kb_chunk_id"], kb_entry["kb_scope"], kb_entry["kb_space_id"]) == ("kbf_1", CHUNK, "space", "ws_kb")
    assert kb_entry["ordinal"] == 1
    assert str(kb_entry["source_alias"]).startswith(SOURCE_ALIAS_PREFIX)
    assert not {"safe_url", "safe_preview_url", "safe_query", "safe_preview", "label", "artifact_id"} & kb_entry.keys()
    # Data sources still use artifact closure.
    assert data_entry["access_state"] == "linked_accessible"
    assert data_entry["safe_url"].endswith(f"/artifacts/{ART}")

    # Session-level sourceRefs also expose viewable KB fragments (frontend looks up by alias).
    kb_ref = next(ref for ref in refs if ref.get("kb_chunk_id") == CHUNK)
    assert kb_ref["source_alias"] == kb_entry["source_alias"]
    assert kb_ref["safe_label"] == "kbf_1-当前名.pdf"
    assert "safe_url" not in kb_ref
    # Replace body marker with the same alias.
    assert f"[ref:{kb_entry['source_alias']}]" in projected[0]["markdown"]


@pytest.mark.anyio
async def test_non_member_gets_restricted_with_nothing_in_raw_body() -> None:
    request = _request({"kbf_2": _file("kbf_2", scope_id="ws_secret")}, user=_viewer())
    _, projected, refs = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_2")])

    kb_entry = _entries(projected)[0]
    assert kb_entry["access_state"] == "linked_restricted"
    assert kb_entry["generic_type"] == "document"
    assert set(kb_entry) == {"id", "source_alias", "access_state", "generic_type", "ordinal"}
    assert all(ref.get("access_state") != "linked_restricted" for ref in refs)  # Do not expose restricted top-level metadata.
    _assert_no_kb_detail(json.dumps({"sections": projected, "sourceRefs": refs}, ensure_ascii=False), "kbf_2")


@pytest.mark.anyio
async def test_personal_file_is_restricted_for_a_shared_viewer() -> None:
    files = {"kbf_p": _file("kbf_p", scope="personal", scope_id="u_owner", uploaded_by="u_owner")}
    refs_in = [_kb_source_ref(CHUNK, "kbf_p")]
    _, mine, _ = await _project(_request(files, user=_viewer("u_owner")), source_refs=refs_in)
    assert _entries(mine)[0]["access_state"] == "linked_accessible"
    assert _entries(mine)[0]["kb_scope"] == "personal" and "kb_space_id" not in _entries(mine)[0]
    _, theirs, their_refs = await _project(_request(files, user=_viewer("u_other")), source_refs=refs_in)
    assert _entries(theirs)[0]["access_state"] == "linked_restricted"
    _assert_no_kb_detail(json.dumps({"sections": theirs, "sourceRefs": their_refs}, ensure_ascii=False), "kbf_p")


@pytest.mark.anyio
async def test_deleted_file_and_unknown_chunk_are_missing() -> None:
    request = _request({"kbf_d": _file("kbf_d", deleted=True)}, user=_viewer())
    _, projected, refs = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_d")])
    assert _entries(projected)[0]["access_state"] == "linked_missing"
    _assert_no_kb_detail(json.dumps({"sections": projected, "sourceRefs": refs}, ensure_ascii=False), "kbf_d")

    # Fragment absent from this session's kb_search results (owning file unknown)⇒invalid; do not query any files.
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())
    _, projected, _ = await _project(request, source_refs=[], sections=_report(OTHER_CHUNK))
    assert _entries(projected)[0]["access_state"] == "linked_missing"
    assert request.app.state.kb_files_store.calls == []


@pytest.mark.anyio
async def test_no_viewer_on_request_is_restricted() -> None:
    request = _request({"kbf_1": _file("kbf_1")}, user=None)
    _, projected, refs = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_1")])
    assert _entries(projected)[0]["access_state"] == "linked_restricted"
    _assert_no_kb_detail(json.dumps({"sections": projected, "sourceRefs": refs}, ensure_ascii=False), "kbf_1")


@pytest.mark.anyio
async def test_judge_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_: object, **__: object) -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(kb_access, "_judge", boom)
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())
    _, projected, refs = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_1")])
    assert _entries(projected)[0]["access_state"] == "linked_restricted"
    _assert_no_kb_detail(json.dumps({"sections": projected, "sourceRefs": refs}, ensure_ascii=False), "kbf_1")


@pytest.mark.anyio
async def test_dialogue_kb_identity_in_report_uses_same_judge() -> None:
    identity = kb_citation_identity("kbf_1", CHUNK)
    sections = [{"id": "sec_src", "kind": "sources", "entries": [{"artifact_id": identity, "label": "登记时的旧名.pdf"}]}]
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())
    _, projected, _ = await _project(request, source_refs=[], sections=sections)
    entry = _entries(projected)[0]
    assert entry["access_state"] == "linked_accessible"
    assert (entry["safe_label"], entry["kb_file_id"], entry["kb_chunk_id"]) == ("kbf_1-当前名.pdf", "kbf_1", CHUNK)


@pytest.mark.anyio
async def test_non_kb_sources_are_byte_identical_with_or_without_the_kb_judge() -> None:
    sections = [
        {"id": "sec_n", "kind": "narrative", "markdown": f"上海贡献最大 [ref:{ART}]，旧数 [num:5.2:亿元:src:tl_old]。"},
        {
            "id": "sec_src",
            "kind": "sources",
            "entries": [
                {"label": "BI: 上海门店营业额", "tool": "bi_query", "artifact_id": ART, "query": "上海门店营业额"},
                {"label": "别人的", "tool": "bi_query", "artifact_id": "art_ffffffffffff"},
                {"label": "自报条目", "tool": "bi_query", "artifact_id": None, "query": "q"},
            ],
        },
    ]
    source_refs = [{"id": "call_1", "tool": "bi_query", "artifact_id": ART, "query": "上海"}]
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())
    _, with_judge, refs_with = await _project(request, source_refs=source_refs, sections=sections)
    baseline = await ViewerSafeCanvasProjector.build(
        root_secret="root-secret",
        workspace_id="ws_chat",
        session_id="ses_1",
        sections=sections,
        source_refs=source_refs,
        report_artifact_closure=[ART],
        artifact_store=_ArtifactStore(),
    )
    without, refs_without = baseline.project(sections, source_refs)
    assert json.dumps([with_judge, refs_with], ensure_ascii=False) == json.dumps([without, refs_without], ensure_ascii=False)
    assert request.app.state.kb_files_store.calls == []


@pytest.mark.anyio
async def test_viewer_noop_save_does_not_persist_the_file_name() -> None:
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())
    projector, projected, _ = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_1")])
    canonical = _report()[1]
    viewer_section = next(section for section in projected if section["kind"] == "sources")
    merged = projector.merge_patch(
        {"entries": canonical["entries"]},
        {"entries": viewer_section["entries"]},
        section_kind="sources",
        section_id="sec_src",
        allow_label_edit=True,
    )
    assert merged["entries"] == canonical["entries"]


@pytest.mark.anyio
async def test_live_section_event_matches_detail_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """live / 重放的 canvas.section_added 与 canvas detail 同一判定（live == persist == replay）。"""
    request = _request({"kbf_1": _file("kbf_1")}, user=_viewer())

    async def refs_for_session(_request: object, _session_id: str) -> list[dict[str, Any]]:
        return [_kb_source_ref(CHUNK, "kbf_1")]

    monkeypatch.setattr(projection, "_source_refs_for_session", refs_for_session)
    event = ("canvas.section_added", {"section_id": "sec_src", "section": _report()[1]})
    (payload,) = await project_event_payloads_for_viewer(request, "turn_1", [event])
    live_entry = payload["section"]["entries"][0]
    _, projected, _ = await _project(request, source_refs=[_kb_source_ref(CHUNK, "kbf_1")])
    detail_entry = _entries(projected)[0]
    assert live_entry == detail_entry
