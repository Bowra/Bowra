"""Scheduled-turn dispatcher — the §3.3③ turn-gate chokepoint (licensing sprint, tech-design d3).

Extracted from the lifespan ``_run_scheduled`` closure to a module-level function so the entitlement gate is
unit-testable (the closure captured ``orchestrator`` by reference and could not be imported). Sources
``orchestrator``/``entitlements`` from ``app.state``; bound to ``app`` via ``lambda job: run_scheduled_turn(app, job)``
at the SchedulerLoop construction to satisfy the ``RunCallback(job) -> (turn_id|None, error|None)`` shape.

An entitlement or credit denial returns a finite stable code from
``SCHEDULED_SKIPPED_CODES`` so ``scheduler_loop._tick`` classifies it as
``skipped`` (not ``failed`` → no 3-strike auto-pause; resumable after
renewal/rollback — d3 LIC-1). Only that code reaches ``finish_run.error``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.published_diagnostics import (
    normalize_scheduled_failure_code,
)
from bowhead_core.scheduler_loop import RunOutcome

from bowhead_gateway.im.error_messages import im_interface_message, normalize_im_locale
from bowhead_gateway.public_url import public_app_url_for_base

if TYPE_CHECKING:
    from bowhead_core.scheduled_store import ScheduledJob
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _turn_status_failure_code(status_value: object) -> str:
    if status_value in {"awaiting_user", "clarification_pending"}:
        return "scheduled_turn_requires_input"
    if status_value == "awaiting_approval":
        return "scheduled_turn_requires_approval"
    if status_value in {"cancelled", "aborted"}:
        return "scheduled_turn_cancelled"
    return "scheduled_turn_failed"


async def _deep_link(
    app: FastAPI,
    job: ScheduledJob,
    session_id: str,
    team_id: str | None,
) -> str | None:
    """Absolute deep link back to the run's result, or None if no public app URL is configured
    (then delivery degrades to summary-only — never blocks). Reuses the existing `/?session=` route."""
    public_entry_store = getattr(app.state, "public_entry_store", None)
    if public_entry_store is None or not team_id:
        return None
    try:
        if job.public_entry_id:
            entry = await public_entry_store.get(job.public_entry_id)
            if entry is None or entry.status != "active" or entry.team_id != team_id:
                return None
        else:
            entry = await public_entry_store.default_for_team(team_id)
        return public_app_url_for_base(entry.base_url, f"/?session={session_id}") if entry is not None else None
    except Exception:
        logger.exception("scheduler.delivery_entry_unavailable job=%s", job.id)
        return None


def _run_summary(result: Any, job_name: str, locale: str) -> str:
    """One-line summary for delivery: the turn's final_answer, truncated. Empty (pure-canvas, no prose)
    → a neutral 'done' line. (Canvas-section-title fallback is a future refinement.)"""
    return _summary_text(getattr(result, "final_answer", "") or "", job_name, locale)


async def _delivery_locale(
    app: FastAPI,
    job: ScheduledJob,
    language_context: Any = None,
    *,
    team_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> str:
    """Locale for delivery copy: the run's language snapshot when present.

    Reconciliation after a clarification answer has no in-memory snapshot; it reads the
    turn's persisted presentation locale, and only when that is absent resolves again
    through the shared channel resolver with the job prompt, as the run did.
    """
    ui_language = getattr(language_context, "ui_language", None)
    if ui_language:
        return normalize_im_locale(ui_language)
    persisted = await _persisted_presentation_locale(app, team_id=team_id, turn_id=turn_id)
    if persisted:
        return persisted
    from bowhead_gateway.channel_language import resolve_channel_turn_language

    context = await resolve_channel_turn_language(
        app,
        user_id=job.user_id,
        workspace_id=getattr(job, "workspace_id", None),
        team_id=team_id,
        session_id=session_id,
        message_text=job.query or "",
    )
    return context.ui_language


async def _persisted_presentation_locale(app: FastAPI, *, team_id: str | None, turn_id: str | None) -> str | None:
    """The turn's persisted presentation locale, or None when the store, scope, or row is absent."""
    store = getattr(app.state, "terminal_protocol_store", None)
    if store is None or not team_id or not turn_id:
        return None
    from bowhead_core.presentation_locale import canonicalize_supported_locale

    try:
        locale = await store.read_persisted_turn_presentation_locale(team_id=team_id, turn_id=turn_id)
    except Exception as exc:  # noqa: BLE001 - A missing snapshot falls back to re-resolution.
        logger.info("scheduler.delivery_locale_snapshot_unavailable turn=%s err=%s", turn_id, type(exc).__name__)
        return None
    return canonicalize_supported_locale(locale)


async def _resolve_team_from_user_store(app: FastAPI, user_id: str) -> str | None:
    user_store = getattr(app.state, "user_store", None)
    if user_store is None:
        return None
    try:
        if hasattr(user_store, "get_user_team_id"):
            return await user_store.get_user_team_id(user_id)
        user = await user_store.get_user(user_id)
        return getattr(user, "team_id", None) if user is not None else None
    except Exception as exc:
        logger.warning("scheduler.team_lookup_failed user=%s err=%s", user_id, exc)
        return None


