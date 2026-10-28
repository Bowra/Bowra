"""TaskPlan H2 approval WebSocket route (sprint multi-agent-v1.5 PR-#20).

支持 frontend TaskPlanCard 的 emit/wait_decision 双向 message:
- Server emit `task_plan` → frontend 渲染 TaskPlanCard
- Frontend post `decision` → server 解析 (含 nonce + decided_at signed msg)
- Backend orchestrator 拿 decision (resolve future) 继续 dispatch

per-session 单 active future (同 session 同一时间只有 1 个 plan 在等)。

Wire to orchestrator:
    from bowhead_gateway.routes.task_plan import (
        get_task_plan_service_for_session,
    )
    orchestrator.task_plan_service = get_task_plan_service_for_session(session_id)

Note: 此实现 per-process state. 多 worker 部署需走 redis pubsub (留 v2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from typing import Any

from bowhead_core.agents.task_plan import (
    TaskPlanCardService,
    TaskPlanOutcome,
    UserDecision,
)
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ws", tags=["task-plan"])


# ─────────────────────────────────────────────────────────────
# Per-session WS connection + pending decision state
# ─────────────────────────────────────────────────────────────

# session_id → active websocket connection
_active_ws: dict[str, WebSocket] = {}

# session_id → asyncio.Future awaiting user decision (resolved by WS message)
_pending_decisions: dict[str, asyncio.Future[UserDecision]] = {}


def _serialize_plan(plan_obj: Any) -> dict[str, Any]:
    """TaskPlan dataclass → JSON-serializable dict."""
    if hasattr(plan_obj, "__dataclass_fields__"):
        d = asdict(plan_obj)
        # Convert Complexity enum to str.
        if "complexity" in d and hasattr(d["complexity"], "value"):
            d["complexity"] = d["complexity"].value
        return d
    return dict(plan_obj)


# ─────────────────────────────────────────────────────────────
# WebSocket route
# ─────────────────────────────────────────────────────────────


@router.websocket("/task-plan/{session_id}")
async def task_plan_ws(websocket: WebSocket, session_id: str) -> None:
    """WS endpoint per-session: receive user decision messages.

    Server-to-client: `{"type": "task_plan", "plan": {...}}` 当 orchestrator emit.
    Client-to-server: `{"type": "decision", "outcome": ..., "nonce": ..., "decided_at": ..., "edited_plan": ...}`.
    """
    await websocket.accept()
    # authz: only session owners may connect (previously no checks after accept: knowing session_id allowed plan reads +
    # decision injection). resolve_ws_user_id: non-dev anonymous → None; owner mismatch → close without reading/sending anything.
    from bowhead_gateway.routes.auth import resolve_ws_user_id

    _uid = resolve_ws_user_id(websocket)
    _authorized = _uid is not None
    if _authorized:
        _ss = getattr(websocket.app.state, "session_store", None)
        if _ss is not None:
            _sess = await _ss.get_session(session_id)
            _authorized = _sess is not None and _sess.user_id == _uid
    if not _authorized:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # Only 1 active WS per session: new connections close old ones to avoid races.
    if session_id in _active_ws:
        with contextlib.suppress(Exception):
            await _active_ws[session_id].close(code=status.WS_1000_NORMAL_CLOSURE)
    _active_ws[session_id] = websocket
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "decision":
                _handle_decision(session_id, msg)
            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                logger.debug("task-plan.ws unknown_msg session=%s type=%s", session_id, mtype)
    except WebSocketDisconnect:
        logger.info("task-plan.ws disconnected session=%s", session_id)
    except Exception as e:
        logger.warning("task-plan.ws error session=%s err=%s", session_id, e)
    finally:
        _active_ws.pop(session_id, None)


def _handle_decision(session_id: str, msg: dict[str, Any]) -> None:
    """Resolve pending future with user decision."""
    future = _pending_decisions.get(session_id)
    if future is None or future.done():
        logger.debug("task-plan.decision no pending future session=%s", session_id)
        return
    try:
        decision = UserDecision(
            outcome=TaskPlanOutcome(msg.get("outcome", "timeout")),
            nonce=str(msg.get("nonce", "")),
            decided_at=int(msg.get("decided_at", 0)),
            edited_plan=msg.get("edited_plan"),
        )
        future.set_result(decision)
    except Exception as e:
        logger.warning("task-plan.decision parse err=%s msg=%s", e, msg)


# ─────────────────────────────────────────────────────────────
# Service factory — given to orchestrator at turn start
# ─────────────────────────────────────────────────────────────


def get_task_plan_service_for_session(session_id: str) -> TaskPlanCardService:
    """Build TaskPlanCardService bound to this session's WS state.

    `emit_plan`: send plan over WS if connected; if no WS, log and rely on timeout.
    `emit_warning`: same.
    `wait_decision`: await future on `_pending_decisions[session_id]`.
    """

    async def _emit_plan(plan: Any) -> None:
        ws = _active_ws.get(session_id)
        if ws is None:
            logger.warning("task-plan.emit_plan no WS connected session=%s", session_id)
            return
        try:
            await ws.send_json(
                {
                    "type": "task_plan",
                    "plan": _serialize_plan(plan),
                }
            )
        except Exception as e:
            logger.warning("task-plan.emit_plan failed session=%s err=%s", session_id, e)

    async def _emit_warning(plan: Any, after_s: int) -> None:
        ws = _active_ws.get(session_id)
        if ws is None:
            return
        try:
            await ws.send_json(
                {
                    "type": "task_plan_warning",
                    "after_s": after_s,
                    "nonce": getattr(plan, "nonce", ""),
                }
            )
        except Exception as e:
            logger.warning("task-plan.emit_warning failed session=%s err=%s", session_id, e)

    async def _wait_decision(_sid: str, timeout_s: int) -> UserDecision | None:
        # Use caller-supplied sid (same as closure session_id).
        loop = asyncio.get_event_loop()
        future: asyncio.Future[UserDecision] = loop.create_future()
        _pending_decisions[session_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            return None
        finally:
            _pending_decisions.pop(session_id, None)

    return TaskPlanCardService(
        emit_plan=_emit_plan,
        emit_warning=_emit_warning,
        wait_decision=_wait_decision,
        total_timeout_s=600,
        warnings_at_s=(60, 300, 540),
    )


# ─────────────────────────────────────────────────────────────
# Diagnostic endpoint (for ops)
# ─────────────────────────────────────────────────────────────


@router.get("/task-plan/_status")
async def task_plan_status(request: Request) -> dict[str, Any]:
    """ops debug: 看当前 active WS / pending future 数. **authed only** —— 原 public, 泄漏所有
    active session_id (配合原无鉴权 WS = 任意注入 decision 的素材)."""
    from bowhead_gateway.routes.auth import resolve_user_id_sync

    resolve_user_id_sync(request)  # 401 if unauthenticated (non-dev anon)
    return {
        "active_ws_sessions": list(_active_ws.keys()),
        "pending_decisions": list(_pending_decisions.keys()),
    }
