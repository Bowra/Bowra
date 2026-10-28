"""批 2 —— `add_chart` / `add_image` 的 `target` 参数（候选 4 §4）。

规则一句话：**`target` 只能收窄曝光，不能放宽**（曝光面 = 所有声明的交集）。
不传 `target` ⇒ 沿用轮级判定 ⇒ **存量行为逐字不变**（这是迁移安全的关键）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.canvas_tools import resolve_surface_target

pytestmark = pytest.mark.unit


def _bus(decision: str | None):
    return SimpleNamespace(_early_surface_decision=decision)


class TestIntersectionRule:
    """候选 4 §4.1 的真值表，逐行钉住。"""

    @pytest.mark.parametrize(
        ("turn", "target", "expected", "why"),
        [
            # Turn-level chat: target cannot relax it.
            ("chat", None, None, "不传 ⇒ 沿用轮级（现状）"),
            ("chat", "chat", "chat", "一致"),
            ("chat", "canvas", None, "★放宽被忽略——用户原话/Lead 明说不该被工具参数推翻"),
            # Turn-level canvas / report turn.
            ("canvas", None, None, "不传 ⇒ 逐字不变"),
            ("canvas", "canvas", "canvas", "一致"),
            ("canvas", "chat", "chat", "★新能力：报告轮里的随手核对图不上画布"),
            # None of the three tiers specified (about half observed on 116).
            (None, None, None, "逐字不变，走轮末证据兜底"),
            (None, "chat", "chat", "★主要收益：那 51% 变得可判定"),
            (None, "canvas", "canvas", "同上，反方向"),
        ],
    )
    def test_truth_table(self, turn, target, expected, why) -> None:
        assert resolve_surface_target(_bus(turn), target) == expected, why

    @pytest.mark.parametrize("bad", ["both", "BOTH", "report", "", "  ", None, 0, [], {"a": 1}])
    def test_illegal_values_fall_back_to_turn_level(self, bad) -> None:
        """`target` 是 LLM 可写面：非法值一律按「没传」处理，不猜、不报错。

        `both` 特意在这条里——它**不是**合法取值（§4.3：交集框架下等同于不传）。
        """
        assert resolve_surface_target(_bus(None), bad) is None
        assert resolve_surface_target(_bus("canvas"), bad) is None

    def test_case_and_whitespace_are_tolerated(self) -> None:
        assert resolve_surface_target(_bus(None), " Chat ") == "chat"
        assert resolve_surface_target(_bus(None), "CANVAS") == "canvas"

    def test_missing_bus_does_not_explode(self) -> None:
        """没有 bus（后端自调 / 测试路径）时按「轮级无声明」处理。"""
        assert resolve_surface_target(None, "chat") == "chat"
        assert resolve_surface_target(None, None) is None


class TestSchemaSurface:
    def test_both_figure_tools_expose_target(self) -> None:
        from bowhead_core.canvas_tools import _add_chart_schema, _add_image_schema

        for schema in (_add_chart_schema(), _add_image_schema()):
            prop = schema.input_schema["properties"].get("target")
            assert prop is not None, f"{schema.name} 少了 target"
            assert prop["enum"] == ["chat", "canvas"], "不做 both（§4.3）"
            assert "target" not in schema.input_schema["required"], "必须可不传"


class TestStoreSemantics:
    """存储层：声明必须留得住——turn 级写路径不能覆盖它，翻案不能翻掉它。"""

    @staticmethod
    async def _store(tmp_path):
        from bowhead_core.canvas_store import CanvasStore

        st = CanvasStore(str(tmp_path / "canvas.db"))
        await st.initialize()
        return st

    @staticmethod
    async def _section(store, canvas_id, turn_id):
        return await store.append_section(
            canvas_id=canvas_id,
            kind="chart",
            payload={"kind": "bar", "title": "T"},
            by_turn_id=turn_id,
        )

    async def test_chat_target_hides_and_persists(self, tmp_path) -> None:
        store = await self._store(tmp_path)
        canvas = await store.ensure_canvas(session_id="s1", workspace_id="ws_1", user_id="u_1", title="c")
        sec = await self._section(store, canvas.id, "t1")
        await store.set_section_surface_target(sec.id, "chat")

        rows = await store.list_sections_appended_by_turn("t1")
        got = next(r for r in rows if r.id == sec.id)
        assert got.surface_target == "chat"
        assert got.hidden_for_chat_mode is True, "chat 必须同时落 hidden，否则中途 F5 图会冒出来"

    async def test_turn_level_hide_skips_explicit_canvas(self, tmp_path) -> None:
        """★ 轮末证据兜底不该推翻「这张图属于画布」的显式声明。"""
        store = await self._store(tmp_path)
        canvas = await store.ensure_canvas(session_id="s1", workspace_id="ws_1", user_id="u_1", title="c")
        declared = await self._section(store, canvas.id, "t1")
        plain = await self._section(store, canvas.id, "t1")
        await store.set_section_surface_target(declared.id, "canvas")

        hidden = await store.mark_turn_hidden_for_chat_mode("t1", "evidence fallback")
        assert plain.id in hidden, "未声明的段照旧被藏"
        assert declared.id not in hidden, "显式 canvas 的段不该被藏"

    async def test_unhide_turn_does_not_resurrect_explicit_chat(self, tmp_path) -> None:
        """★ 翻案是「轮级判定错了」的补救，不该掀翻单张图自己的声明。"""
        store = await self._store(tmp_path)
        canvas = await store.ensure_canvas(session_id="s1", workspace_id="ws_1", user_id="u_1", title="c")
        declared = await self._section(store, canvas.id, "t1")
        plain = await self._section(store, canvas.id, "t1")
        await store.set_section_surface_target(declared.id, "chat")
        await store.mark_turn_hidden_for_chat_mode("t1", "declared chat")

        unhidden = await store.unhide_turn("t1", "reversal")
        assert plain.id in unhidden, "轮级压制的段该被翻回来"
        assert declared.id not in unhidden, "★显式 target=chat 的图不该被翻上画布"

    async def test_inline_figures_still_see_chat_targeted_sections(self, tmp_path) -> None:
        """★★ 最容易弄哑的地方：藏了画布，但 chat 气泡里的图必须还在。

        `list_sections_appended_by_turn` **不滤** `hidden_for_chat_mode`
        ——那正是 D6「藏画布但仍喂气泡」的设计，`_collect_inline_artifacts` 靠它取数。
        """
        store = await self._store(tmp_path)
        canvas = await store.ensure_canvas(session_id="s1", workspace_id="ws_1", user_id="u_1", title="c")
        a = await self._section(store, canvas.id, "t1")
        b = await self._section(store, canvas.id, "t1")
        await store.set_section_surface_target(a.id, "chat")
        await store.set_section_surface_target(b.id, "chat")

        rows = await store.list_sections_appended_by_turn("t1")
        assert {r.id for r in rows} >= {a.id, b.id}, "两张图一段不少"

    async def test_declaration_survives_delete_and_restore(self, tmp_path) -> None:
        """删了再恢复不能静默丢掉声明。

        恢复走的是 delete revision 里的**快照列清单**——新加的列不写进那份清单，
        「删除→恢复」就会把声明悄悄抹成 NULL，而画布上什么都看不出来
        （[[feedback_my_own_fixtures_are_the_happy_path]]：新列最容易漏的就是非顺利路径）。
        """
        import aiosqlite

        store = await self._store(tmp_path)
        canvas = await store.ensure_canvas(session_id="s1", workspace_id="ws_1", user_id="u_1", title="c")
        sec = await self._section(store, canvas.id, "t1")
        await store.set_section_surface_target(sec.id, "chat")
        await store.delete_section(section_id=sec.id, canvas_id=canvas.id)

        async with (
            aiosqlite.connect(store._path) as db,
            db.execute(
                "SELECT id FROM canvas_revisions WHERE section_id=? AND op='delete' ORDER BY rowid DESC LIMIT 1",
                (sec.id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        assert row is not None, "没找到 delete revision——用例前提不成立"
        restored = await store.restore_deleted_sections(canvas_id=canvas.id, revision_id=str(row[0]))
        assert restored, "恢复失败——用例前提不成立"

        rows = await store.list_sections_appended_by_turn("t1")
        got = [r for r in rows if r.id == sec.id]
        assert got, "恢复后该段应回来"
        r = got[0]
        assert r.surface_target == "chat", "★恢复把声明抹掉了"
        # ★ Asserting the new column alone is insufficient: a one-column INSERT **offset** writes into a neighbor,
        # while surface_target may still look correct. Pin neighboring values too.
        assert r.kind == "chart"
        assert r.hidden_for_chat_mode is True
        assert r.payload.get("title") == "T"
        assert r.turn_id == "t1"


class TestRestoreInsertShapeInvariant:
    """恢复路径那条 INSERT 的**列数 / 占位符 / 值元组**必须三者一致。

    这是本批唯一一处「往既有 INSERT 里插列」的改动，也是最容易静默写坏存量内容的
    形态：错位一格不会报错，只会把值写进隔壁列。用结构不变式钉住，比逐列断言更耐改。
    """

    def test_columns_placeholders_and_values_all_match(self) -> None:
        import ast
        import pathlib as _p

        src = (_p.Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "canvas_store.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            sql = node.args[0]
            # Concatenated literals ("INSERT ..." "cols..." ") VALUES (...)") parse as a constant-folded Constant.
            if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                continue
            text = sql.value
            if not text.startswith("INSERT INTO canvas_sections("):
                continue
            if not isinstance(node.args[1], ast.Tuple):
                continue
            found.append((text, len(node.args[1].elts)))

        assert found, "没定位到恢复路径的 INSERT——锚点变了，先看代码再改这条"
        for text, n_values in found:
            cols_sql = text[text.index("(") + 1 : text.index(") VALUES")]
            columns = [c.strip() for c in cols_sql.split(",") if c.strip()]
            placeholders = text[text.index(") VALUES") :].count("?")
            assert len(columns) == placeholders == n_values, (
                f"canvas_sections INSERT 三者不一致：列={len(columns)} 占位符={placeholders} 值={n_values}\n{columns}"
            )

        assert any("surface_target" in t for t, _ in found), "恢复快照没带上 surface_target"


class TestApplyAndSuppress:
    """`_apply_surface_target` = 落库 + 返回「要不要压掉露面事件」。"""

    class _Store:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def set_section_surface_target(self, section_id: str, target: str) -> None:
            self.calls.append((section_id, target))

    async def test_chat_persists_and_suppresses(self) -> None:
        from bowhead_core.canvas_tools import _apply_surface_target

        store = self._Store()
        ctx = SimpleNamespace(canvas_store=store)
        assert await _apply_surface_target(ctx, _bus(None), "sec_1", "chat") is True
        assert store.calls == [("sec_1", "chat")]

    async def test_canvas_persists_but_does_not_suppress(self) -> None:
        from bowhead_core.canvas_tools import _apply_surface_target

        store = self._Store()
        ctx = SimpleNamespace(canvas_store=store)
        assert await _apply_surface_target(ctx, _bus(None), "sec_1", "canvas") is False
        assert store.calls == [("sec_1", "canvas")]

    async def test_no_target_touches_nothing(self) -> None:
        """不传 ⇒ 一行库都不写、一个事件都不压 ⇒ **存量行为逐字不变**。"""
        from bowhead_core.canvas_tools import _apply_surface_target

        store = self._Store()
        ctx = SimpleNamespace(canvas_store=store)
        assert await _apply_surface_target(ctx, _bus(None), "sec_1", None) is False
        assert store.calls == []

    async def test_ignored_widening_touches_nothing(self) -> None:
        """轮级说 chat 时 `target='canvas'` 被忽略——**也不能落库**。

        否则那条 `surface_target='canvas'` 会让轮末的 D6 跳过这一段，
        等于从后门把「被忽略的放宽」实现了。
        """
        from bowhead_core.canvas_tools import _apply_surface_target

        store = self._Store()
        ctx = SimpleNamespace(canvas_store=store)
        assert await _apply_surface_target(ctx, _bus("chat"), "sec_1", "canvas") is False
        assert store.calls == [], "★被忽略的声明不该落库"

    async def test_persist_failure_never_blocks_drawing(self) -> None:
        """落库失败只影响「露不露面」，绝不能让画图本身失败。"""
        from bowhead_core.canvas_tools import _apply_surface_target

        class _Boom:
            async def set_section_surface_target(self, *_: object, **__: object) -> None:
                raise RuntimeError("db down")

        ctx = SimpleNamespace(canvas_store=_Boom())
        assert await _apply_surface_target(ctx, _bus(None), "sec_1", "chat") is True


class TestRevealEmitIsGated:
    """两个 handler 的「露面」事件块必须挂在 `_suppress_reveal` 上。

    ⚠ 这是**结构闸**不是行为闸：跑通真 handler 需要一整套 artifact/canvas 装置，
    本批没建。它能挡住「有人把门删了」，挡不住「门的语义写反了」——后者靠
    上面的单元用例 + 真机验证覆盖。这个边界写在这里，别当成行为已验。
    """

    def test_both_handlers_gate_the_emit_block(self) -> None:
        import pathlib
        import re

        src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "canvas_tools.py").read_text(encoding="utf-8")
        gated = re.findall(r"if bus is not None and not _suppress_reveal:", src)
        assert len(gated) == 2, (
            f"期望 chart / image 两个 handler 都门控，实际 {len(gated)} 处——有人把 `and not _suppress_reveal` 删了，target=chat 的图会照常上画布"
        )
        for handler in ("_add_chart", "_add_image"):
            body = src.split(f"async def {handler}(", 1)[1].split("\nasync def ", 1)[0]
            assert "await _apply_surface_target(" in body, f"{handler} must apply its visibility target before revealing a section"
