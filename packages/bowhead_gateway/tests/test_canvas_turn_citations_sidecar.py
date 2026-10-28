"""source-accessibility §1.6 消费半 c1 —— turnCitations sidecar 与态映射。

契约（consumer-discovery §2.1 / tech-design §4.1）：
- 行由 `citation_presentation` 单点映射（与将来的事件投影同形）；
- OWNED 才释放 safe_label / safe_anchor_id；非 OWNED 的行**只有** {seq, kind, access_state}
  （成对测试，互为 positive control——安全面松掉任何一半都会红）；
- `linked_accessible` 的判定与字节出口 closure 同源；
- sidecar 失败 fail-soft：不挡 canvas 加载，但 turnCitations 为空 + 留 warning。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.source_attribution import Attribution, DisplayHandle, Verdict
from bowhead_gateway.routes.canvas import _turn_citations_sidecar
from bowhead_gateway.source_access_projection import citation_presentation

# ---------------------------------------------------------------- citation_presentation


def _owned(*, exit_candidate: str | None = None, anchor: str | None = None) -> Verdict:
    return Verdict(
        Attribution.OWNED,
        "owned_by_canvas",
        display=DisplayHandle(anchor_id=anchor),
        exit_candidate=exit_candidate,
    )


@pytest.mark.unit
def test_presentation_owned_with_exit_in_closure_is_accessible() -> None:
    row = citation_presentation(
        seq=1,
        kind="data_ref",
        label="bi_query: 营业额",
        verdict=_owned(exit_candidate="art_a"),
        rendered_artifact_ids={"art_a"},
    )
    assert row == {"seq": 1, "kind": "data_ref", "access_state": "linked_accessible", "safe_label": "bi_query: 营业额"}


@pytest.mark.unit
def test_presentation_owned_without_exit_is_labeled_with_anchor() -> None:
    row = citation_presentation(
        seq=2,
        kind="chart_section",
        label="客单价Top3",
        verdict=_owned(anchor="sec_x"),
        rendered_artifact_ids=frozenset(),
    )
    assert row["access_state"] == "linked_labeled"
    assert row["safe_label"] == "客单价Top3"
    assert row["safe_anchor_id"] == "sec_x"


@pytest.mark.unit
def test_presentation_owned_exit_outside_closure_is_labeled_not_accessible() -> None:
    # Exit candidate outside rendered closure ⇒ label, no byte exit; must not upgrade to accessible.
    row = citation_presentation(
        seq=1,
        kind="data_ref",
        label="x",
        verdict=_owned(exit_candidate="art_elsewhere"),
        rendered_artifact_ids={"art_other"},
    )
    assert row["access_state"] == "linked_labeled"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (Verdict(Attribution.FOREIGN, "foreign_artifact"), "linked_restricted"),
        (Verdict(Attribution.MISSING, "artifact_missing"), "linked_missing"),
        (Verdict(Attribution.UNSUPPORTED, "unsupported_kind"), "unsupported"),
        (None, "linked_missing"),  # Missing verdict fails closed to missing, never OWNED.
    ],
)
def test_presentation_non_owned_releases_nothing(verdict, expected) -> None:
    row = citation_presentation(
        seq=3,
        kind="data_ref",
        label="不该泄漏的标签",
        verdict=verdict,
        rendered_artifact_ids={"art_a"},
    )
    assert row == {"seq": 3, "kind": "data_ref", "access_state": expected}


# ---------------------------------------------------------------- Sidecar (fake stores call helper directly).


class _SessionStore:
    def __init__(self, citations: dict[str, list[tuple[int, str, str | None, str]]], turn_ids: list[str]) -> None:
        self._citations = citations
        self._turn_ids = turn_ids

    async def list_turn_citations(self, *, turn_id: str) -> list[tuple[int, str, str | None, str]]:
        return list(self._citations.get(turn_id, []))

    async def list_turns(self, *, session_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=t) for t in self._turn_ids]


class _ArtifactStore:
    def __init__(self, artifacts: list[SimpleNamespace]) -> None:
        self._by_id = {a.id: a for a in artifacts}

    async def get_many(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        return [self._by_id[a] for a in artifact_ids if a in self._by_id]


class _CanvasStore:
    def __init__(self, canvas_by_section: dict[str, str]) -> None:
        self._map = canvas_by_section

    async def get_section_canvas_id(self, section_id: str) -> str | None:
        return self._map.get(section_id)


def _request(session_store, artifact_store=None, canvas_store=None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_store=session_store,
                artifact_store=artifact_store,
                canvas_store=canvas_store,
                kb_extracted_rows_store=None,
            )
        )
    )


def _section(turn_id: str | None, *, hidden: bool = False, payload: dict | None = None, kind: str = "narrative") -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        payload=payload or {"markdown": "text"},
        turn_id=turn_id,
        hidden_for_chat_mode=hidden,
        superseded=False,
        quarantined=False,
    )


_CANVAS = SimpleNamespace(id="cv_1", session_id="s_1", workspace_id="ws_1")


@pytest.mark.asyncio
async def test_sidecar_owned_vs_foreign_paired() -> None:
    """同一轮里：会话产物 → OWNED 带 label；外来产物 → restricted 只有三元组。"""
    session_store = _SessionStore(
        {
            "t_1": [
                (1, "art_mine", "bi_query: 营业额", "data_ref"),
                (2, "art_theirs", "别人的标签", "data_ref"),
                (3, "sec_here", "客单价Top3", "chart_section"),
            ]
        },
        turn_ids=["t_1"],
    )
    artifact_store = _ArtifactStore(
        [
            SimpleNamespace(id="art_mine", created_by_turn="t_1", canvas_id=None),
            SimpleNamespace(id="art_theirs", created_by_turn="t_other", canvas_id="cv_other"),
        ]
    )
    canvas_store = _CanvasStore({"sec_here": "cv_1"})
    sections = [_section("t_1")]
    out = await _turn_citations_sidecar(_request(session_store, artifact_store, canvas_store), _CANVAS, sections, source_refs=[])
    rows = {r["seq"]: r for r in out["t_1"]}
    # Current-turn artifact ownership admits the same byte access in event and
    # detail surfaces. Opening the canvas must not downgrade a live citation.
    assert rows[1]["access_state"] == "linked_accessible"
    assert rows[1]["safe_label"] == "bi_query: 营业额"
    assert rows[2] == {"seq": 2, "kind": "data_ref", "access_state": "linked_restricted"}  # Paired: strip everything.
    assert rows[3]["access_state"] == "linked_labeled"
    assert rows[3]["safe_anchor_id"] == "sec_here"


@pytest.mark.asyncio
async def test_sidecar_cross_turn_session_artifact_stays_labeled() -> None:
    """review F7 边界：会话内**跨 turn**产物（created_by_turn=别的 turn）OWNED 但
    不走本 turn 自产出口臂 ⇒ 仍 labeled——出口臂按引用所在 turn 严格判。"""
    session_store = _SessionStore(
        {"t_1": [(1, "art_prev", "上一轮的表", "data_ref")]},
        turn_ids=["t_1", "t_prev"],
    )
    artifact_store = _ArtifactStore([SimpleNamespace(id="art_prev", created_by_turn="t_prev", canvas_id=None)])
    canvas_store = _CanvasStore({})
    sections = [_section("t_1")]
    out = await _turn_citations_sidecar(_request(session_store, artifact_store, canvas_store), _CANVAS, sections, source_refs=[])
    rows = {r["seq"]: r for r in out["t_1"]}
    assert rows[1]["access_state"] == "linked_labeled"
    assert rows[1]["safe_label"] == "上一轮的表"


@pytest.mark.asyncio
async def test_sidecar_accessible_requires_rendered_closure() -> None:
    """artifact 被段落 typed 字段引用（进 rendered closure）⇒ linked_accessible。"""
    session_store = _SessionStore({"t_1": [(1, "art_chart", "图的数据", "data_ref")]}, turn_ids=["t_1"])
    artifact_store = _ArtifactStore([SimpleNamespace(id="art_chart", created_by_turn="t_1", canvas_id=None)])
    sections = [
        _section("t_1"),
        _section("t_1", kind="chart", payload={"spec": {"data_artifact_id": "art_chart"}}),
    ]
    out = await _turn_citations_sidecar(_request(session_store, artifact_store), _CANVAS, sections, source_refs=[])
    assert out["t_1"][0]["access_state"] == "linked_accessible"


@pytest.mark.asyncio
async def test_sidecar_skips_hidden_turns_and_handles_absent_store() -> None:
    session_store = _SessionStore({"t_hidden": [(1, "art_x", "x", "data_ref")]}, turn_ids=["t_hidden"])
    # Only hidden sections in this turn ⇒ FE renders no body, so sidecar omits it.
    out = await _turn_citations_sidecar(_request(session_store), _CANVAS, [_section("t_hidden", hidden=True)], source_refs=[])
    assert out == {}
    # Missing session_store ⇒ empty (inner fail-soft layer).
    out = await _turn_citations_sidecar(_request(None), _CANVAS, [_section("t_1")], source_refs=[])
    assert out == {}


@pytest.mark.asyncio
async def test_sidecar_cap_truncates() -> None:
    rows = [(i + 1, f"art_{i}", "L", "data_ref") for i in range(513)]
    session_store = _SessionStore({"t_1": rows}, turn_ids=["t_1"])
    out = await _turn_citations_sidecar(_request(session_store, _ArtifactStore([])), _CANVAS, [_section("t_1")], source_refs=[])
    assert len(out["t_1"]) == 512


@pytest.mark.asyncio
async def test_session_store_batch_citations_single_query_contract(tmp_path) -> None:
    """review F7：detail 热读端点用单条 IN 批量查询替代逐 turn N+1。真 SQLite 验契约。"""
    import sqlite3

    from bowhead_core.session_store import SessionStore

    db_path = str(tmp_path / "s.db")
    store = SessionStore(db_path)
    await store.initialize()
    await store.ensure_session(session_id="s1", workspace_id="w", user_id="u1")
    conn = sqlite3.connect(db_path)  # turn_citations FK: seed parent turns first.
    for i, tid in enumerate(("t_1", "t_2"), start=1):
        conn.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES (?,?,?,?,?)",
            (tid, "s1", i, "q", "2026-08-27T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()
    await store.record_turn_citations(
        turn_id="t_1",
        citations=[(1, "art_a", "L1", "data_ref"), (2, "sec_b", "客单价Top3", "chart_section")],
    )
    await store.record_turn_citations(turn_id="t_2", citations=[(1, "art_c", None, "data_ref")])
    out = await store.list_turn_citations_for_turns(turn_ids=["t_1", "t_2", "t_ghost", "t_1"])
    assert out["t_1"] == [(1, "art_a", "L1", "data_ref"), (2, "sec_b", "客单价Top3", "chart_section")]
    assert out["t_2"] == [(1, "art_c", None, "data_ref")]
    assert out["t_ghost"] == []


@pytest.mark.asyncio
async def test_sidecar_flag_gate_and_batch_path_through_real_route(tmp_path, initialize_migrated_core_database) -> None:
    """review F4/F7 residual 收口：flag-ON 的 sidecar 真路由全链（真 SessionStore 的
    **批量分支**被 sidecar 穿过，不只被 store 契约测试盖）；flag 缺省 OFF ⇒ null。"""
    import os
    from unittest import mock

    import httpx2
    from bowhead_core.session_store import SessionStore
    from bowhead_gateway.main import create_app
    from bowhead_gateway.settings import Settings

    from bowhead_core import CanvasStore

    db = tmp_path / "t.db"
    canvas_store = CanvasStore(db)
    await canvas_store.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.canvas_store = canvas_store
    app.state.artifact_fetch_secret = "test-artifact-fetch-secret"
    app.state.orchestrator = None
    app.state.datarc = None
    initialize_migrated_core_database(tmp_path / "s.db")
    session_store = SessionStore(str(tmp_path / "s.db"))
    await session_store.initialize()
    app.state.session_store = session_store

    uid = app.state.settings.dev_default_user
    canvas = await canvas_store.ensure_canvas(session_id="s_cite", workspace_id="w", user_id=uid)
    sec = await canvas_store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "客单价领先。 [cite:1]"},
        by_turn_id="t_cite",
    )
    await session_store.ensure_session(session_id="s_cite", workspace_id="w", user_id=uid)
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute(
        "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES (?,?,?,?,?)",
        ("t_cite", "s_cite", 1, "q", "2026-08-27T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    # chart_section references an existing section in this canvas → linked_labeled + anchor.
    await session_store.record_turn_citations(
        turn_id="t_cite",
        citations=[(1, sec.id, "客单价Top3城市", "chart_section")],
    )

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        # Explicit flag OFF (rollback): turnCitations always null.
        with mock.patch.dict(os.environ, {"FEATURE_SOURCE_ATTRIBUTION_V2": "0"}):
            r_off = await c.get("/api/v1/canvas/by-session/s_cite")
        assert r_off.status_code == 200
        assert r_off.json()["turnCitations"] is None
        # Default (unset) is on since 2026-10-14.
        with mock.patch.dict(os.environ):
            os.environ.pop("FEATURE_SOURCE_ATTRIBUTION_V2", None)
            r_on = await c.get("/api/v1/canvas/by-session/s_cite")
    assert r_on.status_code == 200
    rows = r_on.json()["turnCitations"]["t_cite"]
    assert rows == [
        {
            "seq": 1,
            "kind": "chart_section",
            "access_state": "linked_labeled",
            "safe_label": "客单价Top3城市",
            "safe_anchor_id": sec.id,
        }
    ]
