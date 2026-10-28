from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.subagent_draft_store import (
    REQUEST_LEASE_SECONDS,
    DraftClaimOutcome,
    SubagentDraftCreate,
    SubagentDraftIdempotencyConflictError,
    SubagentDraftSchemaUnsupportedError,
    SubagentDraftStore,
    SubagentDraftVersionConflictError,
)
from bowhead_core.team_subagent_store import (
    TeamSubagentNameConflictError,
    TeamSubagentStore,
)
from bowhead_core.user_store import TeamSubagent


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _migrated_db(tmp_path) -> str:
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    return str(db_path)


@pytest.mark.asyncio
async def test_canvas_generation_receipt_keeps_human_edits_and_applies_selected_fields(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 9, 27, 1, 0, tzinfo=UTC))
    store = SubagentDraftStore(_migrated_db(tmp_path), hmac_key=b"m" * 32, clock=clock)
    draft = await store.create_manual_draft(
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        locale="zh-CN",
        purpose="分析销售",
        idempotency_key="manual-for-ai-task",
    )
    claim = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="ai-suggestion-request",
        request={"draft_id": draft.id, "base_version": 1},
    )
    assert claim.outcome is DraftClaimOutcome.ACQUIRED
    saved = await store.save_manual_draft(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        expected_version=1,
        purpose="分析销售",
        definition={"name": "my-name", "description": "人工内容"},
        text_only_confirmed=False,
    )
    assert saved is not None and saved.row_version == 2
    suggestion = await store.record_canvas_suggestion(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        request_key_hash=claim.idempotency_key_hash,
        lease_token=claim.lease_token,
        base_version=1,
        kind="definition",
        payload={"definition": {"name": "ai-name", "description": "AI 内容"}},
    )
    assert (await store.get_draft(draft.id, team_id="team_a", owner_user_id="user_a")).row_version == 2
    assert (
        await store.claim_request(
            team_id="team_a",
            owner_user_id="user_a",
            idempotency_key="ai-suggestion-request",
            request={"draft_id": draft.id, "base_version": 1},
        )
    ).outcome is DraftClaimOutcome.REPLAY
    assert (
        await store.get_canvas_suggestion_for_request(
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_b",
            request_key_hash=claim.idempotency_key_hash,
        )
        is None
    )
    assert (
        await store.list_canvas_suggestions(
            draft.id,
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_a",
        )
    )[0].id == suggestion.id
    applied, replayed = await store.apply_canvas_suggestion(
        draft.id,
        suggestion.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        expected_version=2,
        purpose="分析销售",
        definition={"name": "my-name", "description": "AI 内容"},
        selected_fields=["description"],
        mutation_id="11111111-1111-4111-8111-111111111111",
    )
    assert not replayed and applied.row_version == 3
    assert applied.definition == {"name": "my-name", "description": "AI 内容"}
    repeated, replayed = await store.apply_canvas_suggestion(
        draft.id,
        suggestion.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        expected_version=2,
        purpose="分析销售",
        definition={"name": "my-name", "description": "AI 内容"},
        selected_fields=["description"],
        mutation_id="11111111-1111-4111-8111-111111111111",
    )
    assert replayed and repeated.id == draft.id
    assert (
        await store.list_canvas_suggestions(
            draft.id,
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_a",
        )
        == []
    )
    second_claim = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="second-suggestion",
        request={"draft_id": draft.id, "base_version": 3},
    )
    second = await store.record_canvas_suggestion(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        request_key_hash=second_claim.idempotency_key_hash,
        lease_token=second_claim.lease_token,
        base_version=3,
        kind="clarification",
        payload={"question": "Which report?"},
    )
    assert not await store.dismiss_canvas_suggestion(
        draft.id,
        second.id,
        team_id="team_a",
        owner_user_id="user_b",
        workspace_id="ws_a",
    )
    assert await store.dismiss_canvas_suggestion(
        draft.id,
        second.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
    )
    assert (
        await store.list_canvas_suggestions(
            draft.id,
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_a",
        )
        == []
    )
    assert (await store.get_draft(draft.id, team_id="team_a", owner_user_id="user_a")).row_version == 3


