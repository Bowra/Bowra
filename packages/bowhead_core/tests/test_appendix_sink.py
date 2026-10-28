"""代码块沉底（canvas-elements，`FEATURE_CANVAS_APPENDIX_SINK` 默认开）。

权威=`docs/sprints/canvas-elements-2026-09-04/tech-design-appendix-sink.md`。

设计 v1 的判据里有一半是**空真断言**（「只有 code 移动」「不发事件」在压根没排序时
全都成立），所以这里的期望序一律**写死在测试里**，不调 `canonical_order` 算出来跟自己比。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from bowhead_core.canvas_rank import canonical_rank
from bowhead_core.canvas_store import MACHINE_REORDER_TURN_ID
from bowhead_core.report_writer_loop import (
    _apply_canonical_order,
    compose_report,
)

pytestmark = pytest.mark.unit


class _Sec:
    def __init__(self, sid: str, kind: str, superseded: bool = False) -> None:
        self.id, self.kind, self.superseded = sid, kind, superseded


class _Store:
    def __init__(self, sections: list[_Sec], manual_reorder: bool = False) -> None:
        self._sections = sections
        self.reordered: list[str] | None = None
        self.reorder_turns: list[Any] = []
        self._manual_reorder = manual_reorder

    async def list_sections(self, canvas_id: str) -> list[_Sec]:
        return self._sections

    async def has_manual_reorder(self, canvas_id: str) -> bool:
        return self._manual_reorder

    async def reorder_sections(self, *, canvas_id: str, ordered_ids: list[str], by_turn_id: Any = None) -> None:
        self.reordered = ordered_ids
        self.reorder_turns.append(by_turn_id)


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


# ── 1. Sort order: sources at the rear, code even later ───────────────────────


def test_sources_ranks_between_body_and_appendix() -> None:
    assert canonical_rank("narrative") < canonical_rank("sources") < canonical_rank("code_block")


# ── 2. Across turns: previous code blocks sink past current body to the end (§2.3 contract) ──


@pytest.mark.anyio
async def test_prior_turn_code_sinks_past_this_turn_body() -> None:
    """★ v1 §2.4 把这件事说反了。

    `promote_document_chrome` 把**本轮** chrome 提到整张画布最前 ⇒ 排序看到的
    第一个 h1 是本轮的、上一轮的 h1 成为分段点 ⇒ 上一轮的代码块沉到整份文档末尾。
    这是口径 C 的取向选择，写成契约钉住；期望序写死。
    """
    sections = [
        # Current-turn chrome (chrome promotion already moved it to the front)
        _Sec("h2", "h1"),
        _Sec("m2", "meta"),
        # Previous-turn report
        _Sec("h1", "h1"),
        _Sec("n1", "narrative"),
        _Sec("g1", "chart"),
        _Sec("c1", "code_block"),
        _Sec("s1", "sources"),
        # Current-turn body
        _Sec("n2", "narrative"),
        _Sec("c2", "code_block"),
    ]
    store, bus = _Store(sections), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t2")

    assert store.reordered == ["h2", "m2", "h1", "n1", "g1", "n2", "s1", "c1", "c2"]
    assert store.reordered.index("c1") > store.reordered.index("n2"), "上一轮的代码块要跨过本轮正文"
    assert store.reordered.index("n1") < store.reordered.index("g1"), "上一轮正文与图的相对序不变"


# ── 3. Collapsed sections do not participate in segmentation (collapse enabled on 116) ──


@pytest.mark.anyio
async def test_superseded_h1_does_not_create_a_segment_boundary() -> None:
    """`list_sections` 不滤 superseded，而 `_segment_bounds` 数所有 h1。

    不滤掉的话，折叠旧报告的 h1 会切出一段，本轮代码块只沉到它之前 = 沉底部分失效。
    """
    sections = [
        _Sec("old_h", "h1", superseded=True),
        _Sec("old_n", "narrative", superseded=True),
        _Sec("h", "h1"),
        _Sec("n", "narrative"),
        _Sec("c", "code_block"),
        _Sec("n2", "narrative"),
    ]
    store, bus = _Store(sections), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t2")

    # Collapsed sections **stay in place**; visible sections refill their own slots in canonical order.
    assert store.reordered == ["old_h", "old_n", "h", "n", "n2", "c"]
    assert store.reordered.index("c") == len(store.reordered) - 1, "代码块沉到末尾"


@pytest.mark.anyio
async def test_superseded_sections_are_not_relocated_to_the_tail() -> None:
    """★ code-review #3：`reorder_sections` 是「传入的排前面 + 未列出者尾随」，
    只传可见段会把整段折叠历史搬到当前报告**后面**（每轮一次）。

    这条钉的是「必须传全序」：折叠段的**下标**不许变。
    """
    sections = [
        _Sec("old_h", "h1", superseded=True),
        _Sec("old_n", "narrative", superseded=True),
        _Sec("c", "code_block"),
        _Sec("h", "h1"),
        _Sec("n", "narrative"),
    ]
    store, bus = _Store(sections), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", "t2")

    assert store.reordered is not None
    assert store.reordered.index("old_h") == 0, "折叠的旧报告不许被搬到当前报告后面"
    assert store.reordered.index("old_n") == 1
    assert store.reordered == ["old_h", "old_n", "h", "n", "c"]


@pytest.mark.anyio
async def test_machine_reorder_never_writes_a_null_turn() -> None:
    """★ code-review #5：机器重排若留下 NULL `by_turn_id`，`has_manual_reorder`
    会把它当成「用户拖过」——服务端自己把自己永久锁死。"""
    store, bus = _Store([_Sec("c", "code_block"), _Sec("n", "narrative")]), _Bus()
    await _apply_canonical_order(store, bus, "cv_x", None)
    assert store.reorder_turns == [MACHINE_REORDER_TURN_ID]


# ── 4. User manual-order guard (negative assertions need a same-fixture positive control) ──


@pytest.mark.anyio
async def test_manual_reorder_skips_sorting() -> None:
    shape = [_Sec("c", "code_block"), _Sec("n", "narrative")]
    guarded = _Store(list(shape), manual_reorder=True)
    bus = _Bus()
    await _apply_canonical_order(guarded, bus, "cv_x", "t1")
    assert guarded.reordered is None, "用户调过顺序 ⇒ 不重排"
    assert bus.events == [], "也不发事件"

    # ★ Positive control: same shape with only the guard disabled; must actually reorder and emit events
    # (otherwise the no-reorder assertion also passes when wiring is entirely absent).
    free = _Store(list(shape), manual_reorder=False)
    bus2 = _Bus()
    await _apply_canonical_order(free, bus2, "cv_x", "t1")
    assert free.reordered == ["n", "c"]
    assert len(bus2.events) == 1


# ── 5. Flag gates only the thin wrapper call; never check it inside the helper ──


# ── 6. Wiring: thin wrapper really calls it, ordered chrome → sorting ──────────


def test_wrapper_promotes_chrome_then_sorts() -> None:
    """★ 钉调用点。前面几条测的都是 helper 本身——把薄封装里那两行删掉，它们照样全绿。

    正对照不能照抄 `test_writer_loop_canonical_order.py:133` 的 `>100 行`
    （`compose_report` 只有约 80 行，那个阈值直接失败），改成断言它确实是那个薄封装。
    """
    src = inspect.getsource(compose_report)
    assert "_compose_report_impl(" in src, "compose_report 不再是薄封装了——判据先失效了"
    assert "_promote_document_chrome_best_effort(" in src, "chrome 提升没收进薄封装"
    assert "_apply_canonical_order(" in src, "薄封装没有调规范排序——代码块不会沉底"
    assert src.index("_promote_document_chrome_best_effort(") < src.index("_apply_canonical_order("), (
        "顺序反了：chrome 提升要在排序之前——排序的分段判据读的就是第一个 h1 的位置"
    )


def test_impl_exits_no_longer_promote_chrome_individually() -> None:
    """收进薄封装之后，四条出口里不许再各挂一份（挂两次是幂等的，但会掩盖漏挂）。"""
    from bowhead_core import report_writer_loop as rwl

    impl_src = inspect.getsource(rwl._compose_report_impl)
    assert "_promote_document_chrome_best_effort(" not in impl_src


# ── 7. Real store: the guard SQL itself (stubs cannot test wiring) ─────────────


@pytest.fixture
async def real_store(tmp_path):
    from bowhead_core import CanvasStore

    st = CanvasStore(tmp_path / "sink.db")
    await st.initialize()
    return st


@pytest.mark.integration
async def test_has_manual_reorder_reads_the_real_ledger(real_store) -> None:
    """★ 上面几条都把 store stub 掉了——SQL 写错列名照样全绿。这条走真库。

    三来源对照（code-review #4/#5 的判据）：
      用户面 reorder（无 turn）→ True；机器 reorder（带 turn）→ False；作业哨兵 → False。
    """
    canvas = await real_store.ensure_canvas(session_id="s_sink", workspace_id="ws_1", user_id="usr_1")
    a = await real_store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "a"}, by_turn_id="t1")
    b = await real_store.append_section(canvas_id=canvas.id, kind="code_block", payload={"code": "x"}, by_turn_id="t1")

    assert await real_store.has_manual_reorder(canvas.id) is False, "什么都没发生时不该判成用户拖过"

    # Machine reorder (with turn): negative control
    await real_store.reorder_sections(canvas_id=canvas.id, ordered_ids=[b.id, a.id], by_turn_id="t1")
    assert await real_store.has_manual_reorder(canvas.id) is False

    # Batch-job sentinel: negative control (previously wrote by_turn_id=None, permanently locking the canvas after one run)
    await real_store.reorder_sections(canvas_id=canvas.id, ordered_ids=[a.id, b.id], by_turn_id=MACHINE_REORDER_TURN_ID)
    assert await real_store.has_manual_reorder(canvas.id) is False

    # User-facing `/reorder` (no turn): positive control
    await real_store.reorder_sections(canvas_id=canvas.id, ordered_ids=[b.id, a.id], by_turn_id=None)
    assert await real_store.has_manual_reorder(canvas.id) is True


@pytest.mark.integration
async def test_apply_canonical_order_on_a_real_store(real_store) -> None:
    """端到端：真 store 上跑一次排序，代码块沉底且 seq 真的重编。"""
    canvas = await real_store.ensure_canvas(session_id="s_sink2", workspace_id="ws_1", user_id="usr_1")
    code = await real_store.append_section(canvas_id=canvas.id, kind="code_block", payload={"code": "x"}, by_turn_id="t1")
    chart = await real_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    title = await real_store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "T"}, by_turn_id="t1")

    await _apply_canonical_order(real_store, _Bus(), canvas.id, "t1")

    got = [s.id for s in await real_store.list_sections(canvas.id)]
    assert got == [title.id, chart.id, code.id]
    seqs = [s.seq for s in await real_store.list_sections(canvas.id)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3, "seq 整份重编且不撞车"
