"""chat-truth B-5.5: composer chart auto-build fallback UT.

Tests _try_auto_build_chart_section + the integration into compose_canvas_from_payload's
chart_section_id strip block. Covers:
  - hint heuristic from caption text
  - successful auto-build when artifact present + valid CSV
  - fallback to caption-only when artifact missing
  - fallback when CSV parse fails
  - fallback when data_artifact_id absent
  - fallback when artifact_store not passed (back-compat)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.canvas_composer import (
    _hint_from_caption,
    _try_auto_build_chart_section,
)

# ─── hint heuristic ──────────────────────────────────────────────────


def test_hint_trend_keywords() -> None:
    assert _hint_from_caption("GMV 月度趋势") == "trend"
    assert _hint_from_caption("Q1 同比走势") == "trend"
    assert _hint_from_caption("各月营收") == "trend"


def test_hint_rank_keywords() -> None:
    assert _hint_from_caption("品牌销售排名") == "rank"
    assert _hint_from_caption("top 10 客户") == "rank"
    assert _hint_from_caption("各品类大到小") == "rank"


def test_hint_share_keywords() -> None:
    assert _hint_from_caption("品类构成占比") == "share"
    assert _hint_from_caption("share 分布") == "share"


def test_hint_compare_keywords() -> None:
    assert _hint_from_caption("Q1 vs Q2 对比") == "compare"
    # "compare" keyword direct hit
    assert _hint_from_caption("compare A and B") == "compare"


def test_hint_none_when_no_keyword() -> None:
    assert _hint_from_caption("一些数据") is None
    assert _hint_from_caption("") is None


# ─── _try_auto_build_chart_section ───────────────────────────────────


@dataclass
class _StubArtifact:
    path: str
    backend: Any
    created_by: str = "u_dev_alice"


class _StubBackend:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.read = AsyncMock(return_value=data)


class _StubArtifactStore:
    def __init__(self, artifact: Any | None = None) -> None:
        self._artifact = artifact

    async def get(self, aid: str, check_created_by: str | None = None) -> Any:
        return self._artifact


@dataclass
class _StubSection:
    id: str
    seq: int

    def to_report_section(self) -> dict:
        return {"id": self.id, "kind": "chart", "seq": self.seq}


class _StubCanvasStore:
    def __init__(self) -> None:
        self.append_section = AsyncMock(
            return_value=_StubSection(id="sec_auto_built_1", seq=42),
        )


class _StubBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_auto_build_succeeds_with_real_csv() -> None:
    """Valid CSV → composer builds chart section successfully."""
    csv_data = b"category,sales\nA,100\nB,200\nC,150\n"
    backend = _StubBackend(csv_data)
    artifact = _StubArtifact(path="/tmp/x.csv", backend=backend)

    artifact_store = MagicMock()
    artifact_store.get = AsyncMock(return_value=artifact)
    artifact_store.backend = backend

    canvas_store = _StubCanvasStore()
    bus = _StubBus()

    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        data_artifact_id="art_xyz",
        caption="品牌销售排名",
        user_id="u_dev_alice",
        turn_id="t_test",
        bus=bus,
    )
    assert new_sid == "sec_auto_built_1"
    # canvas_store.append_section called with chart kind
    _args, kwargs = canvas_store.append_section.call_args
    assert kwargs["kind"] == "chart"
    assert kwargs["canvas_id"] == "canvas_1"
    assert kwargs["by_turn_id"] == "t_test"


@pytest.mark.asyncio
async def test_auto_build_returns_none_when_artifact_missing() -> None:
    """artifact_store.get returns None → fallback to None (caption-only)."""
    artifact_store = MagicMock()
    artifact_store.get = AsyncMock(return_value=None)

    canvas_store = _StubCanvasStore()
    bus = _StubBus()

    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        data_artifact_id="art_missing",
        caption="某图",
        user_id="u_x",
        turn_id="t_test",
        bus=bus,
    )
    assert new_sid is None
    canvas_store.append_section.assert_not_called()


@pytest.mark.asyncio
async def test_auto_build_returns_none_on_empty_csv() -> None:
    """Empty CSV → no rows → fallback to None."""
    csv_data = b""
    backend = _StubBackend(csv_data)
    artifact = _StubArtifact(path="/tmp/x.csv", backend=backend)

    artifact_store = MagicMock()
    artifact_store.get = AsyncMock(return_value=artifact)
    artifact_store.backend = backend

    canvas_store = _StubCanvasStore()
    bus = _StubBus()

    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        data_artifact_id="art_empty",
        caption="空图",
        user_id="u_x",
        turn_id="t_test",
        bus=bus,
    )
    assert new_sid is None


@pytest.mark.asyncio
async def test_auto_build_skips_scalar_when_chart_shape_required() -> None:
    """Computation scalar artifacts should stay callouts, not become one-point charts."""
    csv_data = "利润 / 元\n217778083.62\n".encode()
    backend = _StubBackend(csv_data)
    artifact = _StubArtifact(path="/tmp/scalar.csv", backend=backend)

    artifact_store = MagicMock()
    artifact_store.get = AsyncMock(return_value=artifact)
    artifact_store.backend = backend

    canvas_store = _StubCanvasStore()
    bus = _StubBus()

    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        data_artifact_id="art_scalar",
        caption="利润",
        user_id="u_x",
        turn_id="t_test",
        bus=bus,
        require_chart_shape=True,
    )
    assert new_sid is None
    canvas_store.append_section.assert_not_called()


@pytest.mark.asyncio
async def test_auto_build_returns_none_when_store_missing() -> None:
    """No artifact_store passed → returns None (back-compat)."""
    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=None,
        canvas_store=_StubCanvasStore(),
        data_artifact_id="art_x",
        caption="x",
        user_id="u_x",
        turn_id="t_test",
        bus=_StubBus(),
    )
    assert new_sid is None


@pytest.mark.asyncio
async def test_auto_build_returns_none_when_no_data_aid() -> None:
    """No data_artifact_id → returns None."""
    artifact_store = MagicMock()
    artifact_store.get = AsyncMock()
    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=artifact_store,
        canvas_store=_StubCanvasStore(),
        data_artifact_id="",
        caption="x",
        user_id="u_x",
        turn_id="t_test",
        bus=_StubBus(),
    )
    assert new_sid is None


@pytest.mark.asyncio
async def test_idempotency_cache_same_artifact_built_once(monkeypatch: Any) -> None:
    """P1-4: 2 insight cards 同 data_artifact_id → composer 只调 1 次 auto-build helper."""
    canvas_store = MagicMock()
    canvas_store.ensure_canvas = AsyncMock(return_value=MagicMock(id="canvas_1", title="t"))
    canvas_store.list_sections = AsyncMock(return_value=[])
    canvas_store.append_section = AsyncMock(
        return_value=_StubSection(id="sec_other", seq=1),
    )

    artifact_store = MagicMock()  # presence triggers fallback path
    bus = _StubBus()

    # Count auto-build invocations
    auto_build_calls = {"n": 0}

    async def _stub_auto_build(**kwargs: Any) -> str | None:
        auto_build_calls["n"] += 1
        return "sec_auto_built_shared"

    monkeypatch.setattr(
        "bowhead_core.canvas_composer._try_auto_build_chart_section",
        _stub_auto_build,
    )
    # Stub section builder: 2 insight cards with shared data_artifact_id
    monkeypatch.setattr(
        "bowhead_core.canvas_composer._build_sections_from_payload",
        lambda *_a, **_k: [
            {"kind": "insight_card", "title": "i1", "evidence": [{"kind": "chart", "data_artifact_id": "art_shared", "caption": "排名"}]},
            {"kind": "insight_card", "title": "i2", "evidence": [{"kind": "chart", "data_artifact_id": "art_shared", "caption": "另一节"}]},
        ],
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer.dedup_sections_by_data_hash",
        lambda secs: secs,
    )

    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.types import TurnRequest

    req = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="test",
        turn_id="t_1",
        language_context=None,
    )
    await compose_canvas_from_payload(
        canvas_store=canvas_store,
        request=req,
        bus=bus,
        payload={"insights": [{"title": "i1"}, {"title": "i2"}]},
        artifact_store=artifact_store,
    )
    # Invariant: auto-build helper called ONCE for shared artifact (not twice)
    assert auto_build_calls["n"] == 1, f"expected 1 auto-build for shared artifact, got {auto_build_calls['n']}"


@pytest.mark.asyncio
async def test_computation_artifact_evidence_auto_builds_chart(monkeypatch: Any) -> None:
    """Regression: computation_artifact_id should render a chart when it points at data."""
    appended: list[tuple[str, dict[str, Any]]] = []
    canvas_store = MagicMock()
    canvas_store.ensure_canvas = AsyncMock(return_value=MagicMock(id="canvas_1", title="t"))
    canvas_store.list_sections = AsyncMock(return_value=[])

    async def _append_section(**kwargs: Any) -> _StubSection:
        appended.append((kwargs["kind"], kwargs.get("payload") or {}))
        return _StubSection(id=f"sec_written_{len(appended)}", seq=len(appended))

    canvas_store.append_section = AsyncMock(side_effect=_append_section)
    artifact_store = MagicMock()
    bus = _StubBus()

    auto_build_calls = {"n": 0}

    async def _stub_auto_build(**kwargs: Any) -> str | None:
        auto_build_calls["n"] += 1
        assert kwargs["data_artifact_id"] == "art_comp"
        assert kwargs["require_chart_shape"] is True
        return "sec_auto_comp"

    monkeypatch.setattr(
        "bowhead_core.canvas_composer._try_auto_build_chart_section",
        _stub_auto_build,
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer._build_sections_from_payload",
        lambda *_a, **_k: [
            {
                "kind": "insight_card",
                "title": "i1",
                "evidence": [{"kind": "computation", "computation_artifact_id": "art_comp", "caption": "利润月度走势"}],
            },
        ],
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer.dedup_sections_by_data_hash",
        lambda secs: secs,
    )

    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.types import TurnRequest

    req = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="test",
        turn_id="t_1",
        language_context=None,
    )
    await compose_canvas_from_payload(
        canvas_store=canvas_store,
        request=req,
        bus=bus,
        payload={"insights": [{"title": "i1"}]},
        artifact_store=artifact_store,
    )

    assert auto_build_calls["n"] == 1
    markdown = "\n".join(payload.get("markdown", "") for kind, payload in appended if kind == "narrative")
    assert "[figure:sec_auto_comp]" in markdown


@pytest.mark.asyncio
async def test_chart_and_computation_auto_build_cache_is_namespaced(monkeypatch: Any) -> None:
    """Chart and computation evidence for the same artifact must not share a cache slot."""
    appended: list[tuple[str, dict[str, Any]]] = []
    canvas_store = MagicMock()
    canvas_store.ensure_canvas = AsyncMock(return_value=MagicMock(id="canvas_1", title="t"))
    canvas_store.list_sections = AsyncMock(return_value=[])

    async def _append_section(**kwargs: Any) -> _StubSection:
        appended.append((kwargs["kind"], kwargs.get("payload") or {}))
        return _StubSection(id=f"sec_written_{len(appended)}", seq=len(appended))

    canvas_store.append_section = AsyncMock(side_effect=_append_section)
    artifact_store = MagicMock()
    bus = _StubBus()
    calls: list[bool] = []

    async def _stub_auto_build(**kwargs: Any) -> str | None:
        calls.append(bool(kwargs.get("require_chart_shape")))
        return "sec_auto_comp" if kwargs.get("require_chart_shape") else "sec_auto_chart"

    monkeypatch.setattr(
        "bowhead_core.canvas_composer._try_auto_build_chart_section",
        _stub_auto_build,
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer._build_sections_from_payload",
        lambda *_a, **_k: [
            {
                "kind": "insight_card",
                "title": "i1",
                "evidence": [
                    {"kind": "chart", "data_artifact_id": "art_shared", "caption": "营业额走势"},
                    {"kind": "computation", "computation_artifact_id": "art_shared", "caption": "利润走势"},
                ],
            },
        ],
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer.dedup_sections_by_data_hash",
        lambda secs: secs,
    )

    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.types import TurnRequest

    req = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="test",
        turn_id="t_1",
        language_context=None,
    )
    await compose_canvas_from_payload(
        canvas_store=canvas_store,
        request=req,
        bus=bus,
        payload={"insights": [{"title": "i1"}]},
        artifact_store=artifact_store,
    )

    assert calls == [False, True]
    markdown = "\n".join(payload.get("markdown", "") for kind, payload in appended if kind == "narrative")
    assert "[figure:sec_auto_chart]" in markdown and "[figure:sec_auto_comp]" in markdown


@pytest.mark.asyncio
async def test_scalar_computation_evidence_stays_computation(monkeypatch: Any) -> None:
    """If computation auto-chart declines, the evidence remains a computation entry."""
    appended: list[tuple[str, dict[str, Any]]] = []
    canvas_store = MagicMock()
    canvas_store.ensure_canvas = AsyncMock(return_value=MagicMock(id="canvas_1", title="t"))
    canvas_store.list_sections = AsyncMock(return_value=[])

    async def _append_section(**kwargs: Any) -> _StubSection:
        appended.append((kwargs["kind"], kwargs.get("payload") or {}))
        return _StubSection(id=f"sec_written_{len(appended)}", seq=len(appended))

    canvas_store.append_section = AsyncMock(side_effect=_append_section)
    artifact_store = MagicMock()
    bus = _StubBus()

    async def _stub_auto_build(**kwargs: Any) -> str | None:
        assert kwargs["data_artifact_id"] == "art_scalar"
        assert kwargs["require_chart_shape"] is True
        return None

    monkeypatch.setattr(
        "bowhead_core.canvas_composer._try_auto_build_chart_section",
        _stub_auto_build,
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer._build_sections_from_payload",
        lambda *_a, **_k: [
            {
                "kind": "insight_card",
                "title": "i1",
                "evidence": [{"kind": "computation", "computation_artifact_id": "art_scalar", "caption": "利润"}],
            },
        ],
    )
    monkeypatch.setattr(
        "bowhead_core.canvas_composer.dedup_sections_by_data_hash",
        lambda secs: secs,
    )

    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.types import TurnRequest

    req = TurnRequest(
        session_id="s_1",
        workspace_id="ws_1",
        user_id="u_1",
        member_uid="m_1",
        text="test",
        turn_id="t_1",
        language_context=None,
    )
    await compose_canvas_from_payload(
        canvas_store=canvas_store,
        request=req,
        bus=bus,
        payload={"insights": [{"title": "i1"}]},
        artifact_store=artifact_store,
    )

    markdown = "\n".join(payload.get("markdown", "") for kind, payload in appended if kind == "narrative")
    assert "利润" in markdown
    assert "[figure:" not in markdown


@pytest.mark.asyncio
async def test_auto_build_emits_sse_events_on_success() -> None:
    """Successful auto-build emits section_added (compatibility for older FE)."""
    csv_data = b"month,gmv\n1,1000\n2,2000\n3,3000\n"
    backend = _StubBackend(csv_data)
    artifact = _StubArtifact(path="/tmp/x.csv", backend=backend)

    artifact_store = MagicMock()
    artifact_store.get = AsyncMock(return_value=artifact)
    artifact_store.backend = backend

    canvas_store = _StubCanvasStore()
    bus = _StubBus()

    new_sid = await _try_auto_build_chart_section(
        canvas_id="canvas_1",
        artifact_store=artifact_store,
        canvas_store=canvas_store,
        data_artifact_id="art_trend",
        caption="月度趋势",
        user_id="u_x",
        turn_id="t_test",
        bus=bus,
    )
    assert new_sid == "sec_auto_built_1"
    # bus should have emitted at least one event (section_added)
    assert any(getattr(e, "event_type", "") == "canvas.section_added" for e in bus.events)


@pytest.mark.asyncio
async def test_computation_auto_built_chart_is_anchored_in_document_projection(monkeypatch: Any) -> None:
    """默认（文档形投影开）：自动出图拿到的真 id 成为 narrative 里的块级 [figure:] 锚 + 图注。"""
    appended: list[tuple[str, dict[str, Any]]] = []
    canvas_store = MagicMock()
    canvas_store.ensure_canvas = AsyncMock(return_value=MagicMock(id="canvas_1", title="t"))
    canvas_store.list_sections = AsyncMock(return_value=[])

    async def _append_section(**kwargs: Any) -> _StubSection:
        appended.append((kwargs["kind"], kwargs.get("payload") or {}))
        return _StubSection(id=f"sec_written_{len(appended)}", seq=len(appended))

    canvas_store.append_section = AsyncMock(side_effect=_append_section)

    async def _stub_auto_build(**kwargs: Any) -> str | None:
        return "sec_auto_comp"

    monkeypatch.setattr("bowhead_core.canvas_composer._try_auto_build_chart_section", _stub_auto_build)
    monkeypatch.setattr(
        "bowhead_core.canvas_composer._build_sections_from_payload",
        lambda *_a, **_k: [
            {
                "kind": "insight_card",
                "title": "i1",
                "evidence": [{"kind": "computation", "computation_artifact_id": "art_comp", "caption": "利润月度走势"}],
            },
        ],
    )
    monkeypatch.setattr("bowhead_core.canvas_composer.dedup_sections_by_data_hash", lambda secs: secs)

    from bowhead_core.canvas_composer import compose_canvas_from_payload
    from bowhead_core.types import TurnRequest

    req = TurnRequest(session_id="s_1", workspace_id="ws_1", user_id="u_1", member_uid="m_1", text="test", turn_id="t_1", language_context=None)
    await compose_canvas_from_payload(
        canvas_store=canvas_store, request=req, bus=_StubBus(), payload={"insights": [{"title": "i1"}]}, artifact_store=MagicMock()
    )

    assert not any(kind == "insight_card" for kind, _ in appended)
    mds = [payload["markdown"] for kind, payload in appended if kind == "narrative"]
    md = next(m for m in mds if "## i1" in m)
    # `language_context=None` → English caption label (language-policy.md fallback).
    assert "[figure:sec_auto_comp]" in md and "Figure 1: 利润月度走势" in md
    # A payload without h1 / introduction does not get a separate byline section.
    assert not any(m.startswith(("*更新于", "*Updated")) for m in mds)
