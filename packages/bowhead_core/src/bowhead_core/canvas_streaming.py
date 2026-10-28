"""Canvas multi-segment streaming SSE helpers (P3.5).

Frontend canvas-store 4 reducers 监听:
  - canvas.turn_streaming       — turn 开始 (整 turn 锁)
  - canvas.section_streaming    — section 占位创建
  - canvas.section_chunk        — token-level delta apply
  - canvas.section_done         — section 完成 + 锁解 (last section + canvas.done)

Backend composer / narrator 需要 token-level stream 时 import 这些 emit_* 函数.
现 narrator 是一次性出整 section (canvas.section_added), 这 4 events 给 P4 流式 narrator 准备.
"""

from __future__ import annotations

import logging
from typing import Any

from bowhead_core.events import OrchestratorEvent, TurnEventBus

logger = logging.getLogger(__name__)


async def emit_turn_streaming(bus: TurnEventBus | None, turn_id: str) -> None:
    """Turn 开始 — 前端整 turn 锁开.

    ⚠ report-streaming P1 之后**一般不需要显式调用**：`TurnEventBus.emit` 会在本 turn
    第一条画布产内容事件之前自动补发（`_CANVAS_TURN_OPENERS`），且 exactly-once。
    这里保留是为了「明确知道自己要先上锁」的路径；重复调用会被 bus 幂等压掉。
    全库当前 0 调用方。
    """
    if bus is None:
        return
    try:
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.turn_streaming",
                turn_id=turn_id,
                payload={"turn_id": turn_id},
            )
        )
    except Exception as e:
        logger.warning("canvas.turn_streaming emit failed: %s", e)


async def emit_section_streaming(
    bus: TurnEventBus | None,
    *,
    turn_id: str,
    section_id: str,
    kind: str,
    partial_payload: dict[str, Any] | None = None,
) -> None:
    """Section 占位创建 (前端 hydrate 一个空 section, 等 chunks)."""
    if bus is None:
        return
    try:
        payload: dict[str, Any] = {"section_id": section_id, "kind": kind}
        if partial_payload:
            payload["partial_payload"] = partial_payload
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_streaming",
                turn_id=turn_id,
                payload=payload,
            )
        )
    except Exception as e:
        logger.warning("canvas.section_streaming emit failed: %s", e)


async def emit_section_chunk(
    bus: TurnEventBus | None,
    *,
    turn_id: str,
    section_id: str,
    delta: str,
) -> None:
    """Token-level delta — 前端 PM transaction 流式 append."""
    if bus is None or not delta:
        return
    try:
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_chunk",
                turn_id=turn_id,
                payload={"section_id": section_id, "delta": delta},
            )
        )
    except Exception as e:
        logger.warning("canvas.section_chunk emit failed: %s", e)


async def emit_section_done(
    bus: TurnEventBus | None,
    *,
    turn_id: str,
    section_id: str,
    payload: dict[str, Any],
    is_partial: bool = False,
) -> None:
    """Section 完成 — 前端固化最终 payload, status:done."""
    if bus is None:
        return
    try:
        body: dict[str, Any] = {
            "section_id": section_id,
            "payload": payload,
        }
        if is_partial:
            body["is_partial"] = True
        await bus.emit(
            OrchestratorEvent(
                event_type="canvas.section_done",
                turn_id=turn_id,
                payload=body,
            )
        )
    except Exception as e:
        logger.warning("canvas.section_done emit failed: %s", e)


# ──────────────────────────────────────────────────────
# B-5: Skill trace step (frontend SkillCard listens for this event to render)
# ──────────────────────────────────────────────────────


async def emit_trace_skill(
    bus: TurnEventBus | None,
    *,
    turn_id: str,
    skill_id: str,
    name: str | None = None,
    status: str = "running",  # running / success / error
    inputs: dict[str, Any] | None = None,
    output: str | None = None,
    error_msg: str | None = None,
    dur_ms: int | None = None,
) -> None:
    """
    B-5: Emit `trace.step` event with kind="skill" — 前端 SkillCard 接此渲染.

    调用点:subagent_graph.py 检测 tool_call.name == "skill_invoke" 时调一次
    (running) → 拿到 result/error 后再调一次 (success/error + output + dur_ms).

    前端 lib/types.ts TraceStep 的 kind 已支持 "skill", 此函数仅传值即可.
    """
    if bus is None:
        return
    try:
        body: dict[str, Any] = {
            "kind": "skill",
            "skill_id": skill_id,
            "status": status,
        }
        if name:
            body["name"] = name
        if inputs is not None:
            body["inputs"] = inputs
        if output is not None:
            body["output"] = output
        if error_msg is not None:
            body["error_msg"] = error_msg
        if dur_ms is not None:
            body["dur_ms"] = dur_ms
        await bus.emit(
            OrchestratorEvent(
                event_type="trace.step",
                turn_id=turn_id,
                payload=body,
            )
        )
    except Exception as e:
        logger.warning("trace.step skill emit failed: %s", e)


# ──────────────────────────────────────────────────────
# B-6: SSE keepalive / streaming pause/error detection
# ──────────────────────────────────────────────────────


async def emit_streaming_state(
    bus: TurnEventBus | None,
    *,
    turn_id: str,
    state: str,  # active / paused / error
    reason: str | None = None,
) -> None:
    """
    B-6: 让前端 (W3-3) 显视觉指示。
    - active:正常流(可不发,默认状态)
    - paused:SSE stall (e.g. 5s 无 chunk) — 前端显黄底"暂停"
    - error:流中断(网络 / backend 异常) — 前端显红底"中断"

    调用方:gateway streaming 处装定时器,N 秒无 chunk 触发 paused;
            异常 catch 触发 error.
    """
    if bus is None:
        return
    try:
        body: dict[str, Any] = {"state": state}
        if reason:
            body["reason"] = reason
        await bus.emit(
            OrchestratorEvent(
                event_type="streaming.state",
                turn_id=turn_id,
                payload=body,
            )
        )
    except Exception as e:
        logger.warning("streaming.state emit failed: %s", e)


async def emit_keepalive(bus: TurnEventBus | None, *, turn_id: str) -> None:
    """
    B-6: SSE heartbeat — 周期 emit (建议 15s) 让 frontend 探活.
    若 frontend 30s 未收 keepalive → 视为 error(本地 timeout).
    """
    if bus is None:
        return
    try:
        await bus.emit(
            OrchestratorEvent(
                event_type="keepalive",
                turn_id=turn_id,
                payload={"ts": __import__("time").time()},
            )
        )
    except Exception as e:
        logger.warning("keepalive emit failed: %s", e)
