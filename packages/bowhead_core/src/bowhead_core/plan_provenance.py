"""Plan provenance 导出拼装器 (graph-mode 批4, batch4-provenance-design D-D).

typed 台账四边(PRD R4.1), 全部从既有数据推导——无新表无迁移:
- PRODUCES:  step → payload(payload_json/result_digest) + insight_section_ids
- DERIVED_FROM: step ← input_manifest 实收上游(批3 落列)
- USED:      step → tool_result 事件(subagent_id=f"sub_{step_id}" derive, 批4 D-A)
- SUPERSEDES: 修订/重规划指针(superseded_by, 批3 落列)

section 归属双粒度(D-B): fine=owner 编码(writer-{prof}-{step_id})∪insight_section_ids
反查; coarse=section.turn_id → 该 turn 的 plan 步集合。事件缺失(老 plan/retention
扫过)→ 空值 + completeness 如实计数, 禁启发式模糊 join(审计特征)。
"""

from __future__ import annotations

import json
from typing import Any

from .plan_verify import outcome_from_json

_TOOL_EVENT_TYPES = ("tool_result",)


def tool_event_types() -> tuple[str, ...]:
    """route 侧取数用的事件类型集(单一真源)。"""
    return _TOOL_EVENT_TYPES


def _manifest_dict(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _owner_step_id(owner: str | None, step_ids: set[str]) -> str | None:
    """owner → step id。两代格式:
    - 'writer-{prof}-{ps_hex12}': WP-D 多写手期的域写手章。rsplit 取末段并验
      前缀+在册(review P2-5: profile 名自带连字符, naive split 拿错段)。
    - 'sub_ps_{hex12}' (delivery-surface review #14): M-C 发笔后 plan 步的 peer
      亲笔 narrative, owner=per-spawn id(plan 路预铸 `sub_{step_id}`,
      agent_runner_adapter)——剥 'sub_' 即 step id。
    两代都验在册, 不在册返 None(禁模糊 join 的审计口径不变)。"""
    if not owner:
        return None
    if owner.startswith("writer-"):
        tail = owner.rsplit("-", 1)[-1]
        return tail if tail.startswith("ps_") and tail in step_ids else None
    if owner.startswith("sub_ps_"):
        tail = owner[len("sub_") :]
        return tail if tail in step_ids else None
    return None


def build_plan_provenance(
    *,
    plan: Any,
    steps: list[Any],
    tool_events_by_turn: dict[str, list[Any]],
    sections: list[Any],
) -> dict[str, Any]:
    """纯函数拼装(route 薄壳取数)。tool_events_by_turn: turn_id → tool_result
    事件行(TurnEventRow: .event_type/.payload);sections: canvas Section 行。"""
    step_ids = {s.id for s in steps}
    steps_out: list[dict[str, Any]] = []
    n_with_tools = 0
    non_done_with_tools: dict[str, int] = {}  # status → count (batch 4 debt: old bucket hardcoded failed).
    n_with_manifest = 0
    n_expect_manifest = 0
    insight_owner: dict[str, str] = {}  # section_id → step_id (fine-1 reverse lookup).

    for s in steps:
        derived = f"sub_{s.id}"
        used: list[dict[str, Any]] = []
        for ev in tool_events_by_turn.get(s.turn_id or "", ()):
            p = getattr(ev, "payload", None) or {}
            if p.get("subagent_id") != derived:
                continue
            used.append(
                {
                    "call_id": p.get("call_id"),
                    "tool_name": p.get("tool_name"),
                    "ok": p.get("ok"),
                    "duration_ms": p.get("duration_ms"),
                }
            )
        payload: dict[str, Any] = {}
        if s.payload_json:
            try:
                _pv = json.loads(s.payload_json)
                payload = _pv if isinstance(_pv, dict) else {}
            except (ValueError, TypeError):
                payload = {}
        insight_ids = [str(x) for x in (payload.get("insight_section_ids") or []) if x]
        for sec_id in insight_ids:
            insight_owner.setdefault(sec_id, s.id)
        manifest = _manifest_dict(getattr(s, "input_manifest_json", None))
        # Batch 4 debt: denominator only steps expected to have manifests; driver writes only at dispatch,
        # and `_build_input_manifest` returns None for dependency-free steps (by design, zero noise). Old rule
        # used all steps, treating roots and never-dispatched steps as gaps (normal 3-parallel +1-summary plan
        # reported "1/4"), systematically undercounting; conversely, pending-reset steps retaining old manifests
        # also stay out of numerator, keeping ratio ≤1.
        _expects_manifest = bool(s.depends_on) and str(s.status) in ("running", "done", "failed", "superseded")
        if _expects_manifest:
            n_expect_manifest += 1
            if manifest:
                n_with_manifest += 1
        # Implementation review P1-1: numerator only done steps (denominator=done_n); non-done tool events
        # are D-A evidence but counted separately, or ratios like "3/2" undermine headline consistency.
        # Batch 4 debt: split separate buckets by status; old name hardcoded failed but included superseded/
        # running/blocked; misnamed audit counts are worse than missing numbers.
        if used and str(s.status) == "done":
            n_with_tools += 1
        elif used:
            _st = str(s.status) or "unknown"
            non_done_with_tools[_st] = non_done_with_tools.get(_st, 0) + 1
        _vo = outcome_from_json(getattr(s, "verify_json", None))
        steps_out.append(
            {
                "id": s.id,
                "agent": s.agent_name,
                "status": str(s.status),
                "origin": str(getattr(s, "origin", "") or ""),
                "level": s.level,
                "depends_on": list(s.depends_on),
                "supersedes": None,  # Filled in second pass (reverse pointer).
                "superseded_by": getattr(s, "superseded_by", None),
                "verify": ({"verdict": _vo.verdict, "unmet": list(_vo.unmet_texts())} if _vo is not None else None),
                "produces": {
                    "has_payload": bool(s.payload_json),
                    "result_digest": getattr(s, "result_digest", None),
                    "insight_section_ids": insight_ids,
                },
                "derived_from": (
                    {
                        "received": list(manifest.get("received") or []),
                        "degraded_inputs": list(manifest.get("degraded_inputs") or []),
                        "excluded_inputs": list(manifest.get("excluded_inputs") or []),
                    }
                    if manifest
                    else None
                ),
                "subagent_id": derived,
                "used_tools": used,
                "tokens": {"input": s.tokens_input, "output": s.tokens_output},
                "turn_id": s.turn_id,
            }
        )

    # SUPERSEDES reverse edge: old.superseded_by="ps_x" → new(ps_x).supersedes=old.
    by_id = {d["id"]: d for d in steps_out}
    for d in steps_out:
        ptr = d["superseded_by"] or ""
        if ptr.startswith("ps_") and ptr in by_id:
            by_id[ptr]["supersedes"] = d["id"]

    plan_turn_ids = {s.turn_id for s in steps if s.turn_id}
    sections_out: list[dict[str, Any]] = []
    n_traceable = 0
    n_coarse_only = 0
    n_plan_sections = 0
    n_foreign = 0
    for sec in sections:
        owner_step = _owner_step_id(getattr(sec, "owner", None), step_ids)
        fine = owner_step or insight_owner.get(sec.id)
        is_plan_section = sec.turn_id in plan_turn_ids
        composed_in = [s.id for s in steps if s.turn_id == sec.turn_id and str(s.status) == "done"] if is_plan_section else []
        # Implementation review P2-3: denominator only plan-turn sections; in mixed sessions (chat then
        # plan), pure chat sections are not plan deliverables; including them misleads audits.
        #
        # 116-dogfood P1: old numerator used `fine or composed_in`, but `composed_in` = **all done steps
        # in this turn**, the same constant for every section in that turn. For single-turn plans
        # (vast majority), one done step makes condition true for every section → ratio **always equals denominator**,
        # structurally hiding all gaps. Metric specifically asks which step produced this section
        # (design D-B fine-1/fine-2); coarse is supporting evidence only. Including coarse in numerator = passing off "steps ran this turn"
        # as "attributable to a step", like batch 1 dead flag passing completion count off as verification count.
        # Actual run: only 4 of 30 sections had fine attribution; old rule still reported 30/30.
        # Fix: numerator accepts fine only; separate coarse-only bucket (both buckets sum to denominator, no information lost).
        if is_plan_section or fine:
            n_plan_sections += 1
            if fine:
                n_traceable += 1
            else:
                n_coarse_only += 1
        else:
            n_foreign += 1
        sections_out.append(
            {
                "id": sec.id,
                "kind": sec.kind,
                "turn_id": getattr(sec, "turn_id", None),
                "owner": getattr(sec, "owner", None),
                "produced_by_step": fine,
                "composed_in_steps": composed_in,
            }
        )

    done_n = sum(1 for s in steps if str(s.status) == "done")
    return {
        "plan": {
            "id": plan.id,
            "status": str(plan.status),
            "title": plan.title,
            "current_rev": getattr(plan, "current_rev", 1),
            "replan_count": getattr(plan, "replan_count", 0),
        },
        "steps": steps_out,
        "sections": sections_out,
        "completeness": {
            # Numerator = sections attributable to **a specific step** (owner encoding / insight reverse lookup).
            "sections_traceable": f"{n_traceable}/{n_plan_sections}",
            # Only coarse (same-turn) evidence, no step attribution; complements numerator to form denominator.
            "sections_coarse_only": n_coarse_only,
            "foreign_sections": n_foreign,
            "steps_with_tool_events": f"{n_with_tools}/{done_n}",
            # Split by status (non-done step tool evidence, excluded from above ratio).
            "non_done_steps_with_tool_events": dict(sorted(non_done_with_tools.items())),
            # Denominator = dispatched steps with upstream dependencies (others have no manifest by design).
            "steps_with_manifest": f"{n_with_manifest}/{n_expect_manifest}",
        },
    }
