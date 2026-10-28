"""Sprint α G3: SMTP outbox worker.

Polls user_store.claim_pending_smtp → sends via _send_smtp → marks sent/failed.
Runs as single asyncio.create_task in app startup (sprint α single-worker invariant).

Retry backoff: 1min, 5min, 30min, 2h → dead after 5 attempts.

Per Ops P0-O-3 fix: previously SMTP was direct sync call that swallowed failures
silently. Outbox makes retry visible + admin can re-flush dead rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from bowhead_core.email_config import (
    EmailConfigError,
    SmtpConfig,
    deliver_email,
    resolve_smtp_config,
)

if TYPE_CHECKING:
    from bowhead_core.user_store import UserStore

logger = logging.getLogger(__name__)

# Backoff schedule (attempt_no → delay_seconds)
_BACKOFF = {1: 60, 2: 300, 3: 1800, 4: 7200}
_MAX_ATTEMPTS = 5


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


async def smtp_outbox_worker(store: UserStore, *, poll_interval: float = 30.0) -> None:
    """Forever-loop. One task per process — sprint α single worker invariant.

    P2-O-2 fix: explicit CancelledError re-raise before broad catch so app
    shutdown actually cancels the worker. P2-O-1 fix: jitter on sleep to
    avoid thunder-herd if sprint β ever spawns multiple workers.
    """
    import random

    logger.info("smtp_outbox_worker.start poll_interval=%ss", poll_interval)
    while True:
        try:
            batch = await store.claim_pending_smtp(batch_size=10)
            # email-config sprint: cache resolved configuration per batch, avoiding per-row decryptions/queries.
            # Key = team_id (None=env fallback). cfg_cache hit = SmtpConfig or None (unconfigured);
            # bad_teams = error text for existing but unreadable team configs (review-fix: subsequent rows for that team also report
            # actual team_config_unreadable, not misleading smtp_not_configured).
            cfg_cache: dict[str | None, SmtpConfig | None] = {}
            bad_teams: dict[str | None, str] = {}
            for row in batch:
                outbox_id = row["id"]
                team_id = row.get("team_id")
                try:
                    if team_id in bad_teams:
                        await _handle_failure(
                            store,
                            outbox_id,
                            row["attempts"],
                            bad_teams[team_id],
                        )
                        continue
                    if team_id not in cfg_cache:
                        # strict_team=True: corrupt team config → visible failed_retry failure,
                        # never silently send through env relay (tech-design §6a / security #11).
                        cfg_cache[team_id] = await resolve_smtp_config(
                            team_id,
                            store,
                            strict_team=True,
                        )
                    cfg = cfg_cache[team_id]
                    if cfg is None:
                        await _handle_failure(
                            store,
                            outbox_id,
                            row["attempts"],
                            "smtp_not_configured",
                        )
                        continue
                    # deliver_email = wait_for + to_thread (synchronous smtplib), avoiding event-loop blocking.
                    ok, err = await deliver_email(
                        row["to_email"],
                        row["subject"],
                        row["body"],
                        config=cfg,
                        timeout=15.0,
                        html_body=row.get("html_body"),
                    )
                    if ok:
                        await store.mark_smtp_sent(outbox_id)
                        logger.info("smtp_outbox.sent id=%s purpose=%s", outbox_id, row["purpose"])
                    else:
                        await _handle_failure(
                            store,
                            outbox_id,
                            row["attempts"],
                            (err or "smtp_returned_false")[:200],
                        )
                except asyncio.CancelledError:
                    raise  # P2-O-2: propagate shutdown immediately
                except EmailConfigError as e:
                    # Unreadable team config: record/cache actual cause; subsequent team rows also report team_config_unreadable.
                    reason = f"team_config_unreadable: {e}"[:200]
                    bad_teams[team_id] = reason
                    await _handle_failure(store, outbox_id, row["attempts"], reason)
                except Exception as e:
                    await _handle_failure(
                        store,
                        outbox_id,
                        row["attempts"],
                        f"{type(e).__name__}: {e}"[:200],
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("smtp_outbox_worker.iter_error err=%s", e)

        # Jitter to prevent thunder-herd if multiple workers spawn (sprint β+)
        await asyncio.sleep(poll_interval + random.uniform(0, 5))


async def _handle_failure(
    store: UserStore,
    outbox_id: str,
    current_attempts: int,
    error_msg: str,
) -> None:
    """Compute next backoff or mark dead."""
    # current_attempts has been incremented to N by claim_pending_smtp
    new_attempt_no = current_attempts + 1
    if new_attempt_no >= _MAX_ATTEMPTS:
        await store.mark_smtp_failed(outbox_id, error_msg, next_attempt_at=None)
        logger.warning("smtp_outbox.dead id=%s after %d attempts err=%s", outbox_id, new_attempt_no, error_msg)
    else:
        delay = _BACKOFF.get(new_attempt_no, 7200)
        next_at = _utc_iso(datetime.now(UTC) + timedelta(seconds=delay))
        await store.mark_smtp_failed(outbox_id, error_msg, next_attempt_at=next_at)
        logger.info(
            "smtp_outbox.failed_retry id=%s attempt=%d next_at=%s err=%s",
            outbox_id,
            new_attempt_no,
            next_at,
            error_msg,
        )
