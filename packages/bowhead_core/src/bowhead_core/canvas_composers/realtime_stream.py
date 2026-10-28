"""Realtime canvas streaming — after each tool completes, produce sections for it
and append to canvas_store + emit SSE. Users see Data/SQL tab populate during turn
instead of only at turn end.

Complement to `answer_decomposer`'s end-of-turn aggregation:
    - turn mid: per-tool section streamed here
    - turn end: decompose_answer with `skip_streamed_kinds` so we don't duplicate

Relies on the same extractors in `answer_decomposer`（单调用场景）.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.events import OrchestratorEvent, TurnEventBus

if TYPE_CHECKING:
    from bowhead_core.types import ToolInvocation

logger = logging.getLogger(__name__)


def build_realtime_sections_for_inv(
    inv: ToolInvocation,
    *,
    turn_id: str = "",
) -> list[dict[str, Any]]:
    """对单个 ToolInvocation 产出可流式的 canvas section payload 列表.

    Sprint ι: replace `_extract_sql_blocks` path with `_map_code_block` (3
    triggers: python_sandbox / bi_ask_question / bi_attribute fallback). Each
    emitted dict carries `turn_id` for cross-turn safety (popover §10.1).

    section 的 meta.source_call_id 用来在末尾 compose 时判重.
    返回 [{kind, ...payload}] 列表；handler 端自行 append_section + SSE emit.
    """
    from bowhead_core.canvas_composers.mappers import MapperInput, _map_code_block
    from bowhead_core.types import IntentType

    if inv.error or not inv.result:
        return []

    out: list[dict[str, Any]] = []
    call_id = _call_id(inv)

    try:
        # `_map_code_block` accepts a MapperInput; intent/question are not used
        # by code_block trigger logic. turn_id flows in via mi.turn_id.
        mi = MapperInput(
            intent=IntentType.AMBIGUOUS,
            question="",
            tool_invocations=[inv],
            turn_id=turn_id,
        )
        for payload in _map_code_block(mi):
            meta = payload.setdefault("meta", {})
            meta.setdefault("source_call_id", call_id)
            out.append({"kind": "code_block", **payload})
    except Exception as e:
        logger.warning(
            "realtime_stream.code_block_failed tool=%s err=%s",
            inv.tool_name,
            e,
        )

    return out


def _call_id(inv: ToolInvocation) -> str:
    """从 inv 提取 call_id；新版 ToolInvocation 有 call_id 字段，老版没有就用
    tool_name+arguments hash 作稳定键."""
    cid = getattr(inv, "call_id", "") or ""
    if cid:
        return str(cid)
    # fallback: name+args hash
    import hashlib
    import json as _json

    try:
        raw = _json.dumps(inv.arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(inv.arguments)
    h = hashlib.md5(f"{inv.tool_name}:{raw}".encode()).hexdigest()[:12]
    return f"inv_{h}"


async def stream_sections_for_inv(
    inv: ToolInvocation,
    *,
    canvas_store: Any | None,
    session_id: str,
    workspace_id: str,
    user_id: str,
    parent_bus: TurnEventBus | None,
    turn_id: str,
) -> list[str]:
    """实时把单 tool 产出的 section 写到 canvas_store + SSE 广播.

    返回 emit 的 section_id 列表（给 compose 端判重可用）.
    `canvas_store` 为 None 时 no-op.
    """
    if canvas_store is None or not session_id:
        return []
    # Sprint ι r2 P1 #3: forward turn_id into mappers for cross-turn safety.
    payloads = build_realtime_sections_for_inv(inv, turn_id=turn_id)
    if not payloads:
        return []
    try:
        canvas = await canvas_store.ensure_canvas(
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("realtime_stream.ensure_canvas_failed turn=%s err=%s", turn_id, e)
        return []

    stored_ids: list[str] = []
    failed_count = 0
    for p in payloads:
        kind = p.pop("kind")
        try:
            stored = await canvas_store.append_section(
                canvas_id=canvas.id,
                kind=kind,
                payload=p,
                by_turn_id=turn_id,
            )
        except Exception as e:
            logger.warning(
                "realtime_stream.append_failed turn=%s kind=%s err=%s",
                turn_id,
                kind,
                e,
            )
            failed_count += 1
            continue
        stored_ids.append(stored.id)
        if parent_bus is not None:
            try:
                await parent_bus.emit(
                    OrchestratorEvent(
                        event_type="canvas.section_added",  # type: ignore[arg-type]
                        turn_id=turn_id,
                        payload={
                            "canvas_id": canvas.id,
                            "section": stored.to_report_section(),
                            "seq": stored.seq,
                        },
                    )
                )
            except Exception as e:
                logger.warning("realtime_stream.emit_failed err=%s", e)
    return stored_ids
