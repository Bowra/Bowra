"""批 1-a ConfigDraftStore 单测（tech-design-batch1-draft-store.md §9 测 1-3）.

owner-scoped 读、envelope round-trip、CAS 状态机全矩阵、maintenance 只触 TTL 非空行
（lite_proposal 天然零触碰）。schema 由 alembic upgrade 供给（迁移即被测面的一部分）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.config_draft_store import (
    FORM_STATUS_CONSUMED,
    FORM_STATUS_DISCARDED,
    FORM_STATUS_DRAFT,
    KIND_FORM_DRAFT,
    LITE_STATUS_APPLIED,
    LITE_STATUS_IGNORED,
    LITE_STATUS_PROPOSED,
    LITE_STATUS_REVERTED,
    ConfigDraftSchemaUnavailableError,
    ConfigDraftStore,
)
from bowhead_core.migrations.baseline_schema import initial_schema

_ROWS = [{"field": "mode", "from": "light", "to": "dark", "from_unset": True}]


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return str(path)


@pytest.fixture
def store(db_path: str) -> ConfigDraftStore:
    return ConfigDraftStore(db_path)


async def _create(store: ConfigDraftStore, *, draft_id: str = "ccd_1") -> None:
    await store.create_lite_proposal(
        draft_id=draft_id,
        team_id="team_1",
        owner_user_id="u_1",
        form_id="user_appearance",
        workspace_id=None,
        turn_id="t_1",
        session_id="s_1",
        rows=_ROWS,
        notes={"mode": "提示"},
        workspace_name=None,
    )


@pytest.mark.asyncio
async def test_initialize_verifies_schema(store: ConfigDraftStore, tmp_path: Path) -> None:
    await store.initialize()  # migrated db → ok
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(ConfigDraftSchemaUnavailableError):
        await ConfigDraftStore(str(bare)).initialize()


@pytest.mark.asyncio
async def test_create_get_owner_scoped_and_envelope_roundtrip(store: ConfigDraftStore) -> None:
    await _create(store)
    draft = await store.get_draft("ccd_1", team_id="team_1", owner_user_id="u_1")
    assert draft is not None
    assert draft.status == LITE_STATUS_PROPOSED
    assert draft.payload == {"rows": _ROWS, "notes": {"mode": "提示"}, "workspace_name": None}
    # lite: all three TTL columns NULL (undo has no time limit by decision)
    assert draft.expires_at is None and draft.content_scrub_at is None and draft.purge_at is None
    # Cross-owner / cross-team both return None (both 404 at routing layer).
    assert await store.get_draft("ccd_1", team_id="team_1", owner_user_id="u_2") is None
    assert await store.get_draft("ccd_1", team_id="team_2", owner_user_id="u_1") is None


@pytest.mark.asyncio
async def test_transition_matrix(store: ConfigDraftStore) -> None:
    await _create(store)
    kw = {"team_id": "team_1", "owner_user_id": "u_1"}

    ok = await store.transition("ccd_1", to_status=LITE_STATUS_APPLIED, from_statuses=(LITE_STATUS_PROPOSED,), **kw)
    assert ok.outcome == "ok"
    assert ok.draft is not None and ok.draft.status == LITE_STATUS_APPLIED
    assert ok.draft.applied_at is not None
    assert ok.draft.row_version == 2

    replay = await store.transition("ccd_1", to_status=LITE_STATUS_APPLIED, from_statuses=(LITE_STATUS_PROPOSED,), **kw)
    assert replay.outcome == "replay"
    assert replay.draft is not None and replay.draft.row_version == 2  # Idempotent success without rewriting

    undo = await store.transition("ccd_1", to_status=LITE_STATUS_REVERTED, from_statuses=(LITE_STATUS_APPLIED,), **kw)
    assert undo.outcome == "ok"
    assert undo.draft is not None and undo.draft.reverted_at is not None

    conflict = await store.transition("ccd_1", to_status=LITE_STATUS_APPLIED, from_statuses=(LITE_STATUS_PROPOSED,), **kw)
    assert conflict.outcome == "conflict"
    assert conflict.draft is not None and conflict.draft.status == LITE_STATUS_REVERTED

    missing = await store.transition("nope", to_status=LITE_STATUS_IGNORED, from_statuses=(LITE_STATUS_PROPOSED,), **kw)
    assert missing.outcome == "not_found"

    # Owner isolation: others cannot transition it.
    other = await store.transition(
        "ccd_1", to_status=LITE_STATUS_APPLIED, from_statuses=(LITE_STATUS_APPLIED,), team_id="team_1", owner_user_id="u_2"
    )
    assert other.outcome == "not_found"


# ── Batch 1-b: three form_draft methods (tech-design-batch1b §5 test 1) ──


async def _create_form(store: ConfigDraftStore, *, draft_id: str = "ccd_f1", session_id: str = "s_1") -> None:
    await store.create_form_draft(
        draft_id=draft_id,
        team_id="team_1",
        owner_user_id="u_1",
        form_id="scheduled_job",
        workspace_id="ws_1",
        turn_id="t_1",
        session_id=session_id,
        fields={"name": "周报"},
        field_meta={"name": {"origin": "generated", "needs_confirm": False, "note": None}},
        notes={},
    )


@pytest.mark.asyncio
async def test_create_form_draft_ttl_shape(store: ConfigDraftStore) -> None:
    await _create_form(store)
    draft = await store.get_draft("ccd_f1", team_id="team_1", owner_user_id="u_1")
    assert draft is not None
    assert draft.kind == KIND_FORM_DRAFT
    assert draft.status == FORM_STATUS_DRAFT
    assert draft.expires_at is not None  # Active TTL (expired chip + find_active freshness)
    # scrub/purge both NULL: fields persist forever with events in turn_log; cleanup has no benefit and distorts history (batch1b §2.1).
    assert draft.content_scrub_at is None and draft.purge_at is None
    assert draft.payload == {
        "fields": {"name": "周报"},
        "field_meta": {"name": {"origin": "generated", "needs_confirm": False, "note": None}},
        "notes": {},
        "result": None,
    }


@pytest.mark.asyncio
async def test_find_active_form_draft_scoping(store: ConfigDraftStore) -> None:
    await _create_form(store)
    kw = {"team_id": "team_1", "owner_user_id": "u_1", "session_id": "s_1", "form_id": "scheduled_job"}
    assert (await store.find_active_form_draft(**kw)).id == "ccd_f1"
    assert await store.find_active_form_draft(**{**kw, "session_id": "s_other"}) is None
    assert await store.find_active_form_draft(**{**kw, "form_id": "other_form"}) is None
    # Not active after consumed; find_latest still sees it (nearby query for the consumed guard).
    await store.transition(
        "ccd_f1",
        team_id="team_1",
        owner_user_id="u_1",
        to_status=FORM_STATUS_CONSUMED,
        from_statuses=(FORM_STATUS_DRAFT,),
        result_patch={"job_id": "sj_1", "name": "周报"},
    )
    assert await store.find_active_form_draft(**kw) is None
    latest = await store.find_latest_form_draft(**kw)
    assert latest is not None and latest.status == FORM_STATUS_CONSUMED
    assert latest.payload["result"] == {"job_id": "sj_1", "name": "周报"}  # Receipt stored in payload
    assert latest.payload["fields"] == {"name": "周报"}  # result does not overwrite content


@pytest.mark.asyncio
async def test_update_form_draft_fields_cas(store: ConfigDraftStore) -> None:
    await _create_form(store)
    kw = {"team_id": "team_1", "owner_user_id": "u_1"}
    ok = await store.update_form_draft_fields("ccd_f1", expected_version=1, fields={"name": "月报"}, field_meta={}, notes={}, **kw)
    assert ok.outcome == "ok"
    assert ok.draft.payload["fields"] == {"name": "月报"} and ok.draft.row_version == 2

    stale = await store.update_form_draft_fields("ccd_f1", expected_version=1, fields={"name": "x"}, field_meta={}, notes={}, **kw)
    assert stale.outcome == "conflict"
    assert stale.draft is not None and stale.draft.status == FORM_STATUS_DRAFT  # Triage basis: still draft

    await store.transition("ccd_f1", to_status=FORM_STATUS_DISCARDED, from_statuses=(FORM_STATUS_DRAFT,), **kw)
    after = await store.update_form_draft_fields("ccd_f1", expected_version=3, fields={"name": "y"}, field_meta={}, notes={}, **kw)
    assert after.outcome == "conflict"  # Terminal state forbids content changes
    assert after.draft.status == FORM_STATUS_DISCARDED
    assert (await store.update_form_draft_fields("nope", expected_version=1, fields={}, field_meta={}, notes={}, **kw)).outcome == "not_found"


@pytest.mark.asyncio
async def test_maintenance_only_touches_ttl_rows(store: ConfigDraftStore, db_path: str) -> None:
    """lite_proposal（TTL 全 NULL）零触碰；带 TTL 的 form_draft 行走三段清理。"""
    await _create(store)
    past = "2020-01-01T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO config_copilot_draft(
              id, team_id, owner_user_id, kind, form_id, workspace_id, turn_id,
              session_id, status, payload_json, row_version, expires_at,
              content_scrub_at, purge_at, created_at, updated_at
            ) VALUES (?,?,?,?,?,NULL,NULL,NULL,?,?,1,?,?,NULL,?,?)
            """,
            (
                "ccd_form",
                "team_1",
                "u_1",
                KIND_FORM_DRAFT,
                "scheduled_job",
                "draft",
                '{"schema_version":1,"kind":"config_form_draft","value":{"fields":{}}}',
                past,
                past,
                past,
                past,
            ),
        )
        connection.commit()

    result = await store.run_maintenance()
    assert result.expired == 1  # form_draft: draft→expired
    assert result.scrubbed == 1  # Set payload to NULL
    assert result.purged == 0  # NULL purge_at prevents deletion

    lite = await store.get_draft("ccd_1", team_id="team_1", owner_user_id="u_1")
    assert lite is not None
    assert lite.status == LITE_STATUS_PROPOSED
    assert lite.payload is not None  # Rows persist forever; undo has no time limit

    form = await store.get_draft("ccd_form", team_id="team_1", owner_user_id="u_1")
    assert form is not None
    assert form.status == "expired"
    assert form.payload is None

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE config_copilot_draft SET purge_at=? WHERE id='ccd_form'", (past,))
        connection.commit()
    assert (await store.run_maintenance()).purged == 1
    assert await store.get_draft("ccd_form", team_id="team_1", owner_user_id="u_1") is None


