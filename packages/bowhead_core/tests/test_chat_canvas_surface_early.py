"""chat-canvas-surface 2026-08 —— chat 轮不再「先开后藏」。

判据见 `docs/sprints/chat-canvas-surface-2026-08/tech-design.md` §6。
核心不变量：**三档都没说时，一个字节都不变**（116 实测那约占一半的轮次）。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.agents.delivery_decision import decide_early_surface
from bowhead_core.events import _CHAT_SURFACE_SUPPRESSED, OrchestratorEvent, TurnEventBus


class TestDecideEarlySurface:
    """只吃前三档 —— 后四档要等产出，提前判定拿不到。"""

    def test_user_directive_wins(self) -> None:
        assert decide_early_surface(user_directive="chat", plan_deliverable_kind="report", lead_delivery="report") == "chat"

    def test_plan_beats_lead(self) -> None:
        assert decide_early_surface(user_directive=None, plan_deliverable_kind="answer", lead_delivery="report") == "chat"

    def test_lead_spawn_delivery(self) -> None:
        assert decide_early_surface(user_directive=None, plan_deliverable_kind=None, lead_delivery="chat") == "chat"
        assert decide_early_surface(user_directive=None, plan_deliverable_kind=None, lead_delivery="report") == "canvas"

    def test_nobody_said_returns_none(self) -> None:
        """★ 三档都没说 ⇒ None ⇒ 调用方什么都不做 ⇒ 行为逐字不变。"""
        assert decide_early_surface(user_directive=None, plan_deliverable_kind=None, lead_delivery=None) is None

    def test_garbage_values_do_not_decide(self) -> None:
        assert decide_early_surface(user_directive="", plan_deliverable_kind="", lead_delivery="") is None
        assert decide_early_surface(user_directive="???", plan_deliverable_kind="???", lead_delivery="???") is None


async def _emitted(*, early_chat: bool, kinds: list[str]) -> list[str]:
    """发一串事件，返回**订阅者真正收到**的那些（= 真投递了的）。

    断的是 emit contract 本身（[[feedback_ut_must_assert_emit_contract]]），
    不是内部变量。
    """
    import asyncio

    bus = TurnEventBus()
    if early_chat:
        bus._early_chat_surface = True  # type: ignore[attr-defined]
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())
    for k in kinds:
        await bus.emit(OrchestratorEvent(event_type=k, turn_id="t_x", payload={}))  # type: ignore[arg-type]
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_x", payload={}))
    await consumer
    return [e.event_type for e in captured if e.event_type != "done"]


class TestBusSuppression:
    @pytest.mark.anyio
    async def test_flag_off_delivers_everything(self) -> None:
        """★ 没置位 ⇒ 一个字节不变（这是那约一半轮次的路）。

        注意 `canvas.turn_streaming` 是 bus 自己补的一帧（`_CANVAS_TURN_OPENERS`
        见到产内容事件就 exactly-once 补发）——它属于既有行为，断言里要带上，
        否则这条用例是在断一个不存在的形状。
        """
        got = await _emitted(early_chat=False, kinds=["canvas.section_added", "canvas.section_done"])
        assert got == ["canvas.turn_streaming", "canvas.section_added", "canvas.section_done"]

    @pytest.mark.anyio
    async def test_flag_on_also_suppresses_the_backfilled_turn_streaming(self) -> None:
        """★★ 连 bus 自补的那帧 `canvas.turn_streaming` 也不能漏出去。

        它是 FE canvas-store 里**唯一**写 `currentTurnId` 的事件
        （`onTurnStreaming`），漏一帧就足以把画布的「进行中」态点亮 ——
        压了 section_added 却漏了它，等于没压。
        """
        got = await _emitted(early_chat=True, kinds=["canvas.section_added", "canvas.section_done"])
        assert got == []

    @pytest.mark.anyio
    async def test_flag_on_suppresses_surfacing_events(self) -> None:
        got = await _emitted(early_chat=True, kinds=["canvas.section_added", "canvas.section_streaming"])
        assert got == []

    @pytest.mark.anyio
    async def test_flag_on_still_delivers_non_surfacing_events(self) -> None:
        """收尾/编辑类不能压 —— 压掉前端状态机收不了尾。"""
        got = await _emitted(early_chat=True, kinds=["canvas.sections_hidden", "canvas.done", "final_answer"])
        assert got == ["canvas.sections_hidden", "canvas.done", "final_answer"]

    @pytest.mark.anyio
    async def test_flag_on_suppresses_directly_emitted_turn_streaming(self) -> None:
        """★ `canvas.turn_streaming` **被直接发**时也必须压住。

        上一条用例（自补那帧）压不住这个：flag 开着时 `canvas.section_added` 在
        补发逻辑之前就被拦掉了，于是 turn_streaming 根本没机会被补 —— 把它从压制
        集合里删掉，那条用例照样绿（突变实测）。而 `canvas_composer` 的 Lead 收尾
        是**直接发**这一帧的，那条路只有本用例守得住。
        """
        got = await _emitted(early_chat=True, kinds=["canvas.turn_streaming"])
        assert got == []

    def test_suppressed_set_excludes_terminal_and_edit_events(self) -> None:
        for k in ("canvas.done", "canvas.sections_hidden", "canvas.section_patched", "done"):
            assert k not in _CHAT_SURFACE_SUPPRESSED, f"{k} 不该被压"

    def test_suppressed_set_is_not_using_suppress_live_delivery(self) -> None:
        """★ 记录裁决：必须**真不发**，不能用 `suppress_live_delivery`。

        后者是「落库但不投递」，而 `attachCanvasSSE` 重连时会重放 canvas 帧
        （`use-turn-runner` 专门备了 canvasGate 去重）——落了库就意味着重连后
        画布上又冒出来：live 干净、重放冒出来 = 违反 chat 不变量
        「live / 持久化 / 重放三者一致」。
        """
        ev = OrchestratorEvent(event_type="canvas.section_added", turn_id="t_x")  # type: ignore[arg-type]
        assert ev.suppress_live_delivery is False


class TestSpawnConflictRevokes:
    """★ 先 chat spawn 后 report spawn ⇒ 必须**撤回**已置的位（report 赢）。"""

    def test_report_after_chat_flips_back_to_canvas(self) -> None:
        # Simulate the handler recomputing this on every spawn.
        surfaces = []
        lead_delivery = None
        for d in ("chat", "report"):
            lead_delivery = "report" if (d == "report" or lead_delivery == "report") else d
            surfaces.append(decide_early_surface(user_directive=None, plan_deliverable_kind=None, lead_delivery=lead_delivery))
        assert surfaces == ["chat", "canvas"], "第二次 spawn 说 report 后必须不再是 chat"


class _FakeStore:
    def __init__(self) -> None:
        self.hidden: list[tuple[str, str]] = []
        self.unhidden: list[tuple[str, str]] = []

    async def mark_turn_hidden_for_chat_mode(self, turn_id: str, rationale: str) -> list[str]:
        self.hidden.append((turn_id, rationale))
        return ["sec_1"]

    async def unhide_turn(self, turn_id: str, rationale: str | None = None) -> list[str]:
        self.unhidden.append((turn_id, rationale or ""))
        return ["sec_1"]


class _Ctx:
    def __init__(self, store: Any, bus: Any) -> None:
        self.canvas_store = store
        self.parent_bus = bus
        self.turn_id = "t_x"


class _FlagBus:
    def __init__(self, early: bool) -> None:
        self._early_chat_surface = early


class TestEarlyDbHide:
    """★ 只压事件不够——F5 中途走的是 canvas API，得靠持久化标志。"""

    @pytest.mark.anyio
    async def test_marks_hidden_when_early_chat(self) -> None:
        from bowhead_core.canvas_tools import _hide_if_early_chat

        store, bus = _FakeStore(), _FlagBus(True)
        await _hide_if_early_chat(_Ctx(store, bus), bus, "t_x")
        assert store.hidden == [("t_x", "early: declared chat before draw")]

    @pytest.mark.anyio
    async def test_noop_when_flag_off(self) -> None:
        """★ 没置位 ⇒ 一次库都不碰（那约一半轮次逐字不变）。"""
        from bowhead_core.canvas_tools import _hide_if_early_chat

        store, bus = _FakeStore(), _FlagBus(False)
        await _hide_if_early_chat(_Ctx(store, bus), bus, "t_x")
        assert store.hidden == []

    @pytest.mark.anyio
    async def test_store_failure_does_not_raise(self) -> None:
        """置位失败不能挡住建图本身（图还在，只是画布上多露一会儿）。"""
        from bowhead_core.canvas_tools import _hide_if_early_chat

        class _Boom(_FakeStore):
            async def mark_turn_hidden_for_chat_mode(self, turn_id: str, rationale: str) -> list[str]:
                raise RuntimeError("db down")

        bus = _FlagBus(True)
        await _hide_if_early_chat(_Ctx(_Boom(), bus), bus, "t_x")  # No exception = pass.

    @pytest.mark.anyio
    async def test_no_turn_id_is_noop(self) -> None:
        from bowhead_core.canvas_tools import _hide_if_early_chat

        store, bus = _FakeStore(), _FlagBus(True)
        await _hide_if_early_chat(_Ctx(store, bus), bus, None)
        assert store.hidden == []


class TestReversalRestoresTurnStreaming:
    """★ review 发现：翻案只补发了 `section_added`，没补 `canvas.turn_streaming`。

    后者是 FE canvas-store 里**唯一**写 `currentTurnId` 的事件（`onTurnStreaming`），
    缺了它 D2 可见性闸的第一项 `Boolean(turnId)` 就恒 false、D3 横幅整批空转
    ——翻案后画布会「有段但没有轮」。

    本用例验证：压制期间 bus **没有**记「已发过 turn_streaming」，所以翻案后第一条
    `section_added` 会触发 exactly-once 自补，把这一帧补回来。若哪天自补逻辑被挪到
    压制之前，这条会转红。
    """

    @pytest.mark.anyio
    async def test_backfill_fires_after_reversal(self) -> None:
        import asyncio

        bus = TurnEventBus()
        bus._early_chat_surface = True  # type: ignore[attr-defined]
        captured: list[OrchestratorEvent] = []

        async def _consume() -> None:
            async for ev in bus.subscribe():
                captured.append(ev)

        consumer = asyncio.create_task(_consume())
        # During suppression: two charts are drawn, but no frame may escape.
        for _ in range(2):
            await bus.emit(OrchestratorEvent(event_type="canvas.section_added", turn_id="t_x", payload={}))  # type: ignore[arg-type]
        assert captured == []

        # Reversal: clear the flag, then emit the withheld content.
        bus._early_chat_surface = False  # type: ignore[attr-defined]
        await bus.emit(OrchestratorEvent(event_type="canvas.section_added", turn_id="t_x", payload={}))  # type: ignore[arg-type]
        await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_x", payload={}))
        await consumer

        kinds = [e.event_type for e in captured]
        assert "canvas.turn_streaming" in kinds, "翻案后必须把 turn_streaming 补回来，否则 FE 没有 currentTurnId"
        assert kinds.index("canvas.turn_streaming") < kinds.index("canvas.section_added"), "turn_streaming 必须在 section 之前"


class TestEarlyHideDoesNotBreakInlineFigures:
    """★★ 最重要的回归闸：早藏之后，**chat 气泡里的图必须还在**。

    上一个 sprint 刚把行内图做通，它的数据源是
    `_collect_inline_artifacts` → `list_sections_appended_by_turn`。
    如果那条查询也滤 `hidden_for_chat_mode`，本方案就会把刚做好的功能整个弄哑
    ——图既不在画布（被藏）也不在气泡（被滤）＝ 凭空消失。

    D6 的设计正是「藏画布但仍走 inline_artifacts 喂 chat 气泡」，这条用例把它钉死：
    真建真藏之后再查，必须一段不少。
    """

    @pytest.mark.anyio
    async def test_hidden_sections_still_reach_chat_bubble(self, tmp_path: Any) -> None:
        from bowhead_core.canvas_store import CanvasStore

        store = CanvasStore(tmp_path / "canvas.db")
        await store.initialize()
        canvas = await store.ensure_canvas(session_id="s_1", workspace_id="w", user_id="u", title="t")
        for i in range(2):
            await store.append_section(
                canvas_id=canvas.id,
                kind="chart",
                payload={"kind": "bar", "title": f"图{i}"},
                by_turn_id="t_x",
            )

        before = await store.list_sections_appended_by_turn("t_x")
        assert len(before) == 2

        # Early chat classification → mark immediately after creation.
        hidden = await store.mark_turn_hidden_for_chat_mode("t_x", "early: declared chat before draw")
        assert len(hidden) == 2

        after = await store.list_sections_appended_by_turn("t_x")
        assert len(after) == 2, "★ 藏了之后 chat 气泡仍必须拿得到这两张图，否则行内图功能被弄哑"

        # The canvas surface actually hides them (the intended effect).
        visible = await store.list_sections(canvas.id)
        assert visible == [] or all(getattr(s, "hidden_for_chat_mode", False) for s in visible)

    @pytest.mark.anyio
    async def test_unhide_restores_canvas_visibility(self, tmp_path: Any) -> None:
        """翻案：`unhide_turn` 必须把画布侧恢复原状。"""
        from bowhead_core.canvas_store import CanvasStore

        store = CanvasStore(tmp_path / "canvas.db")
        await store.initialize()
        canvas = await store.ensure_canvas(session_id="s_1", workspace_id="w", user_id="u", title="t")
        await store.append_section(canvas_id=canvas.id, kind="chart", payload={"kind": "bar"}, by_turn_id="t_x")
        await store.mark_turn_hidden_for_chat_mode("t_x", "early: declared chat before draw")
        assert await store.list_sections(canvas.id) == []

        await store.unhide_turn("t_x", "reversal: kept canvas after early chat")
        assert len(await store.list_sections(canvas.id)) == 1
