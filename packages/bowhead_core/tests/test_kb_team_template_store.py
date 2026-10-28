"""kb-matrix-sharing: KbTeamTemplateStore UT。

覆盖：team 隔离 / get 与 revoke 的 team-keyed（IDOR-safe）/ 幂等 republish 保原署名 /
软撤销 + relist / revoke_by_source 级联 / republish 复活软撤销行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.kb.team_template_store import KbTeamTemplateStore

if TYPE_CHECKING:
    from pathlib import Path

TEAM_A = "team_a"
TEAM_B = "team_b"
_SCHEMA1 = '{"fields": [{"name": "金额", "type": "currency"}]}'
_SCHEMA3 = '{"fields": [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}, {"name": "c", "type": "string"}]}'


async def _store(tmp_path: Path) -> KbTeamTemplateStore:
    s = KbTeamTemplateStore(tmp_path / "otpl.db")
    await s.initialize()
    return s


async def _pub(
    s: KbTeamTemplateStore,
    *,
    team: str = TEAM_A,
    source: str = "dt_1",
    ws: str = "ws_a",
    name: str = "合同审阅",
    by: str = "u1",
    schema: str = _SCHEMA1,
    fc: int = 1,
):
    return await s.publish(
        team_id=team,
        source_doc_type_id=source,
        source_workspace_id=ws,
        name=name,
        description="",
        schema_json=schema,
        field_count=fc,
        published_by=by,
    )


async def test_publish_then_list(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    entry, was_re = await _pub(s)
    assert was_re is False
    assert entry.entry_id.startswith("ot_")
    rows = await s.list_live(TEAM_A)
    assert len(rows) == 1
    assert rows[0].source_doc_type_id == "dt_1"


async def test_list_live_team_isolation(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await _pub(s, team=TEAM_A, source="dt_a")
    await _pub(s, team=TEAM_B, source="dt_b")
    a = await s.list_live(TEAM_A)
    b = await s.list_live(TEAM_B)
    assert len(a) == 1
    assert len(b) == 1
    assert a[0].source_doc_type_id == "dt_a"
    assert b[0].source_doc_type_id == "dt_b"


async def test_get_team_keyed(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    entry, _ = await _pub(s, team=TEAM_A)
    assert await s.get(entry.entry_id, team_id=TEAM_A) is not None
    # Cross-team →None (IDOR-safe by construction; caller uses uniform 404 against enumeration).
    assert await s.get(entry.entry_id, team_id=TEAM_B) is None


async def test_republish_idempotent_preserves_attribution(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    e1, was1 = await _pub(s, name="v1", by="u1", fc=1, schema=_SCHEMA1)
    e2, was2 = await _pub(s, name="v2", by="u2", fc=3, schema=_SCHEMA3)
    assert was1 is False
    assert was2 is True
    assert e1.entry_id == e2.entry_id  # Same row (idempotent per source).
    assert e2.name == "v2"
    assert e2.field_count == 3  # Refresh snapshot.
    assert e2.published_by == "u1"  # Retain initial publisher attribution.
    assert e2.published_at == e1.published_at  # Sort anchor unchanged.
    assert len(await s.list_live(TEAM_A)) == 1  # Still one.


async def test_revoke_soft_and_relist(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    entry, _ = await _pub(s)
    assert await s.revoke(entry.entry_id, team_id=TEAM_A) is True
    assert await s.list_live(TEAM_A) == []  # Hidden from list.
    row = await s.get(entry.entry_id, team_id=TEAM_A)  # Soft revoke: row remains.
    assert row is not None
    assert row.revoked_at is not None
    assert await s.revoke(entry.entry_id, team_id=TEAM_A) is False  # Repeated revoke no-ops.


async def test_revoke_team_keyed(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    entry, _ = await _pub(s, team=TEAM_A)
    assert await s.revoke(entry.entry_id, team_id=TEAM_B) is False  # Wrong team no-ops.
    assert len(await s.list_live(TEAM_A)) == 1  # Still live.


async def test_revoke_by_source_cascade(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    await _pub(s, source="dt_x")
    assert await s.revoke_by_source("dt_x") is True
    assert await s.list_live(TEAM_A) == []
    assert await s.revoke_by_source("dt_x") is False  # Already revoked no-ops.


async def test_republish_revives_revoked(tmp_path: Path) -> None:
    s = await _store(tmp_path)
    e1, _ = await _pub(s)
    await s.revoke(e1.entry_id, team_id=TEAM_A)
    e2, was_re = await _pub(s)
    assert was_re is True
    assert e2.entry_id == e1.entry_id
    assert e2.revoked_at is None
    assert len(await s.list_live(TEAM_A)) == 1  # Revived.


async def test_sharing_migration_adds_and_idempotent(tmp_path: Path) -> None:
    """MT-7 / §12-9：copied_from_team_template_id 加列（legacy DB）+ 重复跑幂等。"""
    import aiosqlite
    from bowhead_core.kb.doc_type_migration import migrate_kb_doc_types_add_sharing_columns

    db = str(tmp_path / "legacy.db")
    # legacy kb_doc_types WITHOUT copied_from_team_template_id
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE kb_doc_types (doc_type_id TEXT PRIMARY KEY, workspace_id TEXT, name TEXT)")
        await conn.commit()
    await migrate_kb_doc_types_add_sharing_columns(db)  # Add columns.
    await migrate_kb_doc_types_add_sharing_columns(db)  # Idempotent no-op.
    async with aiosqlite.connect(db) as conn, conn.execute("SELECT name FROM pragma_table_info('kb_doc_types')") as cur:
        cols = [r[0] for r in await cur.fetchall()]
    assert cols.count("copied_from_team_template_id") == 1