# ── Batch 3 / 3-0b: two new payload-envelope keys ─────────────────────────────


@pytest.mark.asyncio
async def test_form_payload_defaults_are_byte_identical(store: ConfigDraftStore) -> None:
    """缺省等价性：不传两个新键时，payload 的键集与批 1-b 完全相同。

    单例面（批 0~1 的五个面）永远走这条路——多出一个键就会随 config.draft_updated
    永存 turn_log 并进 GET DTO，那是行为变更不是纯增量。
    """
    await _create_form(store)
    draft = await store.get_draft("ccd_f1", team_id="team_1", owner_user_id="u_1")
    assert draft is not None and draft.payload is not None
    assert set(draft.payload) == {"fields", "field_meta", "notes", "result"}


@pytest.mark.asyncio
async def test_form_payload_carries_target_and_cumulative(store: ConfigDraftStore) -> None:
    await store.create_form_draft(
        draft_id="ccd_f2",
        team_id="team_1",
        owner_user_id="u_1",
        form_id="team_subagent",
        workspace_id=None,
        turn_id="t_1",
        session_id="s_1",
        fields={"description": "改后的描述"},
        field_meta={},
        notes={},
        target={"id": "osa_1", "label": "数据查询师", "form_id": "team_subagent"},
        changed_fields_cumulative=["description"],
    )
    draft = await store.get_draft("ccd_f2", team_id="team_1", owner_user_id="u_1")
    assert draft is not None and draft.payload is not None
    assert draft.payload["target"]["id"] == "osa_1"
    assert draft.payload["changed_fields_cumulative"] == ["description"]


