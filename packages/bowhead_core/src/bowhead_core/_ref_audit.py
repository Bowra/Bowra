"""Sprint ν1 — Reference audit log via logger.info + SQLite double-write.

Closes Risk r3-P0-N7 + SRE r2-P0-A: prod 没 Loki/Promtail (verified
deploy/compose.yaml uses docker json-file driver 50m × 3 file
rolling — LLM batch 抖动 spike 1h 翻完 audit 现场). 双写 logger.info (ops
立即可见 tail) + SQLite (源-of-truth, 90d retention, 客服查询 / SOC2).

Table: ref_audit_log (复用 main.py:255 共用 db_path, 不新建 DB 文件).
WAL + busy_timeout=5000 在 init 时设 (closes CTO r2 P0-N2/N3 + .backup
锁库自然消除).

4 类事件:
- ref_audit.validate_reject — peer subagent_return validate fail, 让 retry
- ref_audit.strip_degrade — retry > 2, strip 假 ref
- ref_audit.kb_hallucinate — KB ref 不在 allowlist (含在 validate_reject 里)
- ref_audit.thin_strip_degrade — canvas_add_insight thin path strip
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from bowhead_core.types import utc_now_iso

logger = logging.getLogger(__name__)

_DB_PATH: Path | None = None
_INIT_DONE: bool = False


async def init_ref_audit(db_path: Path | str) -> None:
    """Init audit table + WAL pragmas. Idempotent — safe to call multiple
    times. Call from gateway lifespan startup.

    closes CTO r2 P0-N2 (WAL) + P0-N3 (DB path).
    """
    global _DB_PATH, _INIT_DONE
    p = Path(db_path)
    _DB_PATH = p
    async with aiosqlite.connect(p) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ref_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                workspace_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ref_audit_turn
            ON ref_audit_log(turn_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ref_audit_event_at
            ON ref_audit_log(event_type, created_at DESC)
        """)
        await db.commit()
    _INIT_DONE = True
    logger.info("ν_ref_audit.init_done path=%s wal=on", p)


async def emit_ref_audit_event(
    turn_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    user_id: str = "",
    workspace_id: str = "",
) -> None:
    """Double-write: logger.info structured + SQLite. SQLite write is
    best-effort (failure logged, doesn't propagate)."""
    logger.info(
        "ν_ref_audit",
        extra={
            "event_type": event_type,
            "turn_id": turn_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "payload": payload,
        },
    )
    if _DB_PATH is None or not _INIT_DONE:
        return  # logger only — init not yet called (e.g. test path)
    try:
        async with aiosqlite.connect(_DB_PATH) as db:
            await db.execute(
                "INSERT INTO ref_audit_log(turn_id, user_id, workspace_id, event_type, payload_json, created_at) VALUES (?,?,?,?,?,?)",
                (
                    turn_id,
                    user_id,
                    workspace_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            await db.commit()
    except Exception as e:
        logger.warning("ν_ref_audit.sqlite_write_failed err=%s", e)


def _reset_for_test() -> None:
    """Test helper — reset module state."""
    global _DB_PATH, _INIT_DONE
    _DB_PATH = None
    _INIT_DONE = False
