"""Audit persistence stays available independently of Business readers."""

from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.user_store import UserStore


async def test_admin_audit_records_reject_updates(tmp_path: Path) -> None:
    store = UserStore(tmp_path / "audit.db")
    await store.initialize()
    await store.write_admin_audit_log(
        actor_user_id="usr_audit_actor",
        team_id="team_audit",
        action="test_recording",
        scope="team",
        payload={"original": True},
    )
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute("UPDATE admin_audit_log SET payload_json = '{}' WHERE actor_user_id='usr_audit_actor'")
        async with db.execute("SELECT payload_json FROM admin_audit_log") as cursor:
            row = await cursor.fetchone()
    assert row is not None
    assert '"original": true' in row[0]
