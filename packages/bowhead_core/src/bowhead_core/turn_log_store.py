"""Turn event log — 持久化 orchestrator 事件流，支持 replay & 事后审计.

schema:
    turn_events(id, turn_id, seq, event_type, payload_json, occurred_at)

用法：
    1. Orchestrator.emit 时同步写 turn_log_store.record(event)
    2. 前端断线重连 / 调试时：
       GET /api/v1/turns/{turn_id}/events           → 读全部
       GET /api/v1/turns/{turn_id}/replay           → SSE 按序推送

production：
    - seq = 按 turn 严格自增（UNIQUE(turn_id, seq)）
    - payload 直接 json.dumps；不做裁剪（replay 要还原原貌）
    - admitted legacy chat 由 OrderedTurnLogSink 在 live 前同步落库；失败由
      TurnEventBus fail-stop，不得继续 provider/tool 副作用
    - 未纳入 chat admission 的调试/兼容调用方自己决定 best-effort 策略
    - 老 turn 可按 created_at 做 TTL（M3）
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_events (
    id            TEXT PRIMARY KEY,
    turn_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    occurred_at   TEXT NOT NULL,
    UNIQUE(turn_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_turn_events_turn ON turn_events(turn_id, seq);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove transport cursors before comparing durable event identity."""
    return {key: value for key, value in payload.items() if key not in {"seq", "evt_seq"}}


async def _is_canonical_clarification_resolution_receipt(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    event_type: str,
    event_id: str | None,
    payload: dict[str, Any],
) -> bool:
    """Allow one already-committed clarification decision through a failed fence.

    A failed dispatch continues to reject every ordinary legacy writer. The
    sole exception is the deterministic delivery receipt for a decision that
    is already canonical in both the clarification row and its completed
    outbox. This commits no new business fact; it only makes that durable fact
    replayable before resume advances the dispatch generation.
    """
    request_id = payload.get("request_id")
    if (
        event_type != "clarification.resolved"
        or not isinstance(request_id, str)
        or not request_id
        or event_id != f"evt_clarification_resolution_{request_id}"
    ):
        return False
    async with db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('clarification_requests','clarification_resolution_outbox')",
    ) as schema_cursor:
        schema_row = await schema_cursor.fetchone()
    if schema_row is None or int(schema_row[0]) != 2:
        return False
    async with db.execute(
        "SELECT o.payload_json FROM clarification_requests c "
        "JOIN clarification_resolution_outbox o "
        "ON o.request_id=c.request_id AND o.turn_id=c.turn_id "
        "WHERE c.request_id=? AND c.turn_id=? AND c.status='resolved' "
        "AND o.state='completed'",
        (request_id, turn_id),
    ) as receipt_cursor:
        receipt = await receipt_cursor.fetchone()
    if receipt is None or receipt[0] is None:
        return False
    try:
        canonical_payload = json.loads(str(receipt[0]))
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(isinstance(canonical_payload, dict) and _semantic_payload(canonical_payload) == _semantic_payload(payload))


@dataclass
class TurnEventRow:
    id: str
    turn_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: str
    inserted: bool = True


