"""lead-analyst-logic 批3 — peer 失败显式化。

修的静默面（多agent拓扑台账 P0）：崩溃/空产出 peer 此前在 compose 消费的 `_m137`
袋里零痕迹——聚合器分不清「失败」vs「根本没派」，多 peer 报告基于残缺数据照写不留痕。
现 spawn handler 在 `_peer_failure` 时 append `peer_failed` 合成信封；聚合器把失败信封
译成 caveat（仅当同轮有真产出；同 profile 重试成功不记）。

判据纪律：改动前后语义都要有正对照（bail-only 轮仍返 None / 成功轮袋里无合成
信封），防「测试只测新路径、守卫被摘也全绿」。
"""

from __future__ import annotations

from typing import Any

from bowhead_core.agents.lead_orchestrator import _aggregate_subagent_payloads
from bowhead_core.subagent import (
    SubagentResult,
    register_subagent_tool,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

# ─────────────────────────────────────────────────────────────
# 1. Aggregator: failure envelope → caveat (pure function)
# ─────────────────────────────────────────────────────────────


def _substantive(profile: str = "compute") -> dict[str, Any]:
    return {
        "subagent_id": f"sub_{profile}",
        "profile": profile,
        "payload": {
            "title": "营收分析",
            "exec_summary": "整体营收 5.2 亿",
            "kpis": [{"label": "营收", "value": "5.2亿"}],
        },
    }


def _failure(profile: str, kind: str = "peer_failed", message: str = "subagent_failed") -> dict[str, Any]:
    return {
        "subagent_id": f"sub_{profile}_fail",
        "profile": profile,
        "payload": {"error": {"kind": kind, "message": message}},
    }


def test_aggregate_failure_becomes_caveat_alongside_substantive() -> None:
    merged = _aggregate_subagent_payloads(None, bag=[_substantive("compute"), _failure("financial-analyst")])
    assert merged is not None
    assert merged["title"] == "营收分析"
    cavs = merged.get("caveats") or []
    assert any("financial-analyst" in c and "执行失败" in c for c in cavs), cavs
    # review2 P2-2: raw machine codes stay out of user deliverables (kind/message belong to events).
    assert all("peer_failed" not in c and "subagent_failed" not in c for c in cavs), cavs


def test_aggregate_failure_only_bag_still_returns_none() -> None:
    """正对照（纪律②）：单 peer bail-only 轮语义不变——不为失败单独造 canvas。"""
    assert _aggregate_subagent_payloads(None, bag=[_failure("compute")]) is None


def test_aggregate_bail_envelope_fallback_markdown_still_surfaces_without_caveat() -> None:
    """π Item B 回归钉：bail 信封的 fallback_markdown 仍进 merged；无真产出不加 caveat。"""
    bail = {
        "subagent_id": "sub_x",
        "profile": "compute",
        "payload": {
            "error": {"kind": "domain_mismatch", "message": "ws 没这个字段"},
            "fallback_markdown": "本工作区没有相关数据。",
        },
    }
    merged = _aggregate_subagent_payloads(None, bag=[bail])
    assert merged is not None
    assert merged.get("fallback_markdown") == "本工作区没有相关数据。"
    assert not merged.get("caveats")


def test_aggregate_retry_success_suppresses_failure_caveat() -> None:
    """纪律③：同 profile 先失败后交真产出 → 不记 caveat（失败已被覆盖）。"""
    merged = _aggregate_subagent_payloads(None, bag=[_failure("compute"), _substantive("compute")])
    assert merged is not None
    assert not merged.get("caveats")


def test_aggregate_bail_kind_from_other_profile_gets_caveat() -> None:
    merged = _aggregate_subagent_payloads(
        None,
        bag=[_substantive("compute"), _failure("kb-analyst", kind="domain_mismatch", message="KB 为空")],
    )
    assert merged is not None
    cavs = merged.get("caveats") or []
    assert any("kb-analyst" in c for c in cavs), cavs
    assert all("domain_mismatch" not in c for c in cavs), cavs  # Do not leak machine codes.


def test_short_answer_retry_suppresses_caveat() -> None:
    """review P2-2：重试成功走 DF-04 短答时, 同 profile 的早期失败不得再写 caveat
    ——分类必须发生在 short-answer continue 之前。"""
    short_ok = {
        "subagent_id": "sub_retry",
        "profile": "compute",
        "payload": {"delivery_surface": "chat", "exec_summary": "今年 GMV 5.2 亿"},
    }
    merged = _aggregate_subagent_payloads(
        None,
        bag=[_failure("compute"), short_ok, _substantive("financial-analyst")],
    )
    assert merged is not None
    assert not merged.get("caveats"), merged.get("caveats")


def test_failure_plus_short_answer_only_returns_none() -> None:
    """caveat 总闸=真报告产出：「失败+短答」轮不得长出 caveats-only 的 merged
    （否则本该纯 chat 的轮凭空得到 canvas 输入）。"""
    short_ok = {
        "subagent_id": "sub_s",
        "profile": "compute",
        "payload": {"delivery_surface": "chat", "exec_summary": "5.2 亿"},
    }
    assert _aggregate_subagent_payloads(None, bag=[_failure("financial-analyst"), short_ok]) is None


def test_bail_with_fallback_text_gets_no_caveat_even_with_substantive() -> None:
    """review P2-3：bail 的 fallback 文本会并入正文——再写「执行失败未纳入」就自相矛盾。"""
    bail_with_fb = {
        "subagent_id": "sub_b",
        "profile": "kb-analyst",
        "payload": {
            "error": {"kind": "domain_mismatch", "message": "KB 无相关文档"},
            "fallback_markdown": "KB 无相关文档，建议补充上传。",
        },
    }
    merged = _aggregate_subagent_payloads(None, bag=[_substantive("compute"), bail_with_fb])
    assert merged is not None
    assert merged.get("fallback_markdown") == "KB 无相关文档，建议补充上传。"
    assert not merged.get("caveats"), merged.get("caveats")


# ─────────────────────────────────────────────────────────────
# 2. Spawn handler: add crashed peers to the bag (synthetic envelope)
# ─────────────────────────────────────────────────────────────


class _ScriptedRunner:
    """假 runner：按脚本返回 SubagentResult。"""

    def __init__(self, result: SubagentResult) -> None:
        self._result = result
        self.kwargs: dict = {}

    async def run(self, **kwargs: Any) -> SubagentResult:
        self.kwargs = kwargs
        return self._result


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


async def _spawn(result: SubagentResult, profile: str = "compute") -> tuple[Any, ToolExecutionContext, _FakeBus]:
    registry = build_default_registry()
    register_subagent_tool(registry)
    bus = _FakeBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=_ScriptedRunner(result),
        parent_bus=bus,
    )
    inv = await registry.execute("subagent_spawn", {"profile": profile, "task": "查今年利润"}, ctx)
    assert inv.error is None, inv.error
    return inv, ctx, bus


