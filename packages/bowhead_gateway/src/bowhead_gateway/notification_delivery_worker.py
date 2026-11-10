"""Send recorded notification emails (FEATURE_NOTIFICATION_DELIVERY).

Each tick leases due ``notification_deliveries`` rows and, for each, rechecks at send time:

1. the item still exists and is open; an escalation also needs the recipient's row unread and
   unarchived (``resolved`` / ``read`` / ``archived``);
2. the recipient is still an active member of the item's Team and their address passes the member
   allowlist (``not_member``);
3. for an escalation, the recipient has not turned reminder emails off (``opted_out``);
4. the Team has SMTP configured (``smtp_unconfigured``).

Then it sends fixed product copy (never model text) with a link built from the Team's public entry;
a missing link still sends, pointing to the notifications. A transient failure retries on the SMTP
outbox schedule (1 min, 5 min, 30 min, 2 h) and is ``dead`` after 5 attempts; a rejection is
``failed``. Rows, logs and metrics carry user ids, outcome codes and diagnostic references, never
addresses. One worker per gateway; the row lease keeps a second process from sending twice.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bowhead_gateway.im.error_messages import im_interface_message, normalize_im_locale
from bowhead_gateway.public_url import public_app_url_for_base

if TYPE_CHECKING:
    from bowhead_core.notification_store import NotificationDelivery, NotificationItem, NotificationStore

log = logging.getLogger(__name__)

REMINDER_SETTING_KEY = "notification_email_reminders"
_DEFAULT_INTERVAL_S = 30
_LEASE_S = 300
_BATCH = 20
_MAX_ATTEMPTS = 5
# Attempt number just made → seconds until the next one (the SMTP outbox schedule).
_BACKOFF = {1: 60, 2: 300, 3: 1800, 4: 7200}
_PERMANENT_PREFIXES = ("SMTPRecipientsRefused", "SMTPSenderRefused", "SMTPAuthenticationError", "smtp_target_blocked", "invalid to address")
_UNCONFIGURED = {"smtp not configured", "smtp sender not configured"}


def notification_delivery_enabled() -> bool:
    """FEATURE_NOTIFICATION_DELIVERY (default off). Rollout and rollback: docs/灰度功能清单.md."""
    return os.environ.get("FEATURE_NOTIFICATION_DELIVERY", "0").strip().lower() in {"1", "true", "yes"}


def env_interval_s() -> int:
    raw = os.environ.get("BOWHEAD_NOTIFICATION_DELIVERY_INTERVAL_S")
    try:
        return max(5, int(raw)) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        return _DEFAULT_INTERVAL_S


def _metric(name: str, labels: dict[str, str], value: float = 1.0) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(name, labels=labels, value=value)
    except Exception:  # noqa: S110
        pass


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


async def reminder_emails_on(app: Any, user_id: str) -> bool:
    """The recipient's personal switch for escalation emails; unset means on."""
    store = getattr(app.state, "user_settings_store", None)
    if store is None:
        return True
    try:
        return (await store.get(user_id, REMINDER_SETTING_KEY)) != "off"
    except Exception:
        log.warning("notification_delivery.reminder_setting_unavailable user=%s", user_id, exc_info=True)
        return True


async def _locale(app: Any, user_id: str) -> str:
    store = getattr(app.state, "user_settings_store", None)
    if store is not None:
        try:
            value = await store.get(user_id, "ui_language")
        except Exception:
            value = None
        if value and value != "auto":
            return normalize_im_locale(value)
    return "en-US"


async def _link(app: Any, team_id: str, path: str | None) -> str | None:
    """An absolute link through the Team's default public entry; None when there is none."""
    entries = getattr(app.state, "public_entry_store", None)
    if entries is None or not path:
        return None
    try:
        entry = await entries.default_for_team(team_id)
    except Exception:
        log.warning("notification_delivery.entry_unavailable team=%s", team_id, exc_info=True)
        return None
    return public_app_url_for_base(entry.base_url, path) if entry is not None else None


