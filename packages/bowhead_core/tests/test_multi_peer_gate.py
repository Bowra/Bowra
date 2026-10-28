"""确定性兜底：一轮批量派 ≥2 个不同 peer → 先出计划（graph-mode，2026-08-15）。

**为什么有这条兜底**：`plan_propose` 工具模型就是不调。116 三轮 dogfood、分层证伪
三次，前两层每次都过 —— 指令在 prompt 里（Lead 能逐字复述「分 4 步严格分类」+ Step 0
全文）、判断也对（问它「该不该调」它答「该」并正确引用「≥2 个不同专家」），但真跑
就是不调；跨两个模型（deepseek-v4-flash / qwen3.6-flash）、三种 prompt 位置
（不在 prompt / 决策树旁的兄弟块 / 树内 Step 0）全部复现，连用户当场打字
「请立刻调用 plan_propose」都不调。

判据没有再造关键词表：用的是模型自己列出的这一轮 `subagent_spawn` 有几个不同 profile
—— 服务端可观测量。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.plan_propose_tool import (
    MULTI_PEER_THRESHOLD,
    is_plan_propose_sentinel,
    multi_peer_sentinel,
)


def _tc(name: str, profile: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, arguments=({"profile": profile} if profile else {}))


def test_threshold_is_two_distinct_peers() -> None:
    assert MULTI_PEER_THRESHOLD == 2


def test_two_distinct_peers_trip_the_gate() -> None:
    """真机正身：那一轮派的是 financial / business / compliance-analyst 三个。"""
    s = multi_peer_sentinel([_tc("subagent_spawn", "financial-analyst"), _tc("subagent_spawn", "business-analyst")])
    assert s is not None
    # ★ Reuse the existing sentinel criterion: the fallback adds no second mechanism and uses the same card emission path.
    assert is_plan_propose_sentinel(s)
    assert s["source"] == "multi_peer_gate"
    assert "financial-analyst" in s["reason"] and "business-analyst" in s["reason"]


def test_single_peer_does_not_trip() -> None:
    assert multi_peer_sentinel([_tc("subagent_spawn", "compute")]) is None


def test_same_peer_twice_is_not_multi_expert() -> None:
    """★两次同一个 peer 是并行分片，不是「多专家」—— 判据是**不同** profile 的个数。"""
    assert multi_peer_sentinel([_tc("subagent_spawn", "compute"), _tc("subagent_spawn", "compute")]) is None
    # Case/whitespace must not bypass deduplication.
    assert multi_peer_sentinel([_tc("subagent_spawn", "Compute"), _tc("subagent_spawn", " compute ")]) is None


def test_non_spawn_tools_are_ignored() -> None:
    """一轮里并行调若干查询工具是常态，不该被当成多专家。"""
    assert multi_peer_sentinel([_tc("bi_query"), _tc("bi_schema"), _tc("kb_search")]) is None
    assert multi_peer_sentinel([_tc("bi_query"), _tc("subagent_spawn", "compute")]) is None


def test_missing_or_malformed_profile_is_not_counted() -> None:
    """拿不到 profile 就不猜 —— 宁可不拦，也不要凭空弹卡。"""
    assert multi_peer_sentinel([_tc("subagent_spawn"), _tc("subagent_spawn")]) is None
    weird = SimpleNamespace(name="subagent_spawn", arguments="not-a-dict")
    assert multi_peer_sentinel([weird, _tc("subagent_spawn", "compute")]) is None


def test_empty_round_is_safe() -> None:
    assert multi_peer_sentinel([]) is None
