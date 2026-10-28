"""writer-loop 收尾必须把画布排成展示规范序（116 prod 1.1.28 dogfood P1）。

实测：最近 25 个画布里，**有 h1 的 22 个中 11 个（50%）标题被正文压在后面**，
形状每次都一样：`code_block* → chart* → narrative → h1`。用户看到的报告标题在正文中间，
导出的 Word/PDF/Excel 同样如此。

根因不是没人想过排序 —— `canvas_rank` 的模块注释把理由写得清清楚楚：
「writer-loop 在 compose 期才 emit 头部(eyebrow/h1/meta)，seq 晚于 peer-loop 期落库的
code_block / 裸 chart，不重排则 python 代码块领跑报告」。**但它点名的那条路一次都没调它**：
`canonical_order` 只接在确定性 composer 上，writer-loop 侧全无。
而 `FEATURE_REPORT_EDIT_SYNC_V2=1`（116 已开）让前端与导出器信任库里的 seq，错序被原样渲染。
"""

from __future__ import annotations

from typing import Any

from bowhead_core.report_writer_loop import _apply_canonical_order


class _Sec:
    def __init__(self, sid: str, kind: str) -> None:
        self.id, self.kind = sid, kind


class _Store:
    def __init__(self, sections: list[_Sec], manual_reorder: bool = False) -> None:
        self._sections = sections
        self.reordered: list[str] | None = None
        self._manual_reorder = manual_reorder

    async def list_sections(self, canvas_id: str) -> list[_Sec]:
        return self._sections

    async def has_manual_reorder(self, canvas_id: str) -> bool:
        # canvas-elements ordering batch: canonical order yields to user-reordered canvases.
        return self._manual_reorder

    async def reorder_sections(self, *, canvas_id: str, ordered_ids: list[str], by_turn_id: Any = None) -> None:
        self.reordered = ordered_ids


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


# Actual bad-order shape on 116 (11 canvases, including cv_3bcd47f1caf2, share this shape).
_PROD_SHAPE = [
    _Sec("c1", "code_block"),
    _Sec("c2", "code_block"),
    _Sec("g1", "chart"),
    _Sec("g2", "chart"),
    _Sec("n1", "narrative"),
    _Sec("h", "h1"),
    _Sec("m", "meta"),
    _Sec("k", "kpi_row"),
    _Sec("s", "sources"),
]


async def test_title_moves_ahead_of_body() -> None:
    """★ 核心：标题必须领跑，python 代码块沉底。"""
    store, bus = _Store(list(_PROD_SHAPE)), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t_x")
    assert store.reordered is not None, "错序画布竟然没被重排"
    order = store.reordered
    assert order.index("h") < order.index("n1"), f"标题仍在正文之后: {order}"
    assert order.index("h") < order.index("g1"), f"标题仍在图表之后: {order}"
    assert order.index("c1") > order.index("n1"), f"代码块没沉底: {order}"
    # Header blocks lead (eyebrow→h1→meta→kpi_row); body then retains **original relative order**:
    # chart precedes narrative in the input, so stable sorting must not swap them.
    assert order[:3] == ["h", "m", "k"], order
    assert order.index("g1") < order.index("g2") < order.index("n1"), order
    assert order[-2:] == ["c1", "c2"], order  # Code appendix sinks to the bottom in its original order.


async def test_reorder_event_emitted() -> None:
    """前端靠 `canvas.reordered` 实时跟上，不能只改库不发事件。"""
    store, bus = _Store(list(_PROD_SHAPE)), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t_x")
    assert bus.events, "没发 canvas.reordered"


async def test_already_ordered_is_noop() -> None:
    """★ 反向闸：已经是规范序就不许重排、不许发事件（否则每轮都白发一次 reorder）。"""
    ordered = [_Sec("h", "h1"), _Sec("m", "meta"), _Sec("k", "kpi_row"), _Sec("n1", "narrative"), _Sec("c1", "code_block")]
    store, bus = _Store(ordered), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t_x")
    assert store.reordered is None
    assert not bus.events


async def test_stable_within_same_rank() -> None:
    """★ 同 rank 内必须保持相对序——`narrative→[figure:]` 的织入序和多写手章序靠它。"""
    secs = [_Sec("h", "h1"), _Sec("n1", "narrative"), _Sec("n2", "narrative"), _Sec("n3", "narrative")]
    store, bus = _Store(secs), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t_x")
    order = store.reordered or [s.id for s in secs]
    assert order.index("n1") < order.index("n2") < order.index("n3"), order


async def test_empty_canvas_is_safe() -> None:
    store, bus = _Store([]), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t_x")
    assert store.reordered is None


async def test_store_failure_does_not_raise() -> None:
    """★ best-effort：排序失败不能把一次已经写成的报告带崩。"""

    class _Boom(_Store):
        async def list_sections(self, canvas_id: str) -> list[_Sec]:
            raise RuntimeError("db down")

    await _apply_canonical_order(_Boom([]), _Bus(), "cv_x", "t_x")  # No exception = pass.