async def _resolve_scheduled_member_uid(
    app: FastAPI,
    user_store: Any,
    owner_user_id: str,
    datarc_ws: str | None,
    datarc_client: Any,
    log: Any,
) -> str:
    """Resolve the routine owner's memberUid through its team's saved connection."""
    resolver = getattr(app.state, "member_resolver", None)
    if resolver is None or not datarc_ws or datarc_client is None:
        return ""
    email = None
    try:
        u = await user_store.get_user(owner_user_id) if user_store is not None else None
        if u is not None and getattr(u, "email_verified", False):
            email = getattr(u, "email", None)
    except Exception as exc:
        log.info("scheduled.owner_email_lookup_failed owner=%s err=%s", owner_user_id, exc)
    if not email:
        return ""
    try:
        return await resolver.resolve(email, datarc_ws, datarc=datarc_client)
    except Exception as exc:
        log.info("scheduled.member_unresolved owner=%s ws=%s err=%s", owner_user_id, datarc_ws, exc)
        return ""


async def _resolve_job_workflow(
    app: FastAPI,
    job: ScheduledJob,
    team_id: str | None,
) -> tuple[Any, str | None]:
    """把 job 绑定的固化工作流解析成可注入的内容快照（scheduled-recipe-runs S1）。

    授权 scope **不从库里读**——`user_id` 取 job owner、`team_id` 取本次 fire 现算的值。
    `recipe_ref` 只存 `{form, id_or_name, bound_sha?}`，这样即使某个写入通道漏了校验，
    最坏结果是解析不到而不是越权解析到别人的资产。
    """
    from bowhead_core.workflow_binding import ERR_LOST, resolve_bound_workflow

    user_store = getattr(app.state, "user_store", None)
    team_skill_store = None
    if user_store is not None:
        try:
            from bowhead_core.user_store import TeamSkillStoreAdapter

            team_skill_store = TeamSkillStoreAdapter(user_store)
        except Exception as exc:
            logger.warning("scheduler.team_skill_adapter_failed job=%s err=%s", job.id, exc)

    try:
        return await resolve_bound_workflow(
            job.recipe_ref,
            user_id=job.user_id,
            team_id=team_id,
            skill_instance_store=getattr(app.state, "skill_instance_store", None),
            team_skill_store=team_skill_store,
            team_subagent_store=user_store,
            granted_team_skill_ids=None,
        )
    except Exception as exc:
        logger.exception("scheduler.workflow_resolve_failed job=%s error_type=%s", job.id, type(exc).__name__)
        return None, ERR_LOST


async def _resolve_scheduled_agent_binding(
    app: FastAPI,
    job: ScheduledJob,
    team_id: str | None,
) -> tuple[Any | None, str | None]:
    """Load/migrate/recheck the normalized Agent pin for a scheduled recipe."""

    from bowhead_core.scheduled_agent_binding import (
        ScheduledAgentBindingError,
        ScheduledAgentBindingStore,
    )
    from bowhead_core.workflow_binding import FORM_TEAM_SUBAGENT, parse_ref

    parsed = parse_ref(getattr(job, "recipe_ref", None))
    if parsed is None or parsed["form"] != FORM_TEAM_SUBAGENT:
        return None, None
    user_store = getattr(app.state, "user_store", None)
    if user_store is None or not team_id:
        return None, "scheduled_recipe_lost"
    binding_store = ScheduledAgentBindingStore(user_store.db_path)
    try:
        binding = await binding_store.read(job.id)
        if binding is None:
            # Legacy cutover is deliberately exact-ID-only.  Old name-shaped
            # refs cannot silently retarget a same-name Agent.
            try:
                binding = await binding_store.bind_legacy_team_job(
                    job_id=job.id,
                    team_id=team_id,
                    user_id=job.user_id,
                    agent_id=parsed["id_or_name"],
                )
            except ScheduledAgentBindingError:
                # An Agent with no immutable revision cannot be pinned.  Before the
                # baseline import that is the normal state, not a lost recipe -- and
                # pausing is irreversible, because paused schedules never auto-resume.
                # Fire on the compatibility path instead.
                if await binding_store.release_semantics_active():
                    raise
                return None, None
        binding = await binding_store.validate_for_fire(
            job_id=job.id,
            team_id=team_id,
            user_id=job.user_id,
        )
    except ScheduledAgentBindingError as exc:
        return None, exc.code
    except Exception as exc:
        logger.exception(
            "scheduler.agent_binding_resolve_failed job=%s error_type=%s",
            job.id,
            type(exc).__name__,
        )
        return None, "scheduled_recipe_lost"
    return binding, None


