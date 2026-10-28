"""`_localize_clarification_options` 的**接线**（2026-09-01）。

纯函数由 `test_step_preview_locale.py` 钉。这里钉的是它有没有真接上：三个入参
（schema_cache / datarc project / output_language）任何一个取错，函数都会安静地
原样返回——从纯函数测试完全看不出来，线上表现为"改了但没生效"。

⚠ 本轮早些时候就栽过同款：把 datarc project 写成 `getattr(ctx, "datarc_workspace_id")`
（ctx 上没这个字段）会恒 None ⇒ 整条静默不生效。所以这里用一个**带真实字段名**的
上下文替身，取错名字就会红。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bowhead_core.agents.lead_orchestrator import _localize_clarification_options


@dataclass
class _Meta:
    names: list[str]
    display_names: dict[str, str] = field(default_factory=dict)


@dataclass
class _Schema:
    measures: list[_Meta] = field(default_factory=list)
    dimensions: list[_Meta] = field(default_factory=list)


class _Cache:
    def __init__(self, schema: Any) -> None:
        self._s = schema

    def get(self, ws: str, *, count_stats: bool = True) -> Any:
        return self._s


class _Lang:
    def __init__(self, out: str) -> None:
        self.output_language = out


class _Ctx:
    """字段名必须与 `ToolExecutionContext` 一致——这正是本测试要钉的东西。"""

    def __init__(self, *, locale: str = "en-US", ws: str = "1", schema: Any = None) -> None:
        self.schema_cache = _Cache(schema) if schema is not None else None
        self.language_context = _Lang(locale)
        self._ws = ws

    def resolve_datarc_project(self) -> str:
        return self._ws


def _localized_schema() -> _Schema:
    return _Schema(
        dimensions=[
            _Meta(["一级品牌名称英文"], {"en-US": "Brand"}),
            _Meta(["品规"], {}),  # datarc has no en-US label
            _Meta(["产品组"], {"en-US": "Product Group"}),
        ],
    )


_QS = [{"question": "which product level?", "options": ["一级品牌名称英文", "品规", "产品组"], "type": "single_select"}]


async def test_wiring_produces_labels_and_never_touches_options() -> None:
    out = await _localize_clarification_options(_QS, _Ctx(schema=_localized_schema()))
    assert out[0]["option_labels"] == ["Brand", "品规", "Product Group"]
    # ★ Keep options byte-for-byte: the model uses them for bi_query, and datarc recognizes only canonical Chinese names.
    assert out[0]["options"] == ["一级品牌名称英文", "品规", "产品组"]
    assert _QS[0].get("option_labels") is None, "不得就地改调用方的 dict"


async def test_zh_turn_adds_nothing() -> None:
    out = await _localize_clarification_options(_QS, _Ctx(locale="zh-CN", schema=_localized_schema()))
    assert "option_labels" not in out[0]


async def test_missing_pieces_are_silent_noops_not_crashes() -> None:
    """三个入参各缺一个都不许炸，也不许瞎编标签。"""
    # Empty cache **and no datarc client** (cannot fetch) → silently omit the field.
    assert "option_labels" not in (await _localize_clarification_options(_QS, _Ctx(schema=None)))[0]
    assert "option_labels" not in (await _localize_clarification_options(_QS, _Ctx(ws="", schema=_localized_schema())))[0]
    assert "option_labels" not in (await _localize_clarification_options(_QS, object()))[0]


async def test_empty_questions_pass_through() -> None:
    assert await _localize_clarification_options([], _Ctx(schema=_localized_schema())) == []


# Clarification can precede data retrieval, leaving the schema cache cold.
# Exercise the bounded fetch rather than copying the step-preview cache-only rule.


class _ColdCache:
    """空缓存：`get` 恒 None，`put` 记一笔（验证拉回来的顺带进了缓存）。"""

    def __init__(self) -> None:
        self.gets = 0
        self.puts: list[str] = []

    def get(self, ws: str, *, count_stats: bool = True) -> Any:
        self.gets += 1
        return None

    def put(self, ws: str, schema: Any) -> None:
        self.puts.append(ws)

    def lock_for(self, ws: str) -> Any:
        import anyio

        return anyio.Lock()


class _FakeDatarc:
    """最小 DatarcClient 替身：`_fetch_schema_uncached` 只用这几个方法。"""

    def __init__(self, dims: list[_Meta]) -> None:
        self._dims = dims
        self.calls = 0

    async def list_measures(self, ws: str) -> list[Any]:
        self.calls += 1
        return []

    async def list_dimensions(self, ws: str) -> list[Any]:
        self.calls += 1
        return self._dims

    async def get_workspace(self, ws: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(name="regional-sales", description="")


class _CtxCold(_Ctx):
    def __init__(self, dims: list[_Meta]) -> None:
        super().__init__(locale="en-US", ws="1", schema=None)
        self.schema_cache = _ColdCache()
        self.datarc = _FakeDatarc(dims)


async def test_cold_cache_fetches_instead_of_giving_up() -> None:
    """★ 这条是 dogfood 的回归闸：缓存冷 + 有 datarc 客户端 ⇒ 必须拉，不能空手而归。"""
    ctx = _CtxCold([_Meta(["一级品牌名称英文"], {"en-US": "Brand"}), _Meta(["品规"], {})])
    out = await _localize_clarification_options([{"question": "q", "options": ["一级品牌名称英文", "品规"], "type": "single_select"}], ctx)
    assert out[0]["option_labels"] == ["Brand", "品规"], "冷缓存下也必须产出标签"
    assert ctx.datarc.calls > 0, "没去拉 = 又退回那条死路"
    assert ctx.schema_cache.puts, "拉回来的 schema 要顺带进缓存，供本轮后续步骤复用"
    assert out[0]["options"] == ["一级品牌名称英文", "品规"], "回传值仍不许动"


async def test_slow_datarc_must_not_hold_the_card(monkeypatch: Any) -> None:
    """★ 拉 schema 必须封顶：它挡在澄清卡的发出路径上，而 DatarcClient 默认超时是
    **60 秒**。不封顶就是拿「几个标签的中英文」赌「用户 60 秒看不到任何卡片」。

    这条是 review 自查抓到的——我自己在修 dogfood 缺口时把这个 await 加了进去。
    """
    import anyio

    from bowhead_core import step_preview_locale as spl

    class _SlowDatarc(_FakeDatarc):
        async def list_dimensions(self, ws: str) -> list[Any]:
            await anyio.sleep(30)  # Far beyond the cap; a working cap prevents completion.
            return self._dims

    ctx = _CtxCold([_Meta(["一级品牌名称英文"], {"en-US": "Brand"})])
    ctx.datarc = _SlowDatarc([_Meta(["一级品牌名称英文"], {"en-US": "Brand"})])
    monkeypatch.setattr(spl, "_SCHEMA_FETCH_TIMEOUT_SEC", 0.05)

    with anyio.fail_after(5):  # Fails without the cap (the test's positive control).
        out = await _localize_clarification_options([{"question": "q", "options": ["一级品牌名称英文"], "type": "single_select"}], ctx)
    # Timeout omits labels but still sends the card (questions returned unchanged).
    assert "option_labels" not in out[0]
    assert out[0]["options"] == ["一级品牌名称英文"]
