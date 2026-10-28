"""lead-analyst-logic 批2 — 跨 peer BI 查询复用/去重。

还「同 query 双跑双 artifact 各占一行刻意不修」（REPORT_WRITING 2026-08-14）的债：
1. single-flight（`_single_flight_bi_fetch`）：λ-F1 精确缓存只挡串行重复（先完成
   才有 put），并行同 query（多 peer 同轮扇出 / batch 内重复）双双 miss → 双打
   datarc → 双 artifact。现并发同 (turn, cache_key) 只跑一次 fetch。
2. `_prior_peer_bi_queries` 条目升级 {query, artifact_id} + 渲染去重——此前提示语
   声称「可通过 artifact_id 复用」但 id 从没给过下一个 peer。

这条链此前零测试覆盖；本文件同时把老语义（不同 key 不去重 / leader 失败不株连
follower）钉住当正对照。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bowhead_core.subagent import SubagentResult, ToolInvocation, register_subagent_tool
from bowhead_core.tool_registry import (
    _TURN_QUERY_CACHE,
    _TURN_QUERY_CACHE_LRU,
    _TURN_QUERY_INFLIGHT,
    ToolExecutionContext,
    _single_flight_bi_fetch,
    build_default_registry,
    cleanup_turn_query_cache,
)

from bowhead_core import tool_registry as tool_registry_mod


@pytest.fixture(autouse=True)
def _clean_state():
    _TURN_QUERY_CACHE.clear()
    _TURN_QUERY_CACHE_LRU.clear()
    _TURN_QUERY_INFLIGHT.clear()
    yield
    _TURN_QUERY_CACHE.clear()
    _TURN_QUERY_CACHE_LRU.clear()
    _TURN_QUERY_INFLIGHT.clear()


# ─────────────────────────────────────────────────────────────
# 1. _single_flight_bi_fetch concurrency semantics
# ─────────────────────────────────────────────────────────────


class _CountingFetch:
    def __init__(self, results: list[Any] | None = None, delay: float = 0.01) -> None:
        self.calls = 0
        self._results = results
        self._delay = delay

    async def __call__(self) -> Any:
        self.calls += 1
        await asyncio.sleep(self._delay)
        if self._results is not None:
            return self._results[min(self.calls - 1, len(self._results) - 1)]
        return {"ok": True, "artifact_id": f"art_{self.calls}"}


async def test_concurrent_same_key_fetches_once() -> None:
    fetch = _CountingFetch()
    outs = await asyncio.gather(
        _single_flight_bi_fetch("t_sf1", "k1", fetch),
        _single_flight_bi_fetch("t_sf1", "k1", fetch),
        _single_flight_bi_fetch("t_sf1", "k1", fetch),
    )
    assert fetch.calls == 1, f"并发同 key 应只打一次 datarc, got {fetch.calls}"
    # Three calls share the same result/artifact_id (the criterion for eliminating duplicate artifacts).
    assert outs[0] is outs[1] is outs[2]
    # Pool cleared (no leak)
    assert "t_sf1" not in _TURN_QUERY_INFLIGHT


async def test_different_keys_do_not_dedupe() -> None:
    """正对照：不同 key 各自真跑——去重闸没有把不同查询误合并。"""
    fetch = _CountingFetch()
    await asyncio.gather(
        _single_flight_bi_fetch("t_sf2", "ka", fetch),
        _single_flight_bi_fetch("t_sf2", "kb", fetch),
    )
    assert fetch.calls == 2


async def test_leader_error_result_does_not_poison_follower() -> None:
    """leader 返回 error 结果 → follower 回落自己跑（瞬时失败不株连重试权）。"""
    fetch = _CountingFetch(
        results=[
            {"error_kind": "transient", "error_message": "datarc 限流"},
            {"ok": True, "artifact_id": "art_good"},
        ]
    )
    out1, out2 = await asyncio.gather(
        _single_flight_bi_fetch("t_sf3", "k", fetch),
        _single_flight_bi_fetch("t_sf3", "k", fetch),
    )
    assert fetch.calls == 2
    # One error and one good result (scheduling determines order; compare as a set).
    kinds = {("err" if r.get("error_kind") else "ok") for r in (out1, out2)}
    assert kinds == {"err", "ok"}


async def test_leader_exception_propagates_and_follower_refetches() -> None:
    calls = {"n": 0}

    async def _fetch() -> Any:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        if calls["n"] == 1:
            raise RuntimeError("datarc down")
        return {"ok": True, "artifact_id": "art_retry"}

    outs = await asyncio.gather(
        _single_flight_bi_fetch("t_sf4", "k", _fetch),
        _single_flight_bi_fetch("t_sf4", "k", _fetch),
        return_exceptions=True,
    )
    errs = [o for o in outs if isinstance(o, Exception)]
    oks = [o for o in outs if isinstance(o, dict)]
    assert len(errs) == 1 and isinstance(errs[0], RuntimeError)  # Leader's own caller receives its original exception.
    assert len(oks) == 1 and oks[0]["artifact_id"] == "art_retry"  # Follower fallback succeeds.
    assert calls["n"] == 2


async def test_leader_failure_no_follower_stampede() -> None:
    """review P2-1：leader 失败时 N 个 follower 不得齐射——只有一个接任新 leader,
    其余等新 leader 的结果。总 fetch 次数 = 2（失败的 leader + 一个成功的继任者）。"""
    fetch = _CountingFetch(
        results=[
            {"error_kind": "transient", "error_message": "429"},
            {"ok": True, "artifact_id": "art_after_retry"},
        ]
    )
    outs = await asyncio.gather(
        _single_flight_bi_fetch("t_sf7", "k", fetch),
        _single_flight_bi_fetch("t_sf7", "k", fetch),
        _single_flight_bi_fetch("t_sf7", "k", fetch),
        _single_flight_bi_fetch("t_sf7", "k", fetch),
    )
    assert fetch.calls == 2, f"leader 失败后应恰有一个继任者, got {fetch.calls} fetches"
    oks = [o for o in outs if isinstance(o, dict) and not o.get("error_kind")]
    assert len(oks) == 3  # All three followers reuse the successor's result.
    assert all(o is oks[0] for o in oks)


async def test_serial_calls_after_completion_run_again() -> None:
    """正对照：single-flight 只管「在飞」窗口——完成后的串行重复由 λ-F1 缓存管
    （本层不缓存, 池已清）。"""
    fetch = _CountingFetch()
    await _single_flight_bi_fetch("t_sf5", "k", fetch)
    await _single_flight_bi_fetch("t_sf5", "k", fetch)
    assert fetch.calls == 2


async def test_cleanup_resolves_pending_inflight() -> None:
    """turn cancel 竞态：cleanup 必须 resolve pending future, 否则 awaiter 挂死。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow() -> Any:
        started.set()
        await release.wait()
        return {"ok": True}

    leader = asyncio.ensure_future(_single_flight_bi_fetch("t_sf6", "k", _slow))
    await started.wait()
    fut = _TURN_QUERY_INFLIGHT["t_sf6"]["k"]
    cleanup_turn_query_cache("t_sf6")
    assert fut.done() and fut.result() is None
    assert "t_sf6" not in _TURN_QUERY_INFLIGHT
    release.set()
    assert (await leader) == {"ok": True}  # Leader itself is unaffected.