async def _compose(app: Any, delivery: NotificationDelivery, item: NotificationItem) -> tuple[str, str]:
    """Fixed product copy for one email: the type, the names in its metadata and a link."""
    meta = item.metadata or {}
    name = str(meta.get("jobName") or "")
    if item.type == "routine.delivered":
        from bowhead_gateway._scheduled_runner import _deep_link, _delivery_locale

        scheduled = getattr(app.state, "scheduled_store", None)
        job = await scheduled.get(str(meta.get("jobId") or "")) if scheduled is not None else None
        session_id = str(meta.get("sessionId") or "")
        if job is not None:
            locale = await _delivery_locale(app, job, team_id=item.team_id, session_id=session_id, turn_id=str(meta.get("runId") or ""))
            link = await _deep_link(app, job, session_id, item.team_id)
        else:
            locale, link = await _locale(app, delivery.user_id), None
        # The run raised an urgent alert: the owner's result email carries it (no separate alert email).
        urgent = bool(meta.get("flaggedUrgent")) and job is not None and delivery.user_id == job.user_id
        lines = [im_interface_message("scheduled_email_flagged_urgent", locale), ""] if urgent else []
        lines += [im_interface_message("scheduled_email_job", locale, name=name), "", item.body or ""]
        if link:
            lines += ["", im_interface_message("scheduled_email_view_result", locale, url=link)]
        subject_key = "scheduled_email_subject_urgent" if urgent else "scheduled_email_subject"
        return im_interface_message(subject_key, locale, name=name), "\n".join([*lines, "", "— Bowhead"])
    locale = await _locale(app, delivery.user_id)
    link = await _link(app, item.team_id, item.action_url)
    if item.type == "routine.alert":
        subject = im_interface_message("routine_alert_email_subject", locale, name=name)
        body = [im_interface_message("routine_alert_email_body", locale, name=name)]
        body.append(
            im_interface_message("routine_alert_email_link", locale, url=link)
            if link
            else im_interface_message("routine_alert_email_no_link", locale)
        )
        return subject, "\n\n".join([*body, "— Bowhead"])
    if item.type == "approval.required":
        tool = str(meta.get("toolName") or "")
        subject = im_interface_message("notification_reminder_approval_subject", locale, tool=tool)
        first = im_interface_message("notification_reminder_approval_body", locale, tool=tool)
    else:
        subject = im_interface_message("notification_reminder_answer_subject", locale, name=name)
        first = im_interface_message("notification_reminder_answer_body", locale, name=name)
    body = [
        first,
        im_interface_message("notification_reminder_link", locale, url=link)
        if link
        else im_interface_message("notification_reminder_no_link", locale),
    ]
    body.append(im_interface_message("notification_reminder_footer", locale))
    return subject, "\n\n".join([*body, "— Bowhead"])


async def _check(app: Any, store: NotificationStore, delivery: NotificationDelivery) -> tuple[NotificationItem | None, str | None, str | None]:
    """``(item, address, skip_code)``: what to send, or why the row is skipped."""
    item = await store.get_item(delivery.notification_id)
    if item is None:
        return None, None, "item_missing"
    if item.state != "open":
        return item, None, "resolved"
    if delivery.channel == "email_escalation":
        state = await store.get_recipient_state(item.id, delivery.user_id)
        if state is None:
            return item, None, "not_recipient"
        read_at, archived_at = state
        if archived_at:
            return item, None, "archived"
        if read_at:
            return item, None, "read"
    users = getattr(app.state, "user_store", None)
    if users is None:
        return item, None, "not_member"
    members = await users.filter_active_team_members(item.team_id, {delivery.user_id})
    user = await users.get_user(delivery.user_id) if delivery.user_id in members else None
    address = str(getattr(user, "email", "") or "").strip()
    if not address or address.lower() not in await users.team_member_emails(item.team_id):
        return item, None, "not_member"
    if delivery.channel == "email_escalation" and not await reminder_emails_on(app, delivery.user_id):
        return item, None, "opted_out"
    from bowhead_core.email_config import resolve_team_smtp_config

    if await resolve_team_smtp_config(item.team_id, users) is None:
        return item, None, "smtp_unconfigured"
    return item, address, None


