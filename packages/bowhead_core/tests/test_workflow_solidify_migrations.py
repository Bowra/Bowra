"""workflow-solidify S3 — verdict / origin 增量迁移（§4.3 + §7）.

§4.3：skill_instance_store 原**无**迁移块；存量表缺 verdict 列 → set_verdict 写崩。验
PRAGMA-guard ALTER 给**存量表**补列。§7：sessions.origin 列 + set_origin（dry-run scratch
session 隔离标记）。
"""

from __future__ import annotations

import aiosqlite
from bowhead_core.skill_instance_store import SkillInstanceStore

from bowhead_core import SessionStore

# Pre-migration skill_instances shape (no verdict/verdict_json columns), simulating an existing database.
_LEGACY_SKILL_INSTANCES = """
CREATE TABLE skill_instances (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
    source_type TEXT NOT NULL, source_path TEXT NOT NULL,
    version TEXT, description TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    source_turn_id TEXT, size_bytes INTEGER DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id, name)
);
"""


async def test_skill_instance_verdict_migration_on_legacy_table(tmp_path):
    """存量表（无 verdict 列）→ initialize() 必 ALTER 补列，否则 set_verdict 写崩."""
    db = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db) as raw:
        await raw.executescript(_LEGACY_SKILL_INSTANCES)
        await raw.execute(
            "INSERT INTO skill_instances(id,user_id,name,source_type,source_path,"
            "created_at,updated_at) VALUES('sk1','u1','channel-gm','user_generated',"
            "'/x','2026-06-09T00:00:00Z','2026-06-09T00:00:00Z')",
        )
        await raw.commit()

    store = SkillInstanceStore(db)
    await store.initialize()  # Must ALTER existing tables to add verdict/verdict_json.

    ok = await store.set_verdict(user_id="u1", name="channel-gm", verdict="pass", verdict_json='{"checkpoints":["ok"]}')
    assert ok is True
    inst = await store.get(user_id="u1", name="channel-gm")
    assert inst is not None
    assert inst.verdict == "pass"
    assert inst.verdict_json == '{"checkpoints":["ok"]}'


async def test_skill_instance_verdict_fresh_and_missing_row(tmp_path):
    store = SkillInstanceStore(str(tmp_path / "fresh.db"))
    await store.initialize()
    # No trial run → verdict None.
    inst = await store.upsert(user_id="u1", name="r1", source_type="user_generated", source_path="/x")
    assert inst.verdict is None and inst.verdict_json is None
    # Matching record is updated.
    assert await store.set_verdict(user_id="u1", name="r1", verdict="fail") is True
    inst2 = await store.get(user_id="u1", name="r1")
    assert inst2.verdict == "fail"
    # Nonexistent recipe → False (no crash).
    assert await store.set_verdict(user_id="u1", name="nope", verdict="pass") is False


async def test_session_origin_dryrun_isolation(tmp_path):
    """§7：ensure_session 默认 origin=normal；dry-run 后 set_origin=workflow_dryrun."""
    store = SessionStore(str(tmp_path / "sess.db"))
    await store.initialize()
    # Ordinary sessions default to normal.
    s = await store.ensure_session(session_id="s_normal", workspace_id="w1", user_id="u1")
    assert s.origin == "normal"
    # Scratch dry-run session: mark it after creation.
    scratch = await store.ensure_session(session_id="s_dry", workspace_id="w1", user_id="u1")
    assert await store.set_origin(scratch.id, "workflow_dryrun") is True
    reread = await store.get_session("s_dry")
    assert reread is not None and reread.origin == "workflow_dryrun"
    # Original normal session is unaffected (isolation).
    assert (await store.get_session("s_normal")).origin == "normal"
    # Nonexistent session → False.
    assert await store.set_origin("ghost", "workflow_dryrun") is False
