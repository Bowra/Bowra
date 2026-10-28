"""datarc 自报的「本次实际生效口径」要一路带到模型面前（taskType=22 新增字段）。

我们发给 datarc 的是自然语言，它解析成模型/指标/维度/**时间范围**/过滤条件后才执行。
这份自报以前整块丢在 `DownloadDataResult` 之外，于是模型只能拿着行数据猜自己查了什么——
09-10 dogfood 那轮把只过了 10 天的 9 月当成完整月份做环比，就是这条链路上没有口径信息的直接后果
（见 `docs/sprints/dogfood-followup-2026-09-15/spike.md` §2）。

⚠ `message_json` 的值是**展示串**、随成员账户语言变，datarc 文档明写「不应依赖数组元素内部的
文本格式进行查询」——所以这里只钉「原样透传」，**不钉任何解析出来的日期**。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.model_view import project_tool_result
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

_EXPLAIN = {
    "models": ["门店销售"],
    "measures": ["营业额"],
    "date_filters": ["「下单日期」的「上个月」（2026-08-01 至 2026-08-31）"],
}


def _download(*, explain_json: Any = None, explain_message: Any = None) -> Any:
    d = MagicMock()
    d.rows = [{"品牌": "A", "营业额 / 元": 1}]
    d.columns = ["品牌", "营业额 / 元"]
    d.total_rows = 1
    d.csv_filename = "rows.csv"
    d.task_uid = "task"
    d.download_ms = 10
    d.explain_json = explain_json
    d.explain_message = explain_message
    return d


class _ArtifactStore:
    async def put_bytes(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id="art_1", filename=kwargs["filename"])


def _ctx(turn_id: str, dl: Any) -> ToolExecutionContext:
    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut unavailable"))
    datarc.download_data = AsyncMock(return_value=dl)
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,  # ⚠ Unique across files: bi_query has a process-wide turn-scoped cache.
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )


async def _run(args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    return await build_default_registry().get("bi_query").handler(args, ctx)


@pytest.mark.asyncio
async def test_explain_json_reaches_the_model_verbatim() -> None:
    ctx = _ctx("t-explain-ok", _download(explain_json=_EXPLAIN, explain_message="生效模型为「门店销售」…"))
    out = await _run({"queries": ["上个月 各品牌 营业额"]}, ctx)
    assert out["query_explain"] == _EXPLAIN, "口径要原样透传，不加工"


@pytest.mark.asyncio
async def test_prose_message_is_backend_only() -> None:
    """人话那句与 `query_explain` 是同一份内容的两种形态，给模型两份等于花两次预算。"""
    ctx = _ctx("t-explain-prose", _download(explain_json=_EXPLAIN, explain_message="生效模型为「门店销售」…"))
    out = await _run({"queries": ["上个月 各品牌 营业额"]}, ctx)
    assert out["_debug_explain_message"].startswith("生效模型")
    assert "_debug_explain_message" not in project_tool_result("bi_query", out)
    assert project_tool_result("bi_query", out)["query_explain"] == _EXPLAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", [], "not a dict", 0])
async def test_malformed_explain_json_is_simply_absent(bad: Any) -> None:
    """旧版 datarc 不返这个键；响应形状是对方说了算的，缺了或形状不对都只是少一层信息，不报错。"""
    ctx = _ctx(f"t-explain-badjson-{type(bad).__name__}-{bad!r:.8}", _download(explain_json=bad, explain_message="x"))
    out = await _run({"queries": ["上个月 各品牌 营业额"]}, ctx)
    assert "query_explain" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", [], {"a": 1}])
async def test_malformed_explain_message_is_simply_absent(bad: Any) -> None:
    ctx = _ctx(f"t-explain-badmsg-{type(bad).__name__}-{bad!r:.8}", _download(explain_json=_EXPLAIN, explain_message=bad))
    out = await _run({"queries": ["上个月 各品牌 营业额"]}, ctx)
    assert "_debug_explain_message" not in out
    assert out["query_explain"] == _EXPLAIN, "一个字段形状坏了不该连累另一个"


@pytest.mark.asyncio
async def test_batch_sub_results_carry_their_own_explain() -> None:
    """合批时每条子查询各有各的口径；子结果 wrapper 是白名单，不显式转发就等于丢弃。

    走公开入口（多条 `queries`）而不是去够闭包里的 `_wrap_sub_result`——
    接线测试要穿过真实路径，够私有函数正是「渲染侧测了、接线没测」的那个坑。
    """
    ex_a = {"models": ["门店销售"], "date_filters": ["「上个月」（2026-08-01 至 2026-08-31）"]}
    ex_b = {"models": ["门店销售"], "date_filters": ["「今年」（2026-01-01 至 2026-09-19）"]}
    dl_a = _download(explain_json=ex_a, explain_message="A")
    dl_b = _download(explain_json=ex_b, explain_message="B")

    async def _by_text(*args: Any, **kwargs: Any) -> Any:
        text = str(kwargs.get("text") or (args[2] if len(args) > 2 else ""))
        return dl_a if "上个月" in text else dl_b

    datarc = MagicMock()
    datarc.cut_question = AsyncMock(side_effect=RuntimeError("cut unavailable"))
    datarc.download_data = AsyncMock(side_effect=_by_text)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t-explain-batch",
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )
    out = await _run({"queries": ["上个月 各品牌 营业额", "今年 各品牌 营业额"]}, ctx)

    subs = {s["query_text"]: s for s in out["results"]}
    assert subs["上个月 各品牌 营业额"]["query_explain"] == ex_a
    assert subs["今年 各品牌 营业额"]["query_explain"] == ex_b, "两条子查询的口径不能串"