async def test_crashed_peer_leaves_failure_envelope_in_bag() -> None:
    inv, ctx, _bus = await _spawn(SubagentResult(profile="compute", subagent_id="sub_c1", ok=False, final_answer="", error="LLM boom"))
    bag = getattr(ctx, "_m137_subagent_payloads", None) or []
    assert len(bag) == 1, bag
    entry = bag[0]
    assert entry["profile"] == "compute"
    err = entry["payload"]["error"]
    assert err["kind"] == "peer_failed"
    assert isinstance(err["message"], str) and err["message"], err
    # The dict returned to Lead keeps the old ok=False shape (the envelope only adds visibility for compose).
    assert inv.result["ok"] is False


async def test_empty_answer_peer_also_leaves_envelope() -> None:
    """ok=True 但零内容零 payload = _peer_failure 的另一族（subagent_empty_answer）。"""
    _inv, ctx, _bus = await _spawn(SubagentResult(profile="compute", subagent_id="sub_c2", ok=True, final_answer="", payload=None))
    bag = getattr(ctx, "_m137_subagent_payloads", None) or []
    assert len(bag) == 1
    assert bag[0]["payload"]["error"]["kind"] == "peer_failed"


async def test_successful_prose_peer_leaves_no_envelope() -> None:
    """正对照：成功 prose 返回（无 payload）→ 袋保持为空，行为与批3 之前一致。"""
    _inv, ctx, _bus = await _spawn(SubagentResult(profile="compute", subagent_id="sub_c3", ok=True, final_answer="利润前三是 A/B/C"))
    assert not (getattr(ctx, "_m137_subagent_payloads", None) or [])


async def test_payload_peer_bag_unchanged() -> None:
    """正对照：真 payload 返回走原分支，袋里是原 payload 不是信封。"""
    payload = {"title": "T", "exec_summary": "S", "kpis": [{"label": "x", "value": "1"}]}
    _inv, ctx, _bus = await _spawn(SubagentResult(profile="compute", subagent_id="sub_c4", ok=True, final_answer="done", payload=payload))
    bag = getattr(ctx, "_m137_subagent_payloads", None) or []
    assert len(bag) == 1
    assert bag[0]["payload"] is payload