async def _pause_for_scheduled_binding(
    app: FastAPI,
    job: ScheduledJob,
    *,
    reason: str,
) -> None:
    store = getattr(app.state, "scheduled_store", None)
    if store is not None:
        try:
            await store.patch(job.id, status="paused", pause_reason=reason)
        except Exception as exc:
            logger.warning(
                "scheduler.agent_binding_pause_failed job=%s err=%s",
                job.id,
                exc,
            )
    notification = getattr(app.state, "notification_service", None)
    if notification is not None:
        try:
            await notification.emit_scheduled_paused(job, reason=reason)
        except Exception as exc:
            logger.warning(
                "scheduler.agent_binding_notify_failed job=%s err=%s",
                job.id,
                exc,
            )


async def admit_scheduled_execution(
    app: FastAPI,
    job: ScheduledJob,
) -> bool:
    """Re-check Space EXECUTE admission immediately before a worker claims a run."""

    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.access_context import (
        AccessContextError,
        build_access_context,
    )

    user_store = getattr(app.state, "user_store", None)
    if user_store is None:
        logger.warning("scheduler.lifecycle_store_unavailable job=%s", job.id)
        return False
    try:
        await build_access_context(
            user_store=user_store,
            access_store=getattr(app.state, "access_control_store", None),
            user_id=job.user_id,
            workspace_id=job.workspace_id,
            operation=SpaceOperation.EXECUTE,
        )
    except AccessContextError as exc:
        logger.info(
            "scheduler.lifecycle_denied job=%s reason=%s",
            job.id,
            exc.code,
        )
        return False
    except Exception as exc:
        logger.warning(
            "scheduler.lifecycle_lookup_failed job=%s err=%s",
            job.id,
            exc,
        )
        return False
    return True


async def _has_open_clarification(app: FastAPI, turn_id: str) -> bool:
    """这个 turn 是否真有一条待回答的 clarification 行。

    store 缺失或查询失败 → False（退回「记失败」的老语义）：宁可让 owner 看到一次失败，
    也不要把 run 挂在一个可能无卡可答的状态上——那会占着在飞位直到下次 fire。
    """
    store = getattr(app.state, "clarification_store", None)
    if store is None:
        return False
    try:
        rows = await store.get_pending_for_turn(turn_id)
    except Exception as exc:
        logger.warning("scheduler.clarification_probe_failed turn=%s err=%s", turn_id, exc)
        return False
    return bool(rows)


async def _deliver_run_result(
    app: FastAPI,
    job: ScheduledJob,
    *,
    summary: str,
    session_id: str,
    run_id: str,
    team_id: str | None,
    locale: str,
) -> None:
    """把一次成功运行的结果送达 owner（邮件 + in-app + 回跳深链）。

    **单一实现**：cron/run-now 的正常成功路径与 reconciliation 的「用户回答后续跑完成」
    路径共用它。收件人白名单是常设防线——正文带的是工作区分析摘要，而 `notify_channels.email`
    可由对话工具（即 LLM）写入，任意外部地址就是一条周期性 exfil 通道。
    """
    channels = job.notify_channels or {}
    emails = channels.get("email") or []
    if not (emails or channels.get("deliver_on_success")):
        return
    try:
        deep_link = await _deep_link(app, job, session_id, team_id)
        if emails:
            from bowhead_core.smtp_notifier import send_notification

            notify_store = getattr(app.state, "user_store", None)  # Team SMTP first; env fallback.
            # Deliver only to **verified team members** (including the owner); unresolved team -> empty set
            # = drop everything (fail-closed). Drop and log nonmember addresses.
            allow = (
                await notify_store.team_member_emails(team_id)
                if (notify_store is not None and team_id and hasattr(notify_store, "team_member_emails"))
                else set()
            )
            allowed_emails = [e for e in emails if str(e).strip().lower() in allow]
            dropped = len(emails) - len(allowed_emails)
            if dropped:
                logger.warning(
                    "scheduler.delivery_recipients_dropped job=%s dropped=%d (non-member/unverified)",
                    job.id,
                    dropped,
                )
            subject = im_interface_message("scheduled_email_subject", locale, name=job.name)
            lines = [im_interface_message("scheduled_email_job", locale, name=job.name), "", summary]
            if deep_link:
                lines += ["", im_interface_message("scheduled_email_view_result", locale, url=deep_link)]
            lines += ["", "— Bowhead"]
            body = "\n".join(lines)
            # `send_notification` returns `(ok, error)` and **does not raise**. Ignoring it makes an undelivered email
            # a completely silent failure: the run stays successful, in-app delivery proceeds, logs are clean, and users believe email was delivered.
            # (116 dogfood, 2026-08-06: the entire delivery path logged neither success nor failure). Minimal visibility here:
            # warn with failure counts for diagnosis. Omit addresses (recipients are PII; the dropped-address log also omits them).
            sent = 0
            failures: list[str] = []
            for addr in allowed_emails[:5]:
                ok, err = await send_notification(addr, subject, body, team_id=team_id, store=notify_store)
                if ok:
                    sent += 1
                else:
                    failures.append(str(err or "unknown"))
            if failures:
                logger.warning(
                    "scheduler.delivery_email_failed job=%s run=%s sent=%d failed=%d reasons=%s",
                    job.id,
                    run_id,
                    sent,
                    len(failures),
                    sorted(set(failures))[:3],
                )
        notif = getattr(app.state, "notification_service", None)
        if notif is not None:
            await notif.emit_routine_delivered(job, run_id=run_id, session_id=session_id, summary=summary)
    except Exception as exc:
        logger.warning("scheduler.routine_delivered_failed job=%s err=%s", job.id, exc)


