"""V2.8 A2 · 前端 telemetry 入口.

仅接 allowlist 里的指标 (防滥用 / Prometheus cardinality 失控). 后端 inc 后
原 Prometheus /metrics endpoint 暴露给监控.

POST /api/v1/metrics/event
  { "name": "bowhead_trace_strip_expanded_total",
    "labels": {"intent": "trend"} }

返 {"ok": true} 即写成功; 不在 allowlist 静默忽略 (返 ok=false 但 200, 不阻塞前端).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway.auth_mutation_security import require_same_origin_json
from bowhead_gateway.routes.auth import _current_user, _store

logger = logging.getLogger(__name__)
router = APIRouter()
_CURRENT_USER_DEP = Depends(_current_user)
_USER_STORE_DEP = Depends(_store)


# ────────────────────────────────────────────────────────────
# Allowlist: register new metrics here first to prevent exploding Prometheus dimensions.
# ────────────────────────────────────────────────────────────

# {metric_name: {label_key: allowed_value_set | None (any nonempty string is allowed, with a length cap)}}
_ALLOWED: dict[str, dict[str, set[str] | None]] = {
    "bowhead_trace_strip_expanded_total": {
        # Track which intents make users more interested in traces.
        "intent": None,
    },
    "bowhead_sql_chip_clicked_total": {
        # tool_name uses a frontend-supplied sha1 hash to prevent Chinese tokens exploding cardinality (a V2 pitfall).
        "tool_hash": None,
    },
    "bowhead_plan_step_mismatch_total": {
        # Plan heuristic mismatch signals that heuristics need calibration.
        "intent": None,
        "tool_name": None,  # Both have low cardinality, no hashing needed.
    },
    # W11-12: new-component instrumentation (redesign sprint).
    "bowhead_mobile_drawer_total": {
        "action": {"open", "close_overlay", "close_esc", "close_nav"},
    },
    "bowhead_sidebar_state_changed_total": {
        "from": {"expanded", "compact", "overlay", "drawer"},
        "to": {"expanded", "compact", "overlay", "drawer"},
        "viewport": {"wide_desktop", "narrow_desktop", "mobile"},
        "trigger": {"control", "shortcut", "breakpoint"},
    },
    "bowhead_nav_destination_opened_total": {
        "primary": {"chat", "reports", "library", "capabilities"},
        "leaf": {
            "chat",
            "reports",
            "files",
            "knowledge",
            "skills",
            "tools",
            "workflows",
            "subagents",
            "scheduled",
        },
        "surface": {"expanded", "compact", "overlay", "drawer"},
        "role": {"owner", "team_admin", "audit_admin", "member"},
        # Previously the entire presentation (sidebar form) label emitted by unified-sidebar.tsx was ignored.
        "presentation": {"expanded", "compact", "overlay", "drawer"},
    },
    "bowhead_sidebar_recent_opened_total": {
        "source": {"pinned", "recent", "search"},
        "surface": {"expanded", "overlay", "flyout", "drawer"},
    },
    "bowhead_sidebar_search_total": {
        "action": {"results", "no_results", "partial", "error", "open"},
        "kind": {"all", "session", "report", "file", "knowledge"},
        "surface": {"expanded", "overlay", "flyout", "drawer"},
    },
    "bowhead_sidebar_session_action_total": {
        # delete was added in batch 2 session soft-delete + 8-second undo (adbd63916), but omitted here;
        # values outside the set become other, making deletion **invisible** in monitoring,
        # while the shared test_metrics_allowlist_parity gate has failed ever since.
        "action": {"pin", "unpin", "archive", "restore", "undo", "delete", "rename"},
        "outcome": {"success", "error", "blocked"},
        # topbar: the open session's title-bar "⋯" menu (rename).
        "surface": {"expanded", "overlay", "flyout", "drawer", "topbar"},
    },
    "bowhead_settings_opened_total": {
        "surface": {"user_menu", "deep_link"},
        "scope": {"personal", "team", "space"},
    },
    "bowhead_admin_destination_opened_total": {
        "destination": {"approvals", "traces", "credits", "audit", "access"},
        # Direct leaf-route menu links added a source; unregistered labels are ignored entirely,
        # preventing separate measurement of the two entries (caught by the parity gate).
        "surface": {"user_menu", "admin_entry"},
    },
    "bowhead_dark_toggle_total": {
        "to": {"light", "dark"},
    },
    "bowhead_cmd_k_total": {},
    "bowhead_section_chrome_menu_total": {
        # edit_with_ai: section-chrome.tsx has always emitted it; previously counted as other (caught by parity tests).
        "action": {"open", "delete", "copy", "duplicate", "edit_with_ai"},
        "kind": None,  # Section kind, low cardinality (~20 types).
    },
    "bowhead_user_menu_total": {
        "action": {"open", "sign_out", "open_settings"},
    },
    "bowhead_citation_popover_total": {
        "kind": {"num", "ref"},
        "has_src": {"true", "false"},
    },
    "bowhead_canvas_citation_total": {
        "action": {"open_panel", "open_kb_popover", "open_run", "legacy_no_source"},
        "kind": {"numbered", "legacy_num"},
        "quality": {"artifact_only", "unavailable", "never_linked"},
    },
    "bowhead_canvas_ai_rewrite_total": {
        "action": {"start", "done", "cancel", "fail", "abort", "apply", "reject"},
        "preset": {"concise", "professional", "grammar", "different_angle", "custom", "unknown"},
        "mode": {"backend", "mock"},
    },
    "bowhead_canvas_data_focus_total": {
        "action": {"request", "reject"},
        "entry_point": {"chat", "report", "code", "source_panel", "unknown"},
        "result_type": {"report", "artifact", "code"},
        "reason": {"report_only", "unknown"},
        # Drop high-cardinality artifact ids from Prometheus labels.
    },
    "bowhead_results_workspace_total": {
        "action": {
            "pane_opened",
            "navigator_opened",
            "result_selected",
            "viewer_mode_changed",
            "open_failed",
            "downloaded",
            "referenced",
            "cross_link_opened",
            "navigator_pinned",
            "navigator_unpinned",
            "unsupported_feedback",
        },
        "result_type": {"report", "artifact", "code", "none"},
        "group": {"deliverables", "evidence", "none"},
        "entry_point": {
            "header",
            "navigator",
            "chat",
            "report",
            "code",
            "source_panel",
            "deep_link",
            "unknown",
        },
        "mode": {"preview", "raw", "details", "none"},
        "error_class": {
            "unavailable",
            "unsupported",
            "too_large",
            "corrupt",
            "network",
            "unknown",
            "none",
        },
    },
    "bowhead_canvas_report_perf_total": {
        "action": {"set_content"},
        "sections": {"0", "1-5", "6-15", "16-30", "31-60", "60+"},
        "doc_size": {"<50kb", "50-150kb", "150-500kb", "500kb+"},
        "duration": {"<16ms", "16-50ms", "50-150ms", "150ms+"},
    },
    "bowhead_chart_fallback_table_total": {
        "reason": {"unsupported_kind", "no_canvas", "config_error", "render_error"},
        "kind": None,
    },
    # Batch 0 0-F (chart-table-ux): persistent chart interaction metrics inform batch 2 inspect-layer density decisions.
    "bowhead_chart_action_total": {
        # expand_table is batch 3's table-expansion action: the key signal for batch 2 inspect-layer density decisions
        # (how often users hit truncated rows). Missing registration makes _record_metric_event rewrite it as
        # other, mixing it with future unregistered actions (the same pitfall just occurred in batch 0).
        "action": {"kind_switch", "download_png", "download_csv", "open_data", "expand_table"},
        "kind": None,  # Chart kind, low cardinality (~15 types).
    },
    # Batch 2 §3.1 (chart-table-ux): inspect-layer interactions show which controls are used and should remain.
    "bowhead_chart_inspect_total": {
        "action": {
            "open",
            "tab_chart",
            "tab_data",
            "sort",
            "sort_clear",
            "top_n",
            "data_labels",
            "ref_line",
            "toggle_series",
            "download_png",
            "download_csv",
            "copy_image",
            "followup",
            "view_kind",
        },
    },
    "bowhead_chart_kind_switch_total": {
        "from": None,  # Chart kind, low cardinality.
        "to": None,
    },
    # Batch 0 0-A (chart-table-ux): silent V2 export fallback to legacy is now observable.
    # R8 (2026-08-14, actual 116 device): only capability failures were registered, so prepare's own failure
    # branch (the actual production case: surface_projection_conflict downgraded all V2 exports across the fleet)
    # always counted as other. Keep allowed values synchronized with FE PrepareExportFailureReason.
    "bowhead_export_legacy_fallback_total": {
        "reason": {
            "v2_capability_unavailable",
            "forced",
            "save_failed",
            "reorder_failed",
            "unsaved_presentation_override",
            "surface_projection_conflict",
            "surface_render_incomplete",
        },
        "format": {"pdf", "pptx", "xlsx", "docx"},
    },
    "bowhead_report_library_total": {
        "action": {"load_success", "load_error", "empty", "copy_link", "delete", "delete_fail"},
    },
    "bowhead_canvas_export_total": {
        "action": {"start", "done", "fail", "timeout"},
        # docx is a valid ExportFmt value, previously unregistered, so all docx exports counted as other.
        "fmt": {"pdf", "pptx", "xlsx", "docx"},
    },
    "bowhead_composer_layout_mode_total": {
        "surface": {"home", "workbench"},
        "mode": {"comfortable", "compact", "ultra"},
    },
    "bowhead_design_flatten_ui_total": {
        "action": {"start", "done", "fail"},
    },
    "bowhead_report_link_total": {
        "action": {"route_404"},
        "surface": {"reports"},
    },
    "bowhead_formatting_toolbar_total": {
        "action": {"bold", "italic", "link_add", "link_remove"},
    },
    "bowhead_home_proposal_ui_total": {
        "action": {"impression", "start"},
        "strategy": {"llm", "deterministic", "local"},
        "kind": {"analyze", "create", "act", "automate"},
        "accepted": {"true", "false", "na"},
    },
    "bowhead_section_split_total": {
        "kind": None,
    },
    "bowhead_session_tab_total": {
        "tab": {"chat", "canvas"},
    },
    # V5 PRD-E3: tool detail drawer
    "bowhead_tool_drawer_opened_total": {
        "tool_hash": None,  # sha1 hash prevents Chinese tool_names exploding cardinality.
    },
    "bowhead_tool_drawer_copy_total": {
        "section": {"request", "response"},
    },
    # Sprint ν1 Phase 3a — sourceLookup contract assertion
    # FE reports narratives containing citation markers with an empty sourceLookup.
    # No turn label (high cardinality); total count only. Use audit logs for debugging.
    "frontend_source_lookup_empty": {},
}

_LABEL_VAL_MAX = 64


class MetricEvent(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    labels: dict[str, str] = Field(default_factory=dict)


# Per-flush limit. FE batches by _FLUSH_INTERVAL_MS, usually with single-digit counts;
# this prevents an entire page's accumulated events arriving at once. FE splits excess into more batches.
_BATCH_MAX = 50


class MetricEventBatch(BaseModel):
    events: list[MetricEvent] = Field(min_length=1, max_length=_BATCH_MAX)


def _record_metric_event(event: MetricEvent) -> tuple[bool, str | None]:
    """校验并 inc 一条指标。返回 (是否记上, 未记上的原因)。

    从原 `/event` handler 原样抽出——`/event` 与 `/events` 必须走同一套 allowlist、
    同一套 label 校验，否则批量入口会成为绕过 cardinality 防护的旁路。
    """
    spec = _ALLOWED.get(event.name)
    if spec is None:
        logger.debug("metrics_ingest.not_allowed name=%s", event.name)
        return False, "not_allowed"

    # Results Workspace events deliberately carry a complete finite tuple.
    # Reject the entire event on unknown/missing keys or values rather than
    # silently retaining a partial tuple that could hide a privacy regression.
    if event.name == "bowhead_results_workspace_total":
        if set(event.labels) != set(spec):
            return False, "invalid_labels"
        if any(not value or len(value) > _LABEL_VAL_MAX or (spec[key] is not None and value not in spec[key]) for key, value in event.labels.items()):
            return False, "invalid_labels"

    # Validate label keys + cap value lengths (prevent cardinality growth).
    safe_labels: dict[str, str] = {}
    for k, v in (event.labels or {}).items():
        if k not in spec:
            continue  # Ignore unknown labels.
        v = (v or "")[:_LABEL_VAL_MAX]
        allowed_vals = spec[k]
        if allowed_vals is not None and v not in allowed_vals:
            v = "other"
        safe_labels[k] = v

    try:
        from bowhead_core import metrics as _m

        _m.inc(event.name, labels=safe_labels, help=f"V2.8 A2 frontend-reported · {event.name}")
    except Exception as e:
        logger.warning("metrics_ingest.inc_failed name=%s err=%s", event.name, e)
        return False, "inc_failed"
    return True, None


@router.post("/event")
async def ingest(
    payload: MetricEvent,
    request: Request,
    user: Any = _CURRENT_USER_DEP,
    user_store: Any = _USER_STORE_DEP,
) -> dict[str, Any]:
    """前端 fire-and-forget 调用. 不在 allowlist → 静默 ok=false."""
    await _require_ingest_access(request, user_id=user.id, user_store=user_store)
    ok, reason = _record_metric_event(payload)
    return {"ok": True} if ok else {"ok": False, "reason": reason}


@router.post("/events")
async def ingest_batch(
    payload: MetricEventBatch,
    request: Request,
    user: Any = _CURRENT_USER_DEP,
    user_store: Any = _USER_STORE_DEP,
) -> dict[str, Any]:
    """批量入口——**一批只占一个限流单位**。

    为什么要有它（2026-08-08 真机）：前端 `track()` 每条指标裸发一个
    `fetch(keepalive)`。30 节报告水合期间 `bowhead_canvas_report_perf_total`
    连发十几条 → 每条一个连接 → 浏览器连接槽耗尽 `ERR_INSUFFICIENT_RESOURCES`，
    且把 `_require_ingest_access` 的 120/60s 预算按「条」而不是按「次上报」烧掉。

    ⚠ **不做去重**：这些是 counter，14 次 setContent 就该记 14 次；在传输层去重
    等于把仪表要测的东西抹掉（那 14 次本身就是它要报的性能问题）。这里治的是
    「一次上报占一个连接、一个限流额度」，语义一条不少。
    """
    await _require_ingest_access(request, user_id=user.id, user_store=user_store)
    accepted = 0
    rejected: dict[str, int] = {}
    for event in payload.events:
        ok, reason = _record_metric_event(event)
        if ok:
            accepted += 1
        else:
            key = reason or "unknown"
            rejected[key] = rejected.get(key, 0) + 1
    return {"ok": True, "accepted": accepted, "rejected": rejected}


ComposerSurface = Literal["home", "workbench"]
ComposerMode = Literal["comfortable", "compact", "ultra"]


class _ComposerEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    occurred_at: datetime
    surface: ComposerSurface
    composer_mode: ComposerMode


class ComposerToolsOpened(_ComposerEventBase):
    event_name: Literal["composer_tools_opened"]
    composer_mode: Literal["compact", "ultra"]


class ComposerModelPanelOpened(_ComposerEventBase):
    event_name: Literal["composer_model_panel_opened"]
    carrier: Literal["popover", "sheet"]


class ComposerModelPreferenceChanged(_ComposerEventBase):
    event_name: Literal["composer_model_preference_changed"]
    source: Literal["team_default", "personal"]


class ComposerThinkingEffortChanged(_ComposerEventBase):
    event_name: Literal["composer_thinking_effort_changed"]
    effort: Literal["auto", "fast", "deep"]


class ComposerPlanningToggled(_ComposerEventBase):
    event_name: Literal["composer_planning_toggled"]
    enabled: bool
    entry: Literal["toolbar", "tools_sheet"]


class ComposerSend(_ComposerEventBase):
    event_name: Literal["composer_send"]
    action: Literal["start", "steer", "replay"]


class ComposerAttachmentAction(_ComposerEventBase):
    event_name: Literal["composer_attachment_action"]
    action: Literal["select", "upload", "remove"]
    result: Literal["accepted", "rejected", "success", "failed", "cancelled"]
    kind: Literal["file", "bundle"]
    size_band: Literal["small", "large"]


class ComposerSendResult(_ComposerEventBase):
    """The browser may report only failures that an HTTP server cannot observe."""

    event_name: Literal["composer_send_result"]
    result: Literal["transport_error"]
    model_control_batch: Literal["a", "b", "c"]
    has_attachments: bool


class ComposerRuntimeFallbackShown(_ComposerEventBase):
    event_name: Literal["composer_runtime_fallback_shown"]
    kind: Literal["model", "effort", "both"]
    reason: Literal[
        "selection_stale",
        "selection_not_allowed",
        "catalog_unavailable",
        "catalog_policy_changed",
        "effort_not_allowed",
        "effort_model_incompatible",
        "provider_rejected_effort",
        "fallback_target_effort_limited",
        "preferred_target_unavailable",
        "thinking_effort_emergency_disabled",
    ]


ComposerProductEvent = Annotated[
    ComposerToolsOpened
    | ComposerModelPanelOpened
    | ComposerModelPreferenceChanged
    | ComposerThinkingEffortChanged
    | ComposerPlanningToggled
    | ComposerSend
    | ComposerAttachmentAction
    | ComposerSendResult
    | ComposerRuntimeFallbackShown,
    Field(discriminator="event_name"),
]


async def _require_ingest_access(request: Request, *, user_id: str, user_store: Any) -> None:
    require_same_origin_json(request)
    rate_limit_check = getattr(user_store, "rate_limit_check", None)
    if not callable(rate_limit_check):
        raise HTTPException(503, {"code": "metrics_store_unavailable"})
    if not await rate_limit_check(
        f"composer_telemetry:{user_id}",
        limit=120,
        window_s=60,
    ):
        raise HTTPException(429, {"code": "rate_limited"})


def _composer_event_detail(payload: ComposerProductEvent) -> str:
    if isinstance(payload, ComposerAttachmentAction):
        return ":".join(
            (payload.action, payload.result, payload.kind, payload.size_band),
        )
    for field in (
        "carrier",
        "source",
        "effort",
        "entry",
        "action",
        "kind",
        "result",
    ):
        value = getattr(payload, field, None)
        if value is not None:
            return str(value)
    return "none"


@router.post("/composer-event")
async def ingest_composer_event(
    payload: ComposerProductEvent,
    request: Request,
    user: Any = _CURRENT_USER_DEP,
    user_store: Any = _USER_STORE_DEP,
) -> dict[str, Any]:
    """Authenticated, same-origin and durable-deduplicated Composer events."""
    await _require_ingest_access(request, user_id=user.id, user_store=user_store)
    session_store = getattr(request.app.state, "session_store", None)
    record = getattr(session_store, "record_composer_telemetry_event", None)
    if not callable(record):
        raise HTTPException(503, {"code": "composer_telemetry_unavailable"})
    try:
        first_seen = await record(
            user_id=user.id,
            event_id=str(payload.event_id),
            occurred_at=payload.occurred_at,
        )
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_occurred_at"}) from exc
    except Exception:
        logger.exception("composer_telemetry.persist_failed")
        raise HTTPException(503, {"code": "composer_telemetry_unavailable"}) from None
    if not first_seen:
        return {"ok": True, "duplicate": True}

    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_composer_product_event_total",
            labels={
                "event_name": payload.event_name,
                "surface": payload.surface,
                "composer_mode": payload.composer_mode,
                "detail": _composer_event_detail(payload),
            },
            help="Durably deduplicated member Composer product events",
        )
        if isinstance(payload, ComposerSendResult):
            _m.inc(
                "composer_transport_error_total",
                labels={
                    "result": payload.result,
                    "surface": payload.surface,
                },
                help="Untrusted browser-observed Composer transport failures",
            )
            if payload.has_attachments:
                _m.inc(
                    "composer_attachment_transport_error_total",
                    labels={"result": payload.result},
                    help="Untrusted browser-observed attachment transport failures",
                )
        elif isinstance(payload, ComposerAttachmentAction):
            _m.inc(
                "composer_attachment_lifecycle_total",
                labels={
                    "surface": payload.surface,
                    "action": payload.action,
                    "result": payload.result,
                    "kind": payload.kind,
                    "size_band": payload.size_band,
                },
                help="Content-free attachment lifecycle outcomes",
            )
    except Exception:
        logger.exception("composer_telemetry.counter_failed event=%s", payload.event_name)
    return {"ok": True, "duplicate": False}
