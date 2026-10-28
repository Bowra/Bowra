"""SSE 事件契约全量守卫 (chat-solidify 2026-07-03, chat-pipeline.md).

历史: 事件契约三处各自漂移(EventType Literal / 真 emit 集 / FE 白名单), 造成
≥4 轮同款事故 — agent_spawned 丢→「Lead 消失」、V4 气泡全空、planning 卡在真
浏览器不出、citation 点击无效。此前唯一守卫只盖 plan 事件(plan-sse-whitelist
.test.ts); 本测试把三方一致性锁成双向断言:

  A. 后端字面 emit 的每个事件名 ∈ EventType Literal(禁 type:ignore 混入)
  B. 后端 FE-facing 事件 ⊆ FE 白名单(wire() ∪ canvas-sse) — 防「发了浏览器丢」
  C. FE 白名单每个名 ∈ 后端 emit 集 ∪ 显式 extra — 防死 handler 再堆积
  D. FE 白名单每个名在 use-turn-runner 或 turn-store-dispatch 有 case — 防挂名 no-op

新事件接入流程(四处考虑): EventType Literal + 后端 emit + FE wire()/router +
IM im/turn_render.py fold。任何一侧变更若破坏本测试, 先读 chat-pipeline.md。
静态扫描按字面量抓 emit; 动态转发(如 decomposer 的 emit 回调)不在扫描面内 —
它们的名字必须同时存在字面 emit 站点, 否则加进 _BE_INTERNAL 并写明理由。
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import get_args

from bowhead_core.events import EventType
from bowhead_core.terminal_protocol import TERMINAL_READER_RESERVED_EVENT_TYPES

_PKGS = Path(__file__).resolve().parents[2]  # .../packages
_CORE_SRC = _PKGS / "bowhead_core" / "src" / "bowhead_core"
_GW_SRC = _PKGS / "bowhead_gateway" / "src" / "bowhead_gateway"
_FE = _PKGS / "bowhead_frontend" / "lib"

_RUNNER = (_FE / "use-turn-runner.ts").read_text(encoding="utf-8")
_CANVAS_SSE = (_FE / "canvas-sse.ts").read_text(encoding="utf-8")
_DISPATCH = (_FE / "turn-store-dispatch.ts").read_text(encoding="utf-8")

_NAME_RE = r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*"

# ── Collect literal backend emits ──────────────────────────────────────────
# Scope matters: bare `event_type="x"` includes other event systems such as audit logs/notification center
# (login_success / memory.entry.* …), so OrchestratorEvent construction must anchor
# event_type search within the window after `OrchestratorEvent(`.
_EVENT_TYPE_IN_WINDOW = re.compile(rf'event_type\s*=\s*"({_NAME_RE})"')
_ORCH_EVENT_OPEN = re.compile(r"OrchestratorEvent\(")
_EMIT_PATTERNS = (
    re.compile(rf'OrchestratorEvent\(\s*"({_NAME_RE})"'),  # Positional arguments.
    # Terminal runtime can persist a reader-visible marker directly through
    # the fenced writer instead of constructing an OrchestratorEvent first.
    re.compile(rf'_writer\.accept_event\(\s*event_type\s*=\s*"({_NAME_RE})"'),
    # 3 fixed arguments (bus, turn_id, "name"); third must be literal; exclude dynamic forwarding
    # wrapper (`_emit_event(bus, sctx.turn_id, name, {...})` would falsely capture payload keys).
    re.compile(rf'_emit_event\(\s*[^,"]+,\s*[^,"]+,\s*"({_NAME_RE})"'),
    # plan_driver hooks: `\(\s*` tolerates ruff format wrapping long calls across lines
    # (`_emit(\n    hooks,\n    "step_blocked"`); after repository fmt in 2026-07,
    # single-line pattern once missed step_blocked/plan_blocked, causing false failures.
    re.compile(rf'_emit\(\s*hooks,\s*"({_NAME_RE})"'),
    # orch._event / canvas_composer._event short helpers; negative lookbehind blocks
    # long audit-domain names such as emit_ref_audit_event (not on turn bus).
    re.compile(rf'(?<![A-Za-z0-9_])_event\(\s*[^,()]+,\s*"({_NAME_RE})"'),
)
# Final declarations: constant name -> event string. cca triggers store event names in Final constants, then
# emit using `make_event(_EVENT, ...)` variables, missed by literal patterns.
_FINAL_DECL = re.compile(rf'([A-Za-z_]\w*)\s*:\s*Final\s*=\s*"({_NAME_RE})"')
# Variable event positions: make_event(IDENT / OrchestratorEvent(IDENT / event_type=IDENT.
# ⚠ code-review 2026-07-04 gap: old `loose & declared` counted Final constants only if already declared;
# new cca-style events omitted from Literal + FE whitelist never entered emitted set,
# allowing emitted-but-browser-dropped events through a green guard, defeating its purpose. Now, any IDENT resolving to Final map
# counts as **actual emit** (no & declared), so test_backend_emits_are_declared catches
# missing declarations. Resolve only IDENTs in Final map to avoid including non-event variables.
_VAR_EVENT_REFS = (
    re.compile(r"make_event\(\s*([A-Za-z_]\w*)"),
    re.compile(r"OrchestratorEvent\(\s*([A-Za-z_]\w*)\s*[,)]"),
    re.compile(r"event_type\s*=\s*([A-Za-z_]\w*)\b"),
)
# Domain-scoped pattern: direct "event": frames recognized only in chat SSE generator.
# (Previously also had a `self._emit` pattern specific to subagent_graph.py; that file and LangGraph
# backend retired in 2026-08, so scanning branch removed too; do not leave a dead rule that never matches.)
_RAW_FRAME = re.compile(rf'"event":\s*"({_NAME_RE})"')


@functools.lru_cache(maxsize=1)
def _collect_backend_emits() -> set[str]:
    # code-review 2026-07-04: five test calls would rglob+regex scan both source trees five times.
    # Result unchanged throughout test session → compute once, reuse via lru_cache (set reused immutably;
    # callers must not mutate it in place).
    names: set[str] = set()
    for root in (_CORE_SRC, _GW_SRC):
        for py in root.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for pat in _EMIT_PATTERNS:
                names.update(pat.findall(text))
            # Build Final map **per file**: three cca triggers all use `_EVENT` with different values;
            # a global map overwrites all but the last. Declaration and use share a file (as in cca), so resolve within each file.
            final_map = dict(_FINAL_DECL.findall(text))
            for pat in _VAR_EVENT_REFS:
                for ident in pat.findall(text):
                    if ident in final_map:
                        names.add(final_map[ident])
            if py.name == "chat.py":
                names.update(_RAW_FRAME.findall(text))
            for m in _ORCH_EVENT_OPEN.finditer(text):
                win = text[m.end() : m.end() + 300]
                got = _EVENT_TYPE_IN_WINDOW.search(win)
                if got:
                    names.add(got.group(1))
    return names


# ── Parse FE whitelist ─────────────────────────────────────────────────────
def _fe_wire_whitelist() -> set[str]:
    """wire() 的 addEventListener 数组(锚定首元素 turn_started) + 单独 listener。"""
    m = re.search(r'\(\[\s*("turn_started".*?)\]\s*as\s*const\)', _RUNNER, re.S)
    assert m, "use-turn-runner.ts wire() 白名单数组没找到 — 解析器需随源码更新"
    # Strip // comments first: historical array comments mention deleted events ("turn.plan" etc.), excluded from whitelist.
    block = re.sub(r"//[^\n]*", "", m.group(1))
    names = set(re.findall(rf'"({_NAME_RE})"', block))
    names.update(re.findall(rf'es\.addEventListener\("({_NAME_RE})"', _RUNNER))
    return names


def _fe_canvas_whitelist() -> set[str]:
    m = re.search(r"const\s+entries[^=]*=\s*\[(.*?)\n\s*\];", _CANVAS_SSE, re.S)
    block = m.group(1) if m else _CANVAS_SSE
    return set(re.findall(rf'\[\s*"({_NAME_RE})"\s*,', block))


def _fe_router_cases() -> set[str]:
    cases = set(re.findall(rf'case\s+"({_NAME_RE})"', _RUNNER))
    cases |= set(re.findall(rf'case\s+"({_NAME_RE})"', _DISPATCH))
    return cases


# ── Explicit ignore/extra lists (each needs a reason; read chat-pipeline.md before adding) ──
_BE_INTERNAL = {
    "peer.iter": "subagent 迭代遥测, FE 不渲染",
    "llm.call_timing": "逐次模型调用首 chunk/总耗时遥测(e027f2296, chat-pipeline 2026-09-27: 内部事件, FE 不听)",
    "notice": "subagent 内部提示, 暂无 FE 消费者(接 UI 前先过 §6 契约一)",
    "trace.step": "canvas_streaming 时间线遥测(2026-07-03 从 FE 白名单摘除)",
    "tool_finding": "chat-v3 遗留遥测; step_done.summary 已覆盖该信息",
    "payload_missing_guard": "canvas composer 遥测",
    "turn.satisfaction_recorded": "满意度回执(HTTP 响应已含结果)",
    "clarification.directive_cancelled": "directive 取消回执(HTTP 响应已含结果); IM 可选消费",
    "turn_cancelled": "web FE 靠后续 done{cancelled} 收尾; IM turn_render 消费",
    "turn.step_retry_requested": "retry stub ack(O5 假按钮已摘, 事件留 API 兼容)",
    "turn.step_skip_requested": "skip stub ack(同上)",
    "interrupted_check_actions": "S5 durable effect-uncertainty evidence; S6 projects the committed receipt manifest to Web/IM",
}

# Reader-first names are not normal OrchestratorEvent emitters. The disclosure
# event may be appended only by its receipt-fenced post-terminal Store seam.
# Keep this manifest separate from _BE_INTERNAL: moving an entry out of here
# requires a real producer and the normal FE/IM contract review.
_BE_READER_RESERVED = {
    "turn.effect_disclosure_settled": {
        "owner": "durable-chat-execution",
        "reason": "post-terminal content-free effect overlay control state",
        "future_consumer": "effect disclosure hydrate/live overlay reader",
    },
}

_FE_ALLOWED_EXTRA = {
    "clarification.required": "dot 版 replay 兼容(omicron-12 后端发下划线版)",
    "thinking": (
        "thinking 模型摘要竖井: FE 白名单/reducer/state 完整, 但后端 emit 断线(provider 转发路已不存在; chat-pipeline.md 记录, 修复或整删待定)"
    ),
    "plan_preview": ("L1 启发式计划预览竖井 dormant: FE case+runner.plan 渲染块在, 后端 0 emit(workflow_distill 只读历史行; W7 评估整删)"),
    "tool_call": (
        "唯一的后端 emit 点在 subagent_graph.py:134, 随 LangGraph backend 于 2026-08 下线"
        "(Agent SDK 调研 A2)。**FE handler 必须留着**: 116 生产库仍有 17 行 tool_call 存量"
        "事件(退役前的 debate profile 无条件走 graph), 删掉 handler = 那些历史会话重放时"
        "少渲染一段。属于「后端不再发、但存量行还要能放」这一档, 不是死 handler。"
    ),
}

# These names come from SSE generator/dispatch constants; no router case needed:
_NO_ROUTER_CASE_NEEDED = {
    "keepalive",
    "stream.checkpoint",
    "streaming.state",
    # Agent run limits, phase 3: transient progress next to keepalive; the listener only
    # updates runner state, like streaming.state. Never on the bus, persisted or replayed.
    "turn.progress",
    "terminal.protocol_error",
    # Content-free durable marker: the listener must advance the exact event
    # sequence, but no chat reducer should project it.
    "turn.llm_invocation",
}


def test_backend_emits_are_declared() -> None:
    declared = set(get_args(EventType))
    emitted = _collect_backend_emits()
    undeclared = sorted(emitted - declared)
    assert not undeclared, f"后端 emit 了未声明的事件名(禁 type:ignore 混入, 先加进 EventType): {undeclared}"


def test_final_constant_events_are_collected_as_real_emits() -> None:
    """code-review 2026-07-04 洞的回归锁 — cca 触发器经 make_event(Final常量) emit,
    必须被当**真 emit**收集(不再只 loose & declared)。否则新写一个 cca 式事件却漏了
    EventType/白名单, 守卫会全绿放行。这里正向断言现有 cca.* 三兄弟确实被 collect 到,
    锁住"变量形式 emit 也算真 emit"这条机制。"""
    emitted = _collect_backend_emits()
    for name in ("cca.tool_create_open", "cca.subagent_creator_open", "cca.workflow_solidify_open"):
        assert name in emitted, f"{name} 经 make_event(Final常量) emit 却没被 collect → 变量形式解析破了, 守卫对 cca 式新事件失去防护"


def test_fe_facing_emits_are_whitelisted() -> None:
    emitted = _collect_backend_emits()
    fe = _fe_wire_whitelist() | _fe_canvas_whitelist()
    missing = sorted(emitted - fe - set(_BE_INTERNAL))
    assert not missing, (
        f"后端在发、FE 白名单没有 → 浏览器静默丢(历史事故同款)。要么补 wire()/canvas-sse + router, 要么进 _BE_INTERNAL 并写理由: {missing}"
    )


def test_fe_whitelist_has_no_dead_names() -> None:
    emitted = _collect_backend_emits()
    dead = sorted(_fe_wire_whitelist() - emitted - set(_FE_ALLOWED_EXTRA) - TERMINAL_READER_RESERVED_EVENT_TYPES)
    assert not dead, f"FE 白名单里有后端不发的名(死 handler 再堆积): {dead}"


def test_canvas_whitelist_has_no_dead_names() -> None:
    emitted = _collect_backend_emits()
    dead = sorted(_fe_canvas_whitelist() - emitted - set(_FE_ALLOWED_EXTRA))
    assert not dead, f"canvas-sse 白名单里有后端不发的名: {dead}"


def test_wired_names_have_router_case() -> None:
    cases = _fe_router_cases()
    wired = _fe_wire_whitelist() - _NO_ROUTER_CASE_NEEDED - TERMINAL_READER_RESERVED_EVENT_TYPES
    no_case = sorted(n for n in wired if n not in cases)
    assert not no_case, f"白名单挂了名但 use-turn-runner/turn-store-dispatch 均无 case(纯 no-op): {no_case}"


def test_ignore_lists_stay_minimal() -> None:
    """ignore 清单只许列真实存在的名 — 防清单本身腐烂。"""
    emitted = _collect_backend_emits()
    stale = sorted(n for n in _BE_INTERNAL if n not in emitted)
    assert not stale, f"_BE_INTERNAL 列了后端已不发的名(该删): {stale}"


def test_reader_reserved_events_are_exact_declared_and_not_bus_emitted() -> None:
    assert set(_BE_READER_RESERVED) == {
        "turn.effect_disclosure_settled",
    }
    assert set(_BE_READER_RESERVED) == TERMINAL_READER_RESERVED_EVENT_TYPES
    declared = set(get_args(EventType))
    emitted = _collect_backend_emits()
    wired = _fe_wire_whitelist() | _fe_canvas_whitelist()
    for name, metadata in _BE_READER_RESERVED.items():
        assert name in declared
        assert name not in emitted
        assert name not in _BE_INTERNAL
        assert name in wired
        assert set(metadata) == {"future_consumer", "owner", "reason"}
        assert all(isinstance(value, str) and value for value in metadata.values())


def test_internal_events_are_not_wired_to_chat_ui() -> None:
    """An event cannot be both internal-only and browser-visible."""
    overlap = sorted(set(_BE_INTERNAL) & _fe_wire_whitelist())
    assert not overlap, f"_BE_INTERNAL 与 chat SSE 白名单重叠，事件契约说明已腐烂: {overlap}"
