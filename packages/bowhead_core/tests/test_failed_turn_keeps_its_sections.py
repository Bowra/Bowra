"""轮次失败时，本轮产出的 section 不能被「chat 模式」隐藏掉。

隐藏的**前提**是「这些图会内联进对话气泡」——chat 模式下画布让位给对话。
轮次失败时对话侧根本没渲染过终答，前提不成立，于是两边都没有。

116 dogfood 2026-09-24 `t_7f66c0d681c6`：画了 2 张图 + 2 段代码，4 个 section 全被
打上 `hidden_for_chat_mode=1`（理由 `chat-mode (no insight/recs/exec-narrative): chart×2,code_block×2`），
而那一轮撞 wallclock 失败 ⇒ 图既不在画布也不在对话，用户完全够不着，
20 分钟的取数与出图白跑。真机复核过：`?report=1` 整页空白，点「成果」栏也是空的。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bowhead_core.agents import lead_orchestrator as lo
from bowhead_core.canvas_store import CanvasStore


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:  # noqa: ANN001
        self.events.append(ev)


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "w.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


async def _seed_two_charts(store: CanvasStore, turn_id: str) -> list:
    cv = await store.get_canvas_by_session("s1")
    for i in range(2):
        await store.append_section(
            canvas_id=cv.id,
            kind="chart",
            payload={"spec": {"kind": "line", "title": f"图{i}", "rows": [{"x": 1, "y": 2}]}},
            by_turn_id=turn_id,
            owner="sub_x",
        )
    return [s for s in await store.list_sections(cv.id) if s.turn_id == turn_id]


def _bus_with_decision(store: CanvasStore, sections: list, turn_id: str) -> _Bus:
    bus = _Bus()
    bus._terminal_surface_decision = {  # type: ignore[attr-defined]
        "store": store,
        "artifact_store": None,
        "turn_id": turn_id,
        "sections": sections,
        "has_deliverable": False,
        "response_kind": "chat",
        "rationale": "chat-mode (no insight/recs/exec-narrative): chart×2",
    }
    bus._d6_session_id = "s1"  # type: ignore[attr-defined]
    bus._d6_user_id = "u"  # type: ignore[attr-defined]
    return bus


async def _visible_count(store: CanvasStore, turn_id: str) -> int:
    """画布列表里还看得见几段——`list_sections` 本身就会滤掉 `hidden_for_chat_mode`，
    所以这正是「用户打开画布能看到什么」的口径（数 hidden 标志反而数不到：那些行不返回）。"""
    cv = await store.get_canvas_by_session("s1")
    return sum(1 for s in await store.list_sections(cv.id) if s.turn_id == turn_id)


@pytest.mark.parametrize("status", ["failed", "error"])
async def test_a_failed_turn_keeps_its_charts_on_the_canvas(store: CanvasStore, status: str) -> None:
    """★ 两种 status 写法都要认：`turn_runner` 发 "failed"，Lead 自己合成的 done 发 "error"。"""
    sections = await _seed_two_charts(store, "t_fail")
    bus = _bus_with_decision(store, sections, "t_fail")

    kind = await lo._apply_terminal_surface_decision(bus, status=status)

    assert kind == "canvas", "失败轮必须把画布留给用户"
    assert await _visible_count(store, "t_fail") == 2, "失败轮的 section 被隐藏了 —— 用户两边都看不到"


async def test_a_successful_chat_turn_still_hides_them(store: CanvasStore) -> None:
    """★ 负对照：成功的 chat 轮照旧隐藏。对话气泡会内联这两张图，
    不隐藏就会「对话里一份、画布里一份」渲两遍。"""
    sections = await _seed_two_charts(store, "t_ok")
    bus = _bus_with_decision(store, sections, "t_ok")

    await lo._apply_terminal_surface_decision(bus, status="completed")

    assert await _visible_count(store, "t_ok") == 0, "成功的 chat 轮本该隐藏"


async def test_no_status_behaves_like_before(store: CanvasStore) -> None:
    """★ 第二个负对照：调用方没传 status（老调用点 / 重放）时必须是**原行为**，
    不能因为「拿不到状态」就一律保留 —— 那会让每个 chat 轮都渲两遍。"""
    sections = await _seed_two_charts(store, "t_none")
    bus = _bus_with_decision(store, sections, "t_none")

    await lo._apply_terminal_surface_decision(bus)

    assert await _visible_count(store, "t_none") == 0


async def test_the_call_site_passes_the_status_through() -> None:
    """★ 接线守卫：纯判据再对，调用点不传 status 就恒走老路（突变实测会存活）。"""
    import inspect

    src = inspect.getsource(lo)
    assert '_apply_terminal_surface_decision(bus, status=payload.get("status"))' in src
