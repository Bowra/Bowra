"""Domain service for emitting product notifications."""

from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from bowhead_core.notification_team import NotificationSubject, NotificationTeamResolver
from bowhead_core.notification_types import NotificationTypeError, validate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from bowhead_core.approval_store import Approval, ApprovalStore
    from bowhead_core.notification_store import NotificationItem, NotificationStore, NotificationWrite
    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)

# Requester-facing fallback copy for every way an access request stops being active (expiry,
# deleted resource, an approval that did not take effect). Deliberately neutral: it never says
# why. The frontend renders the localized ``access_request_expired_title`` instead.
ACCESS_REQUEST_NO_LONGER_ACTIVE = "This request is no longer active. Request access again if you still need it."

# routine_notify levels → stored priority. The level is the Agent's only influence on delivery.
ROUTINE_ALERT_PRIORITIES: dict[str, str] = {"info": "p3", "attention": "p2", "urgent": "p1"}
# At most this many alerts per task in any rolling 24 hours (plus one per run, by dedupe).
ROUTINE_ALERT_DAILY_LIMIT = 5

# Canonical personal Memory page. The standalone `/memory` page was folded into Settings; the
# frontend still maps legacy persisted `/memory` action URLs to this path.
MEMORY_SETTINGS_PATH = "/settings/memory/"


def space_assignment_action_url(requester_user_id: str, team_id: str) -> str:
    """Open the existing member Space-grant dialog."""
    return "/team/settings/people/?" + urlencode({"member": requester_user_id, "team": team_id})


def _metric_inc(name: str, labels: dict[str, str], value: float = 1.0) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(name, labels=labels, value=value)
    except Exception:  # noqa: S110
        pass


def _emit_metric(type_: str, outcome: str) -> None:
    _metric_inc(
        "bowhead_notification_emit_total",
        labels={"type": type_, "outcome": outcome},
    )


def _turn_url(approval: Approval) -> str:
    if approval.session_id:
        return f"/?session={approval.session_id}"
    return "/"


def _error_preview(error: str | None, limit: int = 240) -> str | None:
    if not error:
        return None
    return error[:limit] + ("..." if len(error) > limit else "")


# scheduled-recipe-runs S3 · FR-5.6: pauses caused by invalid workflow bindings must explain the next action.
# Default body "连续失败已被自动暂停, 请检查后手动恢复" is **misleading** for one-time structural failures
# such as author deletion: users inspect task errors instead of rebinding /
# confirming new content / contacting the author. Keys are bare names; normalization strips `scheduled_`. Runtime callers
# pass stable code `scheduled_recipe_lost` (workflow_binding.ERR_LOST), while docs use bare names; both spellings
# must reach the same branch or these branches are dead for real callers.
_PAUSED_RECIPE_COPY: dict[str, tuple[str, str]] = {
    "recipe_lost": (
        "定时任务已暂停: 绑定的流程不可用",
        "「{name}」绑定的流程已被删除或你已失去访问权限, 任务已暂停; 请重新绑定一个流程后恢复。",
    ),
    "recipe_changed": (
        "定时任务已暂停: 绑定的流程有改动",
        "「{name}」绑定的流程内容已被作者修改, 任务已暂停; 请确认新版内容无误后恢复。",
    ),
    "recipe_rejected": (
        "定时任务已暂停: 绑定的流程未通过校验",
        "「{name}」绑定的流程未通过准入校验, 任务已暂停; 请联系流程作者修改后重新绑定。",
    ),
}


def _paused_copy(job: Any, reason: str) -> tuple[str, str]:
    """暂停通知的标题/正文；未知 reason 退回既有默认话术（不炸、不出空文案）。

    模板用 `replace` 而不是 `str.format`：任务名是用户自取的, 里面一个 `{` 就能让
    format 抛 KeyError/ValueError, 把「通知用户任务暂停了」变成「一条通知都发不出」。
    """
    entry = _PAUSED_RECIPE_COPY.get((reason or "").removeprefix("scheduled_"))
    if entry is None:
        return "定时任务已自动暂停", f"「{job.name}」连续失败已被自动暂停, 请检查后手动恢复"
    return entry[0], entry[1].replace("{name}", str(job.name))


# Drop entire URLs/domains: hints come from bound-workflow prose, and clickable links are direct phishing payloads.
_HINT_URL_RE = re.compile(r"(?i)(?:[a-z][a-z0-9+.\-]*:/{0,2}|www\.|[a-z0-9\-]+\.(?:com|cn|net|org|io|co|xyz|top|info|me))\S*")
# Markdown/quote symbols: prevent hints becoming links, code blocks or impersonated product copy on any rendering surface.
_HINT_MARKUP_RE = re.compile(r"[`*_~\[\]()<>{}|\\\"'!]")
_HINT_MAX_CHARS = 60


def _sanitize_question_hint(raw: str | None, limit: int = _HINT_MAX_CHARS) -> str | None:
    """把 clarification 问题原文压成一条受限、无标记、无链接的短提示。

    为什么不直接用原文（PRD T4）：那段文本由绑定的流程操纵, 而通知是**产品口吻 + 深链**
    的高信任面, 原样复述等于替攻击者递话。故这里只保留「像个名词」的残渣, 且调用方默认
    不传（question_hint=None）; 传了也只落 metadata 并带来源标记, 正文永远是固定句式。
    """
    if not raw:
        return None
    text = unicodedata.normalize("NFKC", str(raw))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")  # Control/zero-width characters
    text = _HINT_URL_RE.sub(" ", text)
    text = _HINT_MARKUP_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


@dataclass(frozen=True)
class NotificationDraft:
    """One notification event, before Team resolution and recipient filtering.

    ``title``/``body`` are the stored fallback; the frontend renders localized copy from the type
    and ``metadata``. ``dedupe_key`` is the item's identity within its Team; existing keys are kept
    so open items keep folding across upgrades. ``priority`` overrides the type's default within
    the priorities its registration allows.
    """

    type: str
    subject: NotificationSubject
    recipients: Sequence[tuple[str, str | None]]
    title: str
    body: str | None
    dedupe_key: str
    action_url: str | None = None
    actions: Sequence[Mapping[str, Any]] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    actor_user_id: str | None = None
    priority: str | None = None
    # An access request folded into an approver digest exists so it can be resolved later, but
    # is delivered to nobody yet.
    allow_no_recipients: bool = False
    # FEATURE_NOTIFICATION_DELIVERY: people to email on a new occurrence, for a type with the
    # ``email_delivery`` channel (a scheduled result, an urgent task alert). Filtered like recipients.
    email_delivery_to: Sequence[str] = ()


