"""Gateway recorder for the diagnostic LLM request record.

`record` is installed as the router's request recorder and runs on the model call
path, so it only appends to a bounded buffer. The shared flush loop writes batches
in short transactions separate from billing. A failed batch is dropped and counted:
this is diagnostics, and a crash may lose buffered records
(docs/design/llm-request-record-tech-design.md). The same loop purges records past
their 14-day retention in small batches so it never holds the writer for long.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.llm_request_record import purge_expired_request_records, write_request_records
from bowhead_router.request_record import RequestRecordEntry, set_request_recorder

from bowhead_gateway.buffered_flush import BufferedFlushLoop

_DEFAULT_INTERVAL_S = 10.0
_DEFAULT_MAX_BUFFER = 2_000
_FLUSH_BATCH = 50
_PURGE_INTERVAL_S = 600.0
_PURGE_BATCH = 500
#: Bounds one purge run; a larger backlog continues on the next run.
_PURGE_MAX_BATCHES = 20

log = logging.getLogger(__name__)


class LLMRequestRecorder(BufferedFlushLoop[RequestRecordEntry]):
    log_name = "llm_request_record"
    requeue_on_failure = False

    def __init__(self, db_path: str, cipher: ReferenceCipher, *, interval_s: float = _DEFAULT_INTERVAL_S, max_buffer: int = _DEFAULT_MAX_BUFFER):
        super().__init__(interval_s=interval_s, max_buffer=max_buffer, flush_batch=_FLUSH_BATCH)
        self._db_path = db_path
        self._cipher = cipher
        self._next_purge_at = 0.0

    def record(self, entry: RequestRecordEntry) -> None:
        self._enqueue(entry)

    def _on_drop(self, count: int) -> None:
        try:
            from bowhead_core.metrics import inc

            inc(
                "bowhead_llm_request_record_dropped_total",
                value=float(count),
                help="Diagnostic LLM request records dropped (buffer full or write failure).",
            )
        except Exception:  # noqa: S110 — a metric must not break the recorder.
            pass

    async def _before_flush(self) -> None:
        if time.monotonic() < self._next_purge_at:
            return
        self._next_purge_at = time.monotonic() + _PURGE_INTERVAL_S
        try:
            purged = await self.purge_expired()
        except Exception:
            log.warning("llm_request_record.purge_failed", exc_info=True)
            return
        if purged:
            log.info("llm_request_record.purged records=%d", purged)

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Delete expired records batch by batch, one short transaction each."""
        now = now or datetime.now(UTC)
        purged = 0
        for _ in range(_PURGE_MAX_BATCHES):
            async with open_shared_ctx(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    deleted = await purge_expired_request_records(db, now, limit=_PURGE_BATCH)
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            purged += deleted
            if deleted < _PURGE_BATCH:
                break
        return purged

    async def _write_batch(self, batch: list[RequestRecordEntry]) -> int:
        async with open_shared_ctx(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                written = await write_request_records(db, self._cipher, batch)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return written


def start_llm_request_recorder(db_path: str) -> LLMRequestRecorder:
    """Build the recorder, install it on the router, and start its flush loop."""
    recorder = LLMRequestRecorder(db_path, ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")))
    set_request_recorder(recorder.record)
    recorder.start()
    return recorder


async def stop_llm_request_recorder(recorder: LLMRequestRecorder | None) -> None:
    """Uninstall the recorder, then flush what is buffered and stop."""
    set_request_recorder(None)
    if recorder is not None:
        await recorder.stop()