async def deliver_once(app: Any, store: NotificationStore, *, owner: str, now: datetime | None = None) -> dict[str, int]:
    """One tick: lease due rows, recheck, send, settle. Returns counts by final status of this tick."""
    from bowhead_core.smtp_notifier import send_notification

    now = now or _now()
    leased = await store.lease_due_deliveries(owner=owner, now=_iso(now), lease_until=_iso(now + timedelta(seconds=_LEASE_S)), limit=_BATCH)
    counts: dict[str, int] = {}

    async def settle(delivery: NotificationDelivery, status: str, code: str | None, *, ref: str | None = None, retry_at: str | None = None) -> None:
        await store.settle_delivery(delivery_id=delivery.id, owner=owner, status=status, outcome_code=code, diagnostic_ref=ref, retry_at=retry_at)  # type: ignore[arg-type]
        final = "retry" if retry_at else status
        counts[final] = counts.get(final, 0) + 1
        _metric("bowhead_notification_delivery_total", {"channel": delivery.channel, "status": final, "outcome_code": code or ""})

    for delivery in leased:
        try:
            item, address, skip = await _check(app, store, delivery)
        except Exception:
            ref = f"ndg_{uuid4().hex[:12]}"
            log.warning("notification_delivery.check_failed id=%s ref=%s", delivery.id, ref, exc_info=True)
            await settle(delivery, "pending", "check_failed", ref=ref, retry_at=_iso(now + timedelta(seconds=_BACKOFF[1])))
            continue
        if skip is not None or item is None or address is None:
            await settle(delivery, "skipped", skip)
            continue
        subject, body = await _compose(app, delivery, item)
        ok, err = await send_notification(address, subject, body, team_id=item.team_id, store=getattr(app.state, "user_store", None))
        if ok:
            await settle(delivery, "sent", None)
            _metric_latency(delivery, now)
            continue
        error = str(err or "unknown")
        ref = f"ndg_{uuid4().hex[:12]}"
        log.warning("notification_delivery.send_failed id=%s user=%s ref=%s reason=%s", delivery.id, delivery.user_id, ref, error.split(":", 1)[0])
        if error in _UNCONFIGURED:
            await settle(delivery, "skipped", "smtp_unconfigured", ref=ref)
        elif error.startswith(_PERMANENT_PREFIXES):
            await settle(delivery, "failed", "rejected", ref=ref)
        elif delivery.attempts + 1 >= _MAX_ATTEMPTS:
            await settle(delivery, "dead", "retries_exhausted", ref=ref)
        else:
            delay = _BACKOFF.get(delivery.attempts + 1, _BACKOFF[4])
            await settle(delivery, "pending", "send_failed", ref=ref, retry_at=_iso(now + timedelta(seconds=delay)))
    return counts


def _metric_latency(delivery: NotificationDelivery, now: datetime) -> None:
    try:
        from bowhead_core import metrics as _m

        waited = (now - datetime.fromisoformat(delivery.not_before)).total_seconds()
        _m.observe("bowhead_notification_delivery_latency_seconds", max(0.0, waited), labels={"channel": delivery.channel})
    except Exception:  # noqa: S110
        pass


async def run_notification_delivery_worker(app: Any, *, store: NotificationStore, interval_s: int, stop_event: asyncio.Event | None = None) -> None:
    owner = f"ndw_{uuid4().hex[:10]}"
    log.info("notification_delivery.started interval_s=%s", interval_s)
    try:
        while True:
            try:
                await deliver_once(app, store, owner=owner)
            except Exception as exc:
                log.warning("notification_delivery.tick_failed err=%s", exc)
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        log.info("notification_delivery.cancelled")
        raise
    finally:
        log.info("notification_delivery.stopped")
