"""CCA P3 · S2.1 — team_subagent data layer (tech-design §1).

Mirrors team_tool_source: team-scoped, IDOR-safe (every read/write ANDs team_id),
BEGIN IMMEDIATE dup-check on (team_id, name), JSON columns (tool_refs / skills /
output_schema) round-trip. The peer/routing `name` is immutable post-create.

asyncio.run wrappers (no pytest-asyncio dep), same as test_team_tool_source.py.
"""

from __future__ import annotations

import asyncio

import pytest
from bowhead_core.user_store import TeamSubagent, UserStore, _id, _now


def _run(coro):
    return asyncio.run(coro)


def _sa(team_id="team_a", name="sales-expert", **kw) -> TeamSubagent:
    return TeamSubagent(
        id=_id("osa"),
        team_id=team_id,
        name=name,
        created_by="u1",
        created_at=_now(),
        updated_at=_now(),
        **kw,
    )


async def _store(tmp_path) -> UserStore:
    s = UserStore(str(tmp_path / "u.db"))
    await s.initialize()
    return s


def test_create_get_round_trip_all_fields(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        sa = _sa(
            display_name="Sales Expert",
            description="answers sales questions",
            system_prompt="You are a sales analyst.",
            tool_refs=["teammcp.crm.getDeal", "bi_query"],
            model="analyst",
            max_iterations=12,
            skills=["analyst-method-unit-economics"],
            output_schema={"type": "object", "additionalProperties": False},
        )
        await s.create_team_subagent(sa)
        got = await s.get_team_subagent("team_a", sa.id)
        assert got is not None
        assert got.name == "sales-expert"
        assert got.tool_refs == ["teammcp.crm.getDeal", "bi_query"]  # JSON list round-trip
        assert got.skills == ["analyst-method-unit-economics"]
        assert got.output_schema == {"type": "object", "additionalProperties": False}
        assert got.model == "analyst" and got.max_iterations == 12
        assert got.status == "enabled" and got.visibility == "team_wide"

    _run(go())


def test_get_by_name_only_enabled(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        await s.create_team_subagent(_sa(name="active-one"))
        await s.create_team_subagent(_sa(name="disabled-one", status="disabled"))
        assert (await s.get_team_subagent_by_name("team_a", "active-one")) is not None
        # only_enabled (default True) hides a disabled record from dispatch resolve
        assert (await s.get_team_subagent_by_name("team_a", "disabled-one")) is None
        assert (await s.get_team_subagent_by_name("team_a", "disabled-one", only_enabled=False)) is not None

    _run(go())


def test_idor_cross_team_reads_return_none(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        sa = _sa(team_id="team_a", name="secret-expert")
        await s.create_team_subagent(sa)
        # team_b must NOT see team_a's subagent — by id OR by name
        assert (await s.get_team_subagent("team_b", sa.id)) is None
        assert (await s.get_team_subagent_by_name("team_b", "secret-expert")) is None
        assert (await s.list_team_subagents("team_b")) == []

    _run(go())


def test_duplicate_name_in_team_rejected(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        await s.create_team_subagent(_sa(name="dup"))
        with pytest.raises(ValueError):
            await s.create_team_subagent(_sa(name="dup"))
        # same name in a DIFFERENT team is fine (per-team uniqueness)
        await s.create_team_subagent(_sa(team_id="team_b", name="dup"))

    _run(go())


def test_list_only_enabled(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        await s.create_team_subagent(_sa(name="a"))
        await s.create_team_subagent(_sa(name="b", status="disabled"))
        assert {x.name for x in await s.list_team_subagents("team_a")} == {"a", "b"}
        assert {x.name for x in await s.list_team_subagents("team_a", only_enabled=True)} == {"a"}

    _run(go())


def test_update_meta_changes_fields_but_not_name(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        sa = _sa(name="fixed-name", description="old")
        await s.create_team_subagent(sa)
        await s.update_team_subagent_meta(
            "team_a",
            sa.id,
            description="new",
            tool_refs=["teammcp.x.y"],
            status="disabled",
        )
        got = await s.get_team_subagent("team_a", sa.id, only_enabled=False)
        assert got is not None
        assert got.description == "new"
        assert got.tool_refs == ["teammcp.x.y"]
        assert got.status == "disabled"
        assert got.name == "fixed-name"  # routing identity immutable post-create

    _run(go())


def test_delete_is_team_bound(tmp_path) -> None:
    async def go():
        s = await _store(tmp_path)
        sa = _sa(name="del-me")
        await s.create_team_subagent(sa)
        assert (await s.delete_team_subagent("team_b", sa.id)) is False  # wrong team → no-op
        assert (await s.get_team_subagent("team_a", sa.id)) is not None
        assert (await s.delete_team_subagent("team_a", sa.id)) is True
        assert (await s.get_team_subagent("team_a", sa.id)) is None

    _run(go())


# ── review fix (round 2): _UNSET omit-vs-null at the STORE layer, raw-column asserted ──
# The route-level test cannot tell SQL NULL from the string "null" (json.loads("null") is None),
# so these read the raw output_schema_json / model columns via aiosqlite.


async def _raw_col(s: UserStore, sid: str, col: str):
    import aiosqlite

    async with aiosqlite.connect(s.db_path) as db:  # type: ignore[attr-defined]  # noqa: SIM117
        async with db.execute(f"SELECT {col} FROM team_subagent WHERE id=?", (sid,)) as cur:
            row = await cur.fetchone()
    return None if row is None else row[0]


def test_update_meta_omit_keeps_output_schema_and_model(tmp_path) -> None:
    """_UNSET (the default — field NOT passed) must KEEP the column: an unrelated update does
    not wipe model/output_schema."""

    async def go():
        s = await _store(tmp_path)
        sa = _sa(name="keep-os", model="analyst", output_schema={"type": "object", "additionalProperties": False})
        await s.create_team_subagent(sa)
        await s.update_team_subagent_meta("team_a", sa.id, description="changed")  # omit both
        assert (await _raw_col(s, sa.id, "model")) == "analyst"  # kept
        raw = await _raw_col(s, sa.id, "output_schema_json")
        assert raw is not None and "object" in raw  # JSON kept

    _run(go())


def test_update_meta_explicit_none_writes_sql_null_not_string_null(tmp_path) -> None:
    """explicit None must write a REAL SQL NULL (not json.dumps(None) == 'null'); raw column
    asserted so a regression to the string 'null' is caught (route tests can't see it)."""

    async def go():
        s = await _store(tmp_path)
        sa = _sa(name="clear-os", model="analyst", output_schema={"type": "object", "additionalProperties": False})
        await s.create_team_subagent(sa)
        await s.update_team_subagent_meta("team_a", sa.id, model=None, output_schema=None)
        assert (await _raw_col(s, sa.id, "output_schema_json")) is None  # SQL NULL, not "null"
        assert (await _raw_col(s, sa.id, "model")) is None  # SQL NULL
        # and it round-trips back to None through the model
        got = await s.get_team_subagent("team_a", sa.id, only_enabled=False)
        assert got is not None and got.output_schema is None and got.model is None

    _run(go())
