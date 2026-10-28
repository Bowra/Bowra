"""没有结构化口径时，那句人话必须仍然到得了模型面前。

★ 这是 E 交接 §2 记的那个「功能在 116 上是死的」：
116 连的实例只返 `message`、不返 `message_json` ⇒ `query_explain` 恒不设；
而唯一带信息的 `message` 落在 `_debug_explain_message`，又被 `model_view` 的后端专用名单摘掉
⇒ 模型一个字都拿不到，接口径这件事等于没做。

退路的形状：**不带 `_debug_` 前缀**的 `query_explain_text`（`_debug_` 在本仓是后端专用约定，
靠删 strip 那行放行会把 csv 文件名一起放出去）。有结构化版时不设它——两份同义内容白花预算。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.model_view import project_tool_result
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

_EXPLAIN = {"models": ["门店销售"], "measures": ["营业额"]}
_MESSAGE = "生效模型为「门店销售」；查询指标为「营业额」；时间范围为「日期」的「2026年」（2026-01-01 至 2026-09-18）。"


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
    d.csv_count = 1
    d.download_errors = []
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
        turn_id=turn_id,  # ⚠ Unique across files: bi_query has a process-wide, turn-scoped cache.
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s1",
    )


async def _run(ctx: ToolExecutionContext) -> Any:
    return await build_default_registry().get("bi_query").handler({"queries": ["2026年 各品牌 营业额"]}, ctx)


@pytest.mark.asyncio
async def test_text_reaches_the_model_when_structured_is_absent() -> None:
    """116 实例的真实形态：只有 message。模型面必须拿得到它。"""
    out = await _run(_ctx("t-explain-fb-textonly", _download(explain_message=_MESSAGE)))

    assert "query_explain" not in out
    assert out["query_explain_text"] == _MESSAGE
    # The projected model view is the criterion: the backend-only list removes `_debug_`, not this field too.
    assert project_tool_result("bi_query", out)["query_explain_text"] == _MESSAGE


@pytest.mark.asyncio
async def test_no_duplicate_when_structured_is_present() -> None:
    """dev 实例形态：两者都有。结构化那份已经说完了，人话不再占第二份预算。"""
    out = await _run(_ctx("t-explain-fb-both", _download(explain_json=_EXPLAIN, explain_message=_MESSAGE)))

    assert out["query_explain"] == _EXPLAIN
    assert "query_explain_text" not in out
    assert "_debug_explain_message" in out, "人话那份仍留给后端排障"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", [], {"a": 1}, 0])
async def test_malformed_message_sets_no_fallback(bad: Any) -> None:
    """形状是对方说了算的；坏值只是少一层信息，不许凭空造出一个空口径字段。"""
    out = await _run(_ctx(f"t-explain-fb-bad-{type(bad).__name__}-{bad!r:.8}", _download(explain_message=bad)))

    assert "query_explain_text" not in out
    assert "_debug_explain_message" not in out


@pytest.mark.asyncio
async def test_backend_only_key_is_still_stripped_from_the_model_view() -> None:
    """退路不是「把 `_debug_` 放行」——那条约定还管着 csv 文件名等一串键。"""
    out = await _run(_ctx("t-explain-fb-strip", _download(explain_message=_MESSAGE)))
    projected = project_tool_result("bi_query", out)

    assert "_debug_explain_message" not in projected
    assert "_debug_csv_filename" not in projected