@pytest.mark.asyncio
async def test_manual_draft_is_space_bound_resumable_and_cas_saved(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 9, 27, 1, 0, tzinfo=UTC))
    store = SubagentDraftStore(_migrated_db(tmp_path), hmac_key=b"m" * 32, clock=clock)
    draft = await store.create_manual_draft(
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        locale="zh-CN",
        purpose="只总结文本",
        idempotency_key="manual-first-task",
    )
    assert draft.status == "editing"
    assert draft.workspace_id == "ws_a"
    assert draft.definition is None
    assert (
        await store.create_manual_draft(
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_a",
            locale="zh-CN",
            purpose="只总结文本",
            idempotency_key="manual-first-task",
        )
    ).id == draft.id
    with pytest.raises(SubagentDraftIdempotencyConflictError):
        await store.create_manual_draft(
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_b",
            locale="zh-CN",
            purpose="只总结文本",
            idempotency_key="manual-first-task",
        )
    definition = {"name": "", "description": "只总结文本", "tool_refs": []}
    saved = await store.save_manual_draft(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        workspace_id="ws_a",
        expected_version=1,
        purpose="只总结文本",
        definition=definition,
        text_only_confirmed=True,
    )
    assert saved is not None and saved.row_version == 2
    assert saved.definition == definition
    assert saved.status == "editing" and saved.validation_status == "unknown"
    assert saved.warnings == [{"code": "subagent_text_only_confirmation"}]
    assert (
        await store.create_manual_draft(
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_a",
            locale="zh-CN",
            purpose="只总结文本",
            idempotency_key="manual-first-task",
        )
    ).id == draft.id
    assert (
        await store.save_manual_draft(
            draft.id,
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_b",
            expected_version=2,
            purpose="",
            definition={},
            text_only_confirmed=False,
        )
        is None
    )
    with pytest.raises(SubagentDraftVersionConflictError):
        await store.save_manual_draft(
            draft.id,
            team_id="team_a",
            owner_user_id="user_a",
            workspace_id="ws_a",
            expected_version=1,
            purpose="",
            definition={},
            text_only_confirmed=False,
        )
    assert [item.id for item in await store.list_active_drafts(team_id="team_a", owner_user_id="user_a")] == [draft.id]
    assert [
        item.id
        for item in await store.list_active_drafts(
            team_id="team_a",
            owner_user_id="user_a",
            source_mode="manual",
            workspace_id="ws_a",
        )
    ] == [draft.id]
    assert (
        await store.list_active_drafts(
            team_id="team_a",
            owner_user_id="user_a",
            source_mode="manual",
            workspace_id="ws_b",
        )
        == []
    )
    clock.value += timedelta(hours=25)
    assert await store.list_active_drafts(team_id="team_a", owner_user_id="user_a") == []


@pytest.mark.asyncio
async def test_request_claim_is_hashed_leased_and_replayable(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, 0, 0, tzinfo=UTC))
    store = SubagentDraftStore(
        _migrated_db(tmp_path),
        hmac_key=b"x" * 32,
        clock=clock,
    )
    await store.initialize()

    first = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="plain-key-never-store",
        request={"purpose": "分析销售", "locale": "zh-CN"},
    )
    assert first.outcome == DraftClaimOutcome.ACQUIRED

    waiting = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="plain-key-never-store",
        request={"purpose": "分析销售", "locale": "zh-CN"},
    )
    assert waiting.outcome == DraftClaimOutcome.IN_PROGRESS

    conflict = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="plain-key-never-store",
        request={"purpose": "不同用途", "locale": "zh-CN"},
    )
    assert conflict.outcome == DraftClaimOutcome.CONFLICT

    clock.value += timedelta(seconds=REQUEST_LEASE_SECONDS - 1)
    still_leased = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="plain-key-never-store",
        request={"purpose": "分析销售", "locale": "zh-CN"},
    )
    assert still_leased.outcome == DraftClaimOutcome.IN_PROGRESS

    clock.value += timedelta(seconds=2)
    takeover = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="plain-key-never-store",
        request={"purpose": "分析销售", "locale": "zh-CN"},
    )
    assert takeover.outcome == DraftClaimOutcome.ACQUIRED
    assert takeover.taken_over is True

    assert await store.complete_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key_hash=first.idempotency_key_hash,
        lease_token=takeover.lease_token,
        outcome_code="purpose_insufficient",
        result_draft_id=None,
    )
    replay = await store.claim_request(
        team_id="team_a",
        owner_user_id="user_a",
        idempotency_key="plain-key-never-store",
        request={"purpose": "分析销售", "locale": "zh-CN"},
    )
    assert replay.outcome == DraftClaimOutcome.REPLAY
    assert replay.outcome_code == "purpose_insufficient"

    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            """
            SELECT idempotency_key_hash, request_fingerprint
            FROM team_subagent_draft_request
            """,
        ).fetchone()
        assert row is not None
        assert "plain-key-never-store" not in row
        assert all(len(value) == 64 for value in row)