@pytest.mark.asyncio
async def test_update_is_whole_payload_overwrite_target_must_be_replayed(store: ConfigDraftStore) -> None:
    """把 r1-P1-2 那个坑钉成**显式契约**：update 是整包覆盖，不做隐式合并。

    调用方不回传 target ⇒ target 被抹掉。这不是 bug，是「payload 由调用方全权决定」
    的既有语义；但它正是 r1-P1-2 的成因（第三轮起 target 恒 None ⇒ 换目标判据恒失效），
    所以必须有一条测试写明它，而不是靠下一个人记得。
    """
    await store.create_form_draft(
        draft_id="ccd_f3",
        team_id="team_1",
        owner_user_id="u_1",
        form_id="team_subagent",
        workspace_id=None,
        turn_id="t_1",
        session_id="s_1",
        fields={"description": "v1"},
        field_meta={},
        notes={},
        target={"id": "osa_1", "label": "A"},
        changed_fields_cumulative=["description"],
    )
    got = await store.get_draft("ccd_f3", team_id="team_1", owner_user_id="u_1")
    assert got is not None

    # ① Not returned → erased (negative case)
    await store.update_form_draft_fields(
        "ccd_f3",
        team_id="team_1",
        owner_user_id="u_1",
        expected_version=got.row_version,
        fields={"description": "v2"},
        field_meta={},
        notes={},
    )
    after = await store.get_draft("ccd_f3", team_id="team_1", owner_user_id="u_1")
    assert after is not None and after.payload is not None
    assert "target" not in after.payload

    # ② Explicitly returned → retained (positive control: proves the store supports target at all)
    await store.update_form_draft_fields(
        "ccd_f3",
        team_id="team_1",
        owner_user_id="u_1",
        expected_version=after.row_version,
        fields={"description": "v3"},
        field_meta={},
        notes={},
        target={"id": "osa_1", "label": "A"},
        changed_fields_cumulative=["description"],
    )
    final = await store.get_draft("ccd_f3", team_id="team_1", owner_user_id="u_1")
    assert final is not None and final.payload is not None
    assert final.payload["target"]["id"] == "osa_1"
    assert final.payload["changed_fields_cumulative"] == ["description"]
