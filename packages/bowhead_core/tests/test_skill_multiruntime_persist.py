"""skill-multiruntime S1 (G5) —— risk_report 落库单测.

覆盖：personal skill_instance_store 的 risk_report_json 往返 + 存量表 PRAGMA 迁移；
team_skill 表迁移加列。风险信号只落库（S1 不做硬闸，FE 展示 defer）。
"""

from __future__ import annotations

import aiosqlite
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import UserStore


async def test_personal_risk_report_roundtrip(tmp_path) -> None:
    store = SkillInstanceStore(tmp_path / "s.db")
    await store.initialize()
    inst = await store.upsert(
        user_id="u1",
        name="s1",
        source_type="user_upload",
        source_path="/x",
        risk_report_json='{"langs": {".sh": 1}, "keywords": ["curl "]}',
    )
    assert inst.risk_report_json == '{"langs": {".sh": 1}, "keywords": ["curl "]}'
    got = await store.get(user_id="u1", name="s1")
    assert got is not None
    assert got.risk_report_json == '{"langs": {".sh": 1}, "keywords": ["curl "]}'


async def test_personal_default_none(tmp_path) -> None:
    store = SkillInstanceStore(tmp_path / "s.db")
    await store.initialize()
    inst = await store.upsert(user_id="u1", name="s1", source_type="user_upload", source_path="/x")
    assert inst.risk_report_json is None


async def test_personal_migration_adds_column(tmp_path) -> None:
    """存量表缺 risk_report_json 列 → initialize() PRAGMA-guard ALTER 补上。"""
    db = tmp_path / "old.db"
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "CREATE TABLE skill_instances ("
            "id TEXT PRIMARY KEY, user_id TEXT, name TEXT, source_type TEXT, source_path TEXT, "
            "version TEXT, description TEXT, enabled INTEGER, source_turn_id TEXT, size_bytes INTEGER, "
            "created_at TEXT, updated_at TEXT, UNIQUE(user_id, name))"
        )
        await conn.commit()
    store = SkillInstanceStore(db)
    await store.initialize()
    inst = await store.upsert(user_id="u1", name="s1", source_type="user_upload", source_path="/x", risk_report_json='{"k": 1}')
    assert inst.risk_report_json == '{"k": 1}'


async def test_team_skill_table_has_risk_report_col(tmp_path) -> None:
    """UserStore.initialize() 的 idempotent-ALTER 给 team_skill 表加 risk_report_json。"""
    db = tmp_path / "u.db"
    us = UserStore(str(db))
    await us.initialize()
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("PRAGMA table_info(team_skill)")
        cols = {row[1] for row in await cur.fetchall()}
    assert "risk_report_json" in cols