@pytest.mark.asyncio
async def test_ready_draft_is_owner_scoped_and_versioned(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, 1, 0, tzinfo=UTC))
    store = SubagentDraftStore(
        _migrated_db(tmp_path),
        hmac_key=b"y" * 32,
        clock=clock,
    )
    await store.initialize()
    draft = await store.create_draft(
        SubagentDraftCreate(
            id="sad_123456789abc",
            team_id="team_a",
            owner_user_id="user_a",
            source_mode="intent",
            source_turn_id=None,
            status="ready",
            validation_status="valid",
            locale="zh-CN",
            purpose_text="每周分析门店销售异常",
            goal_summary="识别销售异常",
            definition={
                "name": "sales-watch",
                "display_name": "销售异常分析",
                "description": "分析销售异常",
                "system_prompt": "基于证据分析。",
                "tool_refs": ["bi_query"],
                "max_iterations": 10,
                "skills": [],
                "model": "analyst",
                "output_schema": None,
                "visibility": "creator_only",
            },
            recommendations=[],
            assumptions=[],
            warnings=[],
            clarification=None,
            validation_issues=[],
            catalog_fingerprint="sha256:catalog",
            definition_fingerprint="sha256:definition",
            idempotency_key_hash="a" * 64,
            mutation_id="00000000-0000-4000-8000-000000000000",
        ),
    )
    assert draft.row_version == 1
    assert draft.definition is not None
    assert (
        await store.get_draft(
            draft.id,
            team_id="team_a",
            owner_user_id="user_b",
        )
        is None
    )
    assert (
        await store.get_draft(
            draft.id,
            team_id="team_b",
            owner_user_id="user_a",
        )
        is None
    )

    with sqlite3.connect(store.db_path) as connection:
        now = clock.now().isoformat()
        connection.execute(
            """
            INSERT INTO team_subagent_draft_request(
              team_id, owner_user_id, idempotency_key_hash,
              request_fingerprint, state, result_draft_id,
              outcome_code, lease_expires_at, expires_at,
              created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "team_a",
                "user_a",
                "a" * 64,
                "f" * 64,
                "completed",
                draft.id,
                "draft_ready",
                now,
                now,
                now,
                now,
            ),
        )
        connection.commit()

    discarded = await store.discard_draft(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
        expected_version=1,
        request_outcome_code="delivery_failed",
    )
    assert discarded is not None
    assert discarded.status == "discarded"
    assert discarded.row_version == 2
    assert discarded.purpose_text is None
    assert discarded.definition is None
    with sqlite3.connect(store.db_path) as connection:
        request = connection.execute(
            """
            SELECT state, result_draft_id, outcome_code
            FROM team_subagent_draft_request
            WHERE team_id=? AND owner_user_id=?
              AND idempotency_key_hash=?
            """,
            ("team_a", "user_a", "a" * 64),
        ).fetchone()
    assert request == ("failed", None, "delivery_failed")


@pytest.mark.asyncio
async def test_unknown_definition_envelope_fails_closed(tmp_path) -> None:
    store = SubagentDraftStore(
        _migrated_db(tmp_path),
        hmac_key=b"z" * 32,
    )
    await store.initialize()
    create = SubagentDraftCreate(
        id="sad_abcdef123456",
        team_id="team_a",
        owner_user_id="user_a",
        source_mode="intent",
        source_turn_id=None,
        status="ready",
        validation_status="valid",
        locale="en-US",
        purpose_text="Analyze weekly sales",
        goal_summary="Analyze sales",
        definition={"name": "sales-agent"},
        recommendations=[],
        assumptions=[],
        warnings=[],
        clarification=None,
        validation_issues=[],
        catalog_fingerprint="sha256:catalog",
        definition_fingerprint="sha256:definition",
        idempotency_key_hash="b" * 64,
        mutation_id="00000000-0000-4000-8000-000000000001",
    )
    draft = await store.create_draft(create)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            UPDATE team_subagent_draft
            SET definition_json='{"schema_version":99,"kind":"subagent_draft_definition","value":{}}'
            WHERE id=?
            """,
            (draft.id,),
        )
        connection.commit()

    with pytest.raises(SubagentDraftSchemaUnsupportedError):
        await store.get_draft(
            draft.id,
            team_id="team_a",
            owner_user_id="user_a",
        )


@pytest.mark.asyncio
async def test_maintenance_expires_scrubs_and_purges_in_bounded_batches(
    tmp_path,
) -> None:
    clock = FakeClock(datetime(2026, 7, 31, 2, 0, tzinfo=UTC))
    store = SubagentDraftStore(
        _migrated_db(tmp_path),
        hmac_key=b"m" * 32,
        clock=clock,
    )
    await store.initialize()
    base = SubagentDraftCreate(
        id="sad_maintenance01",
        team_id="team_a",
        owner_user_id="user_a",
        source_mode="intent",
        source_turn_id=None,
        status="blocked",
        validation_status="invalid",
        locale="zh-CN",
        purpose_text="分析销售",
        goal_summary="分析销售",
        definition={"name": "sales-agent"},
        recommendations=[],
        assumptions=[],
        warnings=[],
        clarification=None,
        validation_issues=[{"code": "blocked"}],
        catalog_fingerprint="sha256:catalog",
        definition_fingerprint=None,
        idempotency_key_hash="c" * 64,
        mutation_id="00000000-0000-4000-8000-000000000002",
    )
    await store.create_draft(base)

    clock.value += timedelta(days=1, seconds=1)
    result = await store.run_maintenance(limit=10)
    assert result.expired == 1

    clock.value += timedelta(days=6)
    result = await store.run_maintenance(limit=10)
    assert result.scrubbed == 1
    expired = await store.get_draft(
        base.id,
        team_id="team_a",
        owner_user_id="user_a",
    )
    assert expired is not None
    assert expired.purpose_text is None

    clock.value += timedelta(days=23)
    result = await store.run_maintenance(limit=10)
    assert result.purged == 1
    assert (
        await store.get_draft(
            base.id,
            team_id="team_a",
            owner_user_id="user_a",
        )
        is None
    )


