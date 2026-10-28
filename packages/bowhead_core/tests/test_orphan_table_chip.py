"""Attach an unambiguous current-turn table missing from the answer bubble.

A sandbox CSV can remain tool_data rather than a deliverable. Cover the fallback
without changing Artifact kind or guessing among multiple candidate outputs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import _append_orphan_table_chip


def _art(aid: str, filename: str, mime: str = "text/csv", size: int = 1024, kind: str = "sandbox_output") -> Any:
    return SimpleNamespace(id=aid, filename=filename, mime=mime, size=size, kind=kind, path=f"/tmp/{filename}")


class _Store:
    def __init__(self, arts: list[Any] | None = None, boom: bool = False) -> None:
        self._a = arts or []
        self._boom = boom

    async def list_result_artifacts_for_turn(self, turn_id: str) -> list[Any]:
        if self._boom:
            raise RuntimeError("store down")
        return self._a

    async def signed_download_url(self, *a: Any, **k: Any) -> str:
        return "https://example/dl"


def _ctx(store: Any) -> Any:
    return SimpleNamespace(artifact_store=store)


@pytest.mark.anyio
async def test_single_tabular_artifact_gets_a_chip() -> None:
    """★ 正主：唯一表格产物 + 气泡空 ⇒ 补 chip。"""
    out = await _append_orphan_table_chip([], _ctx(_Store([_art("art_1", "mapping.csv")])), "t_1")
    assert len(out) == 1
    assert out[0].get("kind") == "file"


@pytest.mark.anyio
async def test_ambiguous_multiple_tables_are_not_guessed() -> None:
    """★★ 用户点的那一点：多个产物里分不出交付物和中间产物。

    那轮是 anomalies.csv(39B) / manager_summary.csv(556B) / mapping.csv(5111B)，
    按大小或时间都不可靠（另有一轮是两个同名 out.csv）。宁可不给，也不能把
    中间产物当答案递给用户。
    """
    arts = [_art("a", "anomalies.csv", size=39), _art("b", "manager_summary.csv", size=556), _art("c", "mapping.csv", size=5111)]
    assert await _append_orphan_table_chip([], _ctx(_Store(arts)), "t_1") == []


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["chart", "kpi", "table", "file"])
async def test_main_path_wins_no_double_render(kind: str) -> None:
    """★ 正对照：主路已经挂上了东西 ⇒ 兜底不插一脚（否则双份渲染）。

    没有这条，「见到 CSV 就挂」的坏实现也会让第一条绿。
    """
    existing = [{"kind": kind, "id": "sec_1"}]
    out = await _append_orphan_table_chip(list(existing), _ctx(_Store([_art("art_1", "mapping.csv")])), "t_1")
    assert out == existing


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filename", "mime"),
    [("sandbox-stdout.txt", "text/plain"), ("dirty_table.py", "text/x-python")],
)
async def test_non_tabular_artifact_ignored(filename: str, mime: str) -> None:
    """stdout / 代码文件不是交付物，别给它们挂 chip。

    ★ 必须**一次只放一个**：早先版本一次塞两个非表格产物，去掉 mime 过滤后
    数量变成 2、被「≥2 不猜」那条挡住，于是突变照样绿——判别器被另一条规则兜住了。
    """
    assert await _append_orphan_table_chip([], _ctx(_Store([_art("a", filename, mime=mime)])), "t_1") == []


@pytest.mark.anyio
async def test_bi_query_dump_is_not_a_deliverable() -> None:
    """★★ review 抓到的真回归：`bi_query` 每返一次行也落一个 text/csv
    （`source=bi_fetch_rows` / `kind=tool_data`），那是**喂给沙箱的原始数据**。

    第一版只按 mime 过滤 ⇒ 116 生产 21 天里 81 次命中有 71 次（88%）挂的是它，
    正是用户点的「产物可能是中间产物」。
    """
    dump = _art("art_bi", "bi_fetch_rows.csv", kind="tool_data")
    assert await _append_orphan_table_chip([], _ctx(_Store([dump])), "t_1") == []


@pytest.mark.anyio
async def test_sandbox_output_wins_over_coexisting_bi_dump() -> None:
    """★★ 另一半：同轮既有 BI 转储又有唯一沙箱表 —— 这是**最常见**的真实形态。

    116 实测：只产 1 个沙箱表的 114 轮里，104 轮同轮还有 BI 转储；第一版把两者
    一起数进 `tabular`、被「≥2 不猜」误挡 ⇒ 该救的 91% 一个没救到。
    """
    arts = [_art("art_bi", "bi_fetch_rows.csv", kind="tool_data"), _art("art_sb", "mapping.csv")]
    out = await _append_orphan_table_chip([], _ctx(_Store(arts)), "t_1")
    assert len(out) == 1 and out[0].get("kind") == "file"


def _chart(rows: list[dict[str, Any]], cols: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "chart", "id": "sec_1", "spec": {"kind": "table", "rows": rows, "columns": cols}}


_REGIONLESS = _chart(
    [{"中文品牌_2026": "示例产品甲", "销售数量_2026": v} for v in (180.0, 330.0, 360.0)],
    [{"key": "中文品牌_2026", "type": "categorical"}, {"key": "销售数量_2026", "type": "numeric"}],
)
_WITH_REGION = _chart(
    [{"大区经理辖区": d, "销售数量": v} for d, v in (("ONC东一大区", 286005.0), ("ONC东二大区", 220017.0))],
    [{"key": "大区经理辖区", "type": "categorical"}, {"key": "销售数量", "type": "numeric"}],
)


@pytest.mark.anyio
async def test_degenerate_chart_does_not_count_as_success() -> None:
    """A chart with indistinguishable rows does not prove successful delivery.

    The fallback may still expose the source table containing useful grouping labels.
    """
    out = await _append_orphan_table_chip([dict(_REGIONLESS)], _ctx(_Store([_art("art_1", "out.csv")])), "t_1")
    assert len(out) == 2, f"退化的图不该算主路成功：{out}"
    assert out[-1].get("kind") == "file"


@pytest.mark.anyio
async def test_a_real_chart_still_suppresses_the_fallback() -> None:
    """★ 正对照：图是好的就别插一脚（否则每轮都多一个重复 chip）。"""
    existing = [dict(_WITH_REGION)]
    out = await _append_orphan_table_chip(list(existing), _ctx(_Store([_art("art_1", "out.csv")])), "t_1")
    assert out == existing


@pytest.mark.anyio
@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "table"},  # No rows/columns.
        {"kind": "table", "rows": "nope", "columns": []},
        {"kind": "table", "rows": [{"a": 1}], "columns": [{"key": "a", "type": "categorical"}]},  # Single row.
    ],
)
async def test_unparseable_or_single_row_specs_are_treated_as_success(spec: Any) -> None:
    """判不出来就**当它是好的**——兜底宁可不插手，也不能因为读不懂 spec 就重复挂。"""
    out = await _append_orphan_table_chip([{"kind": "chart", "id": "sec_1", "spec": spec}], _ctx(_Store([_art("art_1", "out.csv")])), "t_1")
    assert len(out) == 1


@pytest.mark.anyio
async def test_no_artifacts_is_a_noop() -> None:
    assert await _append_orphan_table_chip([], _ctx(_Store([])), "t_1") == []


@pytest.mark.anyio
async def test_fails_open() -> None:
    """★ 兜底本身不能变成新的失败面。"""
    assert await _append_orphan_table_chip([], _ctx(None), "t_1") == []
    assert await _append_orphan_table_chip([], _ctx(_Store(boom=True)), "t_1") == []


# ── Wiring: a correct pure function does not prove it is connected ──────────────────


class _CanvasStore:
    async def list_sections_appended_by_turn(self, turn_id: str, **kw: Any) -> list[Any]:
        return []


@pytest.mark.anyio
async def test_fallback_is_wired_into_collect_inline_artifacts() -> None:
    """★ 实测：把这行从 `_collect_inline_artifacts` 摘掉，上面 9 条一条都不响。

    判「兜底真被执行」必须走真收集函数，不能只测纯函数
    （[[feedback_comment_states_intent_not_outcome]]）。
    """
    from bowhead_core.agents.lead_orchestrator import _collect_inline_artifacts

    class _AStore(_Store):
        async def list_deliverables_for_turn(self, turn_id: str) -> list[Any]:
            return []

    ctx = SimpleNamespace(
        canvas_store=_CanvasStore(),
        artifact_store=_AStore([_art("art_1", "mapping.csv")]),
        parent_bus=None,
    )
    out = await _collect_inline_artifacts(ctx, "t_1")
    assert [a.get("kind") for a in out] == ["file"], f"兜底没生效：{out}"
