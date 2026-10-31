"""G §2.5 / §2.6：判定归工具，不归提示词。

用户指出「这是 tool 的功能吧为啥又到提示词了」：结构化口径就在工具手里，「有没有按这个词筛」
「是不是被截成了前 N」都是一比即知的事实，由工具写进 `_preflight_hint`，模型照着事实处理。

- §2.5 交叉判定：去噪后仍有没认出的词 **且** 结构化口径里没有 `filters` 键 ⇒ 陈述事实「没按它筛过」。
  只陈述事实、不下结论（用户拍板）；只有文本版的实例（116 现状）不断言。
- §2.6 Top-N：结构化口径里有 `limit` 键 ⇒ 标注「这批行本身就只有这些，不是全集」。
  拿不到结构化口径就不说——不从自家问句里猜「前N」，旧实例确实不截断，猜会做出假陈述。

判据依据：2026-09-19 dev 实测「没筛就没有 `filters` 键」「带前10 返 10 行且多出 `limit` 键」，
键名不随成员账户语言变（docs/datarc-api-dump/async_task_download_data.md §2b）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_bi_bridge.types import CutResult, CutToken
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

_UNFILTERED = "contains no `filters` field"
_LIMITED = "按请求里的条数限制截断了结果"
_INLINE_CUT = "Only a preview of the returned rows is shown"

_NO_FILTER = {"models": ["门店表"], "measures": ["营业额"], "dimensions": ["市", "各月"]}
_WITH_FILTER = {"models": ["门店表"], "measures": ["营业额"], "filters": ["上海"]}
_WITH_LIMIT = {"models": ["品牌表"], "measures": ["营业额"], "dimensions": ["品牌"], "order": ["营业额降序"], "limit": ["前10"]}


def _download(*, explain_json: Any = None, explain_message: Any = None, n_rows: int = 3) -> Any:
    d = MagicMock()
    d.rows = [{"市": f"区{i}", "营业额 / 元": i} for i in range(n_rows)]
    d.columns = ["市", "营业额 / 元"]
    d.total_rows = n_rows
    d.csv_filename = "rows.csv"
    d.task_uid = "task"
    d.download_ms = 10
    d.explain_json = explain_json
    d.explain_message = explain_message
    d.csv_count = 1
    d.download_errors = []
    return d


class _ArtifactStore:
    async def put_bytes(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id="art_1", filename=kwargs["filename"])


def _cut(*unknown: str) -> CutResult:
    toks = [CutToken(word=w, prop="unknown", unit_idx=i) for i, w in enumerate(unknown)]
    toks.append(CutToken(word="营业额", prop="m1", unit_idx=len(toks)))
    return CutResult(tokens=toks)


async def _run(turn_id: str, dl: Any, cut: CutResult, query: str = "华东地区 2026年 营业额") -> dict:
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(return_value=cut)
    datarc.download_data = AsyncMock(return_value=dl)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,  # ⚠ Unique across files: bi_query has a process-wide turn-scoped cache.
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )
    return await build_default_registry().get("bi_query").handler({"queries": [query]}, ctx)


def _hint(out: dict) -> str:
    return out.get("_preflight_hint") or ""


# ── §2.5 Cross-checks ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_filter_in_engine_report_states_the_fact() -> None:
    """116 dogfood 那一题的形状：问华东，datarc 自报分组「市」「各月」、没有过滤项。"""
    out = await _run("t-sig-unfiltered", _download(explain_json=_NO_FILTER), _cut("华东地"))
    hint = _hint(out)
    assert _UNFILTERED in hint
    assert "「华东地」" in hint


@pytest.mark.asyncio
async def test_filter_present_does_not_claim_unfiltered() -> None:
    """正对照：datarc 自报真按某个值筛了 ⇒ 不许说「没筛过」，否则又是一条天天响的提醒。"""
    out = await _run("t-sig-filtered", _download(explain_json=_WITH_FILTER), _cut("华东地"))
    assert _UNFILTERED not in _hint(out)


@pytest.mark.asyncio
async def test_text_only_instance_does_not_assert() -> None:
    """116 连的实例只返文本版：拿不到结构化事实就只保留 A 的原措辞，不断言「没筛过」。"""
    out = await _run(
        "t-sig-textonly",
        _download(explain_message="生效模型为「门店表」；查询指标为「营业额」；分组维度为「市」。"),
        _cut("华东地"),
    )
    hint = _hint(out)
    assert "「华东地」" in hint, "A 那句仍要在"
    assert _UNFILTERED not in hint


@pytest.mark.asyncio
async def test_operation_word_gets_the_fact_but_no_conclusion() -> None:
    """已知噪声档：「汇总」过得了去噪、查询本无筛选意图 ⇒ 事实照报，但那句话里不许下结论。"""
    out = await _run("t-sig-opword", _download(explain_json=_NO_FILTER), _cut("汇总"), query="2026年 各品类 营业额 汇总")
    fact = next((ln for ln in _hint(out).splitlines() if _UNFILTERED in ln), "")
    assert fact, "事实句应当出现"
    for verdict in ("错", "不可用", "不能用"):
        assert verdict not in fact, f"事实句里出现了结论性措辞：{verdict!r}"


@pytest.mark.asyncio
async def test_no_unknown_words_says_nothing() -> None:
    """正常的不筛选查询（「各品类营业额」）本来就没有过滤项——没有没认出的词就不许报。"""
    out = await _run("t-sig-clean", _download(explain_json=_NO_FILTER), _cut(), query="2026年 各品类 营业额")
    assert _UNFILTERED not in _hint(out)


# ── §2.6 Top-N ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_key_marks_rows_as_not_the_full_set() -> None:
    out = await _run("t-sig-limit", _download(explain_json=_WITH_LIMIT, n_rows=10), _cut(), query="2026年 各品牌 营业额 前10")
    assert out["_rows_limited_by_query"] is True
    assert _LIMITED in _hint(out)


@pytest.mark.asyncio
async def test_no_limit_key_says_nothing() -> None:
    out = await _run("t-sig-nolimit", _download(explain_json=_NO_FILTER), _cut(), query="2026年 各品类 营业额")
    assert "_rows_limited_by_query" not in out
    assert _LIMITED not in _hint(out)


@pytest.mark.asyncio
async def test_text_only_instance_does_not_guess_the_limit() -> None:
    """问句里带「前10」、文本版里也写了限制——都不拿来猜：旧实例确实不截断，猜会做出假陈述。"""
    out = await _run(
        "t-sig-limit-textonly",
        _download(explain_message="生效模型为「品牌表」；结果数量限制为「前10」。", n_rows=10),
        _cut(),
        query="2026年 各品牌 营业额 前10",
    )
    assert "_rows_limited_by_query" not in out
    assert _LIMITED not in _hint(out)


@pytest.mark.asyncio
async def test_limit_and_inline_cut_are_both_stated_distinctly() -> None:
    """两种截断是两回事：datarc 截断＝数据本身就这些；inline 截断＝全量在 artifact 里。措辞不能混。"""
    out = await _run("t-sig-limit-inline", _download(explain_json=_WITH_LIMIT, n_rows=40), _cut(), query="2026年 各品牌 营业额 前40")
    hint = _hint(out)
    assert _LIMITED in hint
    assert _INLINE_CUT in hint