@dataclass
class NotificationService:
    store: NotificationStore
    user_store: UserStore
    enabled: bool = True
    # FEATURE_NOTIFICATION_DELIVERY: write email rows (escalations, configured deliveries) for the
    # delivery worker. Off: no rows, and producers keep their previous direct email paths.
    delivery_enabled: bool = False
    teams: NotificationTeamResolver = field(init=False)

    def __post_init__(self) -> None:
        self.teams = NotificationTeamResolver(self.user_store)

    async def emit(self, draft: NotificationDraft) -> NotificationWrite | None:
        """Write one event: registry check, Team resolution, recipient filtering, one transaction.

        - An unregistered type or a priority its type does not allow writes nothing.
        - The Team comes from the subject (`NotificationTeamResolver`), never from the legacy
          ``users.team_id``. When no Team resolves, an item can still reach its owner alone, under
          the owner's own namespace; nobody else is kept.
        - In a resolved Team, recipients who are not active members are dropped.
        """
        if not self.enabled:
            _emit_metric(draft.type, "disabled")
            return None
        try:
            spec = validate(draft.type, draft.priority)
        except NotificationTypeError as exc:
            logger.warning("notification.emit_rejected type=%s code=%s", draft.type, exc.code)
            _emit_metric(draft.type, exc.code)
            return None
        resolved = await self.teams.resolve(draft.subject, personal=spec.scope == "personal")
        if resolved is None:
            _emit_metric(draft.type, "no_team")
            return None
        recipients: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for uid, role in draft.recipients:
            if uid and uid not in seen:
                seen.add(uid)
                recipients.append((uid, role))
        if resolved.authoritative:
            members = await self.teams.active_members(resolved.team_id, seen)
            recipients = [(uid, role) for uid, role in recipients if uid in members]
        else:
            recipients = [(uid, role) for uid, role in recipients if uid == draft.subject.owner_user_id]
        if not recipients and not draft.allow_no_recipients:
            _emit_metric(draft.type, "no_recipients")
            return None
        escalate_after_s = spec.escalation_delay_s if self.delivery_enabled and "email_escalation" in spec.channels else None
        email_to: set[str] = set()
        if self.delivery_enabled and "email_delivery" in spec.channels and draft.email_delivery_to:
            wanted = {uid for uid in draft.email_delivery_to if uid}
            if resolved.authoritative:
                email_to = wanted & await self.teams.active_members(resolved.team_id, wanted)
            else:
                email_to = wanted & {draft.subject.owner_user_id or ""}
        result = await self.store.write(
            team_id=resolved.team_id,
            category=spec.category,
            type=draft.type,
            priority=draft.priority or spec.default_priority,
            title=draft.title,
            body=draft.body,
            subject_type=draft.subject.subject_type,
            subject_id=draft.subject.subject_id,
            actor_user_id=draft.actor_user_id,
            action_url=draft.action_url,
            actions=[dict(action) for action in draft.actions],
            metadata=dict(draft.metadata),
            dedupe_key=draft.dedupe_key,
            recipients=recipients,
            occurrence=spec.occurrence,
            reopen_resolved=spec.reopen_resolved,
            escalate_after_s=escalate_after_s,
            email_delivery_to=email_to,
        )
        _emit_metric(draft.type, "already_resolved" if result.outcome == "resolved_unchanged" else "success")
        return result

    async def _emit_item(self, draft: NotificationDraft) -> NotificationItem | None:
        result = await self.emit(draft)
        return result.item if result is not None else None

    # ── Space assignment ──────────────────────────────────────────────────

    async def emit_space_assignment_requested(
        self,
        *,
        team_id: str,
        requester_user_id: str,
    ) -> NotificationItem | None:
        """Notify Team governors that a member has no active Space."""

        if not self.enabled:
            _emit_metric("space_assignment_requested", "disabled")
            return None
        admin_ids = await self.user_store.list_team_users_by_roles(
            team_id,
            {"owner", "team_admin"},
            include_creator=True,
        )
        if not admin_ids:
            _emit_metric("space_assignment_requested", "no_recipients")
            return None
        labels = await self.user_store.get_team_member_labels(team_id, [requester_user_id])
        requester_name = labels.get(requester_user_id, requester_user_id)
        action_url = space_assignment_action_url(requester_user_id, team_id)
        return await self._emit_item(
            NotificationDraft(
                type="space_assignment_requested",
                subject=NotificationSubject("team_space_assignment", team_id, team_id=team_id),
                recipients=[(user_id, "space_assignment_admin") for user_id in admin_ids],
                title=f"{requester_name} needs a space",
                body="This team member has no available space. Review their space permissions.",
                dedupe_key=f"space.assignment.requested:{requester_user_id}",
                action_url=action_url,
                actions=[{"id": "review", "label": "Manage space permissions", "kind": "link", "url": action_url}],
                metadata={"requesterUserId": requester_user_id, "requesterName": requester_name},
                actor_user_id=requester_user_id,
            )
        )

    async def resolve_space_assignment_request(
        self,
        *,
        team_id: str,
        requester_user_id: str,
    ) -> NotificationItem | None:
        """Resolve an open request and notify the requester after first access."""

        if not self.enabled:
            return None
        resolved = await self.store.resolve_by_dedupe(
            team_id=team_id,
            dedupe_key=(f"space.assignment.requested:{requester_user_id}"),
            reason="space_access_available",
        )
        if resolved is None:
            return None
        return await self._emit_item(
            NotificationDraft(
                type="space_assignment_ready",
                subject=NotificationSubject("team_space_assignment", team_id, team_id=team_id, owner_user_id=requester_user_id),
                recipients=[(requester_user_id, "requester")],
                title="空间已分配",
                body="你现在可以进入空间并开始工作。",
                dedupe_key=f"space_assignment_ready:{requester_user_id}",
                action_url="/",
                actions=[{"id": "open", "label": "开始使用", "kind": "link", "url": "/"}],
            )
        )

    # ── Tool approvals ────────────────────────────────────────────────────

    async def _approval_team_id(self, approval: Approval) -> str | None:
        return await self.teams.workspace_team(approval.workspace_id, subject="approval", subject_id=approval.id)

    def _approval_subject(self, approval: Approval, team_id: str | None) -> NotificationSubject:
        return NotificationSubject("approval", approval.id, team_id=team_id, owner_user_id=approval.user_id)

    def _approval_metadata(self, approval: Approval) -> dict[str, Any]:
        return {
            "approvalId": approval.id,
            "toolName": approval.tool_name,
            "riskLevel": approval.risk_level,
            "turnId": approval.turn_id,
            "sessionId": approval.session_id,
        }

    async def emit_approval_required(self, approval: Approval) -> NotificationItem | None:
        if not self.enabled:
            _emit_metric("approval.required", "disabled")
            return None
        if approval.status != "pending":
            _emit_metric("approval.required", "not_pending")
            return None
        team_id = await self._approval_team_id(approval)
        if not team_id:
            logger.warning("notification.approval_required_no_team approval=%s", approval.id)
            _emit_metric("approval.required", "no_team")
            return None
        admin_ids = await self.user_store.list_team_users_by_roles(
            team_id,
            {"owner", "team_admin"},
            include_creator=True,
        )
        if not admin_ids:
            logger.warning(
                "notification.approval_required_no_recipients approval=%s team=%s",
                approval.id,
                team_id,
            )
            _emit_metric("approval.required", "no_recipients")
            return None
        action_url = f"/admin/approvals/?approvalId={approval.id}"
        item = await self._emit_item(
            NotificationDraft(
                type="approval.required",
                # The requester is not a recipient here; the subject's Team is authoritative.
                subject=NotificationSubject("approval", approval.id, team_id=team_id),
                recipients=[(uid, "approver_admin") for uid in admin_ids],
                title="有操作需要审批",
                body=f"{approval.tool_name} 请求执行写操作, 风险等级 {approval.risk_level}",
                dedupe_key=f"approval.required:{approval.id}",
                action_url=action_url,
                actions=[{"id": "review", "label": "Review", "kind": "link", "url": action_url}],
                metadata=self._approval_metadata(approval),
                actor_user_id=approval.user_id,
            )
        )
        if item is None or item.state != "open":
            return None
        return item

    async def emit_approval_submitted(
        self,
        approval: Approval,
        *,
        persistent: bool = False,
    ) -> NotificationItem | None:
        if not self.enabled or not persistent:
            _emit_metric("approval.submitted", "skipped")
            return None
        return await self._emit_item(
            NotificationDraft(
                type="approval.submitted",
                subject=self._approval_subject(approval, await self._approval_team_id(approval)),
                recipients=[(approval.user_id, "requester")],
                title="审批已提交",
                body=f"{approval.tool_name} 已提交给管理员审批",
                dedupe_key=f"approval.submitted:{approval.id}",
                action_url=_turn_url(approval),
                actions=[{"id": "view", "label": "View", "kind": "link", "url": _turn_url(approval)}],
                metadata=self._approval_metadata(approval),
                actor_user_id=approval.user_id,
            )
        )

    async def emit_approval_decided(self, approval: Approval) -> NotificationItem | None:
        if not self.enabled or approval.status not in {"approved", "rejected"}:
            _emit_metric("approval.decided", "skipped")
            return None
        approved = approval.status == "approved"
        type_ = "approval.approved" if approved else "approval.rejected"
        title = "审批已通过" if approved else "审批已拒绝"
        return await self._emit_item(
            NotificationDraft(
                type=type_,
                subject=self._approval_subject(approval, await self._approval_team_id(approval)),
                recipients=[(approval.user_id, "requester")],
                title=title,
                body=f"{approval.tool_name} {title}",
                dedupe_key=f"{type_}:{approval.id}",
                action_url=_turn_url(approval),
                actions=[{"id": "view", "label": "View", "kind": "link", "url": _turn_url(approval)}],
                metadata=self._approval_metadata(approval),
                actor_user_id=approval.decided_by,
            )
        )

    async def emit_approval_executed(self, approval: Approval) -> NotificationItem | None:
        if not self.enabled or approval.status not in {"executed", "execution_failed"}:
            _emit_metric("approval.executed", "skipped")
            return None
        ok = approval.status == "executed"
        type_ = "approval.executed" if ok else "approval.execution_failed"
        title = "审批操作已执行" if ok else "审批通过但执行失败"
        # The requester, plus the approver who already acted on this approval.
        recipients: list[tuple[str, str | None]] = [(approval.user_id, "requester")]
        if not ok and approval.decided_by and approval.decided_by != approval.user_id:
            recipients.append((approval.decided_by, "approver_admin"))
        return await self._emit_item(
            NotificationDraft(
                type=type_,
                subject=self._approval_subject(approval, await self._approval_team_id(approval)),
                recipients=recipients,
                title=title,
                body=f"{approval.tool_name} 已执行" if ok else _error_preview(approval.execution_error),
                dedupe_key=f"{type_}:{approval.id}",
                action_url=_turn_url(approval),
                actions=[{"id": "view", "label": "View", "kind": "link", "url": _turn_url(approval)}],
                metadata=self._approval_metadata(approval),
                actor_user_id=approval.decided_by,
            )
        )

    async def resolve_approval_required(
        self,
        approval: Approval,
        *,
        reason: str,
    ) -> NotificationItem | None:
        if not self.enabled:
            _metric_inc(
                "bowhead_notification_resolve_total",
                labels={"subject": "approval", "outcome": "disabled"},
            )
            return None
        team_ids = await self.teams.closing_teams(await self._approval_team_id(approval), approval.user_id)
        if not team_ids:
            _metric_inc(
                "bowhead_notification_resolve_total",
                labels={"subject": "approval", "outcome": "no_team"},
            )
            return None
        item = None
        for team_id in team_ids:
            item = await self.store.resolve_by_dedupe(
                team_id=team_id,
                dedupe_key=f"approval.required:{approval.id}",
                reason=reason,
            )
            if item is not None:
                break
        _metric_inc(
            "bowhead_notification_resolve_total",
            labels={"subject": "approval", "outcome": "resolved" if item else "not_open"},
        )
        return item

    # ── content-permissions wave 2: access-request + approval ──────────────
    async def _user_display(self, uid: str) -> str:
        try:
            u = await self.user_store.get_user(uid)
            return getattr(u, "display_name", None) or getattr(u, "email", None) or "同事"
        except Exception:
            return "同事"

    async def emit_access_requested(
        self,
        req: Any,
        res: Any,
        *,
        recipients: set[str],
    ) -> NotificationItem | None:
        """Notify the approvers chosen by the caller's routing that someone requested access.
        Approver recipients CAN see the label (they ``can_manage`` the resource).

        An empty ``recipients`` set still creates the item (so it can be resolved later) but
        delivers it to nobody, e.g. when the request folds into a digest."""
        if not self.enabled:
            return None
        who = await self._user_display(req.requester_id)
        action_url = "/settings/access-requests/"
        item = await self._emit_item(
            NotificationDraft(
                type="access_requested",
                subject=NotificationSubject(res.kind, res.resource_id, team_id=req.team_id),
                recipients=[(uid, "approver") for uid in sorted(uid for uid in recipients if uid)],
                title="有访问申请待处理",
                body=f"{who} 申请访问「{res.label}」",
                dedupe_key=f"access.requested:{req.id}",
                action_url=action_url,
                actions=[{"id": "review", "label": "处理", "kind": "link", "url": action_url}],
                metadata={
                    "requestId": req.id,
                    "kind": res.kind,
                    "resourceId": res.resource_id,
                    "requesterName": who,
                    "resourceLabel": res.label,
                },
                actor_user_id=req.requester_id,
                allow_no_recipients=True,
            )
        )
        if item is None or item.state != "open":
            return None
        return item

    async def emit_access_decided(
        self,
        req: Any,
        *,
        approved: bool,
        action_url: str | None = None,
        stale: bool = False,
        outcome: str | None = None,
    ) -> NotificationItem | None:
        """Notify the requester of the decision. NO resource label (anti-fishing): the
        requester only learns that "a manager" decided (no actor id), plus the approver's note
        verbatim, the granted permission and the optional grant expiry (D9). An approval
        carries a direct ``action_url``."""
        if not self.enabled:
            return None
        # ``approved_pending``: approved, waiting for the Agent release to take effect.
        outcome = outcome or ("stale" if stale else ("approved" if approved else "rejected"))
        title = {
            "stale": ACCESS_REQUEST_NO_LONGER_ACTIVE,
            "approved": "Your access request was approved",
            "approved_pending": "Your access request was approved and will take effect soon",
            "rejected": "Your access request was declined",
        }[outcome]
        metadata: dict[str, Any] = {"requestId": req.id, "outcome": outcome, "decidedBy": "manager"}
        note = getattr(req, "decision_note", None)
        if note and not stale:
            metadata["note"] = note
        if approved and not stale:
            if getattr(req, "granted_permission", None):
                metadata["grantedPermission"] = req.granted_permission
            if getattr(req, "grant_expires_at", None):
                metadata["grantExpiresAt"] = req.grant_expires_at
        return await self._emit_item(
            NotificationDraft(
                type="access_request_decided",
                subject=NotificationSubject(req.kind, req.resource_id, team_id=req.team_id, owner_user_id=req.requester_id),
                recipients=[(req.requester_id, "requester")],
                title=title,
                body=note if note and not stale else None,
                dedupe_key=f"access.request_decided:{req.id}",
                action_url=action_url,
                actions=([{"id": "open", "label": "Open", "kind": "link", "url": action_url}] if action_url else ()),
                metadata=metadata,
            )
        )

    async def emit_access_request_release_failed(self, req: Any, *, label: str) -> NotificationItem | None:
        """Tell the Agent Owner that an approved request's release did not go live
        (candidate rejected, stale or withdrawn, or a release without the requester)."""
        if not self.enabled or not req.owner_id:
            return None
        who = await self._user_display(req.requester_id)
        return await self._emit_item(
            NotificationDraft(
                type="access_request_release_failed",
                subject=NotificationSubject(req.kind, req.resource_id, team_id=req.team_id, owner_user_id=req.owner_id),
                recipients=[(req.owner_id, "owner")],
                title="An approved access request did not take effect",
                body=f"The release adding {who} to “{label}” did not go live",
                dedupe_key=f"access.release_failed:{req.id}",
                action_url="/settings/access-requests/",
                metadata={"requestId": req.id, "requesterName": who, "resourceLabel": label},
            )
        )

    async def emit_access_request_expired(self, req: Any) -> NotificationItem | None:
        """Tell the requester a pending request expired (14 days without a decision).

        Sent for real and shadow rows alike: the requester must not be able to tell a missing
        resource from an unanswered request. No resource label, no approver identity."""
        if not self.enabled:
            return None
        return await self._emit_item(
            NotificationDraft(
                type="access_request_expired",
                subject=NotificationSubject(req.kind, req.resource_id, team_id=req.team_id, owner_user_id=req.requester_id),
                recipients=[(req.requester_id, "requester")],
                title=ACCESS_REQUEST_NO_LONGER_ACTIVE,
                body=None,
                dedupe_key=f"access.request_expired:{req.id}",
                metadata={"requestId": req.id, "outcome": "expired"},
            )
        )

    async def emit_access_request_escalated(self, req: Any, *, label: str, recipients: set[str]) -> NotificationItem | None:
        """72h without an owner decision → ask team owner/team_admin to decide (D5)."""
        if not self.enabled:
            return None
        targets = sorted(uid for uid in recipients if uid)
        if not targets:
            return None
        who = await self._user_display(req.requester_id)
        action_url = "/settings/access-requests/"
        item = await self._emit_item(
            NotificationDraft(
                type="access_requested_escalated",
                subject=NotificationSubject(req.kind, req.resource_id, team_id=req.team_id),
                recipients=[(uid, "approver") for uid in targets],
                title="An access request is waiting for a decision",
                body=f"{who} requested access to “{label}” and the owner has not responded for 72 hours",
                dedupe_key=f"access.escalated:{req.id}",
                action_url=action_url,
                actions=[{"id": "review", "label": "Review", "kind": "link", "url": action_url}],
                metadata={
                    "requestId": req.id,
                    "kind": req.kind,
                    "resourceId": req.resource_id,
                    "requesterName": who,
                    "resourceLabel": label,
                },
                actor_user_id=req.requester_id,
            )
        )
        if item is None or item.state != "open":
            return None
        return item

    async def refresh_access_digest(self, *, team_id: str, approver_id: str, pending_request_ids: list[str], threshold: int) -> None:
        """Keep one ``access_requests_digest`` item per approver while they have ``threshold`` or
        more pending requests; below it, resolve the digest and make sure every remaining
        pending request item is delivered to them individually again."""
        if not self.enabled or not approver_id:
            return
        dedupe_key = f"access_requests_digest:{approver_id}"
        count = len(pending_request_ids)
        if count >= threshold:
            await self.emit(
                NotificationDraft(
                    type="access_requests_digest",
                    subject=NotificationSubject(None, None, team_id=team_id),
                    recipients=[(approver_id, "approver")],
                    title=f"You have {count} access requests pending",
                    body=None,
                    dedupe_key=dedupe_key,
                    action_url="/settings/access-requests/",
                    actions=[{"id": "review", "label": "Review", "kind": "link", "url": "/settings/access-requests/"}],
                    metadata={"count": count},
                )
            )
            return
        await self.store.resolve_by_dedupe(team_id=team_id, dedupe_key=dedupe_key, reason="below_threshold")
        for request_id in pending_request_ids:
            await self.store.add_recipients_to_open(
                team_id=team_id,
                dedupe_key=f"access.requested:{request_id}",
                recipients=[(approver_id, "approver")],
            )

    async def emit_access_admin_decided(self, req: Any, owner_id: str, label: str) -> None:
        """When an team-admin (not the owner) decides, tell the owner their resource's
        sharing was changed on their behalf."""
        if not self.enabled or not owner_id or req.decided_by == owner_id:
            return
        who = await self._user_display(req.decided_by or "")
        verb = "通过" if req.status == "approved" else "拒绝"
        await self.emit(
            NotificationDraft(
                type="access_request_admin_decided",
                subject=NotificationSubject(req.kind, req.resource_id, team_id=req.team_id, owner_user_id=owner_id),
                recipients=[(owner_id, "owner")],
                title="管理员代你处理了访问申请",
                body=f"管理员 {who} 已{verb}一项对「{label}」的访问申请",
                dedupe_key=f"access.admin_decided:{req.id}",
                action_url="/settings/access-requests/",
                metadata={
                    "requestId": req.id,
                    "adminName": who,
                    "resourceLabel": label,
                    "decision": "approved" if req.status == "approved" else "rejected",
                },
                actor_user_id=req.decided_by,
            )
        )

    async def resolve_access_requested(
        self,
        team_id: str,
        request_id: str,
        *,
        reason: str,
    ) -> NotificationItem | None:
        if not self.enabled:
            return None
        # The 72h escalation item closes together with the request item.
        await self.store.resolve_by_dedupe(
            team_id=team_id,
            dedupe_key=f"access.escalated:{request_id}",
            reason=reason,
        )
        return await self.store.resolve_by_dedupe(
            team_id=team_id,
            dedupe_key=f"access.requested:{request_id}",
            reason=reason,
        )

    # ── Reconciliation ────────────────────────────────────────────────────

    async def reconcile_approval_required(
        self,
        approval_store: ApprovalStore,
        *,
        limit: int = 500,
    ) -> int:
        if not self.enabled:
            _metric_inc(
                "bowhead_notification_reconcile_total",
                labels={"type": "approval.required", "outcome": "disabled"},
            )
            return 0
        items = await self.store.list_open_required_approval_items(limit=limit)
        resolved = 0
        for item in items:
            approval_id = item.subject_id or str(item.metadata.get("approvalId") or "")
            if not approval_id:
                continue
            approval = await approval_store.get(approval_id)
            if approval is None or approval.status == "pending":
                continue
            resolved_item = await self.store.resolve_by_dedupe(
                team_id=item.team_id,
                dedupe_key=item.dedupe_key,
                reason=f"reconciled:{approval.status}",
            )
            if resolved_item is not None:
                resolved += 1
        if resolved:
            _metric_inc(
                "bowhead_notification_reconcile_total",
                labels={"type": "approval.required", "outcome": "resolved"},
                value=float(resolved),
            )
        return resolved

    async def reconcile_open_actions(
        self,
        checks: Mapping[str, Callable[[NotificationItem], Awaitable[str | None]]],
        *,
        limit: int = 500,
    ) -> dict[str, int]:
        """Close open action items whose subject no longer asks for anything.

        ``checks`` maps a type to a reader of its subject's current state: it returns the reason to
        close the item, or None to keep it. A resolve call missed by a producer (a crash between
        the state change and the notification, a path that never closed it) then cannot keep a
        badge lit indefinitely. A failing check keeps its item and the sweep goes on.
        """
        if not self.enabled or not checks:
            return {}
        resolved: dict[str, int] = {}
        for item in await self.store.list_open_items(types=checks.keys(), limit=limit):
            try:
                reason = await checks[item.type](item)
            except Exception as exc:
                logger.warning("notification.reconcile_check_failed type=%s id=%s err=%s", item.type, item.id, exc)
                continue
            if not reason:
                continue
            if await self.store.resolve_by_dedupe(team_id=item.team_id, dedupe_key=item.dedupe_key, reason=f"reconciled:{reason}"):
                resolved[item.type] = resolved.get(item.type, 0) + 1
        for type_, count in resolved.items():
            _metric_inc(
                "bowhead_notification_reconcile_total",
                labels={"type": type_, "outcome": "resolved"},
                value=float(count),
            )
        return resolved

    async def backfill_pending_approvals(
        self,
        approval_store: ApprovalStore,
        *,
        limit: int = 500,
    ) -> int:
        if not self.enabled:
            _metric_inc(
                "bowhead_notification_backfill_total",
                labels={"type": "approval.required", "outcome": "disabled"},
            )
            return 0
        approvals = await approval_store.list_pending(limit=limit)
        emitted = 0
        for approval in approvals:
            latest = await approval_store.get(approval.id)
            if latest is None or latest.status != "pending":
                continue
            if await self.emit_approval_required(latest) is not None:
                emitted += 1
        if emitted:
            _metric_inc(
                "bowhead_notification_backfill_total",
                labels={"type": "approval.required", "outcome": "emitted"},
                value=float(emitted),
            )
        return emitted

    # ── Scheduled jobs / Routines (notification P2 `scheduled.*`) ──────────────────

    async def _resolve_team_from_job(self, job: Any) -> str | None:
        """The Team that owns a scheduled job: its workspace's Team, as the run's access context uses."""
        return await self.teams.workspace_team(job.workspace_id, subject="scheduled_job", subject_id=job.id)

    def _job_subject(self, job: Any, team_id: str | None = None) -> NotificationSubject:
        """A job's items: its workspace's Team; without one, only the owner, under their own namespace."""
        if team_id:
            return NotificationSubject("scheduled_job", job.id, team_id=team_id, owner_user_id=job.user_id)
        return NotificationSubject("scheduled_job", job.id, workspace_id=job.workspace_id, owner_user_id=job.user_id)

    async def emit_scheduled_failed(self, job: Any, _error: str | None) -> NotificationItem | None:
        """Notify the job owner a scheduled run failed. Per-job dedupe collapses consecutive failures into
        one (badge-counted) item; cleared on recovery via emit_scheduled_recovered."""
        if not self.enabled:
            return None
        action_url = "/team/settings/scheduled/"
        # Run errors are rendered from finite i18n codes on the destination page.
        # Notification records must not snapshot exception prose or legacy raw rows.
        return await self._emit_item(
            NotificationDraft(
                type="scheduled.failed",
                subject=self._job_subject(job),
                recipients=[(job.user_id, "owner")],
                title="定时任务执行失败",
                body=f"「{job.name}」执行失败，请打开定时任务查看原因。",
                dedupe_key=f"scheduled.failed:{job.id}",
                action_url=action_url,
                actions=[{"id": "view", "label": "查看", "kind": "link", "url": action_url}],
                metadata={"jobId": job.id, "jobName": job.name, "failureCount": job.failure_count},
            )
        )

    async def emit_scheduled_paused(self, job: Any, *, reason: str = "repeated_failures") -> NotificationItem | None:
        """Notify owner + team admins a job auto-paused (action required).

        ``reason``（credential-readiness D3）：
        - ``repeated_failures``（默认）——既有行为，连续失败自动暂停，per-job 一条。
        - ``team_model_unconfigured``——团队未配置模型。拆两条 item：admin 侧按
          **团队**折叠（同团队 N 个任务只刷一条，PRD Q8）并带模型设置直达；owner
          侧 per-job 告知（owner 常无权修团队配置，不给死链接，PRD 6.0.3 ①）。
        - ``scheduled_recipe_lost`` / ``_changed`` / ``_rejected``（scheduled-recipe-runs
          S3 · FR-5.6）——绑定的流程失效。走 ``_PAUSED_RECIPE_COPY`` 的专用文案。
        """
        if not self.enabled:
            return None
        # Administrators come only from the job's own Team. When the workspace has no Team row the
        # item still reaches the owner, but no Team's administrators are added.
        admin_team_id = await self._resolve_team_from_job(job)
        if reason == "team_model_unconfigured":
            return await self._emit_scheduled_paused_model_unconfigured(job, admin_team_id=admin_team_id)
        recipients: list[tuple[str, str | None]] = [(job.user_id, "owner")]
        if admin_team_id:
            admin_ids = await self.user_store.list_team_users_by_roles(
                admin_team_id,
                {"owner", "team_admin"},
                include_creator=True,
            )
            recipients.extend((uid, "admin") for uid in admin_ids if uid != job.user_id)
        action_url = "/team/settings/scheduled/"
        title, body = _paused_copy(job, reason)
        return await self._emit_item(
            NotificationDraft(
                type="scheduled.paused",
                subject=NotificationSubject("scheduled_job", job.id, team_id=admin_team_id, owner_user_id=job.user_id),
                recipients=recipients,
                title=title,
                body=body,
                dedupe_key=f"scheduled.paused:{job.id}",
                action_url=action_url,
                actions=[{"id": "view", "label": "查看", "kind": "link", "url": action_url}],
                metadata={"jobId": job.id, "jobName": job.name, "reason": reason},
            )
        )

    async def _emit_scheduled_paused_model_unconfigured(
        self,
        job: Any,
        *,
        admin_team_id: str | None,
    ) -> NotificationItem | None:
        """credential-readiness D3：团队未配模型导致的暂停——两条 item 按受众分叉。

        单条 item 只有一个 body，无法对不同收件人说不同的话（管理员要「去配置」、
        owner 要「等管理员」），故拆两条。admin 侧 dedupe 按团队折叠：同团队第 N 个
        任务暂停只 reopen 同一条，不轰炸（Q8）。文案陈述资源需求而非否定角色（Q9）。

        The administrator item exists only when the job's own Team is known (``admin_team_id``);
        otherwise only the owner is told.
        """
        admin_ids: set[str] = set()
        admin_item: NotificationItem | None = None
        if admin_team_id:
            admin_ids = set(
                await self.user_store.list_team_users_by_roles(
                    admin_team_id,
                    {"owner", "team_admin"},
                    include_creator=True,
                )
            )
            # ① admin item: actionable, links to model settings; collapsed per team.
            admin_item = await self._emit_item(
                NotificationDraft(
                    type="scheduled.paused",
                    subject=NotificationSubject("team", admin_team_id, team_id=admin_team_id),
                    recipients=[(uid, "admin") for uid in sorted(admin_ids)],
                    title="定时任务已暂停：团队还未配置模型",
                    body="团队还未配置模型, 相关定时任务已自动暂停; 配置后请到定时任务页恢复",
                    dedupe_key=f"scheduled.paused:model:{admin_team_id}",
                    action_url="/team/settings/llm/",
                    actions=[{"id": "configure", "label": "去配置模型", "kind": "link", "url": "/team/settings/llm/"}],
                    metadata={"jobId": job.id, "jobName": job.name, "reason": "team_model_unconfigured"},
                )
            )
        # ② owner item: informational, per-job; owners often lack model configuration rights, so omit dead settings links.
        owner_item = admin_item
        if admin_item is None or job.user_id not in admin_ids:
            action_url = "/team/settings/scheduled/"
            owner_item = await self._emit_item(
                NotificationDraft(
                    type="scheduled.paused",
                    subject=NotificationSubject("scheduled_job", job.id, team_id=admin_team_id, owner_user_id=job.user_id),
                    recipients=[(job.user_id, "owner")],
                    title="定时任务已自动暂停",
                    body=f"「{job.name}」已暂停: 该功能需要团队模型配置, 需团队管理员配置后可恢复",
                    dedupe_key=f"scheduled.paused:model:{job.id}:owner",
                    action_url=action_url,
                    actions=[{"id": "view", "label": "查看", "kind": "link", "url": action_url}],
                    metadata={"jobId": job.id, "jobName": job.name, "reason": "team_model_unconfigured"},
                )
            )
        _emit_metric("scheduled.paused", "model_unconfigured")
        return owner_item

    async def emit_scheduled_recovered(self, job: Any, run_id: str) -> NotificationItem | None:
        """A job succeeded after prior failures: resolve the open failed/paused items (clears the badge)
        and drop an informational (P3, non-badge) recovered notice to the owner."""
        if not self.enabled:
            return None
        subject_team_id = await self._resolve_team_from_job(job)
        for closing_team_id in await self.teams.closing_teams(subject_team_id, job.user_id):
            for dedupe_key in (f"scheduled.failed:{job.id}", f"scheduled.paused:{job.id}"):
                await self.store.resolve_by_dedupe(team_id=closing_team_id, dedupe_key=dedupe_key, reason="recovered")
        return await self._emit_item(
            NotificationDraft(
                type="scheduled.recovered",
                subject=self._job_subject(job, subject_team_id),
                recipients=[(job.user_id, "owner")],
                title="定时任务已恢复",
                body=f"「{job.name}」已重新成功执行",
                dedupe_key=f"scheduled.recovered:{job.id}:{run_id}",
                action_url="/team/settings/scheduled/",
                metadata={"jobId": job.id, "jobName": job.name},
            )
        )

    # ── Memory ────────────────────────────────────────────────────────────

    async def emit_memory_candidates(
        self,
        *,
        user_id: str,
        created_count: int,
        preview: str = "",
    ) -> NotificationItem | None:
        """memory-revive S1: dreaming 蒸馏出新候选后的兜底通知（badge + 铃铛条目）。

        chat 内联卡是主送达面（done 后 REST 轮询）；本条目兜接轮询窗口外才看到的
        用户 + 承接「候选会过期」的可见性。per-user dedupe：连续多次蒸馏折叠成一条
        （registry ``renew``：新一批换正文、刷新时间并重新标成未读），不做 per-candidate 轰炸。
        """
        if not self.enabled or not user_id or created_count <= 0:
            return None
        action_url = MEMORY_SETTINGS_PATH
        body = f"从最近的对话里学到 {created_count} 条建议，去确认要不要记住"
        if preview:
            body = f"{body}：{preview[:60]}"
        return await self._emit_item(
            NotificationDraft(
                type="memory.candidate",
                subject=NotificationSubject("memory_candidates", user_id, owner_user_id=user_id),
                recipients=[(user_id, "owner")],
                title="有新的记忆建议待确认",
                body=body,
                dedupe_key=f"memory.candidate:{user_id}",
                action_url=action_url,
                actions=[{"id": "review", "label": "去确认", "kind": "link", "url": action_url}],
                metadata={"createdCount": created_count, "preview": preview[:60]},
            )
        )

    async def emit_memory_candidates_expiring(
        self,
        *,
        user_id: str,
        expiring_count: int,
    ) -> NotificationItem | None:
        """S2 C3: pending 候选即将过期（>23d，GC 30d auto-reject 前）的预告。

        复用 `memory.candidate` action 类型与 per-user dedupe_key（badge/resolve 逻辑
        零改）——只换标题/正文成「即将过期」。「一生一次」由 candidate 侧的
        `expiry_warned_at` 标记保证（GC 只对未标记候选调本方法），本条目 dedupe 是
        per-user 折叠、与 per-candidate 标记正交。
        """
        if not self.enabled or not user_id or expiring_count <= 0:
            return None
        action_url = MEMORY_SETTINGS_PATH
        return await self._emit_item(
            NotificationDraft(
                type="memory.candidate",
                subject=NotificationSubject("memory_candidates", user_id, owner_user_id=user_id),
                recipients=[(user_id, "owner")],
                title="记忆建议即将过期",
                body=f"有 {expiring_count} 条记忆建议即将过期（超过 30 天未处理会自动忽略），去看看要不要记住",
                dedupe_key=f"memory.candidate:{user_id}",
                action_url=action_url,
                actions=[{"id": "review", "label": "去确认", "kind": "link", "url": action_url}],
                metadata={"expiringCount": expiring_count},
            )
        )

    async def resolve_memory_candidates(self, *, user_id: str) -> None:
        """review-fix P1（memory-revive S1）: 用户 pending 候选清零时解掉 badge 条目。

        兄弟 action 类型全有 resolve 闭环（approval 决策 / access decided /
        scheduled recovered）,缺这条会让铃铛「需处理」永久红——action 计数只看
        state='open',与 read_at 无关,mark_all_read 清不掉。调用方:decide 路由
        （approve/reject/bulk）与 GC auto-reject 之后,均在确认 pending==0 时。
        """
        if not self.enabled or not user_id:
            return
        team_id = await self.teams.owner_namespace(user_id)
        if not team_id:
            return
        await self.store.resolve_by_dedupe(
            team_id=team_id,
            dedupe_key=f"memory.candidate:{user_id}",
            reason="decided",
        )

    # ── Routine results and waiting runs ──────────────────────────────────

    async def emit_routine_delivered(
        self,
        job: Any,
        *,
        run_id: str,
        session_id: str,
        summary: str,
        email_user_ids: Sequence[str] = (),
    ) -> NotificationItem | None:
        """Phase B opt-in success delivery: an in-app (P3, non-badge → Updates tab) notice that a routine
        produced a result, with a deep link back. Pairs with the external email delivery.

        ``email_user_ids`` (FEATURE_NOTIFICATION_DELIVERY): Team members to email the result; the
        delivery worker sends, retries and records each outcome for the run history."""
        if not self.enabled:
            return None
        action_url = f"/?session={session_id}"
        return await self._emit_item(
            NotificationDraft(
                type="routine.delivered",
                subject=self._job_subject(job),
                recipients=[(job.user_id, "owner")],
                # Language-neutral stored title: the frontend localizes from metadata.jobName
                # (notification-copy.ts `routine_delivered_title`); rows written before this keep their title.
                title=str(job.name or ""),
                body=summary,
                dedupe_key=f"routine.delivered:{job.id}:{run_id}",
                action_url=action_url,
                actions=[{"id": "view", "label": "查看完整结果", "kind": "link", "url": action_url}],
                metadata={"jobId": job.id, "jobName": job.name, "runId": run_id, "sessionId": session_id},
                email_delivery_to=email_user_ids,
            )
        )

    @staticmethod
    def _needs_input_dedupe_key(job: Any, run_id: str) -> str:
        """挂起通知的 dedupe key = run 身份。

        同一次挂起被重复 emit（tick 重入 / 重试）折叠成一条不刷屏; 换一次挂起就是新条目,
        上一条由 `resolve_routine_needs_input` 关掉。emit 与 resolve 共用这一处拼装, 免得
        两侧字符串漂移后 badge 永远关不掉（这类 bug 全绿也逮不到）。
        """
        return f"routine.needs_input:{job.id}:{run_id}"

    async def emit_routine_needs_input(
        self,
        job: Any,
        *,
        run_id: str,
        session_id: str | None,
        question_hint: str | None = None,
    ) -> NotificationItem | None:
        """定时任务跑到歧义点挂起等人回答（scheduled-recipe-runs S3 · FR-5.6）。

        **仅 in-app、仅 owner**：只有 owner 知道自己任务的口径, 抄送管理员既答不了也是噪音;
        不走邮件——挂起是「回来答一句」, 不是事故。

        **正文固定句式, 不带 clarification 原文**（PRD T4）：问题文本由绑定的流程操纵, 而
        通知是产品口吻 + 深链的高信任面, 原样复述等于替攻击者递话。原文只在 session 内
        clarification 卡的既有不可信语境里渲染。`question_hint` 默认关闭; 即使传了也只经
        `_sanitize_question_hint` 落 metadata 并标注来源, 由 FE 决定是否以「引用外部内容」
        的样式展示——正文任何情况下都不带。
        """
        if not self.enabled:
            return None
        # Deep-link to the card's session (same as routine.delivered). Missing session_id falls back to the task page,
        # rather than a dead `/?session=None` link opening a blank page; both callers' RunOutcome
        # allow session_id=None.
        action_url = f"/?session={session_id}" if session_id else "/team/settings/scheduled/"
        metadata: dict[str, Any] = {
            "jobId": job.id,
            "jobName": job.name,
            "runId": run_id,
            "sessionId": session_id,
        }
        hint = _sanitize_question_hint(question_hint)
        if hint:
            metadata["questionHint"] = hint
            metadata["questionHintSource"] = "bound_workflow"  # Source marker: not product copy; do not render in the product's voice.
        return await self._emit_item(
            NotificationDraft(
                type="routine.needs_input",
                subject=self._job_subject(job),
                recipients=[(job.user_id, "owner")],
                title="定时任务需要你补充信息",
                body=f"「{job.name}」需要你补充一处口径才能继续, 点开回答一句即可。",
                dedupe_key=self._needs_input_dedupe_key(job, run_id),
                action_url=action_url,
                actions=[{"id": "answer", "label": "去回答", "kind": "link", "url": action_url}],
                metadata=metadata,
            )
        )

    async def emit_routine_alert(
        self,
        job: Any,
        *,
        run_id: str,
        session_id: str | None,
        level: str,
    ) -> tuple[str, NotificationItem | None]:
        """A scheduled task's own Agent flags this run for its owner (``routine_notify``).

        Returns ``(status, item)``, status one of ``queued``, ``duplicate`` (this run already
        alerted), ``rate_limited`` (5 alerts for the task in the last 24 hours) or ``disabled``.

        The Agent's only input is the level, which becomes the priority. Title and body are fixed
        product copy naming the task: model-written text never enters a notification (PRD FR12).
        The owner is the only recipient.
        """
        if not self.enabled:
            return "disabled", None
        priority = ROUTINE_ALERT_PRIORITIES.get(level)
        if priority is None:
            raise ValueError(f"unknown routine alert level: {level}")
        dedupe_key = f"routine.alert:{job.id}:{run_id}"
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        recent = await self.store.count_recent(type="routine.alert", subject_type="scheduled_job", subject_id=job.id, since=since)
        if recent >= ROUTINE_ALERT_DAILY_LIMIT:
            existing = await self._open_routine_alert(job, run_id)
            if existing is not None:
                return "duplicate", existing
            _emit_metric("routine.alert", "rate_limited")
            return "rate_limited", None
        action_url = f"/?session={session_id}" if session_id else "/team/settings/scheduled/"
        result = await self.emit(
            NotificationDraft(
                type="routine.alert",
                subject=self._job_subject(job),
                recipients=[(job.user_id, "owner")],
                title="定时任务提醒",
                body=f"「{job.name}」在这次运行中标记了一条需要你留意的结果。",
                dedupe_key=dedupe_key,
                action_url=action_url,
                actions=[{"id": "view", "label": "查看", "kind": "link", "url": action_url}],
                metadata={"level": level, "jobId": job.id, "jobName": job.name, "runId": run_id, "sessionId": session_id},
                priority=priority,
                # Urgent: one email to the owner, sent and retried by the delivery worker.
                email_delivery_to=[job.user_id] if level == "urgent" else (),
            )
        )
        if result is None:
            return "disabled", None
        return ("queued" if result.new_occurrence else "duplicate"), result.item

    async def _open_routine_alert(self, job: Any, run_id: str) -> NotificationItem | None:
        for team_id in await self.teams.closing_teams(await self._resolve_team_from_job(job), job.user_id):
            item = await self.store.get_open_by_dedupe(team_id=team_id, dedupe_key=f"routine.alert:{job.id}:{run_id}")
            if item is not None:
                return item
        return None

    async def routine_alert_raised(self, job: Any, *, run_id: str) -> bool:
        """Whether this run flagged something for its owner (delivery mode "only when flagged")."""
        return await self._open_routine_alert(job, run_id) is not None

    async def resolve_scheduled_job_notifications(
        self,
        job: Any,
        *,
        reason: str = "job_deleted",
    ) -> int:
        """任务本身没了 → 关掉它名下所有还开着的通知（挂起待办 + 暂停告知）。

        不关的话：铃铛留着「定时任务需要你补充信息」，点进去的深链指向一个已无 job/run 归属的
        会话；`scheduled.paused` 那条还会同时挂在全体管理员的待办里。**主体消失是终态**，
        比逐条 dedupe 更根本，所以按 subject 一次关完（run 级 dedupe key 带 run_id，删除时
        根本不知道有多少条）。best-effort：删除已经成功了，通知没关干净不该把 204 变成 5xx。
        """
        if not self.enabled:
            return 0
        closed = 0
        for team_id in await self.teams.closing_teams(await self._resolve_team_from_job(job), job.user_id):
            closed += await self.store.resolve_open_by_subject(
                team_id=team_id,
                subject_type="scheduled_job",
                subject_id=job.id,
                reason=reason,
            )
        return closed

    async def resolve_routine_needs_input(
        self,
        job: Any,
        *,
        run_id: str,
        reason: str = "answered",
    ) -> NotificationItem | None:
        """run 离开 awaiting 时关掉挂起通知——badge 闭环（FR-5.6）。

        action 计数只看 ``state='open'``, 与已读无关: 不 resolve 就是铃铛永久红, 用户学会
        无视铃铛后挂起机制形同虚设。调用方 = 所有离开 awaiting 的 CAS 转移**赢家**
        （reconciliation 收尾 / 过期 / run-now 换新）。返 None = 没有 open 条目可关
        （重复调用或 CAS 输家）——幂等且不抛, 调用方无需先查状态。
        """
        if not self.enabled:
            return None
        for team_id in await self.teams.closing_teams(await self._resolve_team_from_job(job), job.user_id):
            item = await self.store.resolve_by_dedupe(
                team_id=team_id,
                dedupe_key=self._needs_input_dedupe_key(job, run_id),
                reason=reason,
            )
            if item is not None:
                return item
        return None

    # ── Feedback ──────────────────────────────────────────────────────────

    async def emit_feedback_candidate_receipt(
        self,
        *,
        team_id: str,
        candidate_id: str,
        target_kind: str,
        final_state: str,
        decided_at: str,
        recipient_ids: list[str],
    ) -> NotificationItem | None:
        """反馈候选终态回执（agent-feedback-loop 批 2b-i，tech-design §2.4）。

        内容**最小化是安全契约**：只含 {候选短 id, 类型, 终态, 时间}——不含
        proposed 正文 / rubric / decide_note / 决策人身份。通知会随会话分享与
        导出面外泄（report 分享＝session 读已实证），正文永远走认证 API。

        A repeat (the sweep backfilling expiration receipts) never makes a handled receipt unread
        again: the type is registered ``once`` and not reopenable.
        """
        if not self.enabled:
            _emit_metric("feedback_candidate_receipt", "disabled")
            return None
        if not recipient_ids:
            _emit_metric("feedback_candidate_receipt", "no_recipients")
            return None
        kind_label = {
            "agent_def": "Agent 定义",
            "team_skill": "团队技能",
            "semantic": "业务口径",
            "tool_source": "工具连接",
            "new_asset": "新资产",
            "retire": "退役",
        }.get(target_kind, target_kind)
        state_label = {
            "accepted": "已采纳",
            "rejected": "未采纳",
            "expired": "已过期",
        }.get(final_state, final_state)
        # 116 dogfood: ISO microseconds/timezone are too rough for user display; receipts need only minute precision.
        when = decided_at
        with contextlib.suppress(ValueError):
            when = datetime.fromisoformat(decided_at).strftime("%Y-%m-%d %H:%M")
        return await self._emit_item(
            NotificationDraft(
                type="feedback_candidate_receipt",
                subject=NotificationSubject("feedback_candidate", candidate_id, team_id=team_id),
                recipients=[(user_id, "feedback_contributor") for user_id in recipient_ids],
                title=f"你参与的建议{state_label}",
                body=f"建议 {candidate_id} · {kind_label} · {state_label} · {when}",
                dedupe_key=f"feedback.candidate.receipt:{candidate_id}",
                metadata={
                    "candidateId": candidate_id,
                    "finalState": final_state,
                    "targetKind": target_kind,
                    "decidedAt": decided_at,
                },
            )
        )

    # ── Sharing ───────────────────────────────────────────────────────────

    async def emit_content_shared(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        actor_name: str,
        kind: str,
        resource_id: str,
        resource_label: str,
        action_url: str,
        grant_id: str,
        recipient_ids: Sequence[str],
    ) -> NotificationItem | None:
        """A direct share reached these people (PRD §5.4: direct shares only; Team-wide changes and revocations do not notify).

        One item per grant: sharing the same resource with another principal is a new event with
        its own time, and must not rewrite the item an earlier recipient already has. The sharer
        never notifies themselves.
        """
        recipients = [(uid, None) for uid in recipient_ids if uid and uid != actor_user_id]
        if not recipients:
            return None
        return await self._emit_item(
            NotificationDraft(
                type="content_shared",
                subject=NotificationSubject(kind, resource_id, team_id=team_id),
                recipients=recipients,
                title=f"{actor_name} 分享了「{resource_label}」",
                body=None,
                dedupe_key=f"share:{kind}:{resource_id}:{grant_id}",
                action_url=action_url,
                metadata={
                    "kind": kind,
                    "resource_id": resource_id,
                    "actorName": actor_name,
                    "resourceLabel": resource_label,
                },
                actor_user_id=actor_user_id,
            )
        )

    # ── Team Skill approval ───────────────────────────────────────────────

    async def emit_skill_approval_submitted(
        self,
        *,
        team_id: str,
        skill_id: str,
        skill_label: str,
        submission_key: str,
        actor_user_id: str,
        actor_name: str,
    ) -> NotificationItem | None:
        """Ask the Team's administrators to review a submitted Skill: an action item until decided.

        ``submission_key`` identifies this submission (its submit time). A newer submission of the
        same Skill supersedes an older open one, so administrators see one item per Skill.
        """
        if not self.enabled:
            return None
        admin_ids = await self.user_store.list_team_users_by_roles(team_id, {"owner", "team_admin"})
        recipients = [(uid, None) for uid in admin_ids if uid != actor_user_id]
        if not recipients:
            return None
        dedupe_key = f"skill_approval:submitted:{skill_id}:{submission_key}"
        existing = await self.store.get_open_by_dedupe(team_id=team_id, dedupe_key=dedupe_key)
        if existing is None:
            await self.resolve_skill_approval_submitted(team_id=team_id, skill_id=skill_id, reason="resubmitted")
        return await self._emit_item(
            NotificationDraft(
                type="skill_approval_submitted",
                subject=NotificationSubject("team_skill", skill_id, team_id=team_id),
                recipients=recipients,
                title=f"{actor_name} 提交了 skill「{skill_label}」待审批",
                body=None,
                dedupe_key=dedupe_key,
                action_url="/team/settings/workflows/",
                metadata={
                    "kind": "team_skill",
                    "resource_id": skill_id,
                    "event": "submitted",
                    "decision": None,
                    "actorName": actor_name,
                    "skillLabel": skill_label,
                    "reason": "",
                },
                actor_user_id=actor_user_id,
            )
        )

    async def emit_skill_approval_decided(
        self,
        *,
        team_id: str,
        skill_id: str,
        skill_label: str,
        author_user_id: str | None,
        actor_user_id: str,
        actor_name: str,
        decision: str,
        reason: str | None = None,
    ) -> NotificationItem | None:
        """Close the submission's action item and tell the author how it was decided."""
        if not self.enabled:
            return None
        await self.resolve_skill_approval_submitted(team_id=team_id, skill_id=skill_id, reason=decision)
        if not author_user_id or author_user_id == actor_user_id:
            return None
        note = (reason or "").strip()[:80]
        title = f"你的 skill「{skill_label}」已通过审批" if decision == "approved" else f"你的 skill「{skill_label}」被拒绝：{note}"
        return await self._emit_item(
            NotificationDraft(
                type="skill_approval_decided",
                subject=NotificationSubject("team_skill", skill_id, team_id=team_id, owner_user_id=author_user_id),
                recipients=[(author_user_id, None)],
                title=title,
                body=None,
                dedupe_key=f"skill_approval:decided:{skill_id}:{datetime.now(UTC).isoformat()}",
                action_url="/skills/",
                metadata={
                    "kind": "team_skill",
                    "resource_id": skill_id,
                    "event": "decided",
                    "decision": decision,
                    "actorName": actor_name,
                    "skillLabel": skill_label,
                    "reason": note,
                },
                actor_user_id=actor_user_id,
            )
        )

    async def resolve_skill_approval_submitted(self, *, team_id: str, skill_id: str, reason: str) -> int:
        """Close a Skill's open submission items (decided, withdrawn, deleted or superseded)."""
        if not self.enabled:
            return 0
        return await self.store.resolve_open_by_subject(
            team_id=team_id,
            subject_type="team_skill",
            subject_id=skill_id,
            reason=reason,
            types=["skill_approval_submitted"],
        )
