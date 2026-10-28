from __future__ import annotations

import json
import sqlite3

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_taxonomy import (
    AgentTaxonomyConflictError,
    AgentTaxonomyError,
    AgentTaxonomyForbiddenError,
    AgentTaxonomyStore,
    normalize_taxonomy_suggestion,
    normalize_team_tag,
    validate_taxonomy_listing_conn,
)

_TIME = "2026-09-05T08:00:00+00:00"


def _seed(db_path) -> None:
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO teams(id,name,status,created_by_user_id,created_at) VALUES ('team_taxonomy','Taxonomy Team','active','user_owner',?)",
            (_TIME,),
        )
        connection.executemany(
            "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            (
                ("user_owner", "team_taxonomy", "owner@example.com", "active", _TIME),
                ("user_admin", "team_taxonomy", "admin@example.com", "active", _TIME),
                ("user_member", "team_taxonomy", "member@example.com", "active", _TIME),
            ),
        )
        connection.executemany(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            (
                ("tm_owner", "user_owner", "team_taxonomy", "member", "active", _TIME),
                ("tm_admin", "user_admin", "team_taxonomy", "team_admin", "active", _TIME),
                ("tm_member", "user_member", "team_taxonomy", "member", "active", _TIME),
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,system_prompt,tool_refs_json,
              model,max_iterations,skills_json,output_schema_json,visibility,created_by,
              status,created_at,updated_at,owner_user_id,row_version,audience_mode,
              lifecycle_state,release_sequence,definition_mode
            ) VALUES (
              'osa_taxonomy','team_taxonomy','finance-partner','财务伙伴','分析经营数据',
              'Analyze financial performance.','[]',NULL,10,'[]',NULL,'creator_only','user_owner',
              'enabled',?,?,'user_owner',1,'private','active',0,'revisioned'
            )
            """,
            (_TIME, _TIME),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,base_release_id,definition_json,listing_json,
              audience_json,validation_json,row_version,updated_by,created_at,updated_at
            ) VALUES (
              'awc_taxonomy','team_taxonomy','osa_taxonomy',NULL,?, ?,
              '{"effective_mode":"private","group_ids":[],"schema_version":1,"user_ids":[]}',
              '{"status":"passed"}',1,'user_owner',?,?
            )
            """,
            (
                json.dumps({"system_prompt": "Analyze financial performance."}),
                json.dumps(
                    {"display_name": "财务伙伴", "description": "分析经营数据"},
                    ensure_ascii=False,
                ),
                _TIME,
                _TIME,
            ),
        )
        connection.commit()


