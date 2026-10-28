"""datarc 限流（「查询过于频繁」）必须退避重试，而不是把这条查询丢掉.

2026-08-18 某私有化客户实测：一天 104 条 bi_query 子查询里 45 条失败（43.3%），**全部**
是同一句「查询过于频繁，请稍后再试」。规律：一次批量无论发 2/3/5/8/10 条，**永远只有 2
条成功**——该 datarc 实例并发上限就是 2，而我们默认 6 条并行发。此前无重试无退避，被拒的
查询直接丢掉，agent 拿不到数据还照常往下走，用户看到的就是「BI 老报 error」。

判据成对写：限流要重试并最终成功；**非**限流的 datarc 错误一次都不许重试（重试只是把同
一个错再撞一遍）。只测「重试了」不测「不该重试的没重试」，等于没测。
"""

from __future__ import annotations

import pytest
from bowhead_bi_bridge.types import CutResult, DownloadDataResult
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_core import tool_registry as tr


class _StubDatarc:
    """按 query 计次的 datarc 替身——注错就注在真实缝 `download_data` 上。"""

    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour
        self.calls: list[str] = []

    async def download_data(self, *_a, **kw):
        q = str(kw.get("text") or (_a[1] if len(_a) > 1 else ""))
        self.calls.append(q)
        return self._behaviour(q, self.calls.count(q))

    async def cut_question(self, *_a, **_kw):
        return CutResult()


def _ctx(dc, turn_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        # The turn-level query cache reuses results by (turn_id, query); change turn_id between cases,
        # otherwise the next case reads the previous cache and never calls download_data.
        turn_id=turn_id,
        datarc=dc,
        active_datarc_project="1",
    )


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """退避基数压到近 0，测试不真睡。"""
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_BACKOFF_S", "0.05")
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_RETRIES", "3")


_QUERIES = ["2026年各省份销售金额", "2026年各省份销售数量"]


async def _run(behaviour, turn_id: str):
    dc = _StubDatarc(behaviour)
    spec = build_default_registry().get("bi_query")
    out = await spec.handler({"queries": list(_QUERIES)}, _ctx(dc, turn_id))
    return out, dc.calls


# ─── Rate limiting: must retry until successful ──────────────────────────────


async def test_throttled_query_is_retried_until_it_succeeds(monkeypatch) -> None:
    def behaviour(q: str, nth: int):
        if nth <= 2:
            raise RuntimeError("查询过于频繁，请稍后再试")
        # Use the actual contract type instead of a handmade stand-in: bespoke fixtures cover only the happy path; missing fields
        # turn a successful-retry test into a failed-retry test.
        return DownloadDataResult(
            columns=["中文省份", "销售金额"],
            rows=[{"中文省份": "广东省", "销售金额": 1.0}],
            total_rows=1,
            query_text=q,
        )

    out, calls = await _run(behaviour, "t_case1")
    results = out["results"]
    assert [r["ok"] for r in results] == [True, True], [r.get("error_message") for r in results]
    # Each query is attempted 3 times (2 rate limits + 1 success).
    assert len(calls) == 6


async def test_gives_up_after_bounded_attempts_and_labels_rate_limited(monkeypatch) -> None:
    """一直被限流 → 不能无限重试；且 error_kind 要能与普通 datarc 错误区分。"""

    def behaviour(q: str, nth: int):
        raise RuntimeError("查询过于频繁，请稍后再试")

    out, calls = await _run(behaviour, "t_case2")
    results = out["results"]
    assert [r["ok"] for r in results] == [False, False]
    assert {r["error_kind"] for r in results} == {"datarc_rate_limited"}
    # retries=3 → at most 4 attempts per query
    assert len(calls) == 8


# ─── Negative control: never retry non-rate-limit errors ─────────────────────


async def test_non_throttle_datarc_error_is_not_retried(monkeypatch) -> None:
    """★ 没有这条，上面的「重试了」可能只是「什么错都重试」。"""

    def behaviour(q: str, nth: int):
        raise RuntimeError("维度不存在: 特殊采购")

    out, calls = await _run(behaviour, "t_case3")
    results = out["results"]
    assert [r["ok"] for r in results] == [False, False]
    assert {r["error_kind"] for r in results} == {"datarc_error"}
    assert len(calls) == 2  # One attempt per query


async def test_retries_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_BI_THROTTLE_RETRIES", "0")

    def behaviour(q: str, nth: int):
        raise RuntimeError("查询过于频繁，请稍后再试")

    out, calls = await _run(behaviour, "t_case4")
    assert len(calls) == 2
    assert {r["error_kind"] for r in out["results"]} == {"datarc_rate_limited"}


# ─── The criterion itself ───────────────────────────────────────────────────


def test_throttle_marker_matching() -> None:
    assert tr._is_datarc_throttled("查询过于频繁，请稍后再试")
    assert tr._is_datarc_throttled("Too Many Requests")
    assert not tr._is_datarc_throttled("维度不存在")
    assert not tr._is_datarc_throttled("")
