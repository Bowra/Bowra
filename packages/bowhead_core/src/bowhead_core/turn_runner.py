"""Run one admitted Turn through the shared Lead lifecycle.

Orchestrator delegates to this per-turn runner. It binds the caller's execution
context, dispatches the Lead, and handles cancellation, persistence, and
post-turn work. Lead orchestration owns planning, tool execution, and final
answer/report composition. Runtime state stays local to this runner and Turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bowhead_router.errors import InvocationOutcomeUnknownError

from bowhead_core.chat_turn_source import current_attachment_positions
from bowhead_core.published_diagnostics import get_or_build_chat_turn_failure


class TeamModelRuntimeUnavailableError(RuntimeError):
    """A team-scoped turn reached Core without its authenticated team Router."""


def _make_subagent_runner_with_catalog(
    *,
    router: Any,
    tool_registry: Any,
    custom_store: Any,
    product_terms_catalog: Any | None,
    team_subagent_store: Any = None,  # cca P3: UserStore (team_subagent CRUD) for team-scoped resolve
) -> Any:
    """构造 SubagentRunner 并把 i18n product terms catalog 挂到实例上.

    SubagentRunner __init__ 不接 catalog 参数（保持模块签名稳定），通过 attr 注入.
    """
    from bowhead_core.subagent import SubagentRunner

    runner = SubagentRunner(
        router=router,
        tool_registry=tool_registry,
        backend=subagent_backend_from_env(),  # Defined in this module at L1358.
        custom_store=custom_store,
        team_subagent_store=team_subagent_store,
    )
    runner._product_terms_catalog = product_terms_catalog  # type: ignore[attr-defined]
    return runner


def _effective_router(orch: Any, request: Any) -> Any:
    """本 turn 生效的 Router：团队请求只接受 Gateway 注入的 team Router。

    byo-no-clone：per-turn 的 router override 走 TurnRequest（与 datarc_client / vector_client /
    每轮连接句柄同款），而不是重建共享 Orchestrator 单例——`dataclasses.replace(orch, router=…)`
    会丢掉 orch 上动态挂的 sidecar（subagent_runner / plan_store / scheduled_store / …），
    导致 BYO 团队每个 turn 的 subagent_spawn 崩「subagent_runner/bus not configured on context」。

    只有明确没有团队语义的 dev / legacy 内部请求可以使用进程 Router。带 ``team_id``
    却没有 ``team_router`` 时必须 fail closed；否则任何漏接的 continuation 都会跨过团队
    边界落到部署 Router。
    """
    team_router = getattr(request, "team_router", None)
    if team_router is not None:
        return team_router
    if getattr(request, "team_id", None):
        raise TeamModelRuntimeUnavailableError("team_model_runtime_unavailable")
    return getattr(orch, "router", None)


def _effective_kb_retriever(orch: Any, request: Any) -> Any:
    """本 turn 生效的 KB retriever；团队请求也禁止落入进程级模型配置。"""
    team_kb_retriever = getattr(request, "team_kb_retriever", None)
    if getattr(request, "team_id", None):
        # KB is optional, so an unavailable team KB runtime means "no retriever" rather
        # than a hard turn failure.  It must not mean permission to use deployment-backed
        # embeddings/retrieval through orch.kb_retriever.
        return team_kb_retriever
    return team_kb_retriever or getattr(orch, "kb_retriever", None)


async def _resolve_unconfigured_capability_tools(orch: Any, request: Any) -> frozenset[str]:
    """Turn adapter for the shared discovery/execution readiness resolver."""
    from bowhead_core.capability_resolver import resolve_unconfigured_capability_tools

    unconfigured = await resolve_unconfigured_capability_tools(orch, getattr(request, "team_id", "") or "")
    if not getattr(request, "config_lite_supported", False):
        # Delivery tools need the member to confirm in the web chat (same criterion as send_email).
        from bowhead_core.google_delivery_tool import TOOL_NAMES as GOOGLE_DELIVERY_TOOLS

        unconfigured = unconfigured | frozenset(GOOGLE_DELIVERY_TOOLS)
    return unconfigured


def _effective_subagent_runner(orch: Any, request: Any) -> Any:
    """本 turn 生效的 SubagentRunner.

    - **BYO 团队**（request.team_router 有值）：据团队 router **现建**。subagent 的 router 在构造时
      焊死（`SubagentRunner.router`）、运行时不从 ctx 重解析，所以只有现建才能让 peer/compute
      子 agent 真走团队模型。**不读** `orch.subagent_runner`（那是启动时用全局 router 建的单例；
      且历史 clone 路径会把它丢成 None → 崩「subagent_runner/bus not configured on context」）。
    - **显式 no-team dev / legacy**：复用 `orch.subagent_runner`（进程级单例 / 测试注入的 fake），行为与改动前一致；
      单例缺失（无 lifespan 的 harness）时才据 `orch.router` 兜底现建。

    `_make_subagent_runner_with_catalog` 只持引用、无 per-turn 状态、无 I/O，现建极廉价。
    """
    team_router = getattr(request, "team_router", None)
    if team_router is not None:
        return _make_subagent_runner_with_catalog(
            router=team_router,
            tool_registry=getattr(orch, "tool_registry", None),
            custom_store=getattr(orch, "custom_subagent_store", None),
            product_terms_catalog=getattr(orch, "product_terms_catalog", None),
            team_subagent_store=_team_subagent_store(orch),
        )
    if getattr(request, "team_id", None):
        raise TeamModelRuntimeUnavailableError(
            "team_model_runtime_unavailable",
        )
    existing = getattr(orch, "subagent_runner", None)
    if existing is not None:
        return existing
    router = getattr(orch, "router", None)
    if router is None:
        return None
    return _make_subagent_runner_with_catalog(
        router=router,
        tool_registry=getattr(orch, "tool_registry", None),
        custom_store=getattr(orch, "custom_subagent_store", None),
        product_terms_catalog=getattr(orch, "product_terms_catalog", None),
        team_subagent_store=_team_subagent_store(orch),
    )


from bowhead_core.agents.lead_transient import init_lead_transient
from bowhead_core.attachments_injector import PreparedAttachment, prepare_attachments
from bowhead_core.documents.package_io import defer_office_content
from bowhead_core.events import (
    OrchestratorEvent,
    TurnEventBus,
    raise_if_terminal_bus_failed,
)
from bowhead_core.memory_readiness import memory_storage_ready
from bowhead_core.skill_import.flags import bundle_import_draft_enabled
from bowhead_core.types import (
    IntentType,
    ToolInvocation,
    TurnRequest,
    TurnResult,
    TurnStatus,
    normalize_legacy_turn_status,
    utc_now_iso,
)
from bowhead_router import Message

if TYPE_CHECKING:
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.session_store import SessionStore
    from bowhead_core.subagent import SubagentBackend
    from bowhead_core.tool_registry import ToolExecutionContext

logger = logging.getLogger(__name__)

# V4 PRD-D-4-4: fire-and-forget snapshot save tracker (prevents GC of pending tasks).
# Mirrors the bowhead_gateway main.py _bg_tasks pattern (spike #5).
_PACK_SNAPSHOT_BG_TASKS: set[asyncio.Task] = set()


# ─── chat-hydrate-overhaul: trace + summary persistence helpers ──
def _invocations_to_trace_rows(
    *,
    turn_id: str,
    intent: IntentType | None,
    invocations: list[ToolInvocation],
    ui_language: str | None = None,
) -> list[Any]:
    """Convert turn finalize state → list[TraceStepRow] (FE TraceStep 单源对齐).

    顺序 (seq):
    - 0: classify step (intent_decision 后 FE 注入的 "classify" step)
    - 1..N: invocations + nested 展平 (subagent_spawn 内部 tool 标 subagent_id 关联到 spawn parent)

    Round 5 fix: nested 必须展平。FE turn-stream 用 subagent_id 把 nested 渲染到
    spawn parent 节点下 (NOT sibling), 不展会丢真实 step 数 (live "8 步" → hydrate "1 步")。
    """
    from bowhead_core.session_store import TraceStepRow

    # chat-polish-r2 Phase 4 V-2 + T-4/T-5 persist fix (2026-05-26):
    # Reuse subagent.py helpers so persist path = LIVE SSE path. Previously
    # persist used raw `str(inv.result)` truncated to 800 chars → user saw
    # raw JSON dumps on archived/verified turn expand (V-2). Plus kind only
    # looked at inv.error (Python exception), missing tools returning
    # `{ok: False}` cleanly (T-4/T-5 archived path).
    from bowhead_core.subagent import (
        _extract_result_ok,
        _is_non_failure_result,
        _prettify_output_preview,
        _surface_inner_skill_name,
    )

    rows: list[TraceStepRow] = []
    seq_counter = [0]  # Mutable cell shares seq across _walk recursion.

    def _walk(invs: list[ToolInvocation], parent_subagent_id: str | None) -> None:
        for inv in invs:
            # chat-polish-r2 F3 (T-4/T-5 persist gap): kind uses result.ok,
            # not just Python exception, same as G1 subagent.py.
            # feedback-0713 #1/#2: exemptions share _is_non_failure_result
            # with live step_done (invalid_args self-correction / corrected code error / expected
            # outcome); live==persist==replay invariant. Persistence previously missed exemption,
            # F5/DTO rendered intended clarification as red error.
            _result_ok = _extract_result_ok(inv.result)
            kind = "error" if inv.error is not None or (_result_ok is False and not _is_non_failure_result(inv.result)) else "success"
            # chat-polish-r3 Phase 3 (B4) — surface inner skill name in
            # persisted trace rows too (replay path = same render as live).
            _surface_name = _surface_inner_skill_name(
                inv.tool_name or "",
                inv.arguments,
            )
            # chat-polish-r2 F2 (V-2): use _prettify_output_preview (≤80 chars,
            # human-friendly per-tool summary) instead of raw 800-char dump.
            # _prettify handles error str + None gracefully; never raises.
            summary_text = _prettify_output_preview(
                _surface_name,
                inv.result,
                inv.error,
                ui_language=ui_language,
            )
            rows.append(
                TraceStepRow(
                    turn_id=turn_id,
                    seq=seq_counter[0],
                    step_id=f"tool_{inv.call_id}",
                    kind=kind,
                    title=getattr(inv, "personal_mcp_label", None),
                    tool_name=_surface_name,
                    duration_ms=int(inv.duration_ms) if inv.duration_ms else None,
                    summary=summary_text or None,
                    subagent_id=parent_subagent_id,
                )
            )
            seq_counter[0] += 1
            # subagent_spawn nested children associate spawn call_id as subagent_id.
            # Frontend backfillSpawnSubagentId also uses spawn-step call_id as subagent_id key.
            if inv.tool_name == "subagent_spawn" and inv.nested:
                _walk(inv.nested, parent_subagent_id=inv.call_id)

    if intent is not None:
        rows.append(
            TraceStepRow(
                turn_id=turn_id,
                seq=seq_counter[0],
                step_id="classify",
                kind="success",
                title="Classification",
                tool_name=None,
                duration_ms=None,
                summary=f"intent={intent.value}",
                subagent_id=None,
            )
        )
        seq_counter[0] += 1

    _walk(invocations, parent_subagent_id=None)
    return rows


# ─── Sprint chat-redesign-2026-05-21 Phase 1.1 — preprocess_attachments ─────
# Runs AFTER ψ-F snapshot, BEFORE Lead starts. Parses each pinned attachment
# in parallel (sem-gated), emits step_started/step_done so FE Step List Card
# surfaces per-file parse progress. Failed parses get aggregated into
# `_psi_failed_metadata` for the Lead system prompt (per PRD §3.6.4 — Lead
# must know which files it can't access so it doesn't fabricate answers about
# them).

_PREPROCESS_CONCURRENCY = 5  # parallel parsers — balance I/O vs CPU on parse


async def _validated_turn_source(request: TurnRequest, store: SessionStore | None) -> Any | None:
    """本轮的 canonical 用户来源行，身份与附件集都对得上才返回（对不上返回 None，不猜）。

    两个读取面共用同一套判据（附件序号、批 2 的 `<bundle_import>`）——各写一套就会出现
    「同一份来源两处判定不一致」。
    """
    reader = getattr(store, "get_chat_turn_user_source", None)
    if not request.attachments or reader is None:
        return None
    try:
        source = await reader(turn_id=request.turn_id, user_id=request.user_id, workspace_id=request.workspace_id)
    except Exception as exc:
        logger.warning("attachments.reference_source_unavailable turn=%s error_type=%s", request.turn_id, type(exc).__name__)
        return None
    if (
        source is None
        or (source.turn_id, source.session_id, source.user_id, source.workspace_id)
        != (request.turn_id, request.session_id, request.user_id, request.workspace_id)
        or list(source.attachment_ids) != request.attachments
        or (source.parts is not None and len(source.parts) > 1)
    ):
        return None
    return source


async def _restore_document_revision_target(request: TurnRequest, store: SessionStore | None) -> None:
    """Restore routing only from the authenticated immutable source; never downgrade."""
    target = request.document_revision_target
    if target is None:
        return

    if store is None:
        raise ValueError("document_revision_unavailable")
    source = await store.get_chat_turn_user_source(
        turn_id=request.turn_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
    )
    if (
        source is None
        or source.session_id != request.session_id
        or source.document_revision_target != target
        or source.user_text != (request.text or "")
        or source.input_document is not None
        or source.parts is not None
        or source.attachment_ids
        or source.attachment_refs
        or request.attachments
    ):
        raise ValueError("document_revision_source_conflict")
    request.document_revision_target = source.document_revision_target


async def _document_revision_receipt(request: TurnRequest, store: SessionStore | None) -> dict:
    """``turn_started`` projection for a revision Turn: the revised draft's title and number.

    Read once before the revision supersedes its parent, and persisted with ``turn_started`` so the
    live receipt, the persisted event and its replay show the same draft. Missing facts omit the key.
    """
    target = request.document_revision_target
    if target is None or store is None:
        return {}
    try:
        receipt = await store.get_document_revision_receipt(
            target=target,
            session_id=request.session_id,
            user_id=request.user_id,
            workspace_id=request.workspace_id,
        )
    except Exception:
        logger.warning("turn.document_revision_receipt_unavailable turn=%s", request.turn_id, exc_info=True)
        return {}
    return {"document_revision": receipt} if receipt else {}


async def _current_attachment_positions(request: TurnRequest, store: SessionStore | None) -> dict[str, int]:
    """Only canonical UI references prove ordinals; flattened IDs do not.

    A bundle occupies one user-visible position even when it has many runtime
    inputs. Missing/ambiguous provenance leaves positions unknown, not guessed.
    """
    source = await _validated_turn_source(request, store)
    if source is None:
        return {}
    return current_attachment_positions(source)


async def _bundle_import_bundles(
    request: TurnRequest,
    store: SessionStore | None,
    artifact_store: Any | None,
    parsed: list[Any] | None,
) -> list[dict[str, Any]]:
    """批 2 `<bundle_import>` 的数据源（flag 关时根本不调）。两条路各自独立 fail-open：

    - KB 包路径（聊天拖 zip）：`attachment_refs` 里带 `bundle_artifact_ids` 的根 id → 查
      `chat_attachment_bundle_inventory`（按上传者过滤）；里面带 `landed_skills`。
    - 不透明 zip（`/files/upload`）：`PreparedAttachment.inventory`；这条路什么都没落，
      所以没有 `landed_skills` 键——渲染侧据此说「本轮没有导入」，不含糊其辞。
    """
    out: list[dict[str, Any]] = []
    for prep in parsed or []:
        inv = getattr(prep, "inventory", None)
        if isinstance(inv, dict) and inv.get("components"):
            # path_kind explains nonpersistence: attachments inherently unpersisted, KB inventories only after persistence.
            out.append({"filename": getattr(prep, "filename", "") or "", "inventory": inv, "path_kind": "attachment"})
    db_path = getattr(artifact_store, "db_path", None)
    source = await _validated_turn_source(request, store) if db_path else None
    if source is not None:
        from bowhead_core.skill_import.bundle_inventory_store import get_bundle_inventory

        seen: set[str] = set()
        failed = 0
        for ref in getattr(source, "attachment_refs", None) or []:
            if not isinstance(ref, dict) or not ref.get("bundle_artifact_ids"):
                continue
            root_id = str(ref.get("file_id") or "")
            if not root_id or root_id in seen:
                continue
            seen.add(root_id)
            try:
                row = await get_bundle_inventory(str(db_path), bundle_artifact_id=root_id, created_by=request.user_id)
            except Exception:  # noqa: BLE001 — missing table/read failure means no block this turn, never affect turn
                failed += 1
                continue
            if row and row.get("components"):
                out.append({"filename": str(ref.get("filename") or ""), "inventory": row, "path_kind": "kb"})
        if failed:
            # Once per turn, not per ref (unmigrated deployments fail every bundle).
            logger.warning("bundle_import.inventory_read_failed turn=%s refs=%d", request.turn_id, failed)
    return out


def _classify_prep_error(prep: PreparedAttachment) -> tuple[str, bool]:
    """Map a PreparedAttachment.error string to (StepError.type, is_transient).

    Heuristic on the error message — keeps the StepError taxonomy consistent
    across parsers without each parser knowing the enum.
    """
    e = (prep.error or "").lower()
    if "unsupported mime" in e:
        return ("unsupported_mime", False)
    if "unsupported zip" in e or "corrupted zip" in e:
        # skill-import batch 1b: zip preflight / corrupt bundle permanent; never tell user retry later.
        return ("corrupted", False)
    if "not found" in e or "missing" in e or "deleted" in e or "unreachable" in e:
        return ("corrupted", False)
    if "read failed" in e or "extraction failed" in e:
        return ("corrupted", True)
    if "vision" in e:
        return ("vision_failed", True)
    if "timeout" in e:
        return ("timeout", True)
    return ("tool_exception", True)


def _prep_to_step_done_payload(
    prep: PreparedAttachment,
    *,
    card_id: str,
    step_id: str,
    duration_ms: int,
) -> dict[str, Any]:
    """Build a step_done event payload dict from a PreparedAttachment result."""
    base: dict[str, Any] = {
        "card_id": card_id,
        "step_id": step_id,
        "step_type": "parse",
        "duration_ms": duration_ms,
    }
    if prep.error:
        etype, is_transient = _classify_prep_error(prep)
        base["state"] = "failure"
        base["error"] = {
            "type": etype,
            "message_zh": prep.error,
            "is_transient": is_transient,
        }
        return base
    # success
    base["state"] = "success"
    if prep.extracted_text:
        preview = f"已解析 ({prep.size} bytes · {len(prep.extracted_text)} chars)"
        if prep.truncated:
            preview += " · 截断"
        base["output_preview"] = preview
    elif prep.note:
        base["output_preview"] = prep.note[:200]
    else:
        base["output_preview"] = f"已解析 ({prep.size} bytes)"
    return base


async def _preprocess_attachments_with_events(
    *,
    snapshot: list[dict[str, Any]],
    bus: TurnEventBus | None,
    turn_id: str,
    artifact_store: Any,
) -> tuple[list[PreparedAttachment], str | None]:
    """Parse each snapshot item in parallel, emitting step_* events around each.

    Returns ``(parsed_results, failed_metadata)``:
    - ``parsed_results``: PreparedAttachment per attachment (success or error
      both included so Lead has a stable list aligned with snapshot).
    - ``failed_metadata``: pre-formatted markdown of failed parses (or None if
      all parsed cleanly) — Lead injects into system prompt so it doesn't
      hallucinate answers about unreadable files.

    Also emits ``step_list_card_created`` once at the top so FE can render
    the card shell as soon as parse begins.
    """
    if not snapshot or artifact_store is None:
        return ([], None)

    # Note: step_list_card_created is emitted unconditionally by TurnRunner.run
    # at turn start (not gated on preprocess scope), so plain-text turns also
    # get a card shell. We DON'T re-emit here.

    sem = asyncio.Semaphore(_PREPROCESS_CONCURRENCY)

    async def _parse_one(item: dict[str, Any]) -> PreparedAttachment | None:
        aid = item.get("artifact_id") or ""
        filename = item.get("filename") or aid or "?"
        mime = item.get("mime") or ""
        size = int(item.get("size") or 0)
        deferred = defer_office_content(filename=filename, mime=mime)
        if not deferred and (item.get("inject") or "full") != "full":
            # Current non-full references also need an explicit position. Keep
            # their existing metadata-only contract, without parsing a body.
            return PreparedAttachment(
                artifact_id=aid,
                filename=filename,
                mime=mime,
                size=size,
                extracted_text="",
                truncated=False,
                summary_only=True,
                error=None,
                inject=str(item["inject"]),
                note="仅载入文件元数据；需要正文时按需读取。",
                current_message_position=item.get("current_message_position"),
            )
        step_id = f"parse_{aid}"
        t_start = time.monotonic()
        async with sem:
            if bus is not None and not deferred:
                try:
                    # 0.39.3 prod walkthrough bug E: user reported step row
                    # showed only "text/csv, 37 bytes" with no filename. Move
                    # filename to front of preview so it's the dominant signal
                    # (mime + size become subordinate metadata).
                    preview = f"{filename} · {mime}, {size} bytes" if filename and filename != "?" else f"{mime}, {size} bytes"
                    await bus.emit(
                        OrchestratorEvent(
                            event_type="step_started",
                            turn_id=turn_id,
                            payload={
                                "card_id": turn_id,
                                "step_id": step_id,
                                "step_type": "parse",
                                "header_zh": f"解析 {filename}",
                                "input_preview": preview,
                                "started_at": utc_now_iso(),
                            },
                        )
                    )
                except Exception as _se:
                    raise_if_terminal_bus_failed(bus)
                    logger.warning(
                        "preprocess.step_started_emit_failed aid=%s err=%s",
                        aid,
                        _se,
                    )
            try:
                preps = await prepare_attachments(
                    store=artifact_store,
                    artifact_ids=[aid],
                )
                prep = preps[0] if preps else None
            except Exception as _pe:
                logger.warning(
                    "preprocess.parse_failed aid=%s err=%s",
                    aid,
                    _pe,
                )
                prep = PreparedAttachment(
                    artifact_id=aid,
                    filename=filename,
                    mime=mime,
                    size=size,
                    extracted_text="",
                    truncated=False,
                    summary_only=False,
                    error=f"preprocess_failed: {type(_pe).__name__}: {_pe}",
                )
            # M1: carry the asset's injection policy onto the prepared result so
            # render_attachments_block (Gate B) can refuse to inline a non-'full'
            # body even if one ever reaches the parser.
            if prep is not None:
                if not prep.content_deferred:
                    prep.inject = str(item.get("inject") or "full")
                prep.current_message_position = item.get("current_message_position")
            duration_ms = int((time.monotonic() - t_start) * 1000)
            if bus is not None and prep is not None and not deferred:
                try:
                    await bus.emit(
                        OrchestratorEvent(
                            event_type="step_done",
                            turn_id=turn_id,
                            payload=_prep_to_step_done_payload(
                                prep,
                                card_id=turn_id,
                                step_id=step_id,
                                duration_ms=duration_ms,
                            ),
                        )
                    )
                except Exception as _de:
                    raise_if_terminal_bus_failed(bus)
                    logger.warning(
                        "preprocess.step_done_emit_failed aid=%s err=%s",
                        aid,
                        _de,
                    )
            return prep

    results = await asyncio.gather(*[_parse_one(it) for it in snapshot])
    parsed: list[PreparedAttachment] = [r for r in results if r is not None]
    failed = [r for r in parsed if r.error]
    failed_metadata: str | None = None
    if failed:
        # Pre-formatted markdown lines — Lead injects verbatim into system prompt.
        # Per PRD §3.6.4: the Lead must mention to the user that these files
        # couldn't be read, not fabricate guesses about content.
        failed_metadata = "\n".join(f"- `{r.filename}` ({r.mime or 'unknown mime'}) — {r.error}" for r in failed)
    return (parsed, failed_metadata)


async def _reference_resume_attachment_snapshot(
    runtime: Any, attachment_ids: list[str], scope_items: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Re-read only original files after a Lead question before delegation."""
    if runtime is None or runtime.generation <= 1 or not attachment_ids:
        return None
    if not await runtime.reprepare_original_attachments_on_resume(attachment_ids):
        return None
    selected = set(attachment_ids)
    items = [item for item in scope_items if item.get("artifact_id") in selected]
    if {item["artifact_id"] for item in items} != selected:
        raise ValueError("reference_attachment_material_unavailable")
    return items


