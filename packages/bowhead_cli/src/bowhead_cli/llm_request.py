"""Operator access to the diagnostic LLM request record (docs/design/llm-request-record-tech-design.md §4.7).

`show` writes and commits an `llm_request_record_viewed` audit row before it
decrypts anything, so content is never shown without an audit trail. `list`
decrypts nothing and is not audited.
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Any

import aiosqlite
from bowhead_core.audit_actions import AuditAction
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.llm_request_record import find_request_record, list_turn_request_records, rebuild_request
from bowhead_core.user_store import UserStore

#: Synthetic audit actor for host-side reads; the person is in the payload.
AUDIT_ACTOR = "system-llm-request-cli"


async def list_records(db_path: Path, turn_id: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        return await list_turn_request_records(db, turn_id)


async def show_record(db_path: Path, ref: str, *, operator: str, cipher: ReferenceCipher) -> dict[str, Any] | None:
    """Audit, then rebuild one record. Returns None when no record matches `ref`."""
    async with aiosqlite.connect(db_path) as db:
        record = await find_request_record(db, ref)
        if record is None:
            return None
        await db.execute("BEGIN IMMEDIATE")
        try:
            await UserStore(str(db_path)).write_admin_audit_log(
                actor_user_id=AUDIT_ACTOR,
                team_id=str(record["team_id"]),
                action=AuditAction.LLM_REQUEST_RECORD_VIEWED,
                target_kind="llm_request_record",
                target_id=str(record["id"]),
                target_team_id=str(record["team_id"]),
                scope="team",
                payload={"turn_id": record["turn_id"], "operator": operator, "os_user": getpass.getuser()},
                conn=db,
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        rebuilt = await rebuild_request(db, cipher, record)
    return {
        "record": rebuilt.record,
        "verified": rebuilt.digest_matches,
        "omitted_chunks": list(rebuilt.omitted_chunks),
        "changes": list(rebuilt.changes),
        "body": json.loads(rebuilt.body),
    }
