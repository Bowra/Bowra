from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_behavior_materializer import AgentBehaviorBackfillService, AgentBehaviorMaterializer
from bowhead_core.agent_dependency_compiler import (
    AgentDependencyCompilation,
    AgentRiskAssessment,
    ExactSkillRuntimeScope,
)

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_TIMESTAMP = "2026-09-06T00:00:00+00:00"


def _migrated_db(tmp_path: Path) -> Path:
    requested = tmp_path / "requested.db"
    assert upgrade(f"sqlite+aiosqlite:///{requested}", target="core") == ("core",)
    return tmp_path / "bowhead.db"


def _seed_revisions(db_path: Path) -> None:
    team_definition = json.dumps({}, sort_keys=True, separators=(",", ":"))
    system_definition = json.dumps(
        {
            "llm_agent_type": "analyst",
            "name": "compute",
            "primary_capability_ref": "cap_compute@1",
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_behavior','Team','active',?)",
            (_TIMESTAMP,),
        )
        db.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES (
              'osa_behavior','team_behavior','behavior','Behavior','Behavior','[]','[]',
              'private','enabled',?,?
            )
            """,
            (_TIMESTAMP, _TIMESTAMP),
        )
        db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (
              'arv_behavior_team','team_behavior','osa_behavior',1,'authored',?,'{}',
              ?,?,?,'agent-risk-v1','{}','user_behavior',?
            )
            """,
            (team_definition, _HASH_A, _HASH_B, _HASH_C, _TIMESTAMP),
        )
        db.execute(
            """
            INSERT INTO system_agent_effective_revision(
              id,team_id,agent_id,manifest_fingerprint,override_version,
              definition_json,definition_hash,created_at
            ) VALUES (
              'arv_behavior_system','team_behavior','system:compute',?,NULL,?,?,?
            )
            """,
            (_HASH_C, system_definition, _HASH_B, _TIMESTAMP),
        )
        db.commit()


def _ready_dependency() -> AgentDependencyCompilation:
    return AgentDependencyCompilation(
        status="ready",
        reason_codes=(),
        dependency_fingerprint=_HASH_B,
        capability_fingerprint=_HASH_C,
        tools=(),
        skills=(),
        skill_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=()),
        risk=AgentRiskAssessment(
            level="low",
            data="none",
            effect="compute",
            automation="interactive",
            scope="user",
            reversibility="reversible",
            reasons=(),
            unknown_dependencies=(),
            approval_required=False,
            runtime_confirmation_required=False,
        ),
    )


def test_materializer_accepts_only_exact_authored_primary_and_never_promotes_shadow() -> None:
    descriptor = AgentBehaviorMaterializer().compile_team_revision(
        revision_id="arv_behavior_team",
        definition={"primary_capability_ref": {"id": "cap_compute", "version": 1}},
        source_definition_hash=_HASH_A,
        dependency=_ready_dependency(),
    )

    assert descriptor.verification_state == "blocked"
    assert descriptor.primary_capability_ref is None
    assert descriptor.eligible_lead_role_ids == ()
    assert "primary_capability_unproven:cap_compute@1" in descriptor.reason_codes


@pytest.mark.asyncio
async def test_backfill_is_dry_run_paged_idempotent_and_verifiable(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    _seed_revisions(db_path)
    service = AgentBehaviorBackfillService(db_path)

    preview = await service.run_page(limit=10)
    assert preview.scanned_count == 2
    assert preview.missing_count == 2
    assert preview.inserted_count == 0
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_revision_behavior").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM system_agent_revision_behavior").fetchone() == (0,)

    applied = await service.run_page(limit=10, apply=True)
    assert applied.inserted_count == 2
    assert applied.blocked_count == 2
    assert applied.primary_declared_count == 1
    assert applied.primary_effective_count == 0
    assert await service.verify() == {
        "scanned": 2,
        "missing": 0,
        "valid": 2,
        "invalid": 0,
        "blocked": 2,
        "shadow": 0,
        "reviewed": 0,
    }

    replay = await service.run_page(limit=10, apply=True)
    assert replay.inserted_count == 0
    assert replay.valid_count == 2


@pytest.mark.asyncio
async def test_backfill_reports_invalid_immutable_sidecar_without_rewriting_it(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    _seed_revisions(db_path)
    incomplete = json.dumps(
        {
            "schema_version": 1,
            "compiler_version": "agent-behavior-v1",
            "source_definition_hash": _HASH_A,
            "behavior_fingerprint": _HASH_C,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO team_subagent_revision_behavior(
              revision_id,team_id,subagent_id,source_definition_hash,descriptor_json,
              behavior_fingerprint,compiler_version,created_at
            ) VALUES ('arv_behavior_team','team_behavior','osa_behavior',?,?,?,?,?)
            """,
            (_HASH_A, incomplete, _HASH_C, "agent-behavior-v1", _TIMESTAMP),
        )
        db.commit()

    result = await AgentBehaviorBackfillService(db_path).run_page(limit=10, apply=True)

    assert result.invalid_count == 1
    assert result.inserted_count == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT descriptor_json FROM team_subagent_revision_behavior",
        ).fetchone() == (incomplete,)