def turn_wall_duration_ms(bus: Any) -> int | None:
    """本执行代 `turn_started` → 此刻的墙钟毫秒；bus 没带起点（非 TurnRunner 路径）时 None。"""
    t0 = getattr(bus, "_turn_wall_t0_perf", None)
    if not isinstance(t0, (int, float)) or isinstance(t0, bool):
        return None
    return max(0, int((time.perf_counter() - t0) * 1000))


def turn_summary_counts(bus: Any, invocations: list[Any] | None) -> tuple[int, int]:
    """(tools_count, llm_count)，覆盖 Lead 与所有 peer / 子代理。

    - tools：bus 上的 `tool_result` 计数（每次工具调用一条，含 peer）；Lead 顶层
      invocations 只是下限。
    - llm：gateway worker 的 per-turn usage sink 计数（contextvar 继承到派生 task）。
      没装 sink 的路径（核心单测、裸 orch）拿不到真值，退回旧口径 1。
    """
    top_level = len(invocations or [])
    tools = max(top_level, int(getattr(bus, "_tool_result_count", 0) or 0))
    llm_raw = getattr(bus, "_turn_llm_call_count", None)
    llm = int(llm_raw) if isinstance(llm_raw, int) and not isinstance(llm_raw, bool) and llm_raw > 0 else 1
    return tools, llm


async def _persist_turn_state(
    *,
    orch: Orchestrator,
    turn_id: str,
    intent: IntentType | None,
    invocations: list[ToolInvocation],
    tokens_in: int,
    tokens_out: int,
    duration_ms: int,
    tools_count: int | None = None,
    tool_loads: int = 0,
    llm_count: int | None = None,
    # ⚠ **Explicit argument required**: persistence runs **after** run_turn finally,
    # turn-start ContextVar already reset to zh-CN default. Omission persists Chinese
    # while live English, violating live==persist==replay;
    # persist_turn_trace INSERT OR REPLACE also overwrites earlier correct version.
    ui_language: str | None = None,
) -> None:
    """Single batch persist trace + summary at turn finalize (done/cancel/error).

    Idempotent (INSERT OR REPLACE), 多次调用安全. Fire-and-forget — 自吞 except
    + log + telemetry, 不冒泡到 turn_runner.run_turn.
    """
    if orch.session_store is None:
        return

    # Sprint ψ-H Phase 1 monitor (P0-5 from CTO/SRE review): emit
    # pipeline_status_divergence_total counter when turns.status (about to be
    # written) differs from the last done event status. π Phase A modeled this
    # but t_19ef6e77 (0.13.x prod) still showed divergence → not fully fixed.
    # Counter signals when DB is still lying for follow-up Phase 2 spike.
    # Phase 3a frontend coalesce: TODO ψ+1 (frontend does not read turn.status directly, separate spike needed).
    try:
        from bowhead_core import metrics as _m_psih

        # Best-effort: read last done event status if available via session_store
        # API. Skip if unavailable — counter just stays 0 (safe).
        _m_psih.inc(
            "pipeline_status_divergence_total_calls",
            labels={"phase": "finalize"},
            help="ψ-H: counter on each _persist_turn_state call; "
            "subset where DB.turn.status diverges from done event status "
            "will be split out once Phase 2 spike identifies write race.",
        )
    except Exception:  # noqa: S110
        pass

    # 1. trace steps
    try:
        rows = _invocations_to_trace_rows(
            ui_language=ui_language,
            turn_id=turn_id,
            intent=intent,
            invocations=invocations,
        )
        await orch.session_store.persist_turn_trace(turn_id=turn_id, steps=rows)
        _record_persist_metric("trace", "ok")
    except Exception as e:
        logger.warning("persist_turn_trace.failed turn=%s err=%s", turn_id, e)
        _record_persist_metric("trace", "fail")

    # 2. summary
    try:
        # tools_count / llm_count: caller supplies all-agent bus counts (turn_summary_counts);
        # absent falls back to Lead top-level invocation count and 1.
        # #300: compute actual cost.
        cost_usd = _estimate_cost_usd(int(tokens_in or 0), int(tokens_out or 0))
        await orch.session_store.persist_turn_summary(
            turn_id=turn_id,
            total_ms=int(duration_ms),
            tools_count=int(tools_count) if tools_count is not None else len(invocations),
            llm_count=int(llm_count) if llm_count is not None else 1,
            cost_text=_format_cost_text(cost_usd),
            cost_cents=round(cost_usd * 100),
            tokens_input=int(tokens_in) if tokens_in else None,
            tokens_output=int(tokens_out) if tokens_out else None,
            raw_json=None,
        )
        _record_persist_metric("summary", "ok")
    except Exception as e:
        logger.warning("persist_turn_summary.failed turn=%s err=%s", turn_id, e)
        _record_persist_metric("summary", "fail")

    def count_skill_loads(items: list[ToolInvocation]) -> int:
        return sum((1 if item.tool_name in {"skill_read", "skill_invoke"} else 0) + count_skill_loads(item.nested or []) for item in items)

    try:
        await orch.session_store.persist_turn_operation_usage(
            turn_id=turn_id,
            skill_loads=count_skill_loads(invocations),
            tool_loads=tool_loads,
            tool_calls=int(tools_count) if tools_count is not None else len(invocations),
        )
    except Exception as e:
        logger.warning("persist_turn_operation_usage.failed turn=%s err=%s", turn_id, e)


# Approximate historical cost estimate for turn summaries.
# Future real multiprovider needs provider-config/model price table; V1 hardcoded.
_PRICE_INPUT_PER_1K = 0.002
_PRICE_OUTPUT_PER_1K = 0.006


def _estimate_cost_usd(tokens_in: int, tokens_out: int) -> float:
    """**美元估算，与 credit 计价无关** —— 落 `turns.cost_usd`，供展示与 Lead 的软成本上限用。

    credit-billing gap 14：系统里并存两套「成本」账，口径必须分清，别互相换算：

    - **credit**（钱账）：`bowhead_core.rate_card.credits_for_tokens`，input+output 合计每 1000
      token 记 1 credit 向上取整，写 `credit_ledger` 并真扣余额。BYO LLM 语境下它是**配额单位**，
      不代表任何真实货币支出。
    - **cost_usd**（本函数）：按写死的 per-1K 单价做粗估，input / output 分别定价，只写
      `turns.cost_usd`，**从不参与扣费**。它既不随团队实际 provider 单价变化，也不区分模型档位，
      所以别拿它对账或给客户报价。

    两者数值不可比：同一个 turn 的 credit 数与 cost_usd 之间没有固定比例。
    """
    return tokens_in / 1000 * _PRICE_INPUT_PER_1K + tokens_out / 1000 * _PRICE_OUTPUT_PER_1K


def _format_cost_text(cost_usd: float) -> str:
    """3-decimal precision (sub-cent). 0 → '$0.000'."""
    return f"${cost_usd:.3f}"


def _record_persist_metric(kind: str, result: str) -> None:
    """Telemetry counter — bowhead_turn_{trace|summary}_persist_total{result=...}."""
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            f"bowhead_turn_{kind}_persist_total",
            labels={"result": result},
            help=f"{kind} persist outcomes (chat-hydrate-overhaul).",
        )
    except Exception:  # noqa: S110
        pass


def _maybe_save_pack_snapshot(
    *,
    orch: Orchestrator,
    request: Any,
    content: str,
    breakdown: dict[str, int],
    pack_version: str,
    executor_block: str = "",
) -> None:
    """Fire-and-forget snapshot save. Does not block the turn (PRD-D-4-4 §3.1).

    功能已正式发布；权限、作用域和存储就绪校验仍须执行。

        记忆重做 批0④（tech-design §3.2）: 本函数此前**全仓零生产调用**——定义在、
        `test_pack_snapshot_save_helper.py` 也在，但没人调；就算被调也被
        `BOWHEAD_REPLAY_ENABLED` / `BOWHEAD_REPLAY_PCT` 双闸挡死（116 容器 env 两个 key 都
        没有 ⇒ 生产恒 0 行）。「测试把死码钉成活的」+「`_PCT` 缺省 0 = 死 flag」同时发作。
        现在真调用点在 `_run_inner`（turn 起点建完快照就写），闸改成布尔的
        `orch.memory_pack_snapshots_enabled`，`/api/v1/memory/replay` 随之复活。
    """
    store = getattr(orch, "memory_pack_snapshots_store", None)
    if store is None:
        return
    if not content:
        return
    if not bool(getattr(orch, "memory_pack_snapshots_enabled", False)):
        return

    async def _save() -> None:
        try:
            await store.save(
                turn_id=request.turn_id,
                user_id=request.user_id,
                team_id=getattr(request, "team_id", None),
                workspace_id=getattr(request, "workspace_id", None),
                content=content,
                sections_breakdown=breakdown,
                pack_version=pack_version,
                # Batch 3: executor's exact bytes must persist in same row, or live/persisted/replay consistency
                # is false for peers; /memory/replay can only show Lead pack.
                executor_block=executor_block,
            )
        except Exception as e:
            logger.warning(
                "memory_pack_snapshot.save_failed turn=%s err=%s",
                getattr(request, "turn_id", "?"),
                e,
            )

    try:
        from bowhead_core.terminal_llm_invocation import (
            create_detached_auxiliary_task,
        )

        task = create_detached_auxiliary_task(
            _save(),
            reason="memory_pack_snapshot",
        )
        _PACK_SNAPSHOT_BG_TASKS.add(task)
        task.add_done_callback(_PACK_SNAPSHOT_BG_TASKS.discard)
    except RuntimeError as e:
        # No running event loop (defensive — should never happen inside turn run)
        logger.debug("memory_pack_snapshot.create_task_skipped: %s", e)


def _target_dict(target: Any) -> dict[str, Any] | None:
    if target is None:
        return None
    from dataclasses import asdict

    return {k: v for k, v in asdict(target).items() if v is not None}


def _should_skip_payload_missing_guard(final_answer: str | None) -> bool:
    """W12 #91 fix helper: Lead 写出 substantive final_answer 时跳过 guard fallback.

    阈值 500 字符: thin answer rewrite 后 ≥500 视为真有内容输出 (常见 fallback / 错误
    summary 通常 <300 字). 真 prod 案例 (t_aa9ccddc529f): final_answer=4565 字完整
    report, 但 subagent 没 return.payload → guard 误触加 'eyebrow + 我跑了一些数据查询'
    fallback 把视觉盖了. fix: caller 用此函数早返跳过 guard.
    """
    SUBSTANTIVE_THRESHOLD = 500
    return bool(final_answer) and len(final_answer.strip()) >= SUBSTANTIVE_THRESHOLD


def _turn_used_skill(invocations: list[ToolInvocation]) -> bool:
    """W12-3 (#74): 递归检测 invocations(含 nested subagent 内的)有没有 skill_invoke.
    用于 session.skill_applied 真信号.
    """
    for inv in invocations:
        if inv.tool_name == "skill_invoke":
            return True
        if inv.nested and _turn_used_skill(inv.nested):
            return True
    return False


def _compute_turn_agent_type(invocations: list[ToolInvocation]) -> str:
    """W12-1: 从 turn 的 tool invocations 推断 agent_type (frontend agent badge 用).

    语义 (V3 review §C.1 方案 A): per-turn 主导 agent.
    - 调过 ≥1 次 subagent_spawn → 取最后一次 spawn 的 profile (compute / analyst / modeler等)
    - 否则 → 'lead'

    取**最后一次** spawn 而非第一次的理由: turn 内多次 spawn 时, 后者通常是
    Lead 调整路由 / 升级换 profile 的最终决策, 比开头那次更代表本 turn 输出.
    """
    spawn_profiles: list[str] = []
    for inv in invocations:
        if inv.tool_name != "subagent_spawn":
            continue
        if not isinstance(inv.arguments, dict):
            continue
        # subagent_spawn argument schema: {"profile": str, "task": str, ...}.
        profile = inv.arguments.get("profile")
        if isinstance(profile, str) and profile:
            spawn_profiles.append(profile)
    if not spawn_profiles:
        return "lead"
    return spawn_profiles[-1]


def _build_artifact_token_minter(orch: Any, user_id: str | None) -> Any | None:
    """Build a fresh artifact token minter for each sandbox call.
    The signing secret (orch.artifact_fetch_secret, set by the gateway) stays captured
    HERE — never stored on the ctx nor reachable from sandbox/core tool code. tools.py calls
    ``ctx._mint_artifact_token()`` right before a python_sandbox run so the token is short-
    lived and never expires mid-turn. Returns None when no secret/user (tools.py then sends
    no token → the runner fetch relies on the existing identity path)."""
    secret = getattr(orch, "artifact_fetch_secret", None)
    if not secret or not user_id:
        return None
    from bowhead_core.artifact_store import mint_artifact_fetch_token

    def _mint() -> str:
        try:
            return mint_artifact_fetch_token(str(secret), str(user_id))
        except Exception:
            return ""

    return _mint


def _team_subagent_store(orch: Any) -> Any | None:
    """Return the existing team-Agent runtime store independently of Creator UI.

    Creator's switch controls authoring only.  Marketplace assignments and the
    post-cutover execution guard in ``SubagentRunner`` control whether an
    existing Agent is advertised/resolved; coupling runtime to the authoring
    switch made hired Agents disappear while the storefront still showed them.
    """
    return getattr(orch, "user_store", None)


def _team_skill_store(orch: Any) -> Any | None:
    """workflow-solidify S4c (rollback kill-switch): wire the team_skill resolve adapter ONLY
    when BOWHEAD_CCA_WORKFLOW_CREATOR is on. Flag-off → None → resolve_skill team 分支跳过
    （already-created team skills go inert）—— 单开关回滚整 feature."""
    from bowhead_core.user_store import TeamSkillStoreAdapter
    from bowhead_core.workflow_creator_trigger import workflow_creator_enabled

    if not workflow_creator_enabled():
        return None
    us = getattr(orch, "user_store", None)
    return TeamSkillStoreAdapter(us) if us is not None else None


#: Agents whose Turns author another Agent's definition. Their instructions describe that Agent,
#: not the user's personal preference.
_AGENT_AUTHORING_PROFILES: frozenset[str] = frozenset({"agent-coach"})


def turn_authors_an_agent(request: Any, result: Any = None) -> bool:
    """True when this Turn edits an Agent definition rather than speaking for the user.

    A rule such as "percentages keep one decimal" sent while authoring an Agent belongs to that
    Agent; proposing it as personal memory would apply it to every chat the user has. Structural
    signals only: a draft/editor/Coach canvas bound to the send, or a delegation to Coach.
    """

    for attr in ("subagent_draft_editor", "agent_draft_target", "coach_canvas_target"):
        if getattr(request, attr, None) is not None:
            return True
    for inv in getattr(result, "tool_invocations", None) or []:
        if getattr(inv, "tool_name", "") != "subagent_spawn":
            continue
        args = getattr(inv, "arguments", None)
        if isinstance(args, dict) and str(args.get("profile") or "").strip().lower() in _AGENT_AUTHORING_PROFILES:
            return True
    return False