@dataclass
class TurnLogStore:
    db_path: str
    _write_lock: asyncio.Lock | None = None

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def record(
        self,
        *,
        turn_id: str,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> TurnEventRow | None:
        """追加一条事件；失败返 None（主流程不能因持久化 hang）.

        用 per-instance lock 串行化并发写，避免 MAX(seq)+1 race → UNIQUE 冲突.
        """
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            try:
                async with open_shared_ctx(self.db_path) as db:
                    await db.execute("BEGIN IMMEDIATE")
                    try:
                        rid = event_id or f"evt_{uuid4().hex[:12]}"
                        occurred = occurred_at or _now()
                        encoded = json.dumps(payload, ensure_ascii=False, default=str)
                        # Exact durable retries are reads, not post-fence
                        # writes. Resolve them before consulting dispatch
                        # settlement so a later owner-loss fence cannot make an
                        # already-persisted receipt appear unavailable.
                        if event_id is not None:
                            async with db.execute(
                                "SELECT turn_id,event_type,payload_json,occurred_at,seq FROM turn_events WHERE id=?",
                                (event_id,),
                            ) as existing_cursor:
                                existing = await existing_cursor.fetchone()
                            if existing is not None:
                                existing_payload = json.loads(str(existing[2]))
                                if (
                                    str(existing[0]) != turn_id
                                    or str(existing[1]) != event_type
                                    or _semantic_payload(existing_payload) != _semantic_payload(payload)
                                ):
                                    await db.execute("ROLLBACK")
                                    logger.error(
                                        "turn_log.durable_event_conflict id=%s turn=%s type=%s",
                                        event_id,
                                        turn_id,
                                        event_type,
                                    )
                                    return None
                                await db.execute("ROLLBACK")
                                return TurnEventRow(
                                    id=event_id,
                                    turn_id=turn_id,
                                    seq=int(existing[4]),
                                    event_type=event_type,
                                    payload=existing_payload,
                                    occurred_at=str(existing[3]),
                                    inserted=False,
                                )

                        # A dispatch may enter `failed` only after its ordered
                        # log tail drains, or through lease recovery. Serialize
                        # behind that transaction and reject every later
                        # writer. A completed clarification outbox may still
                        # project its one deterministic resolution receipt; it
                        # is durable business truth, not a stale worker tail.
                        async with db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turn_spawn_dispatch'",
                        ) as schema_cursor:
                            has_dispatch_fence = await schema_cursor.fetchone()
                        if has_dispatch_fence is not None:
                            async with db.execute(
                                "SELECT 1 FROM chat_turn_spawn_dispatch WHERE turn_id=? AND state='failed'",
                                (turn_id,),
                            ) as fence_cursor:
                                failed_dispatch_fence = await fence_cursor.fetchone()
                            if failed_dispatch_fence is not None and not await _is_canonical_clarification_resolution_receipt(
                                db,
                                turn_id=turn_id,
                                event_type=event_type,
                                event_id=event_id,
                                payload=payload,
                            ):
                                await db.execute("ROLLBACK")
                                logger.info(
                                    "turn_log.failed_dispatch_write_fenced turn=%s type=%s",
                                    turn_id,
                                    event_type,
                                )
                                return None

                        # Row seq = MAX+1 (one row per emit, increments 1, same cadence as bus _seq_counter
                        # so naturally follows emit order).
                        # ⚠ chat-solidify O6 lesson (2026-07-04 code-review rollback): never
                        # use payload["seq"] as row seq: canvas.section_added and others use
                        # section-order index (canvas_tools.py stored.seq, 0/1/2…),
                        # not bus seq (events.py:475 setdefault
                        # deliberately preserves it). As row seq it collides with bus-seq event rows
                        # UNIQUE(turn_id,seq) → record swallows error → canvas section silently
                        # unpersisted → F5 loses section (sprint live≠F5 target). Authoritative bus seq
                        # lives in payload (frontend dedup/resume reads it); row seq only handles
                        # list_events order + max_seq seed: MAX+1 correct for both.
                        async with db.execute(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM turn_events WHERE turn_id=?",
                            (turn_id,),
                        ) as cur:
                            r = await cur.fetchone()
                        seq = int(r[0]) if r else 1
                        await db.execute(
                            "INSERT INTO turn_events(id, turn_id, seq, event_type, payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (rid, turn_id, seq, event_type, encoded, occurred),
                        )
                        await db.commit()
                        return TurnEventRow(
                            id=rid,
                            turn_id=turn_id,
                            seq=seq,
                            event_type=event_type,
                            payload=payload,
                            occurred_at=occurred,
                        )
                    except BaseException:
                        await db.execute("ROLLBACK")
                        raise
            except Exception as e:
                logger.warning("turn_log.record_failed turn=%s type=%s err=%s", turn_id, event_type, e)
                return None

    async def list_events(
        self,
        turn_id: str,
        *,
        event_types: tuple[str, ...] | None = None,
    ) -> list[TurnEventRow]:
        """event_types(批4 review P2-8): SQL 侧类型过滤——单 turn 可上千行且含
        section_chunk 等大 payload, 溯源导出只要 tool_result 一类时禁全量拉。"""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if event_types:
                _ph = ", ".join("?" for _ in event_types)
                _sql = f"SELECT * FROM turn_events WHERE turn_id=? AND event_type IN ({_ph}) ORDER BY seq"  # noqa: S608 — placeholders only
                _args: tuple = (turn_id, *event_types)
            else:
                _sql = "SELECT * FROM turn_events WHERE turn_id=? ORDER BY seq"
                _args = (turn_id,)
            async with db.execute(_sql, _args) as cur:
                rows = await cur.fetchall()
        result: list[TurnEventRow] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            # Terminal Protocol v1 stores stable delivery identity in normalized
            # row columns instead of duplicating it into the business payload.
            # Reconstruct the client-visible envelope at the durable read seam so
            # live, F5 replay and the explicit replay APIs expose byte-equivalent
            # ordering/dedupe metadata.  In particular, canvas `seq` may already
            # mean section order, so `evt_seq` is the unconditional event cursor.
            row_columns = frozenset(row.keys())
            if "protocol_version" in row_columns and row["protocol_version"] == 1:
                event_seq = int(row["event_seq"])
                payload["event_id"] = row["id"]
                payload["event_seq"] = event_seq
                payload["generation"] = int(row["generation"])
                payload.setdefault("seq", event_seq)
                payload["evt_seq"] = event_seq
            result.append(
                TurnEventRow(
                    id=row["id"],
                    turn_id=row["turn_id"],
                    seq=int(row["seq"]),
                    event_type=row["event_type"],
                    payload=payload,
                    occurred_at=row["occurred_at"],
                )
            )
        return result

    async def max_seq(self, turn_id: str) -> int:
        """Return the highest persisted event seq for a turn, or 0 if none."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM turn_events WHERE turn_id=?",
                (turn_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_turn_ids(
        self,
        *,
        limit: int = 100,
    ) -> list[str]:
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT DISTINCT turn_id FROM turn_events ORDER BY occurred_at DESC LIMIT ?",
                (limit,),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [r[0] for r in rows]


class OrderedTurnLogSink:
    """Keep fire-and-forget event writes ordered within each turn.

    Normal events should not block live delivery, while a
    ``persist_before_delivery`` event must wait until both itself and every
    earlier event in the same turn have reached the log.  Creating independent
    tasks is insufficient: a later durable task can acquire the store lock
    before an earlier task has started and become the first replayed row.
    """

    def __init__(self, store: TurnLogStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._tails: dict[
            str,
            asyncio.Task[TurnEventRow | None],
        ] = {}
        self._tasks: set[asyncio.Task[TurnEventRow | None]] = set()

    async def __call__(self, event: Any) -> bool:
        task = await self._enqueue(event)
        if getattr(event, "persist_before_delivery", False):
            # The write task is shared ordering state, not a child operation of
            # whichever emitter happens to await it. Cancelling one peer must
            # not cancel this tail and cascade into every later event.
            row = await asyncio.shield(task)
            if row is not None and not getattr(row, "inserted", True):
                # A durable retry retains its original wire envelope across
                # bus generations; the tentative new cursor is not a new fact.
                event.payload = dict(row.payload)
                event.seq = int(row.payload.get("evt_seq", row.payload.get("seq", row.seq)))
                event.occurred_at = row.occurred_at
                event.suppress_live_delivery = True
            return row is not None
        return True

    async def _enqueue(
        self,
        event: Any,
    ) -> asyncio.Task[TurnEventRow | None]:
        turn_id = str(event.turn_id)
        async with self._lock:
            previous = self._tails.get(turn_id)

            async def _record_after_previous() -> TurnEventRow | None:
                if previous is not None:
                    previous_row = await previous
                    if previous_row is None:
                        return None
                record_kwargs: dict[str, Any] = {
                    "turn_id": turn_id,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "occurred_at": event.occurred_at,
                }
                durable_event_id = getattr(event, "durable_event_id", None)
                if durable_event_id is not None:
                    record_kwargs["event_id"] = durable_event_id
                return await self._store.record(**record_kwargs)

            task = asyncio.create_task(
                _record_after_previous(),
                name=f"turn-log:{turn_id}",
            )
            self._tails[turn_id] = task
            self._tasks.add(task)

            def _discard(
                completed: asyncio.Task[TurnEventRow | None],
            ) -> None:
                self._tasks.discard(completed)
                if self._tails.get(turn_id) is completed:
                    self._tails.pop(turn_id, None)

            task.add_done_callback(_discard)
            return task

    async def drain(self) -> None:
        """Wait for all accepted writes during graceful shutdown."""
        while self._tasks:
            drain = asyncio.gather(
                *tuple(self._tasks),
                return_exceptions=True,
            )
            await asyncio.shield(drain)

    async def drain_turn(self, turn_id: str) -> None:
        """Wait until one Turn's ordered persistence tail is quiescent.

        Dispatch settlement uses this after its worker task has finished, when
        no new events can be enqueued. The loop is defensive against a tail
        replacement between snapshots and keeps cross-process SSE from treating
        a fixed sleep as a persistence barrier.
        """
        while True:
            async with self._lock:
                tail = self._tails.get(turn_id)
            if tail is None:
                return
            await asyncio.shield(tail)
            # Awaiting an already-complete task may not yield long enough for
            # its done callback to prune `_tails`. Remove the exact observed
            # generation ourselves; if a later event replaced it, loop and
            # drain that newer tail instead.
            async with self._lock:
                if self._tails.get(turn_id) is tail and tail.done():
                    self._tails.pop(turn_id, None)