def _turn_status_value(turn: Any) -> str:
    """`Turn.status` 是 str，不是带 `.value` 的枚举（那是 TurnResult 的形状）——两种都兜。"""
    st = getattr(turn, "status", None)
    return str(getattr(st, "value", st) or "")


async def is_scheduled_resume_in_flight(app: FastAPI, turn_id: str) -> bool:
    """挂起的 turn 是不是**正在续跑**（用户已回答、orchestrator 在跑）。

    给 scheduler_loop 的过期分支当判据：正在续跑就别抢着把它判超时，否则 run 被标失败、
    新 run 并行起（双扣费），续跑完成后 reconciliation 找不到 awaiting 行 → 交付静默丢失。
    """
    orch = getattr(app.state, "orchestrator", None)
    if orch is None or getattr(orch, "session_store", None) is None:
        return False
    try:
        turn = await orch.session_store.get_turn(turn_id)
    except Exception as exc:
        logger.warning("scheduler.resume_probe_lookup_failed turn=%s err=%s", turn_id, exc)
        return False
    return turn is not None and _turn_status_value(turn) == "running"


async def reconcile_awaiting_runs(app: FastAPI) -> None:
    """收编挂起的 run（每 tick 最先跑）。

    用户在 chat 里回答后，续跑走的是**普通 chat 路**——它对定时任务一无所知，跑完不会有人
    去终结那条 awaiting run。所以由调度侧反过来查 turn 现态并收尾。四个分支：
    completed → 成功 + 送达；failed/aborted/cancelled → 失败计振；会话被软删 → 立即过期
    （否则 @manual 任务的挂起永远等不到下次 fire，会永久占着在飞位）；其余 → 不动。
    """
    store = getattr(app.state, "scheduled_store", None)
    orch = getattr(app.state, "orchestrator", None)
    if store is None or orch is None or getattr(orch, "session_store", None) is None:
        return
    try:
        runs = await store.list_awaiting_runs()
    except Exception as exc:
        logger.warning("scheduler.reconcile_list_failed err=%s", exc)
        return

    for run in runs:
        if not run.turn_id:
            continue
        try:
            turn = await orch.session_store.get_turn(run.turn_id)
        except Exception as exc:
            logger.warning("scheduler.reconcile_turn_lookup_failed run=%s err=%s", run.id, exc)
            continue
        if turn is None:
            continue
        status = _turn_status_value(turn)
        job = await store.get(run.job_id)
        if job is None:
            continue

        if status == "completed":
            # CAS: expiry/run-now may race for this run. Only the winner delivers; otherwise the records can disagree:
            # a recorded timeout alongside delivery of a successful report.
            settlement = await store.finish_awaiting_after_resolution(
                run.id,
                run_status="success",
            )
            if not settlement.won:
                logger.warning(
                    "scheduler.completed_settlement_rejected run=%s cause=%s",
                    run.id,
                    settlement.cause,
                )
                continue
            session_id = getattr(turn, "session_id", "") or ""
            delivery_team_id = await _resolve_team_from_user_store(app, job.user_id)
            locale = await _delivery_locale(app, job, team_id=delivery_team_id, session_id=session_id or None, turn_id=run.turn_id)
            await _deliver_run_result(
                app,
                job,
                summary=_summary_text(getattr(turn, "final_answer", None), job.name, locale),
                session_id=session_id,
                run_id=run.turn_id,
                team_id=delivery_team_id,
                locale=locale,
            )
            await _resolve_needs_input(app, job, run.id, reason="answered")
        elif status in {"failed", "aborted", "cancelled"}:
            transition = await store.finish_awaiting_with_clarification_invalidation(
                run.id,
                run_status="failed",
                run_error="scheduled_turn_failed",
                clarification_status="dismissed",
                clarification_reason="scheduled_turn_terminated",
            )
            won = transition.won
            if transition.won:
                from bowhead_gateway.clarification_invalidation import (
                    publish_pending_clarification_invalidations,
                )

                try:
                    await publish_pending_clarification_invalidations(
                        app,
                        request_ids=[item.request_id for item in transition.invalidations],
                    )
                except Exception:
                    logger.exception(
                        "scheduler.turn_terminal_invalidation_publish_failed run=%s",
                        run.id,
                    )
            elif transition.cause == "resolution_won":
                # The user answer won the clarification CAS and the resumed
                # Turn subsequently failed.  There is no actionable card to
                # invalidate; only the awaiting run still needs settlement.
                settlement = await store.finish_awaiting_after_resolution(
                    run.id,
                    run_status="failed",
                    run_error="scheduled_turn_failed",
                )
                won = settlement.won
            if won:
                await _resolve_needs_input(app, job, run.id, reason="failed")
        elif await _session_gone(app, turn):
            # A soft-deleted session means this paused run will never receive an answer. Cron jobs have the next fire as fallback;
            # `@manual` has no next fire. Without cleanup here, it permanently occupies an in-flight slot.
            transition = await store.finish_awaiting_with_clarification_invalidation(
                run.id,
                run_status="failed",
                run_error="scheduled_anchor_session_deleted",
                clarification_status="dismissed",
                clarification_reason="scheduled_session_deleted",
            )
            if transition.won:
                from bowhead_gateway.clarification_invalidation import (
                    publish_pending_clarification_invalidations,
                )

                try:
                    await publish_pending_clarification_invalidations(
                        app,
                        request_ids=[item.request_id for item in transition.invalidations],
                    )
                except Exception:
                    logger.exception(
                        "scheduler.session_deleted_invalidation_publish_failed run=%s",
                        run.id,
                    )
                await _resolve_needs_input(app, job, run.id, reason="session_deleted")


