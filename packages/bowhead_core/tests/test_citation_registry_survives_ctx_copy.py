"""引用登记表必须穿得过 peer 派发的 `copy.copy(ctx)`（feedback-triage-2026-08-26）。

116 dogfood 实测：报告轮从 2026-08-20 起**一条引用都没注册** —— `turn.citation_registered`
事件 0、`turn_citations` 落库 0 行、页面角标 0 个，而模型正文里 ` [cite:1]` 标记还在。
边界与 `FEATURE_LEAD_BI_DELEGATION` 在 116 翻开的日子吻合。

病因是**浅拷贝 + 惰性 setattr** 这对组合：

  · peer 派发用 `dispatch_ctx = copy.copy(ctx)`（`subagent.py`）；
  · `ensure_registry` 只在属性**不存在**时 `setattr`；
  · 于是跑在副本上时，表被挂到副本身上，真 parent_ctx 那边始终是空；
  · `_flush_citations` 读的正是 parent_ctx ⇒ 提前 return，什么都不发。

交权之前 Lead 自己取数、先在真 ctx 上建过表，副本按引用共享同一个对象，所以没暴露。

判据写成**不变式**（「副本上的注册必须能被父 ctx 看见」）而不是钉死初始化位置 ——
换个地方 eager-init 也该继续绿。
"""

from __future__ import annotations

import copy

import pytest
from bowhead_core.citations import TurnCitationRegistry, ensure_registry


class _Ctx:
    """最小 ctx 替身：普通对象，`copy.copy` 就是浅拷贝（与生产 dispatch_ctx 同语义）。"""

    def __init__(self) -> None:
        self.role = "analyst"


@pytest.mark.unit
def test_registry_created_on_copy_is_lost_to_parent() -> None:
    """★ 先把病因本身钉住：**不**预建表时，副本上的注册父 ctx 看不见。

    这条不是在断言期望行为，是在锁住「为什么必须 eager-init」——
    哪天 `ensure_registry` 改成能回写父 ctx，这条会红，那时该连同修复一起重写。
    """
    parent = _Ctx()
    peer = copy.copy(parent)

    ensure_registry(peer).register("art_aaa", label="bi_query: x", kind="data_ref")

    assert getattr(parent, "turn_citation_registry", None) is None, "如果这条红了，说明 ensure_registry 的语义变了——请重新评估 eager-init 是否还必要"


@pytest.mark.unit
def test_eager_registry_on_parent_survives_ctx_copy() -> None:
    """★ 不变式：turn 起点先在真 parent_ctx 上建表，副本上的注册就能穿回来。"""
    parent = _Ctx()
    ensure_registry(parent)  # ← The initialization performed at turn start.

    peer = copy.copy(parent)
    seq = ensure_registry(peer).register("art_aaa", label="bi_query: x", kind="data_ref")

    registry = getattr(parent, "turn_citation_registry", None)
    assert isinstance(registry, TurnCitationRegistry)
    assert len(registry.all_citations()) == 1, "副本上的注册没有穿回父 ctx"
    assert registry.all_citations()[0].artifact_id == "art_aaa"
    assert seq == 1


@pytest.mark.unit
def test_multiple_peers_share_one_registry_and_dedup() -> None:
    """并行 peer 各自拿一份副本时，seq 必须在**同一张表**上连续、同产物去重。

    否则两个 peer 各写各的 `[cite:1]`，正文里两个 1 指向不同产物 —— 比不显示更糟。
    """
    parent = _Ctx()
    ensure_registry(parent)

    peer_a = copy.copy(parent)
    peer_b = copy.copy(parent)

    seq_a = ensure_registry(peer_a).register("art_aaa", label="A", kind="data_ref")
    seq_b = ensure_registry(peer_b).register("art_bbb", label="B", kind="data_ref")
    seq_a_again = ensure_registry(peer_b).register("art_aaa", label="A", kind="data_ref")

    registry = parent.turn_citation_registry
    assert (seq_a, seq_b) == (1, 2), "两个 peer 的 seq 不在同一张表上连续"
    assert seq_a_again == seq_a, "同一个产物在另一个 peer 上应复用同一个 seq"
    assert len(registry.all_citations()) == 2


@pytest.mark.unit
def test_turn_setup_eagerly_attaches_registry() -> None:
    """接线闸：turn 起点必须真的调了 `ensure_registry(runtime.parent_ctx)`。

    上面三条测的是 `citations.py` 的语义，**证明不了 lead 那边接上了** ——
    本 bug 的全部要害恰恰在「有没有人在 turn 起点调它」，所以单独钉住。
    """
    from pathlib import Path

    src = Path(__import__("bowhead_core").__file__).parent / "agents" / "lead_orchestrator.py"
    text = src.read_text(encoding="utf-8")
    assert "_ensure_citation_registry(runtime.parent_ctx)" in text, "turn 起点没有 eager-init 引用登记表——peer 副本上的注册会全部丢失"
