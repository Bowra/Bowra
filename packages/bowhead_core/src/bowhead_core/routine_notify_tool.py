"""Lead tool for a scheduled task run to flag its result for the task owner (FEATURE_ROUTINE_NOTIFY).

A scheduled run happens while nobody watches. When the Agent finds something its owner should
see now (a threshold crossed, missing data, an anomaly), it calls ``routine_notify`` with a level:

- ``info``: an in-app notification only;
- ``attention``: also announced in an open Bowhead tab and as a browser notification;
- ``urgent``: also emailed to the owner's verified address, once. With recorded deliveries on, when
  the owner also receives the run's result email, that result email carries the alert instead of
  a second email (it waits up to ``ROUTINE_ALERT_EMAIL_HOLD_S`` for the run to deliver).

Boundaries:

- **Scheduled runs only.** The tool is registered only with the switch on, and the turn adapter
  hides it unless the Turn carries the server-set ``scheduled_run`` reference. A Turn without it
  is refused even if the tool is called.
- **Owner only.** The only recipient is the task owner; the Agent chooses no addresses.
- **No model text in the notification.** Title, body and email copy are fixed product text naming
  the task. The ``reason`` is returned in the tool result, so it appears only in the run's own
  conversation, where Agent output is already shown as such.
- **Bounded.** One alert per run and at most five per task in 24 hours. Past the limit nothing
  reaches the owner, but a task delivering only flagged runs still delivers the run's result.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Final

from bowhead_router.types import ToolSchema

from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME: Final = "routine_notify"
LEVELS: Final = ("info", "attention", "urgent")
REASON_MAX: Final = 280

# (run, item) -> None: email the owner about an urgent alert. Provided by the gateway.
UrgentEmailSender = Callable[[Any, Any], Awaitable[None]]
# run -> whether the run's result email goes to the owner. Provided by the gateway.
ResultEmailProbe = Callable[[Any], Awaitable[bool]]

_service_provider: Callable[[], Any | None] | None = None
_urgent_email: UrgentEmailSender | None = None
_result_email_reaches_owner: ResultEmailProbe | None = None


def routine_notify_enabled() -> bool:
    """FEATURE_ROUTINE_NOTIFY (default off). Rollout and rollback: docs/灰度功能清单.md."""
    return os.environ.get("FEATURE_ROUTINE_NOTIFY", "0").strip().lower() in {"1", "true", "yes"}


def _metric(status: str) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc("bowhead_routine_notify_total", labels={"status": status})
    except Exception:  # noqa: S110
        pass


def _schema() -> ToolSchema:
    return ToolSchema(
        name=TOOL_NAME,
        description=(
            # Offered only in a scheduled run, so state that context: a condition ("only in a scheduled
            # task run") read as "not this conversation" and the Agent refused (116 dogfood, 2026-11-09).
            "This Turn is a run of the owner's scheduled task: it runs unattended, nobody is watching this conversation, "
            "and this tool is available now. Call it to flag this run's result for the task owner when they should look now: "
            "a condition the task's instructions ask to be alerted about holds, a metric crossed the threshold the task watches, "
            "expected data is missing, or the result looks wrong. "
            "Only this call raises an alert: a helper judging that the result needs attention, or your answer saying "
            "the owner was alerted, sends nothing. "
            "Do not call it just to report that the run finished; results are delivered by the task's own settings. "
            "Choose the level: info (in-app only), attention (also a pop-up and browser notification), "
            "urgent (also an email to the owner; when it cannot wait or the instructions ask for it). "
            "The owner is the only recipient. The notification text is fixed and names the task; "
            "your reason is shown only in this run's conversation, so state the finding and the key numbers there. "
            "At most one alert per run. status=queued means it was sent; rate_limited or duplicate means nothing new was sent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": list(LEVELS), "description": "How urgently the owner should look."},
                "reason": {
                    "type": "string",
                    "maxLength": REASON_MAX,
                    "description": "One or two sentences on what needs the owner's attention; shown only in this run's conversation.",
                },
            },
            "required": ["level", "reason"],
            "additionalProperties": False,
        },
    )


async def _handler(args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, Any]:
    run = getattr(ctx, "scheduled_run", None)
    if run is None:
        _metric("unavailable")
        return {"ok": False, "status": "unavailable", "message": "routine_notify works only inside a scheduled task run."}
    level = str(args.get("level") or "")
    if level not in LEVELS:
        return {"ok": False, "status": "invalid_level", "message": f"level must be one of {', '.join(LEVELS)}."}
    reason = str(args.get("reason") or "").strip()[:REASON_MAX]
    service = _service_provider() if _service_provider is not None else None
    if service is None:
        _metric("disabled")
        return {"ok": False, "status": "disabled", "level": level, "reason": reason, "message": "Notifications are switched off."}
    job = _JobRef(run)
    # With recorded deliveries on, the urgent email is a delivery row the worker sends and retries.
    direct_email = not getattr(service, "delivery_enabled", False)
    extra: dict[str, Any] = {}
    if level == "urgent" and not direct_email:
        extra["hold_email"] = await _owner_gets_result_email(run)
    status, item = await service.emit_routine_alert(job, run_id=run.run_id, session_id=run.session_id, level=level, **extra)
    _metric(status)
    if status == "queued" and level == "urgent" and item is not None and _urgent_email is not None and direct_email:
        try:
            await _urgent_email(run, item)
        except Exception:
            # The in-app alert stands; the email is best effort until recorded deliveries exist.
            logger.warning("routine_notify.urgent_email_failed job=%s run=%s", run.job_id, run.run_id, exc_info=True)
    messages = {
        "queued": "The owner was notified.",
        "duplicate": "This run already notified the owner; nothing new was sent.",
        "rate_limited": (
            "This task already sent 5 alerts in the last 24 hours, so no alert was sent. "
            "The run's result is still delivered under the task's settings; do not say the owner was alerted."
        ),
        "disabled": "Notifications are switched off; nothing was sent.",
    }
    return {"ok": status == "queued", "status": status, "level": level, "reason": reason, "message": messages.get(status, "")}


UNRAISED_ALERT_NOTE: Final = (
    "This scheduled task delivers its result only when this run raises an alert, and this run has raised none, "
    "so the owner will receive nothing from it. Only a routine_notify call raises an alert: a helper judging that "
    "the result needs attention, or an answer saying the owner was alerted, sends nothing. If the task's instructions "
    "ask to be alerted and their condition holds, call routine_notify now with the level they ask for, then answer. "
    "Otherwise answer again without saying that anyone was alerted. Keep completed work; do not repeat it."
)


def unraised_alert_note(ctx: Any, invocations: Sequence[Any], tool_names: Iterable[str]) -> str | None:
    """The one-time correction for a Lead answer that ends an "only when flagged" run without an alert.

    In that mode a run without an alert delivers nothing, and on 116 (2026-11-10) the Lead
    twice wrote that it had alerted the owner after a helper judged the result urgent, without
    calling the tool. The final-answer gate returns this note once; the Lead then alerts or
    answers without the claim. Runs in "every run" mode are delivered anyway and are not checked.
    """
    run = getattr(ctx, "scheduled_run", None)
    if run is None or not getattr(run, "delivers_only_when_flagged", False) or TOOL_NAME not in set(tool_names):
        return None
    if any(getattr(inv, "tool_name", None) == TOOL_NAME for inv in invocations):
        return None
    return UNRAISED_ALERT_NOTE


async def _owner_gets_result_email(run: Any) -> bool:
    """Whether the run's result email goes to the owner, so it can carry the urgent alert."""
    if _result_email_reaches_owner is None:
        return False
    try:
        return await _result_email_reaches_owner(run)
    except Exception:
        # Unknown: email the alert now rather than wait for a result email that may not come.
        logger.warning("routine_notify.result_email_probe_failed job=%s run=%s", run.job_id, run.run_id, exc_info=True)
        return False


class _JobRef:
    """The scheduled-job shape `NotificationService` reads, built from the server-set run reference."""

    def __init__(self, run: Any) -> None:
        self.id = run.job_id
        self.user_id = run.owner_id
        self.workspace_id = run.workspace_id
        self.name = run.job_name


def register_routine_notify_tool(
    registry: ToolRegistry,
    *,
    service_provider: Callable[[], Any | None],
    urgent_email: UrgentEmailSender | None = None,
    result_email_reaches_owner: ResultEmailProbe | None = None,
) -> bool:
    """Register ``routine_notify`` when FEATURE_ROUTINE_NOTIFY is on; returns whether it did."""
    global _service_provider, _urgent_email, _result_email_reaches_owner  # noqa: PLW0603 -- startup binding, resolved at invocation time
    if not routine_notify_enabled():
        return False
    _service_provider = service_provider
    _urgent_email = urgent_email
    _result_email_reaches_owner = result_email_reaches_owner
    registry.register(
        ToolSpec(
            schema=_schema(),
            handler=_handler,
            risk_level="low",
            allowed_for_roles=frozenset({"lead"}),
        )
    )
    return True