# ─────────────────────────────────────────────────────────────
# 2. wiring: _query_single actually passes through single-flight
# ─────────────────────────────────────────────────────────────


async def test_bi_query_handler_routes_through_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """spy 模块级 helper（handler 是闭包 patch 不到, helper 特意做成模块级）。
    摘掉 `_query_single` 里的 single-flight 接线 → 本条必红。"""
    seen: dict[str, Any] = {}

    async def _spy(turn_id: str, cache_key: str, fetch: Any) -> Any:
        seen["turn_id"] = turn_id
        seen["cache_key"] = cache_key
        return {"ok": True, "rows": [{"m": 1}], "columns": ["m"], "artifact_id": "art_spy"}

    monkeypatch.setattr(tool_registry_mod, "_single_flight_bi_fetch", _spy)
    reg = build_default_registry()
    ctx = ToolExecutionContext(
        workspace_id="ws_x",
        member_uid="m",
        user_id="u",
        turn_id="t_sf_wire",
        datarc=object(),  # Spy short-circuits before fetch, never touching fake datarc.
        active_datarc_project="42",
        accessible_datarc_workspaces=({"id": "42", "name": "零售集团"},),
    )
    inv = await reg.execute("bi_query", {"queries": ["今年毛利"]}, ctx)
    assert inv.error is None, inv.error
    assert seen.get("turn_id") == "t_sf_wire"
    # N=1 batch returns the single query result directly, without a wrapper.
    assert inv.result.get("artifact_id") == "art_spy"