#: NL-intent wait cap for δ auto-capture decision: δ makes classification + drafting LLM calls,
#: allow actual 116 flash latency margin; task detached, longer wait never delays turn.
_DELTA_SETTLE_TIMEOUT_S = 90.0


async def maybe_capture_nl_intent_post_turn(
    orch: Any,
    request: Any,
    result: Any = None,
    capture_ctx: dict[str, Any] | None = None,
    delta_task: asyncio.Task[Any] | None = None,
) -> Any | None:
    """记住-rebuild Phase 2 — post-turn implicit memory capture, extracted for testability.

    A unit test drives this with a REAL TurnRequest so the field read can't silently
    break — which is exactly how the original ``request.user_message`` (no such field;
    it's ``request.text``) bug slipped past the helper-level tests that called
    capture_nl_intent directly. Reads ``request.text``, classifies via
    ``orch.nl_memory_intent_classifier``, and enqueues a review candidate on a confident
    hit. Never raises (fire-and-forget on the post-turn path)."""
    try:
        clf = getattr(orch, "nl_memory_intent_classifier", None)
        user_text = getattr(request, "text", "") or ""
        user_id = getattr(request, "user_id", None)
        if clf is None or not user_text.strip() or not user_id:
            return None
        # Skip when the explicit `remember` tool already fired this turn — implicit capture
        # is for statements the user did NOT explicitly ask to remember; otherwise we'd
        # Enqueue redundant AI suggestion for something deterministic tool just committed.
        invs = getattr(result, "tool_invocations", None) or []
        if any(getattr(inv, "tool_name", "") == "remember" for inv in invs):
            return None
        if turn_authors_an_agent(request, result):
            logger.info("nl_intent.skipped turn=%s reason=agent_authoring", getattr(request, "turn_id", None))
            return None
        # Collision matrix② (line B batch 1a): already **automatically written** this turn ⇒ skip too.
        # Otherwise one always-in-future request produces memory plus card asking same thing.
        # Same reason as remember rule above: implicit capture for utterances without explicit remember request.
        if capture_ctx is not None and capture_ctx.get("escalate_fired"):
            # Collision matrix③: same utterance persisted as team candidate; user specified destination.
            return None
        # ★★ Wait for actual δ decision before reading captured. on_post_turn only schedules detached classification;
        # turn_runner starts this task immediately afterward, previously reading captured while classifier almost always still running
        # (it and this classifier each make one LLM call); matrix② depended on chance race. Wait, never cancel:
        # timeout keeps old behavior + logs; occasional duplicate card preferable to silently swallowing suggestion.
        if delta_task is not None and not delta_task.done():
            done, _pending = await asyncio.wait({delta_task}, timeout=_DELTA_SETTLE_TIMEOUT_S)
            if not done:
                logger.warning("nl_intent.delta_not_settled turn=%s", getattr(request, "turn_id", None))
        if capture_ctx is not None and capture_ctx.get("captured"):
            return None
        from bowhead_core.env_utils import env_float
        from bowhead_core.memory_tools import capture_nl_intent
        from bowhead_core.memory_turn_requests import from_nl_intent_enqueue, record_best_effort, store_for

        capture = await capture_nl_intent(
            classifier=clf,
            candidate_store=getattr(orch, "memory_candidate_store", None),
            user_text=user_text,
            user_id=user_id,
            turn_id=getattr(request, "turn_id", None),
            min_confidence=env_float("BOWHEAD_NL_INTENT_MIN_CONF", 0.8),
            # credential-readiness D2.1: BYO team uses this-turn team router;
            # absent override falls back to construction-time instance (production RefusalAdapter, D1 rejection attribution).
            router=_effective_router(orch, request),
        )
        # Memory receipt: nl_intent counts as an explicit request only with a recorded enqueue outcome.
        if capture is not None:
            await record_best_effort(
                store_for(getattr(orch, "user_memory_store", None)),
                user_id=user_id,
                turn_id=getattr(request, "turn_id", None),
                request=from_nl_intent_enqueue(capture.candidate, capture.enqueue_outcome),
            )
        return capture
    except Exception as e:
        logger.warning("nl_intent.capture_failed err=%s", e)
        return None


async def maybe_dream_post_turn(
    orch: Any,
    request: Any,
    result: Any = None,
) -> list[Any]:
    """memory-revive S1 — post-turn Dreaming Light distillation, extracted for testability.

    Mirrors ``maybe_capture_nl_intent_post_turn``: reads request/result via getattr,
    never raises (fire-and-forget on the post-turn path). Only completed turns are
    distilled; skips when the explicit ``remember`` tool already fired this turn.
    Builds an existing-memory digest (entries + notes + pending candidates) so the
    distiller can dedupe semantically and propose updates instead of duplicates —
    exact value_json dedup cannot catch rephrasings (discovery §6.2). No bus/SSE
    involvement: candidates are delivered by REST polling + notification（§6.3）.
    """
    try:
        dreaming = getattr(orch, "dreaming_light", None)
        user_text = getattr(request, "text", "") or ""
        user_id = getattr(request, "user_id", None)
        if dreaming is None or not user_text.strip() or not user_id:
            return []
        status = getattr(result, "status", None)
        if str(getattr(status, "value", status) or "") != "completed":
            return []
        invs = getattr(result, "tool_invocations", None) or []
        if any(getattr(inv, "tool_name", "") == "remember" for inv in invs):
            # ★★ Correct deference (explicit remember already handled it), previously **no log line**;
            # distillation not run vs run without proposal looked identical. Actual 2026-09-16 cost:
            # Lead handled future-preference utterance with remember → silent distillation; investigated rate limit at length,
            # though turn outside rate-limit window. Log once, making nothing-happened diagnosable.
            logger.info("dreaming.skipped turn=%s reason=remember_fired", getattr(request, "turn_id", None))
            return []
        if turn_authors_an_agent(request, result):
            logger.info("dreaming.skipped turn=%s reason=agent_authoring", getattr(request, "turn_id", None))
            return []
        try:
            from bowhead_memory.candidates import DreamObservation
        except ImportError:
            return []
        intent = getattr(result, "intent", None)
        obs = DreamObservation(
            turn_id=getattr(request, "turn_id", "") or "",
            user_id=user_id,
            user_text=user_text,
            final_answer=getattr(result, "final_answer", "") or "",
            intent=str(getattr(intent, "value", intent) or ""),
            tool_chain=[n for inv in invs if (n := getattr(inv, "tool_name", ""))],
            existing_memory=await _existing_memory_digest(orch, user_id),
            workspace_id=getattr(request, "workspace_id", None) or None,
            # memory-i18n: candidate language follows output_language (None → empty → distillation
            # prompt adds no language constraint, unchanged behavior).
            output_language=getattr(getattr(request, "language_context", None), "output_language", "") or "",
        )
        # credential-readiness D2.1: BYO team uses this-turn team router, as above.
        created = await dreaming.observe(obs, router=_effective_router(orch, request))
        if created:
            try:
                from bowhead_core import metrics as _mm

                _mm.inc(
                    "memory.dreaming.candidates_created",
                    value=len(created),
                    labels={},
                    help="memory-revive S1: dreaming candidates enqueued post-turn.",
                )
            except Exception as me:
                logger.debug("dreaming.metric_failed err=%s", me)
            # Badge fallback notification: inline chat card primary delivery; bell reaches users returning
            # outside polling window. Per-user dedup folds in service, no bombardment.
            try:
                notify = getattr(orch, "notification_service", None)
                if notify is not None:
                    await notify.emit_memory_candidates(
                        user_id=user_id,
                        created_count=len(created),
                        preview=str(getattr(created[0], "value", "") or "")[:60],
                    )
            except Exception as ne:
                logger.debug("dreaming.notify_failed err=%s", ne)
        return created
    except Exception as e:
        logger.warning("dreaming.post_turn_failed err=%s", e)
        return []


def _truncate_whole_lines(block: str, budget: int) -> str:
    """按行截断：宁可少给一整行，也不给出半个 `[mem_xxx]` 让模型去抄。"""
    if len(block) <= budget:
        return block
    kept: list[str] = []
    used = 0
    for line in block.split("\n"):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


async def _existing_memory_digest(orch: Any, user_id: str) -> str:
    """Compact digest of remembered + queued items for the DREAMING dedupe prompt.

    Best-effort per source（任一 store 失败不拖垮蒸馏）；hard cap 1500 char——
    这是喂给判定规则 5 的上下文，不是召回，无需完整。段序（review-fix F9）：
    pending 候选前置——它是防重提的第一防线（value_json 精确指纹挡不住换措辞时
    唯一的兜底），不能被 entries/notes 打满后截掉。全文过 sanitize_for_injection
    （review-fix F4）：notes/entries/候选 value 都是用户可控内容，进 LLM prompt
    必过消毒 seam（对齐 memory_pack 惯例）。
    """
    parts: list[str] = []
    try:
        cand_store = getattr(orch, "memory_candidate_store", None)
        if cand_store is not None:
            pending = await cand_store.list_for_user(user_id=user_id, status="pending", limit=10)
            if pending:
                # ⚠ No square brackets here: distillation prompt says supersedes uses mem_xxx in
                # existing-memory brackets, but same digest has - [preference] ... too,
                # so model may copy preference as ID (not found, silently dropped). Use kind= prefix.
                lines = [f"- kind={c.kind} {c.key or '(note)'}: {json.dumps(c.value, ensure_ascii=False, default=str)[:80]}" for c in pending]
                parts.append("待确认候选:\n" + "\n".join(lines))
    except Exception as e:
        logger.debug("dreaming.digest_candidates_failed err=%s", e)
    try:
        ums = getattr(orch, "user_memory_store", None)
        if ums is not None:
            from bowhead_core.memory_readiness import memory_storage_ready
            from bowhead_core.user_memory import MemoryRole, summarize_for_prompt

            # Memory rebuild batch 1: when gate on, entries supply [mem_xxx], required to fill distillation
            # supersedes slot (DREAMING_SYSTEM rule 5). **Slot/IDs share gate**;
            # prompt without IDs asks model to invent one.
            canonical = memory_storage_ready()
            entries = await ums.list_for_user(user_id)
            if canonical:
                # Operational rows (tool_context etc.) are not replaceable memories supplied to model.
                entries = [e for e in entries if e.memory_role != MemoryRole.OPERATIONAL]
            block = summarize_for_prompt(entries, with_ids=canonical)
            if block.strip():
                # ★Truncate **whole lines**, never block[:600]: ID-bearing prefix is 18 characters;
                # hard cut turns [mem_1bf370c9c86a] into [mem_1bf370, copied into
                # supersedes ⇒ missing target ⇒ silent continue, again mistaken for unintelligent model.
                # Raise budget to 900: ID prefixes consume ~30% old budget,
                # displacing existing memories used by rule 5 to prevent repeated proposals.
                parts.append(_truncate_whole_lines(block, 900 if canonical else 600))
    except Exception as e:
        logger.debug("dreaming.digest_entries_failed err=%s", e)
    try:
        notes_store = getattr(orch, "memory_notes_store", None)
        if notes_store is not None:
            note = await notes_store.get(user_id=user_id)
            content = getattr(note, "content", "") or ""
            if content.strip():
                parts.append("Notes:\n" + content[:500])
    except Exception as e:
        logger.debug("dreaming.digest_notes_failed err=%s", e)
    digest = "\n\n".join(parts)[:1500]
    if not digest:
        return ""
    try:
        from bowhead_core.prompt_injection_defense import sanitize_for_injection

        return sanitize_for_injection(digest)
    except Exception as e:
        logger.debug("dreaming.digest_sanitize_failed err=%s", e)
        return digest


