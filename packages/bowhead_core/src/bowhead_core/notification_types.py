"""The single definition of every notification type.

Every producer (service wrappers, the Agent outboxes) writes only registered types with a priority
the type allows. The badge set (`ACTION_REQUIRED_TYPES`), the write semantics on repeat
(`occurrence`, `reopen_resolved`) and the frontend's generated copy table all derive from here, so
a new type cannot reach users with a different meaning in each place.

`scripts/check_notification_types.py --write` regenerates
`packages/bowhead_frontend/lib/notification-types.generated.ts` from this module; `make lint` runs
its `--check`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Priority = Literal["p1", "p2", "p3"]
Occurrence = Literal["once", "update", "renew"]
Scope = Literal["team", "personal"]

PRIORITIES: frozenset[str] = frozenset({"p1", "p2", "p3"})
CHANNELS: frozenset[str] = frozenset({"browser", "email_escalation", "email_delivery"})


@dataclass(frozen=True)
class NotificationTypeSpec:
    """How one notification type is stored, counted and re-emitted.

    ``occurrence`` decides what a repeated emit of an open item does:

    - ``once``: nothing changes; recipients not yet on the item are added unread.
    - ``update``: content is replaced, ``created_at`` and every recipient's read/archive state stay.
      A run of the same event (consecutive failures) is one item showing when it started.
    - ``renew``: content is replaced, ``created_at`` moves to now and recipients become unread and
      unarchived again. The row carries the latest batch (memory suggestions).

    ``reopen_resolved`` decides whether an emit after the item was resolved starts a new occurrence
    (reopened, new time, recipients unread) or is ignored.

    ``scope="personal"`` marks types addressed only to the person whose own activity produced them;
    they live in that person's namespace, never carry a Team label and never switch Team.

    ``resolution`` names what closes an action item, for review and tests.
    """

    type: str
    category: str
    default_priority: Priority
    action_required: bool
    occurrence: Occurrence
    reopen_resolved: bool
    scope: Scope = "team"
    allowed_priorities: frozenset[str] = frozenset()
    resolution: str | None = None
    # Channels beyond the in-app list. "browser": an unread item of this type may be announced in
    # the open app and as a browser notification (FEATURE_NOTIFICATION_REACH), when its priority is
    # one of ``browser_priorities``.
    channels: frozenset[str] = frozenset()
    browser_priorities: frozenset[str] = frozenset({"p1"})
    # "email_escalation" (FEATURE_NOTIFICATION_DELIVERY): a fixed rule, no model. An item still open
    # and unread this many seconds after a new occurrence is emailed to each recipient, once.
    # "email_delivery": the type can carry an email its owner configured (a scheduled result, an
    # urgent task alert); the producer decides who, the delivery worker sends it.
    escalation_delay_s: int | None = None

    def __post_init__(self) -> None:
        allowed = self.allowed_priorities or frozenset({self.default_priority})
        if self.default_priority not in allowed or not allowed <= PRIORITIES:
            raise ValueError(f"invalid priorities for notification type {self.type}")
        object.__setattr__(self, "allowed_priorities", allowed)
        if self.action_required and not self.resolution:
            raise ValueError(f"action notification type {self.type} needs a documented resolution")
        if not self.channels <= CHANNELS:
            raise ValueError(f"unknown channel for notification type {self.type}")
        if ("email_escalation" in self.channels) != (self.escalation_delay_s is not None):
            raise ValueError(f"escalation channel and delay must come together for {self.type}")


class NotificationTypeError(ValueError):
    """An emit used an unregistered type or a priority its type does not allow."""

    def __init__(self, code: Literal["unregistered", "invalid_priority"], type_: str) -> None:
        super().__init__(f"{code}: {type_}")
        self.code = code
        self.type = type_


def _spec(
    type_: str,
    category: str,
    priority: Priority,
    *,
    occurrence: Occurrence = "once",
    reopen: bool = True,
    action: bool = False,
    scope: Scope = "team",
    allowed: frozenset[str] = frozenset(),
    resolution: str | None = None,
    browser: frozenset[str] | None = None,
    escalate_after_s: int | None = None,
    email_delivery: bool = False,
) -> NotificationTypeSpec:
    # Something waiting on a person may interrupt them when it is urgent (p1); updates never do,
    # unless a type says which of its priorities may (``browser``).
    channels = set({"browser"}) if action or browser else set()
    if escalate_after_s is not None:
        channels.add("email_escalation")
    if email_delivery:
        channels.add("email_delivery")
    return NotificationTypeSpec(
        type=type_,
        category=category,
        default_priority=priority,
        action_required=action,
        occurrence=occurrence,
        reopen_resolved=reopen,
        scope=scope,
        allowed_priorities=allowed,
        resolution=resolution,
        channels=frozenset(channels),
        browser_priorities=browser or frozenset({"p1"}),
        escalation_delay_s=escalate_after_s,
    )


_P1_P2 = frozenset({"p1", "p2"})
# Decision D1: someone blocked on a person is emailed by rule after 30 minutes still open and unread.
ESCALATION_DELAY_S = 1800

_SPECS: tuple[NotificationTypeSpec, ...] = (
    # ── Space assignment ──
    _spec(
        "space_assignment_requested",
        "space_assignment",
        "p1",
        action=True,
        resolution="NotificationService.resolve_space_assignment_request (first Space access)",
    ),
    _spec("space_assignment_ready", "space_assignment", "p2"),
    # ── Tool approvals ──
    _spec(
        "approval.required",
        "approval",
        "p1",
        reopen=False,
        action=True,
        escalate_after_s=ESCALATION_DELAY_S,
        resolution="NotificationService.resolve_approval_required; sweeper reconcile_approval_required",
    ),
    _spec("approval.submitted", "approval", "p3"),
    _spec("approval.approved", "approval", "p2"),
    _spec("approval.rejected", "approval", "p2"),
    _spec("approval.executed", "approval", "p2"),
    _spec("approval.execution_failed", "approval", "p1"),
    # ── Access requests ──
    _spec(
        "access_requested",
        "access_request",
        "p1",
        reopen=False,
        action=True,
        resolution="NotificationService.resolve_access_requested; sweeper reconciles requests no longer pending",
    ),
    # One item per request; a later outcome (approved after "will take effect soon") replaces its content.
    _spec("access_request_decided", "access_request", "p2", occurrence="update"),
    _spec("access_request_release_failed", "access_request", "p2", occurrence="update"),
    _spec("access_request_expired", "access_request", "p3"),
    _spec(
        "access_requested_escalated",
        "access_request",
        "p2",
        reopen=False,
        action=True,
        resolution="NotificationService.resolve_access_requested; sweeper reconciles requests no longer pending",
    ),
    _spec(
        "access_requests_digest",
        "access_request",
        "p1",
        occurrence="update",
        action=True,
        resolution="NotificationService.refresh_access_digest (below threshold)",
    ),
    _spec("access_request_admin_decided", "access_request", "p2"),
    # ── Scheduled tasks ──
    # Consecutive failures fold into one item that shows when the streak began.
    _spec(
        "scheduled.failed",
        "async_job",
        "p1",
        occurrence="update",
        action=True,
        resolution="NotificationService.emit_scheduled_recovered / resolve_scheduled_job_notifications; sweeper reconciles deleted tasks",
    ),
    _spec(
        "scheduled.paused",
        "async_job",
        "p1",
        occurrence="update",
        action=True,
        resolution="NotificationService.emit_scheduled_recovered / resolve_scheduled_job_notifications; sweeper reconciles tasks no longer paused",
    ),
    _spec("scheduled.recovered", "async_job", "p3", reopen=False),
    _spec("routine.delivered", "async_job", "p3", reopen=False, email_delivery=True),
    # Raised by the task's own Agent through ``routine_notify`` (FEATURE_ROUTINE_NOTIFY): the level it
    # chose is the priority (info p3, attention p2, urgent p1); attention and urgent may interrupt.
    _spec(
        "routine.alert",
        "async_job",
        "p2",
        reopen=False,
        allowed=frozenset({"p1", "p2", "p3"}),
        browser=frozenset({"p1", "p2"}),
        email_delivery=True,
    ),
    _spec(
        "routine.needs_input",
        "async_job",
        "p1",
        reopen=False,
        action=True,
        escalate_after_s=ESCALATION_DELAY_S,
        resolution="NotificationService.resolve_routine_needs_input; sweeper reconciles runs no longer awaiting input",
    ),
    # ── Memory ──
    # One row per user carries the latest batch of suggestions.
    _spec(
        "memory.candidate",
        "memory",
        "p2",
        occurrence="renew",
        action=True,
        scope="personal",
        resolution="NotificationService.resolve_memory_candidates (no pending suggestions left)",
    ),
    # ── Feedback ──
    # Never resolved; an expired-receipt backfill must not make a handled receipt unread again.
    _spec("feedback_candidate_receipt", "feedback", "p2", reopen=False),
    # ── Sharing and Team Skills ──
    _spec("content_shared", "share", "p2", reopen=False),
    _spec(
        "skill_approval_submitted",
        "share",
        "p2",
        reopen=False,
        action=True,
        resolution="NotificationService.resolve_skill_approval_submitted (decided, withdrawn, deleted); sweeper reconciles Skills no longer pending",
    ),
    _spec("skill_approval_decided", "share", "p2", reopen=False),
    # ── Agent versions (agent_version_notifications outbox) ──
    _spec("agent.version.proposal_received", "updates", "p2", reopen=False),
    _spec("agent.version.candidate_prepared", "updates", "p3", reopen=False),
    _spec("agent.version.result_ready", "updates", "p2", reopen=False),
    _spec("agent.version.result_blocked", "updates", "p1", reopen=False),
    _spec("agent.version.result_unknown", "updates", "p2", reopen=False),
    _spec("agent.version.candidate_stale", "updates", "p2", reopen=False),
    # ── Agent evaluation (agent_eval_product outbox) ──
    _spec(
        "agent.eval.regression_failed",
        "action",
        "p1",
        reopen=False,
        action=True,
        allowed=_P1_P2,
        resolution="Archived by the Owner after reviewing the Agent",
    ),
    # ── Agent releases (agent_release_outbox) ──
    _spec(
        "agent.release.approval_required",
        "action",
        "p2",
        reopen=False,
        action=True,
        allowed=_P1_P2,
        resolution="agent_release_outbox resolves the candidate's items on rejection, withdrawal, staleness or activation",
    ),
    _spec("agent.release.rejected", "updates", "p2", reopen=False),
    _spec("agent.release.withdrawn", "updates", "p2", reopen=False),
    _spec("agent.release.stale", "updates", "p2", reopen=False),
    _spec("agent.release.updated", "updates", "p2", reopen=False),
    _spec("agent.release.high_risk", "updates", "p1", reopen=False),
    _spec("agent.release.audience_contracted", "updates", "p1", reopen=False),
)

REGISTRY: MappingProxyType[str, NotificationTypeSpec] = MappingProxyType({spec.type: spec for spec in _SPECS})
if len(REGISTRY) != len(_SPECS):  # pragma: no cover - import-time guard against a duplicated entry
    raise RuntimeError("duplicate notification type registration")

ACTION_REQUIRED_TYPES: frozenset[str] = frozenset(spec.type for spec in _SPECS if spec.action_required)
BROWSER_TYPES: frozenset[str] = frozenset(spec.type for spec in _SPECS if "browser" in spec.channels)


def browser_types_by_priority() -> dict[str, frozenset[str]]:
    """Browser-channel types grouped by the priorities that may be announced (for one SQL filter)."""
    groups: dict[frozenset[str], set[str]] = {}
    for spec in _SPECS:
        if "browser" in spec.channels:
            groups.setdefault(spec.browser_priorities, set()).add(spec.type)
    out: dict[str, frozenset[str]] = {}
    for priorities, types in groups.items():
        for priority in priorities:
            out[priority] = out.get(priority, frozenset()) | frozenset(types)
    return out


def get_spec(type_: str) -> NotificationTypeSpec | None:
    return REGISTRY.get(type_)


def validate(type_: str, priority: str | None = None) -> NotificationTypeSpec:
    """Return the type's spec, raising `NotificationTypeError` for an unregistered type or a priority it does not allow."""
    spec = REGISTRY.get(type_)
    if spec is None:
        raise NotificationTypeError("unregistered", type_)
    if priority is not None and priority not in spec.allowed_priorities:
        raise NotificationTypeError("invalid_priority", type_)
    return spec
