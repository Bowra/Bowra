"""Durable delivery for legacy clarification invalidation intents."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any
from uuid import uuid4

from bowhead_core.events import OrchestratorEvent, get_event_registry

from bowhead_gateway.turn_service import turn_seq_seed

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL_SECONDS = 5.0
STARTUP_BATCH_LIMIT = 100
PERIODIC_BATCH_LIMIT = 100


def _record_delivery(outcome: str) -> None:
    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_clarification_invalidation_delivery_total",
            labels={"outcome": outcome},
            help="Durable clarification invalidation delivery outcomes.",
        )
    except Exception:  # telemetry must not change delivery  # noqa: S110
        pass


def _record_pending(value: int) -> None:
    try:
        from bowhead_core import metrics

        metrics.set_gauge(
            "bowhead_clarification_invalidation_outbox_pending",
            value,
            help="Current pending legacy clarification invalidation intents.",
        )
    except Exception:  # telemetry must not change delivery  # noqa: S110
        pass


def normalized_db_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def require_shared_core_database(*stores: Any) -> Path:
    """Fail startup when stores advertised as atomic use different SQLite files."""

    paths: list[Path] = []
    for store in stores:
        if store is None:
            raise RuntimeError("clarification invalidation store dependency is missing")
        raw = getattr(store, "db_path", None)
        if raw is None:
            raw = getattr(store, "_db_path", None)
        if not raw:
            raise RuntimeError(
                "clarification invalidation store has no database identity",
            )
        paths.append(normalized_db_path(str(raw)))
    unique = set(paths)
    if len(unique) != 1:
        raise RuntimeError(
            f"clarification, scheduled, Turn log and session stores must share one core SQLite database; found {sorted(map(str, unique))}",
        )
    return paths[0]


def require_durable_event_sink() -> None:
    """Require the ordered durable Turn sink before recovery/readiness."""

    sink = getattr(get_event_registry(), "_default_log_sink", None)
    if not callable(sink) or not callable(getattr(sink, "drain_turn", None)):
        raise RuntimeError(
            "clarification invalidation requires an ordered durable Turn log sink",
        )


async def publish_pending_clarification_invalidations(
    app: Any,
    *,
    request_ids: Collection[str] | None = None,
    limit: int = PERIODIC_BATCH_LIMIT,
) -> int:
    """Claim Turn groups and persist their inverse events in stable order."""

    store = getattr(app.state, "clarification_store", None)
    turn_log_store = getattr(app.state, "turn_log_store", None)
    if store is None or turn_log_store is None:
        raise RuntimeError("clarification invalidation publisher is not ready")
    if limit < 1:
        raise ValueError("clarification invalidation publish limit is invalid")

    lock = getattr(app.state, "clarification_invalidation_publish_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.clarification_invalidation_publish_lock = lock
    owner = getattr(app.state, "clarification_invalidation_owner", None)
    if not owner:
        owner = f"gateway_{uuid4().hex}"
        app.state.clarification_invalidation_owner = owner

    delivered = 0
    requested = {value for value in (request_ids or ()) if value}
    registry = get_event_registry()
    async with lock:
        while delivered < limit or delivered == 0:
            rows = await store.claim_invalidation_turn(
                owner=owner,
                request_ids=requested or None,
                limit=max(1, limit - delivered),
            )
            if not rows:
                break
            turn_id = rows[0].turn_id
            group_failed = False
            for row in rows:
                try:
                    renewed = await store.renew_invalidation_turn(
                        turn_id=turn_id,
                        owner=owner,
                    )
                    if renewed < 1:
                        _record_delivery("lease_lost")
                        logger.warning(
                            "clarification_invalidation.lease_lost turn=%s request=%s",
                            turn_id,
                            row.request_id,
                        )
                        group_failed = True
                        break
                    seed = await turn_seq_seed(turn_log_store, turn_id)
                    await registry.emit_out_of_band(
                        turn_id,
                        OrchestratorEvent(
                            event_type="clarification.dismissed",
                            turn_id=turn_id,
                            payload=row.event_payload(),
                            durable_event_id=row.event_id,
                            persist_before_delivery=True,
                        ),
                        fallback_initial_seq=seed,
                    )
                    if not await store.mark_invalidation_delivered(
                        request_id=row.request_id,
                        owner=owner,
                    ):
                        _record_delivery("settle_lost")
                        logger.warning(
                            "clarification_invalidation.settle_lost turn=%s request=%s",
                            turn_id,
                            row.request_id,
                        )
                        group_failed = True
                        break
                    delivered += 1
                    _record_delivery("delivered")
                    requested.discard(row.request_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _record_delivery("failed")
                    logger.exception(
                        "clarification_invalidation.publish_failed turn=%s request=%s attempt=%d",
                        turn_id,
                        row.request_id,
                        row.attempt_count,
                    )
                    group_failed = True
                    break
            if group_failed:
                break
            if request_ids is not None and not requested:
                break
            if delivered >= limit:
                break
    with contextlib.suppress(Exception):
        _record_pending(await store.count_pending_invalidations())
    return delivered


async def clarification_invalidation_publisher_loop(app: Any) -> None:
    """Periodic recovery loop; pending intents survive process crashes."""

    while True:
        try:
            await publish_pending_clarification_invalidations(
                app,
                limit=PERIODIC_BATCH_LIMIT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("clarification_invalidation.periodic_publish_failed")
        await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