async def _shared_grant_prefetch(
    orch: Any,
    request: Any,
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """content-permissions S4（tech-design §2.1 预取模式）: turn 启动一次 SQL 预取
    调用者的"分享给我"资源 id 集（team_subagent / team_skill 各一,deny-wins 已减）。

    返回 (None, None) 当 sharing off / access store 未配 / team·user 缺失 / 查询异常 —
    下游检查点（subagent resolve、Lead peer 装配、skill_resolver）回退纯 visibility
    过滤：分享不生效但绝不越权（fail-safe 方向，tech-design §4 flag 表）。
    """
    try:
        from bowhead_core.share_service import content_sharing_enabled

        if not content_sharing_enabled():
            return None, None
        access = getattr(orch, "access_control_store", None)
        team_id = getattr(request, "team_id", None) or ""
        user_id = getattr(request, "user_id", None) or ""
        if access is None or not team_id or not user_id:
            return None, None
        # CTO-perf P1: fetch the principal's group_ids ONCE, then run the two
        # per-kind grant queries concurrently (was 4 serial SQL: 2× group + 2×
        # grants). grants_for_viewer would re-fetch groups per kind.
        group_ids = await access.principal_group_ids(team_id, user_id)
        subs, skills = await asyncio.gather(
            access.granted_resource_ids(team_id, user_id=user_id, group_ids=group_ids, kind="team_subagent"),
            access.granted_resource_ids(team_id, user_id=user_id, group_ids=group_ids, kind="team_skill"),
        )
        return subs, skills
    except Exception as e:
        logger.warning("content_sharing.prefetch_failed turn=%s err=%s", getattr(request, "turn_id", ""), e)
        return None, None


async def _space_instructions_prefetch(orch: Any, request: Any) -> Any | None:
    """Read the Space instructions once per Turn; Lead and every executor share this snapshot.

    The request's Space was authorized by the gateway. A failed read never blocks the Turn.
    """
    store = getattr(orch, "space_instructions_store", None)
    workspace_id = getattr(request, "workspace_id", None) or ""
    if store is None or not workspace_id:
        return None
    try:
        return await store.snapshot_for_turn(turn_id=request.turn_id, workspace_id=workspace_id)
    except Exception as e:
        logger.warning(
            "space_instructions.prefetch_failed turn=%s err=%s",
            getattr(request, "turn_id", "?"),
            e,
        )
        return None


async def _memory_pack_prefetch(orch: Any, request: Any) -> Any | None:
    """记忆重做 批0④（tech-design §3.2）: turn 起点建**一次**记忆快照，所有人读它。

        今天 `lead_orchestrator` 在 turn 中途自己 `await build_memory_pack(...)`，peer 什么都
        没有。改成与其它 turn 起点预取（`_builtin_override_prefetch` /
        `_resolve_unconfigured_capability_tools`）同款位置的一次预取：
        ① 每 turn 一次 DB 读，不是每个执行体一次；② live / 落库 / 重放看到的记忆**必然一致**
        （chat 不变量）；③ 批3 把注入铺到 peer 与报告主笔时，读点已经在那儿了。

    功能已正式发布；权限、作用域和存储就绪校验仍须执行。

        ⚠ `build_memory_pack` 的 import 必须留在函数体内：`test_live_dogfood.py` 用
        `monkeypatch.setattr(mp, "build_memory_pack", _spy)` 打模块属性来钉「S2 空间过滤穿过
        真实 request→sctx→memory_pack 链路」，模块级 import 会把那条钉子静默变成永远绿。
    """
    if not memory_storage_ready():
        return None
    user_id = getattr(request, "user_id", "") or ""
    user_memory_store = getattr(orch, "user_memory_store", None)
    notes_store = getattr(orch, "memory_notes_store", None)
    if not user_id or (user_memory_store is None and notes_store is None):
        return None
    try:
        from bowhead_core.memory_pack import MemoryPackSnapshot, build_memory_pack

        breakdown: dict[str, int] = {}
        entries: list[Any] = []
        content = await build_memory_pack(
            user_id=user_id,
            user_memory_store=user_memory_store,
            notes_store=notes_store,
            metrics_log=getattr(orch, "metrics_log", None),
            breakdown_out=breakdown,
            entries_out=entries,
            # memory-revive S2 red line: filter always uses request space, never infers from stored memory row.
            workspace_id=getattr(request, "workspace_id", None),
        )
        # Batch 3 (FR1 passthrough): render executor block **once here**, attach to same snapshot.
        # Gate off always empty ⇒ _legacy_run appends nothing, byte-identical.
        executor_block = ""
        if memory_storage_ready() and entries:
            from bowhead_core.memory_injection import render_executor_block, track_truncation

            executor_block, dropped = render_executor_block(entries)
            await track_truncation(
                getattr(orch, "metrics_log", None),
                user_id=user_id,
                dropped=dropped,
                kept=len(entries) - dropped,
            )
        return MemoryPackSnapshot(content=content, breakdown=breakdown, executor_block=executor_block)
    except Exception as e:
        # Memory never blocks turn: failed prefetch → None → Lead builds own fallback.
        logger.warning(
            "memory_pack.prefetch_failed turn=%s err=%s",
            getattr(request, "turn_id", "?"),
            e,
        )
        return None


async def _builtin_override_prefetch(orch: Any, request: Any) -> dict[str, Any] | None:
    """内置子代理管理（tech-design §4.1）: turn 启动一次 SQL 预取本团队对系统内置子代理的 delta。

        返回 ``None`` 当 flag off / store 未接线 / team 缺失 / 查询异常 —— 两个 seam（advertise 的
        ``_gather_lead_peers``、resolve 的 ``_resolve_profile``）都回退到纯代码默认值：治理不生效，
        但 turn 绝不因为读不到治理表而挂（fail-safe 方向）。

    功能已正式发布；权限、作用域和存储就绪校验仍须执行。"""
    try:
        store = getattr(orch, "builtin_subagent_override_store", None)
        team_id = getattr(request, "team_id", None) or ""
        if store is None or not team_id:
            return None
        return await store.list_for_team(team_id)
    except Exception as e:
        logger.warning(
            "builtin_override.prefetch_failed turn=%s err=%s",
            getattr(request, "turn_id", ""),
            e,
        )
        return None


async def _resource_disable_prefetch(orch: Any, request: Any) -> Any | None:
    """Read the team's and user's disable settings once per turn (tech design §8.3).

    Returns ``None`` when no team/database is available; the
    runner then removes nothing, matching today's behaviour.
    """
    from bowhead_core.resource_disable import load_disable_state

    team_id = getattr(request, "team_id", None) or ""
    db_path = getattr(getattr(orch, "session_store", None), "db_path", None)
    if not team_id or not db_path:
        return None
    return await load_disable_state(str(db_path), team_id, getattr(request, "user_id", None))


async def _run_limits_prefetch(orch: Any, request: Any) -> Any | None:
    """Resolve the Team's agent run limits once per turn, for every channel.

    None while FEATURE_AGENT_RUN_LIMITS is off (earlier limits apply). Store errors
    fall back to the defaults inside ``load_team_limits``; they never fail the turn.
    """
    from bowhead_core.agent_run_limits import agent_run_limits_enabled, load_team_limits

    if not agent_run_limits_enabled():
        return None
    return await load_team_limits(getattr(orch, "team_settings_store", None), getattr(request, "team_id", None))


def build_tool_exec_ctx(
    orch: Any,
    request: Any,
    parent_bus: Any,
) -> Any:
    """Standalone ToolExecutionContext factory (sprint multi-agent-v1.5 PR-#23).

    Mirrors the construction inside `TurnRunner._run_inner` (~L869). 提供给
    `agents/turn_runner_hook.py` 调用, 让 v2 path 拿到完整 ctx 跑 SubagentRunner.

    **Note**: 故意 duplicate 与 _run_inner 内 inline 版而不抽公共 helper, 以避免改
    legacy hot path. 字段保持同步是后续维护责任.
    """
    from bowhead_core.tool_registry import ToolExecutionContext

    pycell_counter_ref: list[int] = [0]
    # credential-readiness R6: **synchronous** mirrored factory (no production caller, tests pin mirror
    # invariant) cannot await team resolver; empty set hides nothing, same fail-open direction.
    # Actual path _run_inner _peer_ctx async, resolves now.
    _r6_unconfigured: frozenset = frozenset()
    _ctx = ToolExecutionContext(
        personal_mcp_license_check=getattr(orch, "personal_mcp_license_check", None),
        workspace_id=getattr(request, "workspace_id", ""),
        # workspace-datarc-decouple P1 (R1.2): datarc project for bi _ws (None → fallback to workspace_id).
        active_datarc_project=getattr(request, "active_datarc_project", None),
        # Batch 2 (PRD §3.3b/§3.11): per-user snapshot for sentinel options/provenance names.
        active_datarc_workspace_name=getattr(request, "active_datarc_workspace_name", "") or "",
        accessible_datarc_workspaces=tuple(getattr(request, "accessible_datarc_workspaces", ()) or ()),
        datarc_absence_reason=getattr(request, "datarc_absence_reason", "") or "",  # bi-absent-turn R3.1 (sync both mirror sites).
        unconfigured_capability_tools=_r6_unconfigured,  # credential-readiness R6 (sync both mirrors).
        datarc_context_switcher=getattr(request, "datarc_context_switcher", None),
        config_lite_supported=bool(getattr(request, "config_lite_supported", False)),  # Config copilot batch0.5 (sync both mirrors).
        configuration_settings_target=getattr(request, "configuration_settings_target", None),
        coach_canvas_target=getattr(request, "coach_canvas_target", None),
        document_revision_target=getattr(request, "document_revision_target", None),
        member_uid=getattr(request, "member_uid", None),
        user_id=getattr(request, "user_id", None),
        turn_id=getattr(request, "turn_id", ""),
        # A Datarc client is exclusively resolved from the caller team's saved
        # connection. Never fall back to a deployment-wide client.
        datarc=None if getattr(request, "datarc_unavailable", False) else getattr(request, "datarc_client", None),
        subagent_runner=_make_subagent_runner_with_catalog(
            router=_effective_router(orch, request),  # byo-no-clone: BYO team router wins
            tool_registry=orch.tool_registry,
            custom_store=getattr(orch, "custom_subagent_store", None),
            product_terms_catalog=getattr(orch, "product_terms_catalog", None),
            team_subagent_store=_team_subagent_store(orch),  # cca P3
        ),
        parent_bus=parent_bus,
        vector_client=None if getattr(request, "datarc_unavailable", False) else getattr(request, "vector_client", None),
        skill_instance_store=getattr(orch, "skill_instance_store", None),
        team_skill_store=_team_skill_store(orch),  # workflow-solidify S4c (flag-gated)
        skill_catalog=getattr(orch, "skill_catalog", None),
        artifact_store=getattr(orch, "artifact_store", None),
        scheduled_store=getattr(orch, "scheduled_store", None),  # scheduled_job_create tool (flag-gated)
        canvas_store=getattr(orch, "canvas_store", None),
        session_store=getattr(orch, "session_store", None),
        session_id=getattr(request, "session_id", ""),
        user_text=getattr(request, "text", "") or "",  # template-refresh: verbatim user msg
        approval_gate=getattr(orch, "approval_gate", None),
        subagent_draft_service=getattr(
            request,
            "subagent_draft_service",
            None,
        ),
        subagent_authoring_context_store=getattr(
            orch,
            "subagent_authoring_context_store",
            None,
        ),
        subagent_draft_mcp_entitled=bool(
            getattr(request, "subagent_draft_mcp_entitled", False),
        ),
        subagent_draft_editor=getattr(request, "subagent_draft_editor", None),
        agent_coach_resources=getattr(request, "agent_coach_resources", None),
        pycell_counter_ref=pycell_counter_ref,
        router=_effective_router(orch, request),  # byo-no-clone
        # remember-rebuild (item 2): forward memory stores so remember/recall tools reach them.
        memory_notes_store=getattr(orch, "memory_notes_store", None),
        user_memory_store=getattr(orch, "user_memory_store", None),
        # Memory rebuild batch0④: **synchronous** mirror factory (no production caller, tests pin invariant)
        # cannot await prefetch; None = read-side own-build fallback (current behavior).
        # Actual _run_inner _peer_ctx async, fetches at turn start.
        memory_pack=None,
        # Same reason as memory_pack: this synchronous mirror cannot await the turn-start read.
        space_instructions=None,
        project_memory_store=getattr(orch, "project_memory_store", None),
        schema_cache=getattr(orch, "schema_cache", None),
        clarification_store=getattr(orch, "clarification_store", None),
        language_context=getattr(request, "language_context", None),
        user_profile=getattr(request, "user_profile", None),  # answer_style etc. (sync both mirror sites).
        preferred_provider=getattr(request, "preferred_provider", None),
        preferred_model=getattr(request, "preferred_model", None),
        role="analyst",  # v2 manifest agent path →role=analyst.
        inline_preview_enabled=getattr(orch, "inline_preview_enabled", False),
        kb_retriever=_effective_kb_retriever(orch, request),  # byo-no-clone
        # Batch1/c5 (workspace-space-split): NO `or workspace_id`; space ID is not team ID.
        # (fail-closed, same rule as the cca LOAD POINT below; falsy ⇒ "" ⇒ no team scope).
        team_id=getattr(request, "team_id", "") or "",
        access_context=getattr(request, "access_context", None),
        access_control_store=getattr(orch, "access_control_store", None),
        kb_role=getattr(request, "kb_role", "user"),
        kb_doc_type_store=getattr(orch, "kb_doc_type_store", None),
        kb_extracted_record_store=getattr(orch, "kb_extracted_record_store", None),
        # Sprint 25 kb-per-row-extract: row-level store wire
        kb_extracted_rows_store=getattr(orch, "kb_extracted_rows_store", None),
        # 0.40.4 Bug 8 step a: kb_files_store wire for _kb_search to resolve
        # user's personal-scope workspaces (datarc ws ≠ Bowhead KB scope).
        kb_files_store=getattr(orch, "kb_files_store", None),
        kb_file_authorizer=getattr(orch, "kb_file_authorizer", None),
        metrics_log=getattr(orch, "metrics_log", None),  # Sprint ω Phase 5 P0-1
        # Sprint ar2: tenant for embed collection naming + per-ws calibrated thresholds
        tenant=getattr(orch, "datarc_tenant", ""),
        ar2_thresholds=getattr(orch, "ar2_thresholds_by_ws", {}).get(
            str(getattr(request, "workspace_id", "")),
        )
        if getattr(orch, "ar2_thresholds_by_ws", None)
        else None,
    )
    # cca P3 F-B: per-turn artifact-fetch token minter (closure holds the signing secret;
    # tools.py mints fresh per python_sandbox call). setattr post-construction (like
    # _running_profile) — ctx is a dataclass, no field added.
    _ctx._mint_artifact_token = _build_artifact_token_minter(orch, getattr(request, "user_id", None))
    return _ctx


# ─────────────────────────────────────────────────────────────
# #128 Step 3c: entire TurnRunner body inline.
# ─────────────────────────────────────────────────────────────
async def _require_admitted_legacy_bus(orch: Any, turn_id: str) -> Any:
    registry = getattr(orch, "event_registry", None)
    checkpoint = getattr(registry, "projection_checkpoint", None)
    if not callable(checkpoint):
        raise RuntimeError("legacy_chat_event_registry_unavailable")
    bus, _stable_floor = await checkpoint(turn_id)
    if bus is None:
        raise RuntimeError("legacy_chat_execution_not_admitted")
    if not bool(getattr(bus, "_persist_before_live", False)):
        raise RuntimeError("legacy_chat_execution_not_durable")
    return bus


async def build_pinned_snapshot(astore: Any, artifact_ids: list[str]) -> list[dict[str, Any]]:
    """ψ-F：固定附件的元数据快照（注入 Lead 系统提示）。取不到的与原文件已丢失（对账标为 missing）的跳过。"""
    snapshot: list[dict[str, Any]] = []
    if astore is None:
        return snapshot
    for _aid in artifact_ids:
        try:
            _art = await astore.get(_aid)
            if _art is None or getattr(_art, "status", None) == "missing":
                continue
            snapshot.append(
                {
                    "artifact_id": _aid,
                    "filename": getattr(_art, "filename", "?"),
                    "mime": getattr(_art, "mime", "?"),
                    "size": getattr(_art, "size", 0),
                    # M1 (RD-6): carry the injection policy so the
                    # scope partition below can skip-parse list/
                    # stub assets (the CSV-flood fix).
                    "kind": getattr(_art, "kind", None),
                    "inject": getattr(_art, "inject", None),
                    # Batch 2b: original producing query, only thing Lead can check before
                    # reusing pinned data. Previously filename only, lacking qualifiers
                    # (ONC incident delivered company-wide data as ONC-specific this way).
                    "origin_query": getattr(_art, "origin_query", None),
                    # Batch 2d: identify user-owned material; uploads/KB keep filenames
                    # (decision 10), machine outputs do not.
                    "source": getattr(_art, "source", None),
                }
            )
        except Exception:  # noqa: S110
            pass
    return snapshot


class TurnRunner:
    """Lead turn 执行器 —— 对外就是 `await TurnRunner(orch).run(req)`.

    Read runtime configuration, stores, event construction and cancellation signals
    from the shared Orchestrator facade.

    每次 `run()` 即弃；不保存跨 turn 状态。
    """

    def __init__(self, orchestrator: Orchestrator):
        self._orch = orchestrator

    # ─── Sprint π Phase A: unified persist chokepoint ───────────────────
    # Per tech-design r2 §A: a single switch dispatches turn terminal status
    # to one of mark_completed / mark_clarification_pending / mark_aborted /
    # mark_failed on session_store. Wired into LeadRuntime.persist_turn_state
    # so lead_orchestrator._emit_event("done", ...) can persist before SSE
    # emit (single chokepoint vs. 12-site wrap pattern from r1).
    async def _persist_turn_state(
        self,
        *,
        turn_id: str,
        status_value: str,
        intent: str | None,
        final_answer: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
        error: str | None,
        error_kind: str | None,
    ) -> bool:
        transitioned = await self._transition_turn_state(
            turn_id=turn_id,
            status_value=status_value,
            intent=intent,
            final_answer=final_answer,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
            error=error,
            error_kind=error_kind,
        )
        if transitioned:
            await self._persist_done_summary(
                turn_id=turn_id,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
            )
        return transitioned

    async def _persist_done_summary(self, *, turn_id: str, tokens_input: int, tokens_output: int) -> None:
        """终态一落库就写 summary —— 不依赖 Lead 之后的收尾路径能走到。

        此前 summary 只在 `_run_inner` 里 Lead **正常返回之后**写一次；终态已提交、但随后
        抛异常离开的执行（116 实测私有智能体轮 summary 为 null）就永远没有 summary，
        界面只能显示「—」。这里与 done 事件用同一份数（`_turn_wall_duration_ms` 由 done
        咽喉点在调用本方法前写好），Lead 正常返回后的那次写入覆盖为同值。best-effort：
        summary 是展示投影，写失败不能影响终态。
        """
        store = getattr(self._orch, "session_store", None)
        bus = getattr(self, "_summary_bus", None)
        wall_ms = getattr(bus, "_turn_wall_duration_ms", None)
        if store is None or bus is None or not isinstance(wall_ms, int):
            return
        try:
            tools, llm = turn_summary_counts(bus, None)
            cost_usd = _estimate_cost_usd(int(tokens_input or 0), int(tokens_output or 0))
            await store.persist_turn_summary(
                turn_id=turn_id,
                total_ms=wall_ms,
                tools_count=tools,
                llm_count=llm,
                cost_text=_format_cost_text(cost_usd),
                cost_cents=round(cost_usd * 100),
                tokens_input=int(tokens_input) if tokens_input else None,
                tokens_output=int(tokens_output) if tokens_output else None,
                raw_json=None,
            )
        except Exception as e:
            logger.warning("persist_turn_summary.done_failed turn=%s err=%s", turn_id, e)

    async def _transition_turn_state(
        self,
        *,
        turn_id: str,
        status_value: str,
        intent: str | None,
        final_answer: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
        error: str | None,
        error_kind: str | None,
    ) -> bool:
        orch = self._orch
        store = getattr(orch, "session_store", None)
        if store is None:
            return True  # tests with bare orch mock
        reference_runtime = getattr(self, "_reference_runtime", None)
        if reference_runtime is not None:
            return await reference_runtime.persist_turn_state(
                store=store,
                turn_id=turn_id,
                status_value=status_value,
                intent=intent,
                final_answer=final_answer,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                cost_usd=cost_usd,
                error=error,
                error_kind=error_kind,
            )
        status = (status_value or "").lower().strip()
        worker = getattr(self, "_execution_worker", None)
        worker_args = {"worker": worker} if worker is not None else {}
        if status == "completed":
            return bool(
                await store.mark_completed(
                    **worker_args,
                    turn_id=turn_id,
                    intent=intent,
                    final_answer=final_answer or "",
                    tokens_input=int(tokens_input or 0),
                    tokens_output=int(tokens_output or 0),
                    cost_usd=float(cost_usd or 0.0),
                    error_kind=error_kind,
                )
            )
        if status == "clarification_pending":
            return bool(
                await store.mark_clarification_pending(
                    **worker_args,
                    turn_id=turn_id,
                    intent=intent,
                    final_answer=final_answer or None,
                    tokens_input=int(tokens_input or 0),
                    tokens_output=int(tokens_output or 0),
                    cost_usd=float(cost_usd or 0.0),
                    error_kind=error_kind,
                )
            )
        if status in ("aborted", "cancelled"):
            # Lead emits status="aborted" on asyncio.CancelledError; the
            # legacy turn_runner._finish_cancelled path emits status="cancelled".
            # Both flow through mark_aborted vs. mark_cancelled respectively.
            if status == "cancelled":
                return bool(
                    await store.mark_cancelled(
                        **worker_args,
                        turn_id=turn_id,
                        intent=intent,
                        tokens_input=int(tokens_input or 0),
                        tokens_output=int(tokens_output or 0),
                        cost_usd=float(cost_usd or 0.0),
                        error_kind=error_kind,
                    )
                )
            return bool(
                await store.mark_aborted(
                    **worker_args,
                    turn_id=turn_id,
                    intent=intent,
                    final_answer=final_answer or None,
                    tokens_input=int(tokens_input or 0),
                    tokens_output=int(tokens_output or 0),
                    cost_usd=float(cost_usd or 0.0),
                    error_kind=error_kind or "aborted",
                )
            )
        # error / failed / unknown → mark_failed (terminal sink).
        return bool(
            await store.mark_failed(
                **worker_args,
                turn_id=turn_id,
                error=error or status_value or "unknown_error",
                error_kind=error_kind or "internal",
                intent=intent,
                final_answer=final_answer or None,
                tokens_input=int(tokens_input or 0),
                tokens_output=int(tokens_output or 0),
                cost_usd=float(cost_usd or 0.0),
            )
        )

    async def run(self, request: TurnRequest) -> TurnResult:
        """执行一个 turn，emit 事件到 bus，返回聚合结果.

        Sprint η: wrap with state lifecycle hooks (build state at start, persist
        at end / error / cancel). Set contextvar so subagent.py can pick up state
        without explicit param plumbing.

        The whole turn runs with the model clock bound to the zone admitted with
        ``request`` (``model_clock``): every model-facing "today" (Router banner,
        subagent ``<current_date>``, BI advice) reads it, and tasks started by the
        turn (subagents, tools) inherit the binding, so live, resumed and continued
        executions agree.
        """
        from bowhead_core.model_clock import turn_clock_scope

        with turn_clock_scope(request):
            self._execution_worker = None
            if request.execution_owner_id and getattr(request, "team_id", None) and request.session_id:
                from bowhead_core.document_chat_continuation import DocumentTurnOwner

                self._execution_worker = DocumentTurnOwner(
                    team_id=request.team_id,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    session_id=request.session_id,
                    turn_id=request.turn_id,
                    generation=request.execution_generation,
                    owner_id=request.execution_owner_id,
                )
            orch = self._orch
            terminal_runtime = getattr(request, "terminal_runtime", None)
            if terminal_runtime is None:
                await _require_admitted_legacy_bus(orch, request.turn_id)
                if request.session_id and orch.session_store is None:
                    raise RuntimeError("legacy_chat_session_store_unavailable")
                if orch.session_store is not None:
                    reference_runtime = getattr(request, "reference_runtime", None)
                    if reference_runtime is None:
                        await orch.session_store.require_legacy_chat_execution(turn_id=request.turn_id)
                    else:
                        from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime

                        if not isinstance(reference_runtime, ReferencePrimaryRuntime):
                            raise ValueError("reference_runtime_invalid")
                        await reference_runtime.validate(request)
                        self._reference_runtime = reference_runtime
            # M2.1: register cancel event, unregister finally; docs/M157-cancel-turn-design.md.
            # Tests mock orch with SimpleNamespace; tolerate absent _cancel_events.
            from bowhead_core.orchestrator import TurnCancelledError  # avoid circular at module import
            from bowhead_core.state import (
                is_state_lifecycle_enabled,
                reset_current_state,
                set_current_state,
            )
            from bowhead_core.state_lifecycle import build_lifecycle
            from bowhead_core.terminal_llm_invocation import (
                create_detached_auxiliary_task,
                durable_llm_invocation_scope,
            )

            cancel_event = asyncio.Event()
            cancel_registry_key: str | tuple[str, int] = request.turn_id
            if terminal_runtime is not None:
                registry = getattr(orch, "_terminal_cancel_events", None)
                cancel_registry_key = (
                    request.turn_id,
                    terminal_runtime.scope.generation,
                )
            else:
                registry = getattr(orch, "_cancel_events", None)
            # Sprint η: build lifecycle (Default if flag on, Noop if off)
            # η-#7.5 P1-4 fix: state hydrate INSIDE try so finally cleans up on error
            eta_enabled = is_state_lifecycle_enabled()
            eta_lifecycle = build_lifecycle(
                session_store=getattr(orch, "session_store", None),
                clarification_store=getattr(orch, "clarification_store", None),
                enabled=eta_enabled,
            )

            # Sprint δ: build lifecycle for auto_learner / Confidence Gate hooks.
            # Noop when master flag off OR fact_store missing (test stubs that don't
            # configure δ). δ-#8.5 fix-9: prefer ``orch.fact_store`` (set by app
            # boot); fall back to constructing one from session_store.db_path for
            # backwards-compat with deployments that haven't migrated yet.
            from bowhead_core.delta_lifecycle import build_delta_lifecycle

            _delta_fact_store = getattr(orch, "fact_store", None)
            if _delta_fact_store is None:
                from bowhead_core.registered_fact_store import RegisteredFactStore

                _session_store = getattr(orch, "session_store", None)
                if _session_store is not None and hasattr(_session_store, "db_path"):
                    _delta_fact_store = RegisteredFactStore(_session_store.db_path)
            # ═══ Line B batch 1a: automatic zero-click persistence wiring ═══
            # ★★ **Collision matrix**: one always-in-future utterance can trigger three capture paths;
            # rules here rather than individual modules: coordination needs visibility of all three
            # (closing review demanded decisions at wiring time, never afterward):
            #   ① Explicit remember tool (Lead this turn): **highest priority**, visible same-turn receipt;
            #      when fired, ②③ yield.
            #   ② capture_nl_intent (post-turn candidate pool): existing skip-if-remember-fired
            #      rule (maybe_capture_nl_intent_post_turn); this batch also skips
            #      when ③ wrote, or same utterance yields both memory and card.
            #   ③ This automatic-write path: skip if remember fired (already deterministically persisted).
            # Mutable box: lifecycle constructed **before** turn, collision state known only **after**.
            _capture_ctx: dict[str, Any] = {
                "remember_fired": False,
                # Matrix③: feedback_escalate **successfully persisted** this turn ⇒ user chose destination, personal candidate yields.
                "escalate_fired": False,
                "captured": False,
                # 1c delivery reads here; previously logs only. See seam below.
                "user_message": None,
                "suggested_setting": None,
            }

            async def _on_modification_request(_fact: Any) -> None:
                from bowhead_core.memory_auto_capture import (
                    build_router_drafter,
                    capture_modification_request,
                    outcome_gives_the_utterance_a_home,
                )

                if _capture_ctx["remember_fired"]:
                    logger.info("memory_auto_capture.skipped_remember_fired turn=%s", request.turn_id)
                    return
                if _capture_ctx["escalate_fired"]:
                    # User explicitly requested admin escalation and team candidate persisted ⇒ user-selected destination,
                    # never ask again whether to remember personal preference. Actual 116 (2026-09-14) emitted two cards for same utterance;
                    # criterion agreed with line B agent-feedback-loop: structural signal, not vocabulary.
                    logger.info("memory_auto_capture.skipped_escalate_fired turn=%s", request.turn_id)
                    return
                if turn_authors_an_agent(request, result):
                    logger.info("memory_auto_capture.skipped_agent_authoring turn=%s", request.turn_id)
                    return
                _store = getattr(orch, "user_memory_store", None)
                if _store is None:
                    return
                # ⚠ **Server-deterministic taint**: any tool use this turn means utterance may restate
                # tool output/third-party text ⇒ always demote to confirmation card; no LLM output/frontend trust.
                # **Conservative**: prefer extra demotions to guessing safe tool outputs in 1a.
                _tainted = bool(getattr(result, "tool_invocations", None))
                # ★★ User disables automatic learning ⇒ no steps along this path.
                # Share distillation PauseStateCache (dreaming_light), **never independent lookups**;
                # separate pause decisions cause suggestions paused
                # while automatic writing continues, incomprehensible to user.
                # Missing cache treats **unpaused**: enhancement must not silently fail on unavailable settings;
                # actual fallback is FEATURE_MEMORY_AUTO_CAPTURE=0 (default on).
                _pause_cache = getattr(getattr(orch, "dreaming_light", None), "pause_cache", None)
                _paused = False
                if _pause_cache is not None:
                    try:
                        _paused = await _pause_cache.is_paused(request.user_id)
                    except Exception as e:  # noqa: BLE001 - settings read failure must not fail turn
                        logger.warning("memory_auto_capture.pause_lookup_failed err=%s", e)
                _outcome = await capture_modification_request(
                    user_text=getattr(request, "text", "") or "",
                    suggested_key=getattr(_fact, "fact_key", "") or "",
                    user_id=request.user_id,
                    turn_id=request.turn_id,
                    workspace_id=getattr(request, "workspace_id", None),
                    user_memory_store=_store,
                    drafter=build_router_drafter(_effective_router(orch, request), trace_id=f"{request.turn_id}:capture"),
                    input_is_tainted=_tainted,
                    user_paused=_paused,
                    # ★Tainted requests demote to confirmation card, never discarded.
                    # Without this argument capture_modification_request only returns old
                    # needs_confirmation then does nothing.
                    candidate_store=getattr(orch, "memory_candidate_store", None),
                )
                # Criterion outcome_gives_the_utterance_a_home means memory written **or** existing confirmation card;
                # pure helper enables testing: reverting closure's one-line comparison would fail no tests.
                _capture_ctx["captured"] = outcome_gives_the_utterance_a_home(_outcome)
                # Memory receipt: the structured capture outcome (never the user's or the model's text)
                # decides whether this turn holds an explicit request and what happened to it.
                from bowhead_core.memory_turn_requests import from_capture_outcome, record_best_effort, store_for

                await record_best_effort(
                    store_for(_store),
                    user_id=request.user_id,
                    turn_id=request.turn_id,
                    request=from_capture_outcome(_outcome),
                )
                # The user-reachable path is the memory_turn_requests row above, read by
                # GET /memory/turn-receipt. These two ctx fields only feed the outcome log below.
                from bowhead_core.memory_auto_capture import reason_user_message

                _capture_ctx["user_message"] = reason_user_message(_outcome.reason)
                _capture_ctx["suggested_setting"] = _outcome.suggested_setting
                logger.info(
                    "memory_auto_capture.outcome turn=%s action=%s reason=%s setting=%s has_user_message=%s",
                    request.turn_id,
                    _outcome.action,
                    _outcome.reason,
                    _outcome.suggested_setting or "-",
                    # ★Log only message presence, never original text: measure **delivery gap size**
                    # (turns needing message without delivery surface); text in log pointless and leaks content.
                    bool(_capture_ctx["user_message"]),
                )

            delta_lifecycle = build_delta_lifecycle(
                fact_store=_delta_fact_store,
                # byo-no-clone: δ auto-learner LLM calls honor the team's BYO router too.
                router=_effective_router(orch, request),
                on_modification_request=_on_modification_request,
            )
            # Feishu07.06 #7 (review[8]): plan execution registers turn→plan for cancel_turn
            # to cascade durable plan cancellation (Stop stamps CANCELLED). Place **immediately before try**;
            # lifecycle setup above may raise. Register cancel event/plan only inside try below,
            # or setup failure bypasses finally, leaking process registries.
            _plan_exec_id = getattr(request, "plan_execution_id", None)
            _exec_plans: dict[str, str] | None = getattr(orch, "_executing_plans", None)
            eta_state = None
            eta_state_token = None
            try:
                if registry is not None:
                    cancel_event = registry.setdefault(cancel_registry_key, cancel_event)
                if _plan_exec_id and _exec_plans is not None:
                    _exec_plans[request.turn_id] = _plan_exec_id
                try:
                    eta_state = await eta_lifecycle.on_turn_start(
                        turn_id=request.turn_id,
                        session_id=request.session_id,
                        user_id=getattr(request, "user_id", None),
                        workspace_id=getattr(request, "workspace_id", None),
                    )
                except Exception as e:
                    logger.warning("eta.lifecycle_start_failed turn=%s err=%s", request.turn_id, e)
                    eta_state = None
                # Round 3 P0-1 fix: when flag off, NoopStateLifecycle returns None →
                # set contextvar to None → subagent skips compression entirely.
                # No default state injected, no contextvar pollution.
                eta_state_token = set_current_state(eta_state)
                current_task = asyncio.current_task()
                cancel_watch: asyncio.Task[None] | None = None

                async def _cancel_worker_on_signal() -> None:
                    await cancel_event.wait()
                    if current_task is not None and not current_task.done() and current_task.cancelling() == 0:
                        current_task.cancel("durable_user_cancel")

                try:
                    if current_task is not None:
                        cancel_watch = asyncio.create_task(
                            _cancel_worker_on_signal(),
                            name=(f"turn_cancel_watch:{request.turn_id}:{getattr(request, 'execution_generation', 1)}"),
                        )
                    try:
                        async with durable_llm_invocation_scope(
                            terminal_runtime,
                            cost_estimator=_estimate_cost_usd,
                        ):
                            result = await self._run_inner(request, cancel_event)
                        if terminal_runtime is None and request.session_id:
                            session_store = getattr(orch, "session_store", None)
                            if session_store is not None:
                                try:
                                    canonical_turn = await session_store.get_turn(
                                        request.turn_id,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "turn.cancel_boundary_read_failed turn=%s err=%s",
                                        request.turn_id,
                                        exc,
                                    )
                                else:
                                    canonical_generation = getattr(
                                        canonical_turn,
                                        "execution_generation",
                                        None,
                                    )
                                    try:
                                        request_generation = max(
                                            1,
                                            int(
                                                getattr(
                                                    request,
                                                    "execution_generation",
                                                    1,
                                                )
                                                or 1
                                            ),
                                        )
                                        generation_matches = canonical_generation is None or int(canonical_generation) == request_generation
                                    except (TypeError, ValueError):
                                        generation_matches = False
                                    if (
                                        generation_matches
                                        and normalize_legacy_turn_status(
                                            getattr(canonical_turn, "status", None),
                                        )
                                        == TurnStatus.CANCELLED.value
                                    ):
                                        raise TurnCancelledError(
                                            "turn cancelled by durable Store",
                                        )
                        # Stop may win the Store CAS immediately before Lead
                        # returns. Close that no-await window before accepting a
                        # successful result.
                        self._check_cancel()
                    finally:
                        if cancel_watch is not None:
                            if not cancel_watch.done():
                                cancel_watch.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await cancel_watch
                except TurnCancelledError:
                    # Persist state on cancellation (resume path) — only if state was hydrated
                    if eta_state is not None:
                        try:
                            await eta_lifecycle.on_turn_cancelled(
                                turn_id=request.turn_id,
                                state=eta_state,
                            )
                        except Exception as e:
                            logger.warning("eta.lifecycle_cancel_failed turn=%s err=%s", request.turn_id, e)
                    return await self._finish_cancelled(request)
                except asyncio.CancelledError as cancel_exc:
                    cancel_reason = str(cancel_exc.args[0]) if cancel_exc.args else ""
                    if cancel_reason != "durable_user_cancel":
                        raise
                    if eta_state is not None:
                        try:
                            await eta_lifecycle.on_turn_cancelled(
                                turn_id=request.turn_id,
                                state=eta_state,
                            )
                        except Exception as e:
                            logger.warning(
                                "eta.lifecycle_cancel_failed turn=%s err=%s",
                                request.turn_id,
                                e,
                            )
                    return await self._finish_cancelled(request)
                except InvocationOutcomeUnknownError:
                    raise
                except Exception as e:
                    # Persist state on error path (best-effort) — only if hydrated
                    if eta_state is not None:
                        try:
                            await eta_lifecycle.on_turn_error(
                                turn_id=request.turn_id,
                                state=eta_state,
                                err=e,
                            )
                        except Exception as ee:
                            logger.warning("eta.lifecycle_error_failed turn=%s err=%s", request.turn_id, ee)
                    raise
                else:
                    # Success path — persist HOT state only if hydrated
                    if eta_state is not None:
                        try:
                            await eta_lifecycle.on_turn_end(
                                turn_id=request.turn_id,
                                state=eta_state,
                            )
                        except Exception as e:
                            logger.warning("eta.lifecycle_end_failed turn=%s err=%s", request.turn_id, e)
                    # δ-#7 + δ-#8.5 fix-10: auto_learner + post-verify fire-and-forget.
                    # auto_learner classify on user_message → write fact (Task tracked
                    # in delta_lifecycle._BG_TASKS via _track_task).
                    # post_verify on Lead final_answer → metrics for fact_warnings /
                    # untagged_claim_count. Both must not block TurnResult; failures
                    # logged + swallowed.
                    try:
                        # ★★ Found 2026-09-08: previously request.user_message, but
                        # **TurnRequest lacks field** (uses text) ⇒ getattr always "",
                        # if _user_msg.strip() always false ⇒ **on_post_turn never called**.
                        # Three sources converge: is_provenance_hitl_enabled() True in production,
                        # fact_store constructible (only wrong field blocks),
                        # 116 production registered_facts **0 rows**.
                        # ⚠ Same bug already fixed in capture_nl_intent, this one missed.
                        _user_msg = getattr(request, "text", "") or ""
                        _ws = getattr(request, "workspace_id", "") or ""
                        _proj = getattr(request, "project_id", None)
                        _uid = getattr(request, "user_id", None)
                        # Matrix①: explicit remember fired this turn ⇒ automatic write yields.
                        _capture_ctx["remember_fired"] = any(
                            getattr(_inv, "tool_name", "") == "remember" for _inv in (getattr(result, "tool_invocations", None) or [])
                        )
                        # Matrix③: invocation alone insufficient; missing-target/quota rejection leaves user utterance unhandled.
                        from bowhead_core.memory_auto_capture import escalate_persisted

                        _capture_ctx["escalate_fired"] = escalate_persisted(getattr(result, "tool_invocations", None) or [])
                        # ★on_post_turn only **schedules** detached classification and returns task. Retain it:
                        # NL-intent task below waits for decision before reading _capture_ctx, or sees unwritten stale values.
                        _delta_task: Any = None
                        if delta_lifecycle.is_enabled():
                            if _user_msg.strip():
                                _delta_task = await delta_lifecycle.on_post_turn(
                                    directive=_user_msg,
                                    workspace_id=_ws,
                                    session_id=request.session_id,
                                    turn_id=request.turn_id,
                                    project_id=_proj,
                                    user_id=_uid,
                                )
                            # post-verify on Lead final_answer (best-effort metric).
                            # Wrapped in asyncio.create_task → does not delay return.
                            _final = getattr(result, "final_answer", "") or ""
                            if _final:
                                from bowhead_core.delta_lifecycle import _track_task

                                async def _post_verify(
                                    _lc: Any = delta_lifecycle,
                                    _txt: str = _final,
                                    _w: str = _ws,
                                    _p: Any = _proj,
                                    _u: Any = _uid,
                                    _tid: str = request.turn_id,
                                ) -> None:
                                    try:
                                        v = await _lc.on_post_verify(
                                            _txt,
                                            workspace_id=_w,
                                            project_id=_p,
                                            user_id=_u,
                                        )
                                        from bowhead_core import metrics as _m

                                        _m.inc("delta.post_verify.invocations")
                                        if v.fact_warnings:
                                            _m.inc(
                                                "delta.post_verify.fact_warnings",
                                                value=float(len(v.fact_warnings)),
                                            )
                                        if v.untagged_claim_count > 0:
                                            _m.inc(
                                                "delta.post_verify.untagged_claims",
                                                value=float(v.untagged_claim_count),
                                            )
                                        if not v.passed:
                                            _m.inc(
                                                "delta.post_verify.failed",
                                                labels={"turn_id": _tid[:32]},
                                            )
                                    except Exception as ee:
                                        logger.warning(
                                            "delta.post_verify_bg_failed turn=%s err=%s",
                                            _tid,
                                            ee,
                                        )

                                _track_task(
                                    create_detached_auxiliary_task(
                                        _post_verify(),
                                        reason="delta_post_verify",
                                    )
                                )

                        # remember-rebuild Phase 2: implicit memory capture → HITL AI-suggestion queue.
                        # Independent of δ; the classifier is None when BOWHEAD_NL_INTENT_ENABLED is
                        # off → no task created. Fire-and-forget so it never delays/breaks the turn.
                        # The trigger lives in maybe_capture_nl_intent_post_turn (module-level, unit-
                        # tested with a real TurnRequest so the request.text field read can't silently
                        # regress the way the δ block's stale `user_message` read did).
                        if getattr(orch, "nl_memory_intent_classifier", None) is not None:
                            from bowhead_core.delta_lifecycle import _track_task as _tt_nl

                            _tt_nl(
                                create_detached_auxiliary_task(
                                    maybe_capture_nl_intent_post_turn(
                                        orch,
                                        request,
                                        result,
                                        capture_ctx=_capture_ctx,
                                        delta_task=_delta_task,
                                    ),
                                    reason="nl_memory_intent",
                                )
                            )

                        # memory-revive S1 — Dreaming Light distillation → HITL candidates queue.
                        # dreaming_light is None when BOWHEAD_DREAMING_ENABLED is off (gateway
                        # mirrors the NL-intent classifier gating). Fire-and-forget: never delays
                        # the turn; touches no bus/SSE (post-done live emit is physically
                        # impossible — bus closes at `done`; delivery is REST polling +
                        # notification, see docs/sprints/memory-revive/discovery.md §6.3).
                        if getattr(orch, "dreaming_light", None) is not None:
                            from bowhead_core.delta_lifecycle import _track_task as _tt_dream

                            _tt_dream(
                                create_detached_auxiliary_task(
                                    maybe_dream_post_turn(orch, request, result),
                                    reason="dreaming",
                                )
                            )
                    except Exception as e:
                        logger.warning("delta.lifecycle_post_turn_failed turn=%s err=%s", request.turn_id, e)
                    return result
            finally:
                # η-#7.5 P1-4 fix: only reset if token was actually set
                if eta_state_token is not None:
                    reset_current_state(eta_state_token)
                if registry is not None and registry.get(cancel_registry_key) is cancel_event:
                    registry.pop(cancel_registry_key, None)
                if _exec_plans is not None:  # Feishu #7: unregister turn→plan.
                    _exec_plans.pop(request.turn_id, None)

    async def _run_inner(self, request: TurnRequest, cancel_event: asyncio.Event) -> TurnResult:
        """原 run() body. 关键 boundary 调 self._check_cancel() 抛 TurnCancelledError 出去
        让外层 catch + emit cancelled event."""
        orch = self._orch
        terminal_runtime = getattr(request, "terminal_runtime", None)
        bus = terminal_runtime.bus if terminal_runtime is not None else await _require_admitted_legacy_bus(orch, request.turn_id)
        t_start = time.perf_counter()
        # Attach cancel_event to self for boundary checks, avoiding long argument chain.
        self._cancel_event = cancel_event
        self._t_start = t_start
        # Turn duration: this execution generation's turn_started→done wallclock, same starting point
        # as _finish_cancelled; store on bus, Lead done boundary
        # (lead_orchestrator._emit_event) rewrites done.duration_ms, summary reads same value,
        # keeping live/persisted/replay consistent. Previously Lead-loop-only clock omitted preparation
        # and final writing (actual 116: 121s turn displayed 1min5s).
        with contextlib.suppress(Exception):
            bus._turn_wall_t0_perf = t_start  # type: ignore[attr-defined]
        self._summary_bus = bus
        self._check_cancel()
        await _restore_document_revision_target(request, getattr(orch, "session_store", None))
        revision_receipt = await _document_revision_receipt(request, getattr(orch, "session_store", None))

        # report-streaming D2: started_at is **this turn's beginning**, frontend waiting timer reads it.
        #
        # Payload rather than local frontend timing: F5 resets local timer; waiting p90 173s
        # (actual 116 production), precisely likely refresh window. turn_events persists payload
        # and replays same value → live == persist == replay.
        #
        # Existing event gets field, no new event type, so no browser EventSource allowlist
        # risk (unregistered silently drops frame). Old frontend ignores unknown field, backward compatible.
        from bowhead_core.coach_canvas_target import coach_canvas_projection

        started_accepted = await bus.emit(
            orch._event(
                request.turn_id,
                "turn_started",
                {
                    "session_id": request.session_id,
                    "workspace_id": request.workspace_id,
                    "user_text": request.text,
                    "started_at": datetime.now(UTC).isoformat(),
                    **coach_canvas_projection(getattr(request, "coach_canvas_target", None)),
                    **revision_receipt,
                    "schema_version": "0.9.0",
                },
            )
        )
        if not started_accepted:
            raise RuntimeError("turn_started_event_not_durably_accepted")

        # Sprint chat-redesign-2026-05-21 Phase 1.1 — universal Step List Card
        # shell event. FE relies on this BEFORE any step_started arrives to
        # mount the card container (even plain-text turns get one). Emitted
        # right after turn_started so seq ordering is predictable.
        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="step_list_card_created",
                    turn_id=request.turn_id,
                    payload={"card_id": request.turn_id, "plan": None},
                )
            )
        except Exception as _cce:
            raise_if_terminal_bus_failed(bus)
            logger.warning(
                "chat_redesign.step_list_card_created_emit_failed turn=%s err=%s",
                request.turn_id,
                _cce,
            )

        if request.document_clarification_id is not None:
            from bowhead_core.document_chat_runtime import resume_document_planning

            return await resume_document_planning(self, request, bus)

        access_store = getattr(orch, "access_control_store", None)
        if access_store is not None and not getattr(request, "access_context", None):
            enforce_grants = False
            try:
                from bowhead_core.access_control import resource_grants_enforce_enabled

                enforce_grants = resource_grants_enforce_enabled()
            except Exception as exc:
                logger.warning(
                    "resource_grant.turn_context_guard_failed turn=%s err=%s",
                    request.turn_id,
                    exc,
                )
            if enforce_grants:
                duration_ms = int((time.perf_counter() - t_start) * 1000)
                error = "access_context_missing"
                failure = get_or_build_chat_turn_failure(
                    carrier=bus,
                    reason_code=error,
                    turn_id=request.turn_id,
                )
                failure_marked = terminal_runtime is not None or getattr(orch, "session_store", None) is None
                if terminal_runtime is None and getattr(orch, "session_store", None) is not None and request.session_id:
                    try:
                        failure_marked = bool(
                            await orch.session_store.mark_failed(
                                worker=getattr(self, "_execution_worker", None),
                                turn_id=request.turn_id,
                                error=error,
                                error_kind="access_context_missing",
                                intent=None,
                                final_answer=None,
                                tokens_input=0,
                                tokens_output=0,
                                cost_usd=0.0,
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            "session.mark_failed_access_context_missing_failed turn=%s err=%s",
                            request.turn_id,
                            e,
                        )
                if failure_marked:
                    try:
                        await bus.emit(
                            orch._event(
                                request.turn_id,
                                "turn_failed",
                                {
                                    "reason": failure.reason_code,
                                    "diagnostics": failure.diagnostics,
                                },
                            )
                        )
                        await bus.emit(
                            orch._event(
                                request.turn_id,
                                "done",
                                {
                                    "turn_id": request.turn_id,
                                    "status": TurnStatus.FAILED.value,
                                    "duration_ms": duration_ms,
                                    "reason": "access_context_missing",
                                    "reason_code": failure.reason_code,
                                    "error": error,
                                    "error_kind": "access_context_missing",
                                    "diagnostics": failure.diagnostics,
                                },
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            "turn.access_context_missing_terminal_emit_failed turn=%s occurrence_id=%s err=%s",
                            request.turn_id,
                            failure.occurrence_id,
                            e,
                        )
                return TurnResult(
                    turn_id=request.turn_id,
                    status=TurnStatus.FAILED,
                    intent=IntentType.LOOKUP,
                    final_answer="",
                    tool_invocations=[],
                    tokens_input=0,
                    tokens_output=0,
                    duration_ms=duration_ms,
                    error=error,
                )

        # ── Sprint θ Phase 8c-B: A2 strict — single Lead orchestrator entry ─
        # All turns route through Lead. Hybrid (v2 hook + meta_intent classifier +
        # 4 _handle_* shortcuts + Lead 4-agent legacy) DELETED. Failures here = real
        # bugs to fix, not fall through to legacy.
        from bowhead_core.agents.lead_orchestrator import LeadRuntime
        from bowhead_core.agents.lead_orchestrator import run_turn as _lead_run_turn
        from bowhead_core.agents.task_spec import BusRef
        from bowhead_core.agents.turn_context import SerializableTurnContext
        from bowhead_core.tool_registry import ToolExecutionContext

        _sctx = SerializableTurnContext(
            user_id=request.user_id or "",
            workspace_id=request.workspace_id or "",
            session_id=request.session_id or "",
            turn_id=request.turn_id,
            role="lead",  # paradigm_v2 role binding
            project_id=getattr(request, "project_id", None),
            kb_role=getattr(request, "kb_role", None),
            team_id=getattr(request, "team_id", None) or "",  # cca P3: team scope (Celery-safe carrier)
            plan_execution_id=getattr(request, "plan_execution_id", None),  # Plan execution turn.
            planning_mode=getattr(request, "planning_mode", None),  # hook1 trigger mode.
            bus_ref=BusRef(session_id=request.session_id or "", turn_id=request.turn_id),
        )
        # content-permissions S4 (tech-design §2.1): one turn-start SQL prefetch of caller's
        # shared-with-me IDs (per kind, deny-wins removed); downstream resolve / Lead peer assembly /
        # skill_resolver all memory lookup. Snapshot: midturn revocation leaves ongoing turn unchanged (PRD §3 US3).
        # Failed/unconfigured → (None,None) → checkpoints fall back to visibility-only, fail-safe.
        self._check_cancel()
        # ★Predispatch **user-visible dead time** (frontend Preparing), previously **no timing**;
        # actual 116 2026-09-06 median 15.4s, p90 46.1s, max61.4s,
        # 14 of 40 turns >20s. Further attribution blocked because no segment timed.
        # Only purpose: record each prefetch wallclock so next attribution needs grep, not archaeology.
        # (Memory prefetch measured 30ms, not culprit; retained for comparison.)
        _pre_t0 = time.monotonic()
        _pre_marks: list[tuple[str, float]] = []

        def _pre_mark(_name: str) -> None:
            _pre_marks.append((_name, (time.monotonic() - _pre_t0) * 1000))

        _granted_subagents, _granted_skills = await _shared_grant_prefetch(orch, request)
        _pre_mark("grants")
        # byo-no-clone: resolve the turn's effective Router / KB retriever ONCE (BYO team
        # override on the request wins; else process-global orch.*). The subagent_runner is
        # built FRESH from the effective router so peer/compute subagents honor the team's BYO
        # model — the pre-built orch.subagent_runner singleton is baked with the GLOBAL router,
        # and reading it left ctx.subagent_runner=None for BYO teams (clone dropped the sidecar).
        _eff_router = _effective_router(orch, request)
        _eff_kb_retriever = _effective_kb_retriever(orch, request)
        _eff_subagent_runner = _effective_subagent_runner(orch, request)
        _pre_mark("effective_router")
        _r6_unconfigured = await _resolve_unconfigured_capability_tools(orch, request)
        _pre_mark("unconfigured_tools")
        # Builtin subagent management: same one-time prefetch (≤builtin count rows), spawn/peer assembly memory lookup.
        _builtin_overrides = await _builtin_override_prefetch(orch, request)
        _pre_mark("builtin_overrides")
        _resource_disable = await _resource_disable_prefetch(orch, request)
        _pre_mark("resource_disable")
        _run_limits = await _run_limits_prefetch(orch, request)
        from bowhead_core.execution_limits import load_team_limits as load_execution_limits

        _execution_limits = await load_execution_limits(getattr(orch, "team_settings_store", None), getattr(request, "team_id", None))
        # Memory rebuild batch0④ (tech-design §3.2): one start-of-turn snapshot, sole decision point.
        # Flag off →None →Lead builds fallback (current byte-identical behavior).
        _memory_pack = await _memory_pack_prefetch(orch, request)
        _pre_mark("memory_pack")
        _space_instructions = await _space_instructions_prefetch(orch, request)
        _pre_mark("space_instructions")
        with contextlib.suppress(Exception):  # Timing logs must never affect turn.
            logger.info(
                "turn.prespawn_ms turn=%s total=%.0f %s",
                request.turn_id,
                (time.monotonic() - _pre_t0) * 1000,
                " ".join(f"{_n}={_ms:.0f}" for _n, _ms in _pre_marks),
            )
        if _memory_pack is not None:
            # Persist same snapshot unchanged in memory_pack_snapshots (fire-and-forget, never blocks turn);
            # **First production _maybe_save_pack_snapshot call**: FR9 exact turn injection inspection
            # needs no new table/route/UI; /api/v1/memory/replay directly revived.
            _maybe_save_pack_snapshot(
                orch=orch,
                request=request,
                content=_memory_pack.content,
                breakdown=_memory_pack.breakdown,
                pack_version=_memory_pack.version,
                executor_block=_memory_pack.executor_block,
            )
        _peer_ctx = ToolExecutionContext(
            personal_mcp_license_check=getattr(orch, "personal_mcp_license_check", None),
            workspace_id=request.workspace_id or "",
            # workspace-datarc-decouple P1 (R1.2): datarc project for bi _ws (None → fallback to workspace_id).
            active_datarc_project=getattr(request, "active_datarc_project", None),
            # Batch 2 (PRD §3.3b/§3.11): per-user snapshot for sentinel options/provenance names.
            active_datarc_workspace_name=getattr(request, "active_datarc_workspace_name", "") or "",
            accessible_datarc_workspaces=tuple(getattr(request, "accessible_datarc_workspaces", ()) or ()),
            datarc_absence_reason=getattr(request, "datarc_absence_reason", "") or "",  # bi-absent-turn R3.1 (sync both mirror sites).
            unconfigured_capability_tools=_r6_unconfigured,  # credential-readiness R6 (sync both mirrors).
            datarc_context_switcher=getattr(request, "datarc_context_switcher", None),
            config_lite_supported=bool(getattr(request, "config_lite_supported", False)),  # Config copilot batch0.5 (sync both mirrors).
            configuration_settings_target=getattr(request, "configuration_settings_target", None),
            coach_canvas_target=getattr(request, "coach_canvas_target", None),
            document_revision_target=getattr(request, "document_revision_target", None),
            member_uid=getattr(request, "member_uid", "") or "",
            user_id=request.user_id or "",
            turn_id=request.turn_id,
            datarc=None if getattr(request, "datarc_unavailable", False) else getattr(request, "datarc_client", None),
            vector_client=None if getattr(request, "datarc_unavailable", False) else getattr(request, "vector_client", None),
            # P0-SEC-1: propagate project_id so tool handlers can scope DB / KB
            # queries to the request's project (when tenants partition by it).
            project_id=getattr(request, "project_id", None),
            # byo-no-clone: fresh runner built from the turn's effective (BYO-aware) router.
            subagent_runner=_eff_subagent_runner,
            parent_bus=bus,
            artifact_store=getattr(orch, "artifact_store", None),
            canvas_store=getattr(orch, "canvas_store", None),
            # scheduled_job_create tool (flag-gated). This inline Lead-path ctx — NOT the
            # build_tool_execution_context factory — is what the Lead actually runs with; the
            # factory copy alone leaves ctx.scheduled_store=None at runtime (caught by dogfood,
            # same retrofit pattern as kb_retriever / kb_doc_type_store below).
            scheduled_store=getattr(orch, "scheduled_store", None),
            session_store=getattr(orch, "session_store", None),  # Subagent prior-work memo reads previous spawn.
            session_id=request.session_id or "",
            user_text=request.text or "",  # template-refresh: verbatim user msg for deck intent
            approval_gate=getattr(orch, "approval_gate", None),
            subagent_draft_service=getattr(
                request,
                "subagent_draft_service",
                None,
            ),
            subagent_authoring_context_store=getattr(
                orch,
                "subagent_authoring_context_store",
                None,
            ),
            subagent_draft_mcp_entitled=bool(
                getattr(
                    request,
                    "subagent_draft_mcp_entitled",
                    False,
                ),
            ),
            subagent_draft_editor=getattr(request, "subagent_draft_editor", None),
            agent_coach_resources=getattr(request, "agent_coach_resources", None),
            router=_eff_router,  # byo-no-clone
            # remember-rebuild (item 2): Lead remember/recall tools run with THIS ctx.
            # (inline Lead path, role="lead") — forward the memory stores here.
            memory_notes_store=getattr(orch, "memory_notes_store", None),
            user_memory_store=getattr(orch, "user_memory_store", None),
            memory_pack=_memory_pack,  # Memory rebuild batch0④ (_memory_pack_prefetch above).
            space_instructions=_space_instructions,
            project_memory_store=getattr(orch, "project_memory_store", None),
            schema_cache=getattr(orch, "schema_cache", None),
            clarification_store=getattr(orch, "clarification_store", None),
            language_context=getattr(request, "language_context", None),
            user_profile=getattr(request, "user_profile", None),  # answer_style etc. (sync both mirror sites).
            preferred_provider=getattr(request, "preferred_provider", None),
            preferred_model=getattr(request, "preferred_model", None),
            role="lead",
            kb_role=getattr(request, "kb_role", None) or "user",
            # cca LOAD POINT (§4): team_id from the JWT-derived request (NO `or workspace_id`
            # — fail-closed: falsy ⇒ "" ⇒ no team tools). tool_view is set below once the
            # per-turn team-tool overlay is built; subagents inherit it via parent_ctx (cca ③).
            team_id=getattr(request, "team_id", None) or "",
            access_context=getattr(request, "access_context", None),
            access_control_store=getattr(orch, "access_control_store", None),
            # content-permissions S4: turn prefetch set (_shared_grant_prefetch above).
            granted_team_subagent_ids=_granted_subagents,
            granted_team_skill_ids=_granted_skills,
            # Builtin subagent management: team deltas on system builtins (_builtin_override_prefetch above).
            builtin_subagent_overrides=_builtin_overrides,
            resource_disable_state=_resource_disable,
            agent_run_limits=_run_limits,
            execution_limits=_execution_limits,
            # Discoverability fix (actual LLM dogfood 2026-07-16): _peer_ctx, actual inline Lead ctx,
            # previously lacked skill stores. skill_list/_read/_invoke _list/_resolve read
            # ctx.skill_instance_store + ctx.team_skill_store; absent means user/team uploads
            # neither enter skill_list (section2 skips if store None) nor resolve for invocation;
            # model sees only 37 builtins, never skill_read/skill_invoke. Factory _ctx (build_tool_exec) had stores,
            # inline Lead lacked them, same retrofit pattern as inline_preview_enabled / kb_doc_type_store / scheduled_store
            # (zero stored user skills hid flaw; first real upload exposed it).
            skill_instance_store=getattr(orch, "skill_instance_store", None),
            team_skill_store=_team_skill_store(orch),
            skill_catalog=getattr(orch, "skill_catalog", None),
            tool_view=None,
            # Sprint ω Phase 5 P0-CTO-1 fix (2026-05-12): plumb metrics_log
            # onto the Lead-side peer_ctx so scanner-decision telemetry
            # emits on the v1 turn-runner path too (was missing — rollback
            # signal would have been 50% blind).
            metrics_log=getattr(orch, "metrics_log", None),
            # chat-truth-hotfix-2 (2026-05-15): _peer_ctx was missing this
            # field, defaulting to False in dataclass. Result: peer's tool
            # exec (compute peer bi_query / bi_schema etc.) never built
            # preview_payload → schedule_finding skipped at "no_preview" gate
            # → 0 tool_finding events → FE chat trace tool tile showed "—".
            # build_tool_exec_ctx (line ~398) had this; inline _peer_ctx
            # construction was missing it. Now in sync.
            inline_preview_enabled=getattr(orch, "inline_preview_enabled", False),
            # kb-knowledge-tables: kb_list_tables / kb_query_records / kb_get_record
            # tool ctx (Lead side). Was missing from _peer_ctx — _build_tool_ctx
            # path had them but the inline Lead path was missing the wire, so
            # chat → kb_list_tables silently returned empty. Surfaced by v2 §8.5
            # dogfood gate.
            kb_doc_type_store=getattr(orch, "kb_doc_type_store", None),
            kb_extracted_record_store=getattr(orch, "kb_extracted_record_store", None),
            kb_extracted_rows_store=getattr(orch, "kb_extracted_rows_store", None),
            # 0.40.2 Bug 3: kb_retriever wire was missing from _peer_ctx (only
            # _build_tool_ctx at line ~605 had it). Lead's inline kb_search call
            # → ctx.kb_retriever=None → tool early-return "KB 未配置". Surfaced
            # by Phase 4 dogfood (turn t_f45600c577fc / t_fdc572467516 on prod
            # 0.40.1) — corpus retrievable via /api/v1/kb/search HTTP but not via
            # agent loop. Same retrofit pattern as kb_doc_type_store above.
            kb_retriever=_eff_kb_retriever,  # byo-no-clone: team KB retriever wins
            # 0.40.4 Bug 8 step a: same wire as _build_tool_ctx — kb_files_store
            # so _kb_search resolves user's personal-scope workspaces.
            kb_files_store=getattr(orch, "kb_files_store", None),
            kb_file_authorizer=getattr(orch, "kb_file_authorizer", None),
            # Sprint ar2: tenant + per-ws calibrated thresholds
            tenant=getattr(orch, "datarc_tenant", ""),
            ar2_thresholds=getattr(orch, "ar2_thresholds_by_ws", {}).get(
                str(request.workspace_id or ""),
            )
            if getattr(orch, "ar2_thresholds_by_ws", None)
            else None,
        )
        # cca P3 F-B: per-turn artifact-fetch token minter (closure holds the signing
        # secret; tools.py mints fresh per python_sandbox call). Lead-side peer_ctx.
        _peer_ctx._mint_artifact_token = _build_artifact_token_minter(orch, request.user_id or "")
        # ctx-types-fix Phase 2 (2026-05-19): single-writer init for all
        # turn-start transient slots. Replaces 5 ad-hoc setattr blocks (Bug 3
        # surface — 0.26.0 prod hang was paradigm_v2 / preferred_* never set
        # here). Adding a new transient slot now requires editing both
        # LeadTurnTransient (schema) and init_lead_transient (init) — that
        # two-place edit is the gate that catches forgot-to-wire.
        init_lead_transient(_peer_ctx, request)
        # Read selectors from the immutable user source, never from user prose.
        _chart_source_reader = getattr(orch.session_store, "get_chat_turn_user_source", None)
        if request.attachments and callable(_chart_source_reader):
            _chart_source = await _chart_source_reader(
                turn_id=request.turn_id,
                user_id=request.user_id,
                workspace_id=request.workspace_id,
            )
            if _chart_source is not None and any(ref.get("selectedContext") is not None for ref in _chart_source.attachment_refs):
                from bowhead_core.chart.selected_context import resolve_selected_contexts

                if _chart_source.session_id != request.session_id or list(_chart_source.attachment_ids) != request.attachments:
                    raise ValueError("chart_selection_unavailable")
                _peer_ctx._chart_selected_facts = await resolve_selected_contexts(
                    getattr(orch, "artifact_store", None),
                    _chart_source.attachment_refs,
                    user_id=request.user_id,
                    workspace_id=request.workspace_id,
                    session_id=request.session_id,
                )
        if getattr(request, "resume_turn_id", None):
            logger.info(
                "turn_runner.resume_signal turn=%s glossary_keys=%d",
                request.turn_id,
                len(getattr(request, "resume_glossary", None) or {}),
            )

        # scheduled-recipe-runs S1: scheduled task-bound solidified workflow (gateway runner resolved
        # and admitted before firing). Stash in parent_ctx for lead_orchestrator prompt rendering, like ψ-F
        # pinned snapshot; avoids build_lead_system_prompt parameter.
        _bound_workflow = getattr(request, "bound_workflow", None)
        if _bound_workflow is not None:
            _peer_ctx._bound_workflow = _bound_workflow
        _agent_binding = getattr(
            request,
            "agent_binding",
            None,
        )
        if _agent_binding is not None:
            _peer_ctx._agent_binding = _agent_binding
        _peer_ctx._reference_runtime = getattr(request, "reference_runtime", None)
        _peer_ctx.execution_generation = request.execution_generation
        _peer_ctx.execution_owner_id = request.execution_owner_id
        _peer_ctx.collaboration_store = getattr(request, "collaboration_store", None)
        if request.plan_execution_id:
            from bowhead_core.chat_reference_plan import prepare_plan_runtime

            try:
                _peer_ctx._reference_plan_runtime = await prepare_plan_runtime(orch, request)
            except Exception:
                await orch.plan_store.release_execution_claim(request.plan_execution_id, request.turn_id)
                raise

        # Sprint ψ-F: fetch pinned attachments + inject metadata snapshot onto ctx.
        # Lead orchestrator (lead_orchestrator.py:716+) reads this and appends to
        # Lead system prompt. Snapshot at turn start; mid-turn pin doesn't reflect.
        try:
            from bowhead_core import psi_flags as _psi_flags

            if _psi_flags.get("PSI_PIN_INJECT") and orch.session_store is not None and request.session_id:
                _aids = await orch.session_store.list_pinned_attachments(
                    session_id=request.session_id,
                )
                _pinned_total = len(_aids)
                _astore = getattr(orch, "artifact_store", None)
                # Cap top 30 (most recent — list_pinned_attachments returns ASC,
                # so take from tail). Per PRD §3.6 r2.
                _top_ids = _aids[-30:] if _pinned_total > 30 else _aids
                _snapshot = await build_pinned_snapshot(_astore, _top_ids)
                _peer_ctx._psi_pinned_snapshot = _snapshot
                _peer_ctx._psi_pinned_total = _pinned_total
        except Exception as _pe:
            logger.warning("psi.pinned_fetch_failed turn=%s err=%s", request.turn_id, _pe)

        # Sprint chat-redesign-2026-05-21 Phase 1.1 — preprocess_attachments.
        # Runs sequentially AFTER ψ-F snapshot (which is metadata-only) and
        # BEFORE Lead starts, so Lead sees real extracted content rather than
        # just filenames. Emits per-attachment step_started/step_done so FE
        # Step List Card can render parse progress before the LLM responds.
        #
        # Sprint pptx-from-template M1 (RD-2): turn-lifecycle auto-pin of
        # request.attachments. The chat composer uploads at *attach* time
        # (before a session exists), so a /files/upload?session_id= pin
        # silently no-ops — the durable, session-guaranteed pin point is HERE,
        # after the centralized Gateway adapter has committed Session + Turn.
        # Any attachment used in a
        # turn becomes durable for the next turn, so a template uploaded in
        # turn 1 survives to turn 5 with NO re-upload (the s_a9b693829c3d fix).
        # Default-ON, best-effort (a pin failure must not break the turn).
        if orch.session_store is not None and request.session_id and request.attachments:
            try:
                await orch.session_store.pin_session_attachments(
                    session_id=request.session_id,
                    artifact_ids=list(request.attachments),
                )
            except Exception as _pin_e:
                logger.warning(
                    "turn.autopin_failed turn=%s session=%s err=%s",
                    request.turn_id,
                    request.session_id,
                    _pin_e,
                )

        _parsed_for_bundles: list[Any] = []
        # Scope = pinned_snapshot ∪ request.attachments (deduped by aid). The
        # request.attachments path covers fresh uploads that gateway didn't
        # pre-pin (US-3 "text + xlsx upload") — without this union, those
        # uploads would skip the preprocess step events entirely.
        try:
            _astore_for_preprocess = getattr(orch, "artifact_store", None)
            _scope_aids: list[str] = []
            _scope_items: list[dict[str, Any]] = []
            _seen_aids: set[str] = set()
            # 1. pinned snapshot (already has filename/mime/size metadata)
            for _it in getattr(_peer_ctx, "_psi_pinned_snapshot", None) or []:
                _aid = _it.get("artifact_id") if isinstance(_it, dict) else None
                if _aid and _aid not in _seen_aids:
                    _seen_aids.add(_aid)
                    _scope_items.append(_it)
            # 2. current-turn uploads (resolve metadata via artifact_store)
            for _aid in request.attachments or []:
                if not _aid or _aid in _seen_aids:
                    continue
                _seen_aids.add(_aid)
                if _astore_for_preprocess is None:
                    continue
                try:
                    _art = await _astore_for_preprocess.get(_aid)
                except Exception:
                    _art = None
                if _art is None:
                    # Synthesize minimal record so preprocess emits a parse step
                    # with error rather than skipping silently.
                    _scope_items.append(
                        {
                            "artifact_id": _aid,
                            "filename": _aid,
                            "mime": "",
                            "size": 0,
                        }
                    )
                    continue
                _scope_items.append(
                    {
                        "artifact_id": _aid,
                        "filename": getattr(_art, "filename", "?"),
                        "mime": getattr(_art, "mime", "?"),
                        "size": getattr(_art, "size", 0),
                        "kind": getattr(_art, "kind", None),
                        "inject": getattr(_art, "inject", None),
                    }
                )
            # Gate A (M1 RD-6, AUTHORITATIVE): only inject=='full' assets get
            # fully parsed/extracted. 'list'/'stub' assets (tool_data CSVs,
            # web_clips, deliverable decks) stay metadata-only in the pinned
            # bullet list and emit ZERO parse-step events — this stops the
            # CSV-flood that buried the user's PPT in s_a9b693829c3d. Items with
            # no inject (missing/synthesized) default to 'full' so a missing
            # artifact still surfaces a parse-with-error step.
            #
            # Review fix: the COMPENSATING surfacing (the "Available pinned
            # datasets" bullet list, lead_orchestrator) is gated behind
            # PSI_PIN_INJECT. The skip-parse partition MUST be coupled to the
            # same flag — otherwise (flag OFF) a list/stub item is skipped from
            # parsing AND has no bullet list to surface it, so it vanishes from
            # the prompt. When the flag is off, fall back to legacy (parse all).
            from bowhead_core import psi_flags as _pf

            # Office deferral is request-local and independent of persistent
            # inject/PSI flags. These references still pass through the helper
            # for metadata, without body reads or parse-step events.
            # Canonical UI references prove the user's ordinal; display order alone does not.
            _positions = await _current_attachment_positions(request, orch.session_store)
            _display_order = {_aid: _index for _index, _aid in enumerate(request.attachments or [], 1)}
            for _it in _scope_items:
                _it["current_message_position"] = _positions.get(_it["artifact_id"])
            _scope_items.sort(key=lambda _it: _display_order.get(_it["artifact_id"], len(request.attachments or []) + 1))
            if _pf.get("PSI_PIN_INJECT"):
                _prepared_items = [
                    _it
                    for _it in _scope_items
                    if (_it.get("inject") or "full") == "full"
                    or defer_office_content(filename=_it["filename"], mime=_it["mime"])
                    or _it["artifact_id"] in (request.attachments or [])
                ]
            else:
                _prepared_items = _scope_items  # legacy: no surfacing → parse all, except Office deferral
            # BI resumes restore their original file data from the sealed
            # checkpoint. A Lead question before delegation has no checkpoint:
            # re-read only this source's selected files so vision bytes reach
            # both the resumed Lead and its exact primary. Re-reading is safe
            # here because the primary is still admitted and has done no work.
            _reference_resume = _peer_ctx._reference_runtime is not None and _peer_ctx._reference_runtime.generation > 1
            _reference_reparse_items = await _reference_resume_attachment_snapshot(
                _peer_ctx._reference_runtime, request.attachments or [], _scope_items
            )
            if _reference_reparse_items is not None:
                _prepared_items = _reference_reparse_items
            if request.ordinary_clarification_id is not None:
                # This continuation retains exact parsed material. Reopening
                # validates bytes and access without emitting extraction again.
                _prepared_items = []
            if _prepared_items and _astore_for_preprocess is not None and (not _reference_resume or _reference_reparse_items is not None):
                _parsed, _failed_meta = await _preprocess_attachments_with_events(
                    snapshot=_prepared_items,
                    bus=None if _reference_reparse_items is not None else bus,
                    turn_id=request.turn_id,
                    artifact_store=_astore_for_preprocess,
                )
                _peer_ctx._psi_parsed_results = (
                    [item for item in _parsed if item.artifact_id in (request.attachments or [])]
                    if _peer_ctx._reference_runtime is not None
                    else _parsed
                )
                _parsed_for_bundles = list(_parsed or [])
                if _failed_meta:
                    _peer_ctx._psi_failed_metadata = _failed_meta
        except Exception as _ppe:
            logger.warning(
                "preprocess.attachments_failed turn=%s err=%s",
                request.turn_id,
                _ppe,
            )

        # skill-import batch 2 (FEATURE_BUNDLE_IMPORT_DRAFT off by default): bundle inventory to Lead.
        # Off: no DB lookup/prompt injection, exactly batch1 behavior. Own try: failure is not attachment parse failure.
        try:
            if bundle_import_draft_enabled():
                _bundles = await _bundle_import_bundles(request, orch.session_store, getattr(orch, "artifact_store", None), _parsed_for_bundles)
                if _bundles:
                    _peer_ctx._bundle_import_bundles = _bundles
        except Exception as _bie:
            logger.warning("bundle_import.collect_failed turn=%s err=%s", request.turn_id, type(_bie).__name__)

        # byo-no-clone: reuse the SAME effective-router runner the Lead's tool ctx got, so the
        # planning path (PlanDeps.agent_runner_fn) and the tool path spawn identical, BYO-aware
        # peers. (Previously read orch.subagent_runner = global-router singleton.)
        _theta_subagent_runner = _eff_subagent_runner
        # cca LOAD POINT: build the per-turn team tool VIEW (native base + this team's
        # `teammcp.*` overlay) and hold its in-process MCP ClientSession open for the WHOLE
        # orchestration (Lead + inline-awaited subagents — no detached tasks) via the exit
        # stack; it closes right after _lead_run_turn. The view's handlers need the session
        # live during dispatch. Native-only (view == base, tool_view None) when there's no
        # team_id / no deps / no sources (fail-closed). The persist/cleanup below stays
        # OUTSIDE the stack (the session can close once the Lead has finished).
        self._check_cancel()
        _lead_result: Any = None
        async with contextlib.AsyncExitStack() as _team_tool_stack:
            _tool_view = await self._build_team_tool_view(
                _team_tool_stack,
                request,
                orch,
                _peer_ctx,
            )
            _runtime = LeadRuntime(
                router=_eff_router,  # byo-no-clone: Lead's own LLM calls use the team router
                tool_registry=_tool_view,
                parent_bus=bus,
                parent_ctx=_peer_ctx,
                fact_store=getattr(orch, "fact_store", None),
                subagent_runner=_theta_subagent_runner,
                # ν2 v2: no pre-classify; response_kind is computed post-hoc in
                # lead_orchestrator from result.tool_invocations.
                # Sprint π Phase A: single chokepoint persist into _emit_event("done").
                persist_turn_state=(None if terminal_runtime is not None else self._persist_turn_state),
                # prod-bug-bash-039 Phase 5 (0.39.3) — citation registry persist
                # target. `_flush_citations` calls record_turn_citations() at lead
                # finalize so archived turns can re-hydrate `[cite:N]` chips.
                session_store=getattr(orch, "session_store", None),
                # subagent-unification Phase 2 — handles for dynamic Lead peer
                # routing (flag SUBAGENT_SPEC_ROUTING, default OFF). When the flag is
                # OFF these are unused; threading them now keeps the wire ready for
                # the Phase-3 flag flip. multi_agent_registry is set on `orch` by the
                # gateway lifespan (main.py:743); both default None on harnesses.
                multi_agent_registry=getattr(orch, "multi_agent_registry", None),
                custom_subagent_store=getattr(orch, "custom_subagent_store", None),
                team_subagent_store=_team_subagent_store(orch),  # cca P3: advertise team peers
                plan_store=getattr(orch, "plan_store", None),  # planning-mode (M1 block 4)
            )
            _lead_result = await _lead_run_turn(_sctx, request.text, runtime=_runtime)
            self._check_cancel()
            raise_if_terminal_bus_failed(bus)
        if _lead_result is None:
            # Team-tool session __aexit__ swallowed Lead exception (actual 116: shutdown cancellation swallowed,
            # _lead_result read below raised UnboundLocalError, reported whole turn lead_internal_error).
            # Rethrow shutdown cancellation unchanged for dispatch-lease recovery to mark interrupted; other cases report no result.
            _task = asyncio.current_task()
            if _task is not None and _task.cancelling():
                raise asyncio.CancelledError()
            raise RuntimeError("lead run exited without a result (exception swallowed by team tool session)")
        # P0-CTO-5: lead_orchestrator is sole `done` emitter (covers cancel, cap,
        # refusal/exception, not_ok, and COMPLETED). turn_runner used to wrap a
        # second done here — removed to prevent duplicate terminal events on SSE.
        #
        # P0-CTO-4: persist turn state to session_store. Sprint θ A2 deleted the
        # legacy _handle_* shortcuts that did this; without re-adding here,
        # /sessions API never sees turns and chat hydrate breaks. Pattern mirrors
        # the current terminal settlement path (complete_turn, trace and summary).
        if orch.session_store is not None and request.session_id:
            try:
                _intent_val = _lead_result.intent.value if getattr(_lead_result, "intent", None) is not None else None
                _t_in = int(getattr(_lead_result, "tokens_input", 0) or 0)
                _t_out = int(getattr(_lead_result, "tokens_output", 0) or 0)
                _cost = _estimate_cost_usd(_t_in, _t_out)
                # R6 fix: only call complete_turn for COMPLETED status. Other
                # terminal statuses (clarification_pending / aborted / failed)
                # leave the turn row in `running` state — frontend `/sessions`
                # would mis-label clari turns as "answered" otherwise. Status
                # enum: TurnStatus.COMPLETED / CLARIFICATION_PENDING / ABORTED /
                # FAILED. Cancel + error paths emit done from lead_orchestrator
                # and persist via TurnRunner._finish_cancelled / _persist_turn_state.
                _status = getattr(_lead_result, "status", None)
                _completed = _status is not None and getattr(_status, "value", None) == "completed"
                # Sprint π Phase A: when BOWHEAD_PI_A_PERSIST_ORDER=1 (default),
                # the lead_orchestrator._emit_event("done") chokepoint already
                # persisted via runtime.persist_turn_state — this legacy block
                # is a defensive no-op. Set =0 to roll back to pre-π behavior
                # if chokepoint somehow misfires. See tech-design r2 §A.
                _pi_a_chokepoint_on = os.environ.get("BOWHEAD_PI_A_PERSIST_ORDER", "1") == "1"
                if terminal_runtime is None and _completed and not _pi_a_chokepoint_on:
                    await orch.session_store.complete_turn(
                        turn_id=request.turn_id,
                        intent=_intent_val,
                        final_answer=_lead_result.final_answer or "",
                        tokens_input=_t_in,
                        tokens_output=_t_out,
                        cost_usd=_cost,
                    )
            except Exception as _persist_e:
                logger.warning(
                    "turn_runner.lead_persist_failed turn=%s err=%s",
                    request.turn_id,
                    _persist_e,
                )
            _summary_invocations = list(getattr(_lead_result, "tool_invocations", []) or [])
            _summary_tools, _summary_llm = turn_summary_counts(bus, _summary_invocations)
            # Same value as done.duration_ms at terminal boundary (see _run_inner start comment).
            _summary_wall = getattr(bus, "_turn_wall_duration_ms", None)
            await _persist_turn_state(
                # See _persist_turn_state: ContextVar already reset, explicit argument required.
                ui_language=getattr(getattr(request, "language_context", None), "ui_language", None),
                orch=orch,
                turn_id=request.turn_id,
                intent=getattr(_lead_result, "intent", None),
                invocations=_summary_invocations,
                tokens_in=int(getattr(_lead_result, "tokens_input", 0) or 0),
                tokens_out=int(getattr(_lead_result, "tokens_output", 0) or 0),
                duration_ms=(int(_summary_wall) if isinstance(_summary_wall, int) else int(getattr(_lead_result, "duration_ms", 0) or 0)),
                tools_count=_summary_tools,
                tool_loads=int(getattr(bus, "_team_operation_tool_loads", 0) or 0),
                llm_count=_summary_llm,
            )
            await self._spawn_session_title(request, _lead_result)
        return _lead_result

    async def _spawn_session_title(self, request: TurnRequest, lead_result: Any) -> None:
        """Schedule a title after the first successful turn using its effective team router.

        Without a team router, retain the placeholder title. Runtime composition can
        supply a refusal-only process router, which cannot generate a useful title.
        """

        orch = self._orch
        try:
            # ① Source gate (independent review P1): _run_inner serves **all** turn producers.
            #    Scheduled tick creates s_sched_* session, dry-run creates scratch; both first turns,
            #    so absent guard every run wastes session-title LLM call.
            #    Default False; producers must declare (TurnRequest.interactive_session).
            if not getattr(request, "interactive_session", False):
                return
            status = getattr(lead_result, "status", None)
            if getattr(status, "value", None) != "completed":
                return
            store = getattr(orch, "session_store", None)
            if store is None or not request.session_id:
                return
            # ② Title first turn only (no multi-turn rename: LLM cost and confusing jumps).
            #    get_turn(turn_id).seq single-PK lookup, not list_turns with
            #    SELECT * + full hydration (every final_answer), O(n) long-session query
            #    just for one number (independent review P2). More precise too: **this turn** sequence.
            turn = await store.get_turn(request.turn_id)
            if turn is None or int(getattr(turn, "seq", 0) or 0) != 1:
                return
            router = _effective_router(orch, request)
            if router is None:
                return
            from bowhead_router.usage import usage_context

            from bowhead_core.session_title import generate_and_store_title
            from bowhead_core.terminal_llm_invocation import (
                create_detached_auxiliary_task,
            )

            async def _run_titled() -> None:
                # ③ Attribution (independent review P2): create_task copies caller ContextVar;
                #    without explicit reason, title tokens recorded reason="turn", mixing into
                #    user-driven accounting; usage.py / user_store.py comments separately classify
                #    session_title precisely to distinguish spontaneous background usage.
                with usage_context(team_id=request.team_id, user_id=request.user_id, reason="session_title"):
                    await generate_and_store_title(
                        router=router,
                        session_store=store,
                        session_id=request.session_id,
                        user_text=request.text,
                        final_answer=getattr(lead_result, "final_answer", "") or "",
                        agent_type=orch.config.llm_agent_type,
                        trace_id=f"{request.turn_id}:title_on_success",
                        language_context=request.language_context,
                    )

            task = create_detached_auxiliary_task(
                _run_titled(),
                reason="session_title",
            )
            # Unretained bare create_task return may be GC-collected before finishing.
            # Two existing sites did this; retain on orch until completion, then remove.
            tasks = getattr(orch, "_title_tasks", None)
            if tasks is None:
                tasks = set()
                orch._title_tasks = tasks
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        except Exception as exc:
            # Title failure must never affect turn, mere display enhancement.
            logger.debug("title_gen.spawn_failed turn=%s err=%s", request.turn_id, exc)

    async def _build_team_tool_view(
        self,
        stack: contextlib.AsyncExitStack,
        request: TurnRequest,
        orch: Any,
        ctx: ToolExecutionContext,
    ) -> Any:
        """Resolve the same effective ToolRegistry used by turns and Home."""
        from bowhead_core.capability_resolver import (
            CapabilityPrincipal,
            CapabilityResolver,
        )

        resolver = CapabilityResolver.from_runtime(orch)
        resolved = await resolver.resolve(
            stack,
            CapabilityPrincipal(
                user_id=request.user_id or "",
                team_id=getattr(request, "team_id", None) or "",
                workspace_id=request.workspace_id or "",
                role=getattr(ctx, "role", None),
            ),
            ctx,
        )
        return resolved.tool_view

    def _check_cancel(self) -> None:
        """M2.1: tool loop boundary 检查 cancel signal. 用户点 Stop → 抛 TurnCancelledError.

        外层 (run) catch 此异常 → 走 _finish_cancelled 路径.
        设计: docs/M157-cancel-turn-design.md §3
        """
        ev = getattr(self, "_cancel_event", None)
        if ev is not None and ev.is_set():
            from bowhead_core.orchestrator import TurnCancelledError

            raise TurnCancelledError("turn cancelled by user")

    async def _finish_cancelled(self, request: TurnRequest) -> TurnResult:
        """M2.1: 用户 cancel 后清场 + emit turn_cancelled + done(status=cancelled).

        注意: 已生成的 partial answer / canvas sections 已经通过 SSE 发给前端 (累积态),
        我们这里只发"终态"事件让前端切回输入态. session_store 持久化 status=cancelled.
        """
        orch = self._orch
        terminal_runtime = getattr(request, "terminal_runtime", None)
        bus = terminal_runtime.bus if terminal_runtime is not None else await _require_admitted_legacy_bus(orch, request.turn_id)
        t_start = getattr(self, "_t_start", time.perf_counter())
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        # Sprint π Phase A P0-1 fix (CTO review): persist BEFORE emit done so
        # turn row reaches terminal status before SSE done propagates. This
        # bypass-the-chokepoint site emits done from TurnRunner, not from
        # LeadRuntime (Lead may not have even started — user clicked Stop
        # before classification). Single source of truth = session_store.mark_*.
        # Persist turn state for /sessions API; optional session_store requires absence check.
        # #297: actual partial state, not empty []/None/0; IntentStrip + cost recover.
        partial_state = getattr(bus, "_pi_turn_state", {}) or {}
        if terminal_runtime is not None:
            # Web Stop commits and freezes Terminal v1 usage before signalling
            # this worker. Do not let work completed in the signal race inflate
            # the later compatibility summary beyond the committed receipt.
            frozen_terminal_state = getattr(bus, "_terminal_snapshot", None)
            if isinstance(frozen_terminal_state, dict) and frozen_terminal_state:
                partial_state = frozen_terminal_state
        partial_invocations = list(partial_state.get("invocations") or getattr(self, "_invocations", []) or [])
        partial_intent = partial_state.get("intent")
        if partial_intent is None:
            partial_intent_decision = getattr(self, "_intent_decision", None)
            partial_intent = partial_intent_decision.intent if partial_intent_decision is not None else None
        partial_intent_str = partial_intent.value if hasattr(partial_intent, "value") else str(partial_intent) if partial_intent is not None else None
        partial_tokens_in = int(partial_state.get("tokens_input") or getattr(self, "_tokens_in", 0) or 0)
        partial_tokens_out = int(partial_state.get("tokens_output") or getattr(self, "_tokens_out", 0) or 0)
        partial_cost = float(partial_state.get("cost_usd") or 0.0)
        if not partial_cost and (partial_tokens_in or partial_tokens_out):
            partial_cost = _estimate_cost_usd(partial_tokens_in, partial_tokens_out)
        terminal_confirmed = terminal_runtime is not None or orch.session_store is None
        if orch.session_store is not None and request.session_id:
            try:
                if terminal_runtime is None:
                    terminal_confirmed = bool(
                        await orch.session_store.mark_cancelled(
                            worker=getattr(self, "_execution_worker", None),
                            turn_id=request.turn_id,
                            intent=partial_intent_str,
                            tokens_input=partial_tokens_in,
                            tokens_output=partial_tokens_out,
                            cost_usd=partial_cost,
                            error_kind="user_cancel",
                        )
                    )
                    if not terminal_confirmed:
                        current_turn = await orch.session_store.get_turn(
                            request.turn_id,
                        )
                        terminal_confirmed = bool(current_turn is not None and current_turn.status == TurnStatus.CANCELLED.value)
                        finalize_usage = getattr(
                            orch.session_store,
                            "finalize_cancelled_usage",
                            None,
                        )
                        if terminal_confirmed and callable(finalize_usage):
                            terminal_confirmed = False
                            for attempt in range(2):
                                try:
                                    terminal_confirmed = bool(
                                        await finalize_usage(
                                            turn_id=request.turn_id,
                                            expected_execution_generation=max(
                                                1,
                                                int(
                                                    getattr(
                                                        request,
                                                        "execution_generation",
                                                        1,
                                                    )
                                                    or 1
                                                ),
                                            ),
                                            intent=partial_intent_str,
                                            tokens_input=partial_tokens_in,
                                            tokens_output=partial_tokens_out,
                                            cost_usd=partial_cost,
                                        )
                                    )
                                    break
                                except Exception:
                                    if attempt:
                                        raise
                                    await asyncio.sleep(0)
                            if not terminal_confirmed:
                                raise RuntimeError(
                                    "cancelled turn usage settlement was rejected",
                                )
            except Exception as e:
                terminal_confirmed = False
                logger.warning(
                    "session.mark_cancelled_persist_failed turn=%s err=%s",
                    request.turn_id,
                    e,
                )
                latch_failure = getattr(bus, "latch_fatal_error", None)
                if callable(latch_failure):
                    latch_failure(e)
                raise_if_terminal_bus_failed(bus)
                raise RuntimeError(
                    "cancelled turn usage was not durably settled",
                ) from e
            # chat-hydrate-overhaul: cancel also persists partial trace + summary,
            # letting hydration show progress reached.
            if terminal_confirmed:
                _cancel_tools, _cancel_llm = turn_summary_counts(bus, partial_invocations)
                await _persist_turn_state(
                    # See _persist_turn_state: ContextVar reset, explicit argument required.
                    ui_language=getattr(getattr(request, "language_context", None), "ui_language", None),
                    orch=orch,
                    turn_id=request.turn_id,
                    intent=partial_intent,
                    invocations=partial_invocations,
                    tokens_in=partial_tokens_in,
                    tokens_out=partial_tokens_out,
                    duration_ms=duration_ms,
                    tools_count=_cancel_tools,
                    tool_loads=int(getattr(bus, "_team_operation_tool_loads", 0) or 0),
                    llm_count=_cancel_llm,
                )
        if terminal_confirmed:
            await bus.emit(
                orch._event(
                    request.turn_id,
                    "turn_cancelled",
                    {"turn_id": request.turn_id, "duration_ms": duration_ms},
                )
            )
        # Sprint chat-v3 Phase A — drain in-flight tool_finding tasks before
        # cancel path emits done (closes phase-a-review P0-A3 partial: this
        # path bypasses lead_orchestrator._emit_event chokepoint).
        try:
            from bowhead_core.tool_finding import drain_pending_findings

            await drain_pending_findings(
                turn_id=request.turn_id,
                timeout_s=2.0,
            )
        except Exception:  # noqa: S110
            pass
        # Sprint λ F1 P1-7 — cleanup BI query cache for this turn (releases ~250KB).
        try:
            from bowhead_core.tool_registry import cleanup_turn_query_cache

            cleanup_turn_query_cache(request.turn_id)
        except Exception:  # noqa: S110
            pass
        # This early-cancel path bypasses LeadRuntime's done chokepoint, so it
        # must close the replayable Step List Card itself before done.
        if terminal_confirmed:
            try:
                if not getattr(bus, "_card_finalized_emitted", False):
                    bus._card_finalized_emitted = True  # type: ignore[attr-defined]
                    await bus.emit(
                        orch._event(
                            request.turn_id,
                            "step_list_card_finalized",
                            {
                                "card_id": request.turn_id,
                                "state": "cancelled",
                                "step_count": int(getattr(bus, "_step_count", 0) or 0),
                            },
                        )
                    )
            except Exception as e:
                logger.warning(
                    "turn.cancelled_card_finalize_failed turn=%s err=%s",
                    request.turn_id,
                    e,
                )
            # Persisted report sections visible after refresh at Stop: same counting rule/field as Lead done boundary,
            # persisted/replayed only with this done.
            from bowhead_core.canvas_store import report_sections_saved_field
            from bowhead_core.turn_step_outcome import step_outcome_field

            saved_sections = await report_sections_saved_field(
                getattr(bus, "_d6_canvas_store", None) or getattr(orch, "canvas_store", None),
                request.turn_id,
            )
            # P0-1: emit done AFTER persist and card finalization.
            await bus.emit(
                orch._event(
                    request.turn_id,
                    "done",
                    {
                        "turn_id": request.turn_id,
                        "status": "cancelled",
                        "duration_ms": duration_ms,
                        "intent": partial_intent_str,
                        "tokens_input": partial_tokens_in,
                        "tokens_output": partial_tokens_out,
                        "error_kind": "user_cancel",
                        **saved_sections,
                        **step_outcome_field(bus, "cancelled"),
                    },
                )
            )
        logger.info("turn.cancelled turn=%s dur_ms=%d", request.turn_id, duration_ms)
        return TurnResult(
            turn_id=request.turn_id,
            status=TurnStatus.CANCELLED,
            intent=IntentType.LOOKUP,  # Placeholder: intent may still unclassified.
            final_answer="(cancelled by user)",
            tool_invocations=partial_invocations,
            tokens_input=partial_tokens_in,
            tokens_output=partial_tokens_out,
            cost_usd=partial_cost,
            duration_ms=duration_ms,
        )


def abbrev_thinking(text: str, *, head: int = 160, tail: int = 80) -> str:
    """Thinking 模型输出的推理轨迹经常 800-2000 字. 给日志/SSE emit 用的缩略版：
    前 head 字 + '...[N 字省略]...' + 后 tail 字.
    """
    if not text:
        return ""
    t = text.strip()
    total = len(t)
    if total <= head + tail + 20:
        return t
    omitted = total - head - tail
    return f"{t[:head]} ...[省略 {omitted} 字]... {t[-tail:]}"


def log_thinking(log: logging.Logger, turn_id: str, thinking: str) -> None:
    """缩略打印 thinking —— info 级别，全文可在 SQLite turn_events 看 thinking event."""
    preview = abbrev_thinking(thinking, head=120, tail=60)
    log.info("turn=%s llm_thinking (%d chars): %s", turn_id, len(thinking), preview)


def subagent_backend_from_env() -> SubagentBackend:
    """恒返回 ``legacy`` —— 唯一的 subagent backend。

    ``BOWHEAD_SUBAGENT_BACKEND`` 曾能切到 LangGraph backend，该 backend 已于 2026-08
    下线（Agent SDK 调研 A2）。**故意保留这个函数并继续读一次 env**：真有部署把变量
    留在 .env 里时，要在启动日志里说清楚"这个开关已经没用了"，而不是让人以为切生效了。
    """
    v = (os.environ.get("BOWHEAD_SUBAGENT_BACKEND") or "").strip().lower()
    if v and v != "legacy":
        logging.getLogger(__name__).warning(
            "BOWHEAD_SUBAGENT_BACKEND=%s 已失效 —— LangGraph backend 于 2026-08 下线，本进程使用 legacy。请从部署配置里删掉这个变量。",
            v,
        )
    return "legacy"


def flatten_for_summary(invs: list[ToolInvocation]) -> list[ToolInvocation]:
    """Lead 层 invocations 扁平化（展开 subagent.nested）—— 用于触顶兜底摘要."""
    out: list[ToolInvocation] = []
    for inv in invs:
        out.append(inv)
        nested = getattr(inv, "nested", None)
        if nested:
            out.extend(flatten_for_summary(list(nested)))
    return out


def build_cap_exceeded_summary(
    invs: list[ToolInvocation],
    cap: int,
    *,
    reason: str = "iteration",
) -> str:
    """触顶 / 超时 / 超 token 预算时构造**只引 tool_result 不编数字**的兜底回复.

    reason ∈ {"iteration", "timeout", "token_budget"} —— 生成不同 headline，
    但 body 里 echo 的数据来源保持一致.
    """
    headline = {
        "iteration": f"⚠ **未能在 {cap} 轮工具调用内完成回答**",
        "timeout": f"⚠ **单轮超过 {cap} 秒硬超时**，已强制结束",
        "token_budget": f"⚠ **已消耗超 {cap} 个 token 预算**，已强制结束",
    }.get(reason, "⚠ **触发护栏兜底**")
    lines = [
        f"{headline}，以下是已经真实拿到的数据，未出现的字段我们没查到，**不要基于本摘要做二次推断**。",
        "",
    ]
    flat = flatten_for_summary(invs)
    facts: list[str] = []
    for inv in flat:
        if inv.error:
            continue
        r = inv.result
        if not isinstance(r, dict):
            continue
        if inv.tool_name == "bi_ask_question":
            q = (inv.arguments or {}).get("text") or ""
            q_type = r.get("type")
            metrics = r.get("metrics") or []
            if metrics:
                m_strs = [f"{m.get('name', '')}={m.get('content') or m.get('value') or ''}" for m in metrics if isinstance(m, dict)]
                facts.append(f"- `{q[:60]}` → {', '.join(m_strs[:5])}")
            elif q_type == "image":
                facts.append(f"- `{q[:60]}` → 返回图表（无结构化数值）")
        elif inv.tool_name == "bi_attribute":
            q = (inv.arguments or {}).get("question") or ""
            if r.get("measure_name"):
                facts.append(f"- 归因 `{q[:60]}` → {r.get('measure_name', '')} 同比 {r.get('measure_delta', '')}；rows={len(r.get('rows') or [])}")
        elif inv.tool_name == "skill_invoke":
            ok = bool(r.get("ok")) and not r.get("error")
            nm = (inv.arguments or {}).get("name") or ""
            facts.append(f"- sandbox `{nm}` → {'完成' if ok else '失败 / 空跑'}")
        elif inv.tool_name == "subagent_spawn":
            # Separate subagent itself (nested bi.* already recursively flattened above).
            profile = r.get("profile") or (inv.arguments or {}).get("profile") or ""
            sub_ok = bool(r.get("ok")) and not r.get("error")
            tc = int(r.get("tool_count") or 0)
            facts.append(f"- subagent `{profile}` → {'完成' if sub_ok else '失败/空答'} ({tc} 次内部 tool)")
    if facts:
        lines.append("**已拿到的数据**：")
        lines.extend(facts[:15])
    else:
        lines.append("**已拿到的数据**：（没有任何一次 tool 成功返回结构化数值）")
    lines += [
        "",
        "**建议下一步**：",
        "1. 把问题简化成单一聚合 / 单一维度（去掉 '各 / 每个 / 对比'）重问；",
        "2. 或打开 datarc 面板手动取数后把 CSV 贴回来让 sandbox 计算。",
    ]
    return "\n".join(lines)


def compress_old_messages(messages: list[Message], keep_tail: int) -> list[Message]:
    """长任务护栏：当 messages 数超过 keep_tail+1 时，把中间消息合成一条摘要塞回.

    **两种配对约束**（OpenAI / Qwen schema 要求）：
        1. 每条 `role=tool` 前面必须有 assistant 的 `tool_calls` 引它
        2. 每条 assistant 的 `tool_calls` 后面 tool_call_id 必须都有对应 tool_result
    所以 compact 切片时：
        - tail 起点必须落在非 tool role 上（否则悬空 tool_result → router 报错）
        - middle 尾部不能是"未闭合的 tool_calls"（否则 digest 后 tail 第一条 tool 找不到 caller）

    生成的 digest 是 role=user 的 `<context-digest>` 块.
    """
    if len(messages) <= keep_tail + 1:
        return messages
    start = max(1, len(messages) - keep_tail)
    while start < len(messages) and messages[start].role == "tool":
        start += 1
    while start > 1 and start < len(messages):
        prev = messages[start - 1]
        if prev.role == "assistant" and getattr(prev, "tool_calls", None):
            start -= 1
        else:
            break
    if start >= len(messages) - 1:
        return messages
    head = [messages[0]]
    middle = messages[1:start]
    tail = messages[start:]
    if not middle:
        return messages
    tool_call_ct = sum(1 for m in middle if m.role == "assistant" and m.tool_calls)
    tool_result_ct = sum(1 for m in middle if m.role == "tool")
    highlights: list[str] = []
    for m in middle[-20:]:
        if m.role == "tool":
            preview = (m.content or "").replace("\n", " ")[:140]
            highlights.append(f"· {m.name or 'tool'} → {preview}")
        elif m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                highlights.append(f"· called {tc.name}")
    summary_text = (
        f"[上下文压缩] 已省略 {len(middle)} 条旧消息"
        f"（{tool_call_ct} 次工具调用 / {tool_result_ct} 次返回）。"
        f"最近 20 条精华：\n" + "\n".join(highlights)
    )
    digest_msg = Message(
        role="user",
        content=f"<context-digest>\n{summary_text}\n</context-digest>",
    )
    return [*head, digest_msg, *tail]


# ── Attachment hard gate / explicit BI-keyword detection ───────
_DATA_ATTACHMENT_MIMES = (
    "text/csv",
    "text/tab-separated-values",
    "application/json",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
_DATA_ATTACHMENT_EXTS = (".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".parquet")
# V2.9 A3: template PDF/PPTX differs from data attachments: structure only, no cell-data reads.
_TEMPLATE_ATTACHMENT_MIMES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)
_TEMPLATE_ATTACHMENT_EXTS = (".pdf", ".pptx")
_BI_EXPLICIT_KEYWORDS = (
    "从 datarc",
    "去 datarc",
    "去datarc",
    "从datarc",
    "去 BI",
    "去 bi",
    "去BI",
    "去bi",
    "用数据库",
    "查数据库",
    "查 workspace",
    "从数据库",
    "workspace 数据",
    "from datarc",
    "from the database",
)


def data_attachment_ids(prepared: list) -> list[str]:
    """从 prepared attachments 里挑数据型（csv/xlsx/json）附件的 artifact_id."""
    out: list[str] = []
    for p in prepared or []:
        mime = getattr(p, "mime", "") or ""
        filename = getattr(p, "filename", "") or ""
        aid = getattr(p, "artifact_id", "") or ""
        is_data = mime in _DATA_ATTACHMENT_MIMES or any(filename.lower().endswith(e) for e in _DATA_ATTACHMENT_EXTS)
        if is_data and aid:
            out.append(aid)
    return out


def template_attachment_paths(prepared: list) -> list[tuple[str, str]]:
    """V2.9 A3: 从 prepared attachments 里挑模板型 (pdf/pptx) → [(artifact_id, local_path)]."""
    out: list[tuple[str, str]] = []
    for p in prepared or []:
        mime = getattr(p, "mime", "") or ""
        filename = getattr(p, "filename", "") or ""
        aid = getattr(p, "artifact_id", "") or ""
        local_path = getattr(p, "local_path", "") or ""
        is_template = mime in _TEMPLATE_ATTACHMENT_MIMES or any(filename.lower().endswith(e) for e in _TEMPLATE_ATTACHMENT_EXTS)
        if is_template and aid and local_path:
            out.append((aid, local_path))
    return out


# V2.9 A3: user-text use-template-for-new-period keywords (heuristic, future LLM possible).
_TEMPLATE_CLONE_KEYWORDS = (
    "用这个模板",
    "用这模板",
    "用这份模板",
    "按这个",
    "按这份",
    "按照这个",
    "按照这份",
    "做这个月",
    "做这月",
    "做下个月",
    "做下月",
    "做这一期",
    "做这期",
    "做新一期",
    "做下期",
    "这个月的版本",
    "这月的版本",
    "做 11 月",
    "做 12 月",
    "做 11月",
    "做 12月",  # Common simplified Chinese.
    "套用",
    "复制这个",
    "复刻",
    "克隆",
    "use this template",
    "this month's version",
    "make this month",
    "based on this template",
    "based on this format",
)


def user_wants_template_clone(text: str) -> bool:
    """启发式: 用户文本含"用这模板做新一期"类意图. 跟 attachment 检测组合判 template_clone 路径."""
    if not text:
        return False
    s = text.strip()
    return any(kw in s for kw in _TEMPLATE_CLONE_KEYWORDS)


def user_explicitly_wants_bi(text: str) -> bool:
    """用户在 query 里明确要用 datarc / BI → 不触发附件硬拦截."""
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in _BI_EXPLICIT_KEYWORDS) or any(kw in text for kw in _BI_EXPLICIT_KEYWORDS)


# ── SSE preview / result meta ─────────────────────────────────
def safe_preview(obj: Any, max_len: int = 500) -> str:
    """安全把任意对象转成有限长度字符串预览（SSE 不要 dump 大对象）."""
    import json

    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def result_meta(obj: Any) -> dict[str, Any] | None:
    """提取 tool result 的结构化元数据（给前端显示用，不会被截断破坏）.

    场景：bi_list_measures 返 list[dict]，`result_preview` 500 字截断后 JSON.parse 失败，
    前端拿不到 length 展示 "拿到 0 个指标". 这里直接把 len() 计算好传出去.
    """
    if isinstance(obj, list):
        return {"type": "array", "count": len(obj)}
    if isinstance(obj, dict):
        metrics = obj.get("metrics")
        if isinstance(metrics, list):
            return {"type": "ask_question", "metric_count": len(metrics), "q_type": obj.get("type")}
        rows = obj.get("rows")
        if isinstance(rows, list):
            return {"type": "attribute", "row_count": len(rows), "measure_name": obj.get("measure_name")}
    return None
