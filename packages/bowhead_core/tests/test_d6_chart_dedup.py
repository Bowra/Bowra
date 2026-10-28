"""D6 — add_chart dedups same-title charts within a turn.

The D5 artifact thrash could fire add_chart twice in one turn (a 1-row scratch kpi
+ the full series), producing two chart sections with the same title. _add_chart now dedups
by title: more rows → patch the existing section in place (upgrade); else skip the degenerate
new one. Assertions key on section count + the `deduped` marker (robust to chart-kind inference).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import _add_chart

_AID = "art_0123456789ab"


@dataclass
class _Section:
    id: str
    payload: dict[str, Any]
    seq: int = 0
    kind: str = "chart"

    def to_report_section(self) -> dict[str, Any]:
        return {"id": self.id, **self.payload}


class _FakeCanvasStore:
    def __init__(self) -> None:
        self.sections: list[_Section] = []
        self._n = 0

    async def ensure_canvas(self, **_kw: Any) -> Any:
        return SimpleNamespace(id="canvas_1")

    async def append_section(self, *, canvas_id: str, kind: str, payload: dict, by_turn_id: str, owner: str | None = None) -> _Section:
        self._n += 1
        sec = _Section(id=f"sec_{self._n}", payload=payload, seq=self._n, kind=kind)
        self.sections.append(sec)
        return sec

    async def list_sections_appended_by_turn(self, turn_id: str, *, kinds: list[str] | None = None) -> list[_Section]:
        return [s for s in self.sections if kinds is None or s.kind in kinds]

    async def patch_section(self, *, section_id: str, patch: dict, **_kw: Any) -> _Section | None:
        for s in self.sections:
            if s.id == section_id:
                s.payload = {**s.payload, **patch}
                return s
        return None


class _Backend:
    def __init__(self, csv_text: str) -> None:
        self._csv = csv_text

    async def read(self, _path: str) -> bytes:
        return self._csv.encode()


class _FakeArtStore:
    def __init__(self, csv_text: str) -> None:
        self.backend = _Backend(csv_text)

    async def get(self, _aid: str, *, check_created_by: str | None = None) -> Any:
        return SimpleNamespace(path="/tmp/x.csv")


@dataclass
class _Ctx:
    canvas_store: Any
    artifact_store: Any
    turn_id: str = "t_d6"
    user_id: str = "u"
    workspace_id: str = "w"
    session_id: str = "s1"
    parent_bus: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


_SMALL = "销售额,利润\n100,20"  # 1 data row, numeric-only → kpi
_BIG = "季度,销售额\nQ1,100\nQ2,120\nQ3,90\nQ4,110"  # 4 data rows → series


async def _call(store: _FakeCanvasStore, csv_text: str, title: str) -> dict:
    ctx = _Ctx(canvas_store=store, artifact_store=_FakeArtStore(csv_text))
    return await _add_chart({"data_artifact_id": _AID, "hint": "raw", "title": title}, ctx)


@pytest.mark.asyncio
async def test_small_then_big_upgrades_in_place() -> None:
    # Non-trend title on purpose: the title-based dedup still guards duplicate
    # Same-title charts (e.g. two "各店销售额" bars). A trend title with a scalar
    # is now handled UPSTREAM by artifact reselect + the one-row-scalar safety-net
    # (see test_add_chart_validation.py::test_*trend*scalar*), which recovers
    # the real series or errors the scratch scalar before it reaches this path.
    store = _FakeCanvasStore()
    await _call(store, _SMALL, "各店销售额")
    out2 = await _call(store, _BIG, "各店销售额")
    assert len(store.sections) == 1, "same-title chart must not create a second section"
    assert out2.get("deduped") == "upgraded"
    assert store.sections[0].payload["spec"]["row_count"] == 4


@pytest.mark.asyncio
async def test_big_then_small_skips_degenerate() -> None:
    # Non-trend title — see note in test_small_then_big_upgrades_in_place.
    store = _FakeCanvasStore()
    await _call(store, _BIG, "各店销售额")
    out2 = await _call(store, _SMALL, "各店销售额")
    assert len(store.sections) == 1
    assert out2.get("deduped") == "skipped_degenerate"
    assert store.sections[0].payload["spec"]["row_count"] == 4  # richer one retained


@pytest.mark.asyncio
async def test_distinct_titles_not_deduped() -> None:
    store = _FakeCanvasStore()
    await _call(store, _BIG, "销售趋势")
    out2 = await _call(store, _BIG, "利润趋势")
    assert len(store.sections) == 2
    assert "deduped" not in out2


# ── Different titles, identical data row by row: title criteria miss all of them (observed on 116) ──


async def test_same_data_different_title_tells_the_model() -> None:
    """★★ 116 实测：612 个「同轮 ≥2 张图」的 turn 里 **77 个**有「同数据 + 同图种 +
    不同标题」，而标题也相同的是 **0 个** —— 上面那条按标题判的 dedup 对这一类一张都没拦住。

    `t_650f92d9bbb6` 实录：《种类营业额环比**衰退**TOP20》与《…**增长**TOP20》
    用同一个 artifact、同一批行，两张的第一条都是「装修配件 **+1308%**」——
    标题写「衰退榜」的那张画的是增长榜。同轮还有《近30天营业额**环比变化趋势**》
    用的是绝对营业额。

    **不丢图**：里面混着「只是重画换个标题」（浪费版面）与「标题承诺了另一种切法」
    （图在说假话）两类，机器分不开（判标题措辞是被禁的）。只把事实回给模型 ——
    它是唯一知道这张图本来想表达什么的人。
    """
    store = _FakeCanvasStore()
    await _call(store, _BIG, "种类营业额环比增长TOP20")
    out2 = await _call(store, _BIG, "种类营业额环比衰退TOP20")

    assert len(store.sections) == 2, "两张图都要留着，不能悄悄丢一张"
    assert out2.get("same_data_as") == "种类营业额环比增长TOP20"
    note = out2.get("note") or ""
    assert "同一份数据" in note and "另一种切法" in note, note


async def test_different_data_gets_no_such_note() -> None:
    """★ 负对照：数据真的不同就不该多这句 —— 否则每张图都挂一段噪音。"""
    store = _FakeCanvasStore()
    await _call(store, _BIG, "销售趋势")
    out2 = await _call(store, "季度,销售额\nQ1,999\nQ2,888\nQ3,777\nQ4,666", "利润趋势")

    assert len(store.sections) == 2
    assert "same_data_as" not in out2
    assert "同一份数据" not in (out2.get("note") or "")


async def test_same_title_still_goes_through_the_old_dedup() -> None:
    """★ 第二个负对照：同标题那一支照旧由 D6 处理（升级 / 跳过退化），
    不能被这条新判据抢走 —— 它会把「两张都留着」变成默认行为。"""
    store = _FakeCanvasStore()
    await _call(store, _SMALL, "各店销售额")
    out2 = await _call(store, _BIG, "各店销售额")

    assert len(store.sections) == 1
    assert out2.get("deduped") == "upgraded"
    assert "same_data_as" not in out2


@pytest.mark.parametrize("change", ["series", "kind", "unit", "subtitle", "sort"])
async def test_same_title_does_not_discard_a_different_chart_request(change: str) -> None:
    store = _FakeCanvasStore()
    ctx = _Ctx(canvas_store=store, artifact_store=_FakeArtStore("region,revenue,cost\nA,100,70\nB,50,40\nC,80,30"))
    args = {"data_artifact_id": _AID, "hint": "raw", "title": "Regional results", "force_kind": "bar", "series": ["revenue"]}
    first = await _add_chart(args, ctx)
    changes = {
        "series": {"series": ["cost"]},
        "kind": {"force_kind": "table"},
        "unit": {"column_units": {"revenue": "EUR"}},
        "subtitle": {"subtitle": "Prepared regional observations"},
        "sort": {"sort_by": "revenue", "limit": 2},
    }
    second = await _add_chart({**args, **changes[change]}, ctx)
    assert "error" not in first and "error" not in second
    assert second["section_id"] != first["section_id"], "A shared title must not substitute the first chart for a different requested view"
    assert len(store.sections) == 2


@pytest.mark.parametrize("chat_only", [False, True])
async def test_chart_upgrade_publishes_stored_revision_without_revealing_chat_only_figures(tmp_path: Path, chat_only: bool) -> None:
    store = CanvasStore(tmp_path / "canvas.db")
    await store.initialize()

    class Bus:
        def __init__(self) -> None:
            self.events = []

        async def emit(self, event) -> None:
            self.events.append(event)

    bus = Bus()
    ctx = _Ctx(canvas_store=store, artifact_store=_FakeArtStore(_SMALL), parent_bus=bus)
    args = {"data_artifact_id": _AID, "hint": "raw", "title": "Regional results", **({"target": "chat"} if chat_only else {})}
    await _add_chart(args, ctx)
    bus.events.clear()
    ctx.artifact_store = _FakeArtStore(_BIG)
    args.pop("target", None)
    upgraded = await _add_chart(args, ctx)
    assert upgraded.get("deduped") == "upgraded"
    stored = (await store.list_sections_appended_by_turn(ctx.turn_id, kinds=["chart"]))[0]
    assert stored.rev == 2
    if chat_only:
        assert not bus.events
        assert stored.hidden_for_chat_mode
    else:
        event = next((event for event in bus.events if event.event_type == "canvas.section_patched"), None)
        assert event is not None, "An in-place chart upgrade must reach live and replayed Canvas clients"
        assert event.payload["patch"] == stored.to_report_section()
        assert event.payload["rev"] == stored.rev


async def test_exact_chart_retry_reuses_the_existing_section() -> None:
    store = _FakeCanvasStore()
    first = await _call(store, _BIG, "Regional results")
    second = await _call(store, _BIG, "Regional results")
    assert first["section_id"] == second["section_id"]
    assert len(store.sections) == 1
