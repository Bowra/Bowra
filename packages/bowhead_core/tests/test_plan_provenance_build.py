"""批 4 S4: build_plan_provenance 拼装器 (batch4-provenance-design D-D).

Pins: 四边推导(PRODUCES/DERIVED_FROM/USED/SUPERSEDES)/owner rsplit 解析(连字符
profile 不拿错段)/双语义 step_done 不干扰(只喂 tool_result)/事件缺失退化 +
completeness 如实计数/coarse composed_in。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from bowhead_core.plan_provenance import build_plan_provenance, tool_event_types


def _step(sid: str, **kw):
    base = {
        "id": sid,
        "agent_name": "compute",
        "status": "done",
        "origin": "initial",
        "level": 0,
        "depends_on": (),
        "superseded_by": None,
        "verify_json": None,
        "payload_json": json.dumps({"exec_summary": "ok"}),
        "result_digest": "ok",
        "input_manifest_json": None,
        "tokens_input": 10,
        "tokens_output": 20,
        "turn_id": "t_1",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _ev(subagent_id: str, tool: str, call_id: str = "tl_1", ok: bool = True):
    return SimpleNamespace(
        event_type="tool_result", payload={"subagent_id": subagent_id, "tool_name": tool, "call_id": call_id, "ok": ok, "duration_ms": 5}
    )


def _sec(sec_id: str, *, kind: str = "narrative", turn_id: str = "t_1", owner: str | None = None):
    return SimpleNamespace(id=sec_id, kind=kind, turn_id=turn_id, owner=owner)


def _plan():
    return SimpleNamespace(id="plan_x", status="completed", title="t", current_rev=2, replan_count=1)


def test_four_edges_full_chain() -> None:
    old = _step("ps_old000000001", status="superseded", superseded_by="ps_new000000001", payload_json=None, result_digest=None)
    new = _step(
        "ps_new000000001",
        origin="revision",
        payload_json=json.dumps({"exec_summary": "ok", "insight_section_ids": ["sec_i1"]}),
        input_manifest_json=json.dumps({"expected": ["ps_up"], "received": ["ps_up"], "degraded_inputs": [], "excluded_inputs": []}),
    )
    up = _step("ps_up0000000001", agent_name="kb-analyst")
    events = {"t_1": [_ev("sub_ps_new000000001", "bi_query"), _ev("sub_ps_up0000000001", "kb_search", "tl_2")]}
    secs = [
        _sec("sec_i1", kind="insight_card"),
        # owner encodes fine attribution; hyphenated profile names pin rsplit.
        _sec("sec_w1", owner="writer-financial-analyst-ps_up0000000001"),
        # Legacy indexed owner suffix unparsed →coarse.
        _sec("sec_w2", owner="writer-financial-analyst-0"),
    ]
    out = build_plan_provenance(plan=_plan(), steps=[old, new, up], tool_events_by_turn=events, sections=secs)

    by_id = {s["id"]: s for s in out["steps"]}
    # USED (derive): only own subagent_id.
    assert by_id["ps_new000000001"]["used_tools"] == [{"call_id": "tl_1", "tool_name": "bi_query", "ok": True, "duration_ms": 5}]
    assert by_id["ps_up0000000001"]["used_tools"][0]["tool_name"] == "kb_search"
    assert by_id["ps_old000000001"]["used_tools"] == []
    # Bidirectional SUPERSEDES.
    assert by_id["ps_old000000001"]["superseded_by"] == "ps_new000000001"
    assert by_id["ps_new000000001"]["supersedes"] == "ps_old000000001"
    # DERIVED_FROM
    assert by_id["ps_new000000001"]["derived_from"]["received"] == ["ps_up"]
    # PRODUCES
    assert by_id["ps_new000000001"]["produces"]["insight_section_ids"] == ["sec_i1"]
    # Fine section attribution: insight reverse lookup + owner parsing.
    sec_by_id = {s["id"]: s for s in out["sections"]}
    assert sec_by_id["sec_i1"]["produced_by_step"] == "ps_new000000001"
    assert sec_by_id["sec_w1"]["produced_by_step"] == "ps_up0000000001"
    assert sec_by_id["sec_w2"]["produced_by_step"] is None  # Never guess legacy format.
    # Coarse: same-turn done steps.
    assert "ps_new000000001" in sec_by_id["sec_w2"]["composed_in_steps"]
    # 116-dogfood P1: numerator counts fine attribution only; sec_w2 has produced_by_step=None above,
    # so **must not** count traceable; old coarse-inclusive rule reported 3/3.
    assert out["completeness"]["sections_traceable"] == "2/3"
    assert out["completeness"]["sections_coarse_only"] == 1


def test_peer_authored_owner_sub_ps_resolves_to_step() -> None:
    """delivery-surface review #14：M-C 发笔后 plan 步 peer 亲笔的 narrative
    owner=per-spawn id `sub_ps_<step>`（plan 路预铸 `sub_{step_id}`）——
    provenance fine 归属必须认识这一代 owner，否则亲笔轮 sections_traceable
    全落 coarse。不在册的 sub_ps_* 照旧不猜（审计口径）。"""
    s = _step("ps_a00000000001", turn_id="t_1")
    secs = [
        _sec("sec_pen", owner="sub_ps_a00000000001"),
        _sec("sec_alien", owner="sub_ps_ffffffffffff"),  # Not registered →None.
        _sec("sec_chat_peer", owner="sub_c0ffee000001"),  # Ordinary spawn ID, not plan step →None.
    ]
    out = build_plan_provenance(plan=_plan(), steps=[s], tool_events_by_turn={}, sections=secs)
    sec_by_id = {x["id"]: x for x in out["sections"]}
    assert sec_by_id["sec_pen"]["produced_by_step"] == "ps_a00000000001"
    assert sec_by_id["sec_alien"]["produced_by_step"] is None
    assert sec_by_id["sec_chat_peer"]["produced_by_step"] is None
    assert out["completeness"]["sections_traceable"] == "1/3"


def test_coarse_only_sections_do_not_inflate_traceable() -> None:
    """★116-dogfood P1 回归钉：coarse(同 turn)不得冒充步级归属。

    `composed_in_steps` = 本 turn 全部 done 步 —— 对同 turn 的每个 section 都是同一个
    常量。旧口径把它放进分子, 于是单轮 plan 只要有一个 done 步, 比值就**恒等于分母**,
    这个指标结构上永远报不出缺口(真机 30 个 section 只有 4 个有归属, 照报 30/30)。
    本例全部 section 都无 owner/insight 归属, 正确答案是 0/3 而不是 3/3。
    """
    s = _step("ps_a00000000001", turn_id="t_1")
    secs = [_sec("sec_1"), _sec("sec_2", kind="chart"), _sec("sec_3", owner="report-editor")]
    out = build_plan_provenance(plan=_plan(), steps=[s], tool_events_by_turn={}, sections=secs)

    assert [x["produced_by_step"] for x in out["sections"]] == [None, None, None]
    # Still disclose coarse supporting evidence, but exclude from numerator.
    assert all(x["composed_in_steps"] == ["ps_a00000000001"] for x in out["sections"])
    assert out["completeness"]["sections_traceable"] == "0/3"
    assert out["completeness"]["sections_coarse_only"] == 3


def test_missing_events_degrade_honestly() -> None:
    s = _step("ps_a00000000001")
    out = build_plan_provenance(plan=_plan(), steps=[s], tool_events_by_turn={}, sections=[])
    assert out["steps"][0]["used_tools"] == []
    assert out["steps"][0]["subagent_id"] == "sub_ps_a00000000001"
    assert out["completeness"]["steps_with_tool_events"] == "0/1"


def test_foreign_turn_sections_not_composed_in() -> None:
    """非 plan turn 的 section(普通 chat 写的)不得挂 coarse 归属, 也不稀释
    分母(实现 review P2-3: 混合 session 的 chat section 不是本 plan 交付物)."""
    s = _step("ps_a00000000001", turn_id="t_plan")
    secs = [_sec("sec_chat", turn_id="t_other")]
    out = build_plan_provenance(plan=_plan(), steps=[s], tool_events_by_turn={}, sections=secs)
    assert out["sections"][0]["composed_in_steps"] == []
    assert out["completeness"]["sections_traceable"] == "0/0"
    assert out["completeness"]["foreign_sections"] == 1


def test_failed_step_tools_counted_separately() -> None:
    """实现 review P1-1: 失败步的工具事件单列, 不进 done 口径分子(防 3/2)."""
    ok = _step("ps_ok0000000001")
    bad = _step("ps_bad000000001", status="failed", payload_json=None)
    events = {"t_1": [_ev("sub_ps_ok0000000001", "bi_query"), _ev("sub_ps_bad000000001", "web_fetch", "tl_9", ok=False)]}
    out = build_plan_provenance(plan=_plan(), steps=[ok, bad], tool_events_by_turn=events, sections=[])
    assert out["completeness"]["steps_with_tool_events"] == "1/1"
    assert out["completeness"]["non_done_steps_with_tool_events"] == {"failed": 1}
    by_id = {s["id"]: s for s in out["steps"]}
    assert by_id["ps_bad000000001"]["used_tools"][0]["tool_name"] == "web_fetch"


def test_non_done_tool_bucket_split_by_status() -> None:
    """批4 收债: 单列桶按 status 细分——原名写死 failed 却混装 superseded/
    running, 审计台账里名不副实(读者据此以为"只有失败步有残留工具证据")."""
    bad = _step("ps_bad000000001", status="failed", payload_json=None)
    sup = _step("ps_sup000000001", status="superseded", payload_json=None)
    run = _step("ps_run000000001", status="running", payload_json=None)
    events = {
        "t_1": [
            _ev("sub_ps_bad000000001", "web_fetch", "tl_1"),
            _ev("sub_ps_sup000000001", "bi_query", "tl_2"),
            _ev("sub_ps_run000000001", "kb_search", "tl_3"),
        ]
    }
    out = build_plan_provenance(plan=_plan(), steps=[bad, sup, run], tool_events_by_turn=events, sections=[])
    assert out["completeness"]["non_done_steps_with_tool_events"] == {"failed": 1, "running": 1, "superseded": 1}
    assert out["completeness"]["steps_with_tool_events"] == "0/0"


def test_manifest_denominator_only_counts_steps_that_expect_one() -> None:
    """批4 收债: driver 只在 dispatch 写 manifest, 且无依赖步按设计返 None
    (`_build_input_manifest`)。旧口径拿全部步作分母 → 3 并行+1 汇总的正常计划
    自报 "1/4"(系统性低报缺口)。分母=有上游依赖且已派发的步。"""
    root_a = _step("ps_roota00000001")  # done without dependencies needs no manifest.
    root_b = _step("ps_rootb00000001")
    agg = _step(
        "ps_agg000000001",
        depends_on=("ps_roota00000001", "ps_rootb00000001"),
        input_manifest_json=json.dumps(
            {"expected": ["ps_roota00000001"], "received": ["ps_roota00000001"], "degraded_inputs": [], "excluded_inputs": []}
        ),
    )
    pending = _step("ps_pend00000001", status="pending", depends_on=("ps_agg000000001",), payload_json=None)
    out = build_plan_provenance(plan=_plan(), steps=[root_a, root_b, agg, pending], tool_events_by_turn={}, sections=[])
    assert out["completeness"]["steps_with_manifest"] == "1/1"


def test_stale_manifest_on_reset_step_never_exceeds_denominator() -> None:
    """复位到 pending 的步若残留旧 manifest 也不进分子——比值恒 ≤1(与 P1-1
    同一「超 1 比值毁掉指标自洽性」纪律)."""
    reset = _step(
        "ps_reset0000001",
        status="pending",
        depends_on=("ps_up0000000001",),
        payload_json=None,
        input_manifest_json=json.dumps(
            {"expected": ["ps_up0000000001"], "received": [], "degraded_inputs": ["ps_up0000000001"], "excluded_inputs": []}
        ),
    )
    out = build_plan_provenance(plan=_plan(), steps=[reset], tool_events_by_turn={}, sections=[])
    assert out["completeness"]["steps_with_manifest"] == "0/0"


def test_tool_event_types_single_source() -> None:
    assert tool_event_types() == ("tool_result",)