@pytest.mark.asyncio
async def test_final_create_and_draft_consume_are_one_transaction(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 31, 3, 0, tzinfo=UTC))
    db_path = _migrated_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES (?,?,?,?)",
            ("team_a", "A", "active", clock.now().isoformat()),
        )
        connection.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES (?,?,?,?,?)
            """,
            (
                "user_a",
                "team_a",
                "a@example.com",
                "active",
                clock.now().isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO team_members(
                id,user_id,team_id,role,status,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                "tm_a",
                "user_a",
                "team_a",
                "member",
                "active",
                clock.now().isoformat(),
            ),
        )
        connection.commit()

    draft_store = SubagentDraftStore(
        db_path,
        hmac_key=b"q" * 32,
        clock=clock,
    )
    team_store = TeamSubagentStore(db_path, clock=clock)
    await draft_store.initialize()
    await team_store.initialize()
    first_create = SubagentDraftCreate(
        id="sad_atomiccreate1",
        team_id="team_a",
        owner_user_id="user_a",
        source_mode="intent",
        source_turn_id=None,
        status="ready",
        validation_status="valid",
        locale="zh-CN",
        purpose_text="分析销售",
        goal_summary="分析销售",
        definition={"name": "atomic-agent"},
        recommendations=[],
        assumptions=[],
        warnings=[],
        clarification=None,
        validation_issues=[],
        catalog_fingerprint="sha256:catalog",
        definition_fingerprint="sha256:def-1",
        idempotency_key_hash="d" * 64,
        mutation_id="00000000-0000-4000-8000-000000000003",
    )
    draft = await draft_store.create_draft(first_create)
    now_s = clock.now().isoformat()
    definition = TeamSubagent(
        id="osa_atomiccreate1",
        team_id="team_a",
        name="atomic-agent",
        display_name="Atomic Agent",
        description="Analyze sales",
        system_prompt="Use evidence.",
        tool_refs=["bi_query"],
        max_iterations=10,
        skills=[],
        model="analyst",
        output_schema=None,
        visibility="creator_only",
        created_by="user_a",
        status="enabled",
        created_at=now_s,
        updated_at=now_s,
    )
    await team_store.create_team_subagent(
        definition,
        actor_user_id="user_a",
        audit_action="subagent.create_from_draft",
        require_active_owner=True,
        origin_mode="intent_draft",
        source_draft_id=draft.id,
        source_draft_expected_version=draft.row_version,
        source_definition_fingerprint=draft.definition_fingerprint,
    )

    consumed = await draft_store.get_draft(
        draft.id,
        team_id="team_a",
        owner_user_id="user_a",
    )
    assert consumed is not None
    assert consumed.status == "consumed"
    assert consumed.consumed_definition_id == definition.id
    assert consumed.definition is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT origin_mode, source_draft_id, visibility, status
            FROM team_subagent WHERE id=?
            """,
            (definition.id,),
        ).fetchone() == (
            "intent_draft",
            draft.id,
            "creator_only",
            "enabled",
        )

    second = await draft_store.create_draft(
        replace(
            first_create,
            id="sad_atomiccreate2",
            definition_fingerprint="sha256:def-2",
            idempotency_key_hash="e" * 64,
            mutation_id="00000000-0000-4000-8000-000000000004",
        ),
    )
    conflicting = replace(definition, id="osa_atomiccreate2")
    with pytest.raises(TeamSubagentNameConflictError):
        await team_store.create_team_subagent(
            conflicting,
            actor_user_id="user_a",
            audit_action="subagent.create_from_draft",
            require_active_owner=True,
            origin_mode="intent_draft",
            source_draft_id=second.id,
            source_draft_expected_version=second.row_version,
            source_definition_fingerprint=second.definition_fingerprint,
        )
    still_ready = await draft_store.get_draft(
        second.id,
        team_id="team_a",
        owner_user_id="user_a",
    )
    assert still_ready is not None
    assert still_ready.status == "ready"