# ─────────────────────────────────────────────────────────────
# 3. _prior_peer_bi_queries: propagate artifact_id + deduplicate rendering
# ─────────────────────────────────────────────────────────────


class _SeqRunner:
    """假 runner：第一次返回带 bi_query invocations 的结果, 之后捕获 kwargs。"""

    def __init__(self, first: SubagentResult) -> None:
        self._first = first
        self.all_kwargs: list[dict] = []

    async def run(self, **kwargs: Any) -> SubagentResult:
        self.all_kwargs.append(kwargs)
        if len(self.all_kwargs) == 1:
            return self._first
        return SubagentResult(profile="compute", subagent_id="sub_n", ok=True, final_answer="ok")


class _FakeBus:
    async def emit(self, ev: Any) -> None:
        return None


async def test_prior_queries_carry_artifact_ids_and_dedupe() -> None:
    first = SubagentResult(
        profile="compute",
        subagent_id="sub_1",
        ok=True,
        final_answer="42",
        tool_invocations=[
            ToolInvocation(
                tool_name="bi_query",
                # Duplicate query (writes retain duplicates; rendering deduplicates)
                arguments={"queries": ["今年GMV", "今年GMV", "各品牌营收"]},
                result={
                    "results": [
                        {"ok": True, "query_text": "今年GMV", "_ref_artifact_id": "art_123"},
                        {"ok": True, "query_text": "各品牌营收", "_ref_artifact_id": "art_456"},
                    ]
                },
                error=None,
            ),
            # Legacy single-result shape
            ToolInvocation(
                tool_name="bi_query",
                arguments={"text": "去年销量"},
                result={"artifact_id": "art_789"},
                error=None,
            ),
            # review P1-1: N=1 batch has no wrapper; queries=[one] returns a flat result (no
            # results[]), with top-level id. This is the mainstream shape recommended by the schema.
            ToolInvocation(
                tool_name="bi_query",
                arguments={"queries": ["单条查询"]},
                result={"ok": True, "rows": [], "columns": [], "artifact_id": "art_flat"},
                error=None,
            ),
            # review P1-1 additional case: same query first lacks id (failed call), then has id → render the latter.
            ToolInvocation(
                tool_name="bi_query",
                arguments={"queries": ["重复查"]},
                result=None,
                error="transient",
            ),
            ToolInvocation(
                tool_name="bi_query",
                arguments={"text": "重复查"},
                result={"artifact_id": "art_dup"},
                error=None,
            ),
        ],
    )
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _SeqRunner(first)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
    )
    inv1 = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查数A"}, ctx)
    assert inv1.error is None, inv1.error
    inv2 = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查数B"}, ctx)
    assert inv2.error is None, inv2.error

    extra = str(runner.all_kwargs[1].get("extra_instructions") or "")
    assert "Earlier peer query results" in extra
    # artifact_id really propagates (previously only query text, despite prompts claiming reuse was possible).
    assert "今年GMV → artifact_id=art_123" in extra
    assert "各品牌营收 → artifact_id=art_456" in extra
    assert "去年销量 → artifact_id=art_789" in extra
    # review P1-1: propagate top-level id from flat single-query results (unwrapped N=1 batch) too.
    assert "单条查询 → artifact_id=art_flat" in extra
    # Rendering deduplicates: repeated "今年GMV" occupies one line.
    assert extra.count("今年GMV") == 1
    # review P1-1 additional case: same query with/without id coexist → render the entry with id.
    assert "重复查 → artifact_id=art_dup" in extra
    assert extra.count("重复查") == 1
    # History remains useful without granting the recipient a query or sandbox entry.
    assert "available Tool that can read the listed artifact_id" in extra
    assert "history grants no query or sandbox capability" in extra
    assert "python_sandbox" not in extra and "bi_fetch_rows" not in extra


async def test_first_spawn_has_no_prior_block() -> None:
    """正对照：第一个 peer 没有前史 → 不注入块（与批2 之前一致）。"""
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _SeqRunner(SubagentResult(profile="compute", subagent_id="sub_0", ok=True, final_answer="ok"))
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t2",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
    )
    await registry.execute("subagent_spawn", {"profile": "compute", "task": "查数"}, ctx)
    assert "Earlier peer query results" not in str(runner.all_kwargs[0].get("extra_instructions") or "")
