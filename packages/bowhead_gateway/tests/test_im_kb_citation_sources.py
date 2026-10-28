"""飞书 / Teams 的知识库引用来源列表：按接收人读权限写文件名，群聊与无权限一律「受限来源」。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import bowhead_gateway.kb_citation_access as kb_access
import pytest
from bowhead_core.kb_citations import kb_citation_identity
from bowhead_gateway.im.blocks import SourcesBlock, TextBlock, format_source_lines
from bowhead_gateway.im.turn_render import NeutralTurnState, apply_event, refresh_kb_citation_labels, render_blocks

CHUNK = "chunk_aaaaaaaaaaaaaaaaaaaaaaaa"


@dataclass
class _Ev:
    event_type: str
    payload: dict[str, Any]


def _turn(*citations: tuple[int, str, str]) -> NeutralTurnState:
    state = NeutralTurnState(turn_id="t_1")
    for seq, identity, kind in citations:
        apply_event(state, _Ev("turn.citation_registered", {"citation_seq": seq, "artifact_id": identity, "label": "登记时的旧名.pdf", "kind": kind}))
    apply_event(state, _Ev("final_answer", {"text": "指导价 6499 元起 [cite:2]，数据见 [cite:1]，合同见 [cite:3]。"}))
    state.terminal = True
    return state


class _Files:
    async def get(self, file_id: str) -> Any:
        files = {
            "kbf_mine": SimpleNamespace(
                file_id="kbf_mine", filename="评测.md", scope="personal", scope_id="u_1", uploaded_by="u_1", soft_deleted_at=None
            ),
            "kbf_theirs": SimpleNamespace(
                file_id="kbf_theirs", filename="别人的合同.pdf", scope="personal", scope_id="u_2", uploaded_by="u_2", soft_deleted_at=None
            ),
        }
        return files.get(file_id)


class _Sessions:
    async def get_turn(self, turn_id: str) -> Any:
        return SimpleNamespace(id=turn_id, session_id="s_1")

    async def get_session(self, session_id: str) -> Any:
        return SimpleNamespace(id=session_id, user_id="u_1")


class _Users:
    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(id=user_id, team_id="team_1")


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace(kb_files_store=_Files(), session_store=_Sessions(), user_store=_Users(), access_control_store=None))


@pytest.fixture(autouse=True)
def _no_real_access_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_build(request: object, **_: object) -> object:
        return SimpleNamespace()

    monkeypatch.setattr(kb_access, "build_access_context_from_request", fake_build)


def _sources(state: NeutralTurnState) -> list[str]:
    blocks = render_blocks(state, locale="zh-CN")
    src = [b for b in blocks if isinstance(b, SourcesBlock)]
    assert len(src) == 1
    return format_source_lines(src[0].entries, "zh-CN")[1]


@pytest.mark.anyio
async def test_private_chat_lists_readable_file_name_and_restricts_others() -> None:
    state = _turn(
        (1, "art_data", "data_ref"),
        (2, kb_citation_identity("kbf_mine", CHUNK), "kb_chunk"),
        (3, kb_citation_identity("kbf_theirs", CHUNK), "kb_chunk"),
    )
    await refresh_kb_citation_labels(state, _app(), private_chat=True)
    blocks = render_blocks(state, locale="zh-CN")
    assert isinstance(blocks[0], TextBlock) and "[2]" in blocks[0].markdown and "[cite:" not in blocks[0].markdown
    assert _sources(state) == ["[2] 评测.md · 知识库", "[3] 受限来源"]
    assert "别人的合同" not in repr(blocks) and "登记时的旧名" not in repr(blocks) and "kbf_" not in repr(blocks)


@pytest.mark.anyio
async def test_group_chat_never_names_files() -> None:
    state = _turn((2, kb_citation_identity("kbf_mine", CHUNK), "kb_chunk"))
    await refresh_kb_citation_labels(state, _app(), private_chat=False)
    assert _sources(state) == ["[2] 受限来源"]


@pytest.mark.anyio
async def test_lookup_failure_is_restricted() -> None:
    state = _turn((2, kb_citation_identity("kbf_mine", CHUNK), "kb_chunk"))
    app = _app()
    app.state.session_store = None
    await refresh_kb_citation_labels(state, app, private_chat=True)
    assert _sources(state) == ["[2] 受限来源"]


def test_turn_without_kb_citations_has_no_source_list() -> None:
    state = _turn((1, "art_data", "data_ref"))
    assert not [b for b in render_blocks(state, locale="zh-CN") if isinstance(b, SourcesBlock)]


@pytest.mark.anyio
async def test_teams_answer_body_resolves_markers_and_lists_kb_sources() -> None:
    from bowhead_gateway.routes.teams import teams_answer_body

    state = _turn((1, "art_data", "data_ref"), (2, kb_citation_identity("kbf_mine", CHUNK), "kb_chunk"))
    body = await teams_answer_body(state, "指导价 6499 元起 [cite:2]，数据见 [cite:1]。", _app(), locale="zh-CN")
    assert "[cite:" not in body
    assert body.startswith("指导价 6499 元起 [2]，数据见 [1]。")
    assert body.endswith("**来源**\n[2] 评测.md · 知识库")


@pytest.mark.anyio
async def test_only_sources_cited_in_final_text_are_listed() -> None:
    """编造闸换掉散文后，第一次登记的片段仍在登记表里；来源列表只列终答真的引用的。"""
    state = NeutralTurnState(turn_id="t_1")
    for seq, fid in ((1, "kbf_theirs"), (2, "kbf_mine")):
        apply_event(
            state, _Ev("turn.citation_registered", {"citation_seq": seq, "artifact_id": kb_citation_identity(fid, CHUNK), "kind": "kb_chunk"})
        )
    apply_event(state, _Ev("final_answer", {"text": "只剩一句 [cite:2]。"}))
    state.terminal = True
    await refresh_kb_citation_labels(state, _app(), private_chat=True)
    assert _sources(state) == ["[2] 评测.md · 知识库"]


def test_feishu_report_turn_answer_drops_chat_chips() -> None:
    from bowhead_gateway.feishu.bridge import answer_text_for_card

    chat = NeutralTurnState(turn_id="t_1")
    assert answer_text_for_card(chat, "见 [cite:1]") == "见 [cite:1]"
    report = NeutralTurnState(turn_id="t_2")
    apply_event(report, _Ev("canvas.section_added", {"section": {"id": "sec_1", "kind": "narrative", "markdown": "x"}}))
    assert report.section_list(), "夹具必须真的形成报告轮"
    assert answer_text_for_card(report, "完整报告见画布 [cite:3]。") == "完整报告见画布 。"