@pytest.mark.asyncio
async def test_owner_reviews_suggestion_and_saves_version_bound_taxonomy(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _seed(db_path)
    store = AgentTaxonomyStore(db_path)

    snapshot = await store.agent_manager_snapshot(
        team_id="team_taxonomy",
        agent_id="osa_taxonomy",
        actor_user_id="user_owner",
        locale="zh-CN",
    )
    suggestion = await store.save_suggestion(
        team_id="team_taxonomy",
        agent_id="osa_taxonomy",
        actor_user_id="user_owner",
        suggestion={
            "domains": ["finance"],
            "jobs": ["analysis"],
            "team_tag_ids": [],
            "new_team_tags": ["月度经营"],
            "confidence": 0.88,
            "reasons": ["介绍明确包含财务分析"],
        },
        expected_source_hash=snapshot["source_hash"],
    )
    saved = await store.save_agent_taxonomy(
        team_id="team_taxonomy",
        agent_id="osa_taxonomy",
        actor_user_id="user_owner",
        domains=["finance"],
        jobs=["analysis"],
        team_tag_ids=[],
        new_team_tags=["月度经营"],
        expected_working_copy_row_version=1,
        suggestion_id=suggestion.id,
    )

    assert saved["working_copy_row_version"] == 2
    assert saved["selection"]["domains"] == ["finance"]
    assert saved["selection"]["jobs"] == ["analysis"]
    assert len(saved["selection"]["team_tag_ids"]) == 1
    assert saved["suggestion"]["state"] == "accepted"
    with sqlite3.connect(db_path) as connection:
        listing, validation = connection.execute(
            "SELECT listing_json,validation_json FROM team_subagent_working_copy WHERE id='awc_taxonomy'",
        ).fetchone()
        actions = connection.execute(
            "SELECT action FROM admin_audit_log WHERE team_id='team_taxonomy' ORDER BY rowid",
        ).fetchall()
    decoded = json.loads(listing)
    assert decoded["taxonomy"] == {
        "domains": ["finance"],
        "jobs": ["analysis"],
        "origin": "llm",
        "team_tag_ids": saved["selection"]["team_tag_ids"],
        "version": "agent-taxonomy-v1",
    }
    assert json.loads(validation) == {"status": "not_validated"}
    assert actions == [
        ("agent_taxonomy_suggested",),
        ("agent_team_tag_created",),
        ("agent_taxonomy_confirmed",),
    ]


@pytest.mark.asyncio
async def test_taxonomy_requires_owner_and_rejects_stale_working_copy(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _seed(db_path)
    store = AgentTaxonomyStore(db_path)

    with pytest.raises(AgentTaxonomyForbiddenError):
        await store.agent_manager_snapshot(
            team_id="team_taxonomy",
            agent_id="osa_taxonomy",
            actor_user_id="user_member",
        )
    with pytest.raises(AgentTaxonomyConflictError, match="working_copy_version_conflict"):
        await store.save_agent_taxonomy(
            team_id="team_taxonomy",
            agent_id="osa_taxonomy",
            actor_user_id="user_owner",
            domains=["finance"],
            jobs=["analysis"],
            team_tag_ids=[],
            new_team_tags=[],
            expected_working_copy_row_version=2,
        )


@pytest.mark.asyncio
async def test_admin_merge_keeps_alias_resolution_and_protects_target(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _seed(db_path)
    store = AgentTaxonomyStore(db_path)

    with pytest.raises(AgentTaxonomyForbiddenError):
        await store.create_team_tag(
            team_id="team_taxonomy",
            actor_user_id="user_member",
            display_name="Budget",
        )
    old = await store.create_team_tag(
        team_id="team_taxonomy",
        actor_user_id="user_admin",
        display_name="Budget",
    )
    target = await store.create_team_tag(
        team_id="team_taxonomy",
        actor_user_id="user_admin",
        display_name="Financial planning",
    )
    merged = await store.change_team_tag_status(
        team_id="team_taxonomy",
        actor_user_id="user_admin",
        tag_id=old.id,
        action="merge",
        expected_row_version=old.row_version,
        target_tag_id=target.id,
    )

    assert merged.status == "merged"
    resolution = await store.tag_resolution(team_id="team_taxonomy")
    assert resolution[old.id].id == target.id
    assert resolution[target.id].id == target.id
    with pytest.raises(AgentTaxonomyConflictError, match="has_aliases"):
        await store.change_team_tag_status(
            team_id="team_taxonomy",
            actor_user_id="user_admin",
            tag_id=target.id,
            action="deactivate",
            expected_row_version=target.row_version,
        )


def test_taxonomy_normalization_rejects_risk_spoofing_and_unknown_values() -> None:
    with pytest.raises(AgentTaxonomyError, match="reserved"):
        normalize_team_tag("低风险")
    with pytest.raises(AgentTaxonomyError, match="reserved"):
        normalize_team_tag("HIGH-RISK")
    with pytest.raises(AgentTaxonomyError, match="selection_invalid"):
        normalize_taxonomy_suggestion(
            {
                "domains": ["not-a-domain"],
                "jobs": [],
                "team_tag_ids": [],
                "new_team_tags": [],
                "confidence": 1,
                "reasons": [],
            },
            allowed_team_tag_ids=set(),
        )


@pytest.mark.asyncio
async def test_owner_can_repair_malformed_legacy_taxonomy(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _seed(db_path)
    with sqlite3.connect(db_path) as connection:
        listing = {
            "display_name": "财务伙伴",
            "description": "分析经营数据",
            "taxonomy": {
                "version": "agent-taxonomy-v1",
                "domains": "finance",
                "jobs": [],
                "team_tag_ids": [],
                "origin": "manual",
            },
        }
        connection.execute(
            "UPDATE team_subagent_working_copy SET listing_json=? WHERE id='awc_taxonomy'",
            (json.dumps(listing, ensure_ascii=False),),
        )
        connection.commit()
    store = AgentTaxonomyStore(db_path)

    snapshot = await store.agent_manager_snapshot(
        team_id="team_taxonomy",
        agent_id="osa_taxonomy",
        actor_user_id="user_owner",
    )
    assert snapshot["selection"]["domains"] == []
    saved = await store.save_agent_taxonomy(
        team_id="team_taxonomy",
        agent_id="osa_taxonomy",
        actor_user_id="user_owner",
        domains=["finance"],
        jobs=["analysis"],
        team_tag_ids=[],
        new_team_tags=[],
        expected_working_copy_row_version=1,
    )
    assert saved["selection"]["domains"] == ["finance"]


@pytest.mark.asyncio
async def test_publication_taxonomy_requires_complete_envelope_and_resolvable_tags(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _seed(db_path)
    store = AgentTaxonomyStore(db_path)
    tag = await store.create_team_tag(
        team_id="team_taxonomy",
        actor_user_id="user_admin",
        display_name="Planning",
    )

    async with aiosqlite.connect(db_path) as db:
        await validate_taxonomy_listing_conn(
            db,
            team_id="team_taxonomy",
            listing={
                "taxonomy": {
                    "version": "agent-taxonomy-v1",
                    "domains": ["finance"],
                    "jobs": ["analysis"],
                    "team_tag_ids": [tag.id],
                    "origin": "manual",
                },
            },
        )
        with pytest.raises(AgentTaxonomyError, match="selection_invalid"):
            await validate_taxonomy_listing_conn(
                db,
                team_id="team_taxonomy",
                listing={
                    "taxonomy": {
                        "version": "agent-taxonomy-v1",
                        "domains": ["finance"],
                        "jobs": ["analysis"],
                        "origin": "manual",
                    },
                },
            )
        with pytest.raises(AgentTaxonomyError, match="selection_invalid"):
            await validate_taxonomy_listing_conn(
                db,
                team_id="team_taxonomy",
                listing={
                    "taxonomy": {
                        "version": "agent-taxonomy-v1",
                        "domains": None,
                        "jobs": ["analysis"],
                        "team_tag_ids": [],
                        "origin": "manual",
                    },
                },
            )
        with pytest.raises(AgentTaxonomyError, match="unavailable"):
            await validate_taxonomy_listing_conn(
                db,
                team_id="team_taxonomy",
                listing={
                    "taxonomy": {
                        "version": "agent-taxonomy-v1",
                        "domains": ["finance"],
                        "jobs": ["analysis"],
                        "team_tag_ids": ["att_00000000000000000000000000000000"],
                        "origin": "manual",
                    },
                },
            )