_SUMMARY_MAX_CHARS = 240
_CITE_MARK_RE = re.compile(r"\[cite:\d+\]")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,}).*$", re.MULTILINE)
_MD_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)
_MD_LINE_PREFIX_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|~~)(?=\S)(.+?)(?<=\S)\1")
_MD_INLINE_CODE_RE = re.compile(r"`+([^`]*)`+")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def scheduled_summary_plain_text(final_answer: str | None) -> str:
    """Plain-text preview of a final answer for delivery (in-app body and email alike).

    Resolves internal markers (citations are dropped: the preview has no source list
    to point at), strips Markdown syntax and collapses whitespace, then truncates.
    Returns "" when nothing readable is left.
    """
    from bowhead_gateway.im.blocks import resolve_markers

    text = _CITE_MARK_RE.sub("", final_answer or "")
    text = resolve_markers(text)
    text = _MD_FENCE_RE.sub("", text)
    text = _MD_TABLE_RULE_RE.sub("", text)
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_LINE_PREFIX_RE.sub("", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    for _ in range(2):  # nested emphasis such as ***bold italic***
        text = _MD_EMPHASIS_RE.sub(r"\2", text)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    lines = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.strip().startswith("|") else [line.strip()]
        joined = " · ".join(cell for cell in cells if cell)
        if joined:
            lines.append(joined)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    return text[:_SUMMARY_MAX_CHARS].rstrip() + "…"


def _summary_text(final_answer: str | None, job_name: str, locale: str) -> str:
    """Delivery summary from a final answer (reconciliation only has the persisted text).

    One helper feeds both the in-app notification body and the email body.
    """
    text = scheduled_summary_plain_text(final_answer)
    if not text:
        return im_interface_message("scheduled_run_completed", locale, name=job_name)
    return text


async def _session_gone(app: FastAPI, turn: Any) -> bool:
    from bowhead_gateway.turn_service import is_session_deleted

    orch = getattr(app.state, "orchestrator", None)
    session_id = getattr(turn, "session_id", None)
    if orch is None or not session_id:
        return False
    try:
        return await is_session_deleted(orch, session_id)
    except Exception:
        return False


async def _resolve_needs_input(app: FastAPI, job: Any, run_id: str, *, reason: str) -> None:
    """关掉 needs_input 的待办徽章。它进了 ACTION_REQUIRED 集，不 resolve 就永远红着。"""
    notif = getattr(app.state, "notification_service", None)
    if notif is None:
        return
    try:
        await notif.resolve_routine_needs_input(job, run_id=run_id, reason=reason)
    except Exception as exc:
        logger.warning("scheduler.resolve_needs_input_failed job=%s err=%s", job.id, exc)


async def run_scheduled_turn(app: FastAPI, job: ScheduledJob) -> RunOutcome:
    """Dispatch one scheduled turn.

    三态（S3）：成功 = ``RunOutcome(turn_id=...)``；失败/跳过 = ``RunOutcome(error=code)``；
    **挂起** = ``RunOutcome(turn_id=..., awaiting=True, session_id=...)`` —— 跑到歧义点、
    正等 owner 回答，既不是成功也不是失败，调用方据此把 run 转成 awaiting_input 而不是终结它。
    """
    from uuid import uuid4

    from bowhead_core.space_governance import SpaceOperation
    from bowhead_core.types import TurnRequest

    from bowhead_gateway.access_context import (
        AccessContextError,
        access_context_to_dict,
        build_access_context,
    )
    from bowhead_gateway.datarc_context import resolve_datarc_turn_context

    orchestrator = getattr(app.state, "orchestrator", None)
    if orchestrator is None:
        return RunOutcome(error="orchestrator_not_ready")

    # feedback-0713 F2: a soft-deleted anchor session (R1 report-deletion linkage) fails this run
    # and pauses the job (a permanent condition, following owner_lost_access: otherwise every tick reruns,
    # fails and floods notifications). Headless has no user-initiated continuation signal; omit IM revival. Check
    # before entitlement/datarc setup, so a doomed run does no expensive work.
    if job.anchor_session_id:
        from bowhead_gateway.turn_service import is_session_deleted

        try:
            anchor_deleted = await is_session_deleted(
                orchestrator,
                job.anchor_session_id,
            )
        except Exception as exc:
            logger.exception(
                "scheduler.anchor_session_lookup_failed job=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            return RunOutcome(error="scheduled_turn_failed")
        if anchor_deleted:
            logger.warning(
                "scheduler.anchor_session_deleted_paused job=%s session=%s",
                job.id,
                job.anchor_session_id,
            )
            try:
                _sched_store = getattr(app.state, "scheduled_store", None)
                if _sched_store is not None:
                    await _sched_store.patch(job.id, status="paused")
            except Exception as exc:
                logger.warning("scheduler.pause_failed job=%s err=%s", job.id, exc)
            return RunOutcome(error="scheduled_anchor_session_deleted")

    # Licensing pre-flight gate (§3.3③): the job has no team_id → resolve it from the owner's user_id, then gate.
    # ABOVE run_turn → a denied scheduled turn costs zero LLM. Shadow mode (enforce=0) logs but does not raise.
    team_id: str | None = None
    ent = getattr(app.state, "entitlements", None)
    community = getattr(app.state, "edition", "business") == "community"
    if community and ent is None:
        return RunOutcome(error="authz_unavailable")
    if ent is not None:
        try:
            team_id = await ent.resolve_team_for_user(job.user_id)
            await ent.assert_can_run_turn(team_id)
            # Feature gate (§5): gate the *scheduling* feature on EVERY execution path (cron / run-now /
            # tool-created), not just the REST create. The conversational scheduled_job_create tool skips
            # the route's require_feature_licensed, so without this an unentitled team's tool-created
            # routine would still run. Inert on saas/shadow (is_feature_licensed → True); bites local+ENFORCE.
            if not await ent.is_feature_licensed(team_id, "scheduling"):
                logger.info("scheduler.feature_unlicensed job=%s team=%s", job.id, team_id)
                return RunOutcome(error="feature_unlicensed")
        except EntitlementDenied as exc:
            logger.info("scheduler.entitlement_denied job=%s reason=%s", job.id, exc.reason)
            return RunOutcome(error=normalize_scheduled_failure_code(exc.reason))
        except Exception as exc:
            logger.warning("scheduler.entitlement_check_failed job=%s err=%s", job.id, exc)
            if community:
                return RunOutcome(error="authz_lookup_failed")
    if team_id is None:
        team_id = await _resolve_team_from_user_store(app, job.user_id)

    access_context: dict[str, object] | None = None
    user_store = getattr(app.state, "user_store", None)
    if user_store is not None:
        try:
            ctx = await build_access_context(
                user_store=user_store,
                access_store=getattr(app.state, "access_control_store", None),
                user_id=job.user_id,
                workspace_id=job.workspace_id,
                operation=SpaceOperation.EXECUTE,
            )
            team_id = ctx.team_id
            access_context = access_context_to_dict(ctx)
        except AccessContextError as exc:
            logger.info("scheduler.authz_denied job=%s reason=%s", job.id, exc.code)
            return RunOutcome(error="ws_access_denied")
        except Exception as exc:
            logger.warning("scheduler.authz_check_failed job=%s err=%s", job.id, exc)
            return RunOutcome(error="authz_lookup_failed")

    scheduled_agent_binding, agent_binding_error = await _resolve_scheduled_agent_binding(app, job, team_id)
    if agent_binding_error is not None:
        logger.warning(
            "scheduler.agent_binding_paused job=%s reason=%s",
            job.id,
            agent_binding_error,
        )
        await _pause_for_scheduled_binding(
            app,
            job,
            reason=agent_binding_error,
        )
        return RunOutcome(error=agent_binding_error)

    # Credit pre-flight gate (mirror chat.py:700-734): out-of-credit → skip, do NOT run an LLM turn we
    # cannot bill. Post-flight deduct (by actual tokens) happens after run_turn below. Finite credit codes
    # are classified `skipped` (not `failed`) by scheduler_loop — resumable once topped up.
    if team_id and user_store is not None:
        try:
            from bowhead_gateway.credit_admission import credit_denial_code

            u = await user_store.get_user(job.user_id)
            team = await user_store.get_team(team_id) if u else None
            credit_denial = credit_denial_code(u, team)
            if credit_denial is not None:
                logger.info("scheduler.credit_exhausted job=%s code=%s", job.id, credit_denial)
                return RunOutcome(error=credit_denial)
        except Exception as exc:
            logger.warning("scheduler.credit_precheck_failed job=%s err=%s", job.id, exc)

    # Batch 2/c9 (PRD §3.9 decision 10): routines always query the Datarc workspace **snapshotted at creation**;
    # owner preferences cannot shift it (changing personal context must not change an existing report's data scope).
    # Owner loses snapshot access (empty member resolution) -> pause job + record it (matrix semantics, no silent empty run).
    _pinned_ws = getattr(job, "datarc_workspace_id", None)
    try:
        datarc_ctx = await resolve_datarc_turn_context(
            app,
            team_id=team_id,
            workspace_id=job.workspace_id,
            user_id=job.user_id,  # Power BI Scenario ②: impersonate the routine OWNER
            # Pin short-circuit (architecture review, 2026-07-13): a snapshot skips the per-user ladder,
            # avoiding an accessible call each tick and headless preference writes.
            pinned_project=_pinned_ws,
            log=logger,
        )
    except Exception as exc:
        logger.exception(
            "scheduler.datarc_context_failed job=%s error_type=%s",
            job.id,
            type(exc).__name__,
        )
        return RunOutcome(error="scheduled_datarc_context_failed")
    _active_project = datarc_ctx.active_datarc_project
    if _pinned_ws:
        _active_project = _pinned_ws
        _member_probe = await _resolve_scheduled_member_uid(
            app,
            user_store,
            job.user_id,
            _pinned_ws,
            datarc_ctx.datarc_client,
            logger,
        )
        if not _member_probe:
            logger.warning(
                "scheduler.owner_lost_access_paused job=%s owner=%s pinned_ws=%s",
                job.id,
                job.user_id,
                _pinned_ws,
            )
            try:
                _sched_store = getattr(app.state, "scheduled_store", None)
                if _sched_store is not None:
                    # review P1: the method is patch, not update; update() raises AttributeError
                    # swallowed by the except below, so the job never pauses and every tick reruns, fails and floods notifications.
                    await _sched_store.patch(job.id, status="paused")
            except Exception as exc:
                logger.warning("scheduler.pause_failed job=%s err=%s", job.id, exc)
            return RunOutcome(error="scheduled_owner_lost_datarc_access")

    # credential-readiness D3: scheduled turns must carry the team router (the process Router is
    # RefusalAdapter in prod; reaching it runs real tasks with a fake model). Missing configuration -> pause + notify
    # someone authorized to fix it (job owners often cannot edit team configuration, PRD 6.0.3 ①); produce no content.
    from bowhead_gateway.turn_service import resolve_team_byo_overrides

    team_router, team_kb_retriever = await resolve_team_byo_overrides(app.state, team_id)
    if team_router is None:
        logger.warning(
            "scheduler.team_model_unconfigured_paused job=%s team=%s",
            job.id,
            team_id,
        )
        try:
            _sched_store = getattr(app.state, "scheduled_store", None)
            if _sched_store is not None:
                await _sched_store.patch(job.id, status="paused")
        except Exception as exc:
            logger.warning("scheduler.pause_failed job=%s err=%s", job.id, exc)
        _notif = getattr(app.state, "notification_service", None)
        if _notif is not None:
            try:
                await _notif.emit_scheduled_paused(job, reason="team_model_unconfigured")
            except Exception as exc:
                logger.warning("scheduler.pause_notify_failed job=%s err=%s", job.id, exc)
        return RunOutcome(error="scheduled_team_model_unconfigured")

    # scheduled-recipe-runs S1: resolve and gate the bound saved workflow **server-side** (no resolution fallback chain /
    # content-fingerprint comparison for another author's asset / admission lint / cap). Inject its body into the Lead
    # prompt through TurnRequest here, using the resolved team_id above; doomed runs must not invoke an LLM.
    # All three failures are **permanent conditions** (deleted/inaccessible workflow, changed content awaiting confirmation,
    # failed admission) -> pause + notify, without tick-by-tick retries/floods (owner_lost_access precedent); skipped codes do not count as strikes.
    bound_workflow = None
    if getattr(job, "recipe_ref", None):
        bound_workflow, bind_err = await _resolve_job_workflow(app, job, team_id)
        if bind_err is not None:
            logger.warning("scheduler.workflow_binding_paused job=%s reason=%s", job.id, bind_err)
            _sched_store = getattr(app.state, "scheduled_store", None)
            if _sched_store is not None:
                try:
                    await _sched_store.patch(job.id, status="paused", pause_reason=bind_err)
                except Exception as exc:
                    logger.warning("scheduler.pause_failed job=%s err=%s", job.id, exc)
            _notif = getattr(app.state, "notification_service", None)
            if _notif is not None:
                try:
                    await _notif.emit_scheduled_paused(job, reason=bind_err)
                except Exception as exc:
                    logger.warning("scheduler.pause_notify_failed job=%s err=%s", job.id, exc)
            return RunOutcome(error=bind_err)

    try:
        from bowhead_core.agent_execution_binding import TurnAgentBindingInput

        from bowhead_gateway.channel_language import resolve_channel_turn_language

        session_id = job.anchor_session_id or f"s_sched_{uuid4().hex[:10]}"
        # A scheduled turn has no browser: without saved preferences or a team
        # default/lock it follows the job prompt's language (then the anchor
        # session's previous answer, then English). Snapshotted on the request.
        language_context = await resolve_channel_turn_language(
            app,
            user_id=job.user_id,
            workspace_id=job.workspace_id,
            team_id=team_id,
            session_id=job.anchor_session_id,
            message_text=job.query or "",
        )
        # The model's "today" for a scheduled run is the job's own calendar (its saved
        # IANA zone); an unknown zone keeps the deployment clock. Snapshotted with the
        # language context, so a resumed run keeps the same clock.
        language_context = language_context.with_time_zone(getattr(job, "tz", None))
        tr = TurnRequest(
            turn_id=f"t_sched_{uuid4().hex[:10]}",
            session_id=session_id,
            workspace_id=job.workspace_id,
            datarc_binding=datarc_ctx.connection_binding,
            active_datarc_project=_active_project,  # Batch 2/c9: per-user + snapshot = pin value.
            datarc_client=datarc_ctx.datarc_client,
            datarc_unavailable=datarc_ctx.datarc_unavailable,
            vector_client=datarc_ctx.vector_client,
            # A scheduled turn has no live request, so resolve the datarc memberUid for the job OWNER
            # here (the chat path does this per-request). Passing "" makes every BI call fail with
            # "缺少 memberUid" — caught by run-now dogfood. Mirrors chat._resolve_datarc_member_uid.
            member_uid=await _resolve_scheduled_member_uid(
                app,
                user_store,
                job.user_id,
                _active_project,
                datarc_ctx.datarc_client,
                logger,
            ),
            user_id=job.user_id,
            team_id=team_id,
            access_context=access_context,
            text=job.query,
            attachments=[],
            language_context=language_context,
            # credential-readiness D3: propagate BYO overrides upward (same attachment as chat /send).
            team_router=team_router,
            team_kb_retriever=team_kb_retriever,
            # scheduled-recipe-runs S1: saved workflow pre-read server-side (None = unbound).
            bound_workflow=bound_workflow,
            agent_binding=(TurnAgentBindingInput.from_scheduled(scheduled_agent_binding) if scheduled_agent_binding is not None else None),
        )
        from bowhead_gateway.turn_service import (
            admit_legacy_chat_execution,
            spawn_turn,
        )

        admitted_bus = await admit_legacy_chat_execution(
            orchestrator,
            tr,
        )
        worker = await spawn_turn(
            orchestrator,
            tr,
            user_store=user_store,
            team_id=team_id,
            metrics_log=getattr(app.state, "metrics_log", None),
            entitlements=ent,
            name_prefix="scheduled_turn",
            admitted_legacy_bus=admitted_bus,
            cleanup_grace_s=0,
        )
        result = await worker
        if result is None:
            raise RuntimeError("scheduled_turn_result_unavailable")
    except Exception as e:
        logger.exception(
            "scheduler.turn_failed job=%s error_type=%s",
            job.id,
            type(e).__name__,
        )
        return RunOutcome(error="scheduled_turn_failed")

    # Outcome gate: only a COMPLETED turn is a real success. Anything else (failed / awaiting_approval /
    # cancelled) means the routine couldn't finish unattended → return an error so the scheduler classifies
    # it failed (fires scheduled.failed) and we do NOT deliver a non-result as a result.
    status_val = getattr(getattr(result, "status", None), "value", None)
    if status_val != "completed":
        # S3: reaching ambiguity -> **pause for the owner's answer**, without recording failure. The criterion is an actual
        # open clarification row, not status == clarification_pending: plan-proposal
        # reuses that status but **creates no clarification row** (resolve returns 404). Checking only status
        # pauses a turn with no answerable card: notification sent, blank page on click, then a timeout at the deadline.
        if status_val in {"clarification_pending", "awaiting_user"} and await _has_open_clarification(
            app,
            tr.turn_id,
        ):
            # The durable awaiting row is the proof that every producer event,
            # including clarification_required + paused done, is already in
            # the Turn log.  System invalidation may run on another Gateway
            # immediately after this function returns, so a timed delay is not
            # an ordering fence.
            await orchestrator.event_registry.drain_turn_persistence(tr.turn_id)
            logger.info("scheduler.turn_awaiting_input job=%s turn=%s", job.id, tr.turn_id)
            return RunOutcome(turn_id=tr.turn_id, awaiting=True, session_id=tr.session_id)
        logger.info("scheduler.turn_not_completed job=%s status=%s", job.id, status_val)
        return RunOutcome(error=_turn_status_failure_code(status_val))

    # Phase B delivery (opt-in, success path). Extract `_deliver_run_result` for reconciliation reuse:
    # completion after the user's answer also needs delivery. Duplicating the recipient allowlist/[:5] cap/team fail-closed
    # resolution will eventually omit a safeguard; `notify_channels.email` is writable by the LLM.
    locale = await _delivery_locale(app, job, tr.language_context)
    await _deliver_run_result(
        app,
        job,
        summary=_run_summary(result, job.name, locale),
        session_id=tr.session_id,
        run_id=tr.turn_id,
        team_id=team_id,
        locale=locale,
    )
    return RunOutcome(turn_id=tr.turn_id)
