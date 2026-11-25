"""Owner draft trial, slice 1: revision 0234, its three guards, trial admission, readers and blocked features.

docs/sprints/agent-page-redesign-2026-11-24/trial-tech-design.md §3–§4.5. One migrated domain layout is built
per module and copied for each test, so every case runs on the real core/execution split with the compiled
guards calling their registered predicates.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_behavior_materializer import default_agent_behavior_materializer
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    AgentExecutionBindingStore,
    _team_snapshot_by_execution,
    read_owner_trial_turn_in_connection,
)
from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferencePlan, ReferenceScope, reference_source_hash
from bowhead_core.chat_references import AgentResourceRef, InputDocument, requested_reference_operation
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.published_diagnostics import AGENT_TRIAL_BLOCKED_FEATURE_CODES, AGENT_TRIAL_REFUSAL_CODES
from bowhead_core.session_store import SessionStore, _ensure_chat_session_in_tx, _insert_chat_turn_user_source_in_tx
from bowhead_core.sqlite_domain_runtime import _TRIGGERS, connect_sync
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, domain_database_path
from bowhead_core.user_store import TeamSubagent, UserStore

pytestmark = pytest.mark.integration

_FERNET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_NOW = "2026-11-25T00:00:00+00:00"
_DRAFT_SAVED_AT = "2026-11-25T14:32:00+00:00"
AGENT = "osa_trial"
PROFILE = "trial-agent"
RELEASE = "arl_trial_1"
LIVE_REVISION = "arv_trial_1"
DRAFT_REVISION = "arv_trial_2"
DRAFT_CAPABILITY = "sha256:cap2"
CANDIDATE = "arc_trial_2"
OWNER_TURN = "t_owner_trial"
MEMBER_TURN = "t_member"
_REGISTRY_GUARDS = ("trg_turn_agent_execution_revision", "trg_turn_agent_binding_revision", "trg_turn_agent_binding_assignment")
_MIGRATION = importlib.import_module("bowhead_core.migrations.versions.0234_owner_draft_trial")


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _document(resource: AgentResourceRef) -> InputDocument:
    return InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000001",
                    "resource": resource.model_dump(),
                    "label": "Trial Agent",
                    "parameters": {},
                },
                {"type": "text", "text": " summarize last week's sales"},
            ],
        }
    )


def _trial_resource(candidate_id: str = CANDIDATE) -> AgentResourceRef:
    return AgentResourceRef(kind="agent", source_kind="team", id=AGENT, version=DRAFT_REVISION, trial_candidate_id=candidate_id)


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory) -> Path:
    from bowhead_cli.migrate import upgrade

    core = tmp_path_factory.mktemp("owner-trial-template") / "sqlite" / "bowhead.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("BOWHEAD_FERNET_KEY", _FERNET)
        upgrade(f"sqlite:///{core}")
    return core.parent


async def _insert_revision(db, *, team_id: str, owner_id: str, sequence: int, prompt: str, capability: str) -> dict:
    revision_id = f"arv_trial_{sequence}"
    definition = {
        "max_iterations": 8,
        "model": "analyst",
        "output_schema": None,
        "skill_grants": [],
        "system_prompt": prompt,
        "tool_refs": ["bi_query"],
    }
    listing = {"description": f"Safe trial execution v{sequence}", "display_name": "Trial Agent", "name": PROFILE}
    definition_json = _canonical(definition)
    created_at = _DRAFT_SAVED_AT if sequence == 2 else _NOW
    await db.execute(
        "INSERT INTO team_subagent_revision(id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,definition_hash,"
        "dependency_fingerprint,capability_fingerprint,risk_compiler_version,risk_json,created_by,created_at) "
        "VALUES (?,?,?,?,'authored',?,?,?,?,?,'risk-v1','{}',?,?)",
        (
            revision_id,
            team_id,
            AGENT,
            sequence,
            definition_json,
            json.dumps(listing, sort_keys=True),
            _hash(definition_json),
            f"sha256:dep{sequence}",
            capability,
            owner_id,
            created_at,
        ),
    )
    await db.execute(
        "INSERT INTO agent_safety_report(id,team_id,agent_id,subject_kind,subject_ref,content_hash,dependency_fingerprint,compiler_version,"
        "semantic_scanner_version,status,deterministic_json,semantic_json,safe_summary_json,created_by,created_at) "
        "VALUES (?,?,?,'revision',?,?,?,'safety-v1','none','pass','{}','{}','{}',?,?)",
        (
            f"asr_trial_{sequence}",
            team_id,
            AGENT,
            revision_id,
            _hash(_canonical({"definition": definition, "listing": listing})),
            f"sha256:dep{sequence}",
            owner_id,
            _NOW,
        ),
    )
    materializer = default_agent_behavior_materializer()
    descriptor = materializer.compile_legacy_team_revision(
        revision_id=revision_id,
        definition=definition,
        source_definition_hash=_hash(definition_json),
        dependency_fingerprint=f"sha256:dep{sequence}",
        capability_fingerprint=capability,
    )
    await materializer.persist_team_revision_conn(
        db, revision_id=revision_id, team_id=team_id, subagent_id=AGENT, descriptor=descriptor, created_at=_NOW
    )
    return {"id": revision_id, "safety": f"asr_trial_{sequence}", "dependency": f"sha256:dep{sequence}", "capability": capability}


async def _insert_candidate(core: Path, *, team_id: str, owner_id: str, candidate_id: str, state: str) -> None:
    async with open_shared_ctx(str(core)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO team_subagent_release_candidate(id,team_id,subagent_id,revision_id,base_live_release_id,target_audience_json,"
            "target_audience_hash,capability_fingerprint,dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,"
            "candidate_hash,state,submitted_by,submitted_at,updated_at) "
            "VALUES (?,?,?,?,?,'{}','sha256:aud',?,'sha256:dep2','risk-v1','{}','asr_trial_2',?,?,?,?,?)",
            (candidate_id, team_id, AGENT, DRAFT_REVISION, RELEASE, DRAFT_CAPABILITY, _hash(candidate_id), state, owner_id, _NOW, _NOW),
        )
        await db.commit()


async def _core_write(core: Path, sql: str, params: tuple) -> None:
    async with open_shared_ctx(str(core)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(sql, params)
        await db.commit()


@pytest.fixture
async def ctx(tmp_path, migrated_template, monkeypatch):
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", _FERNET)
    directory = tmp_path / "sqlite"
    shutil.copytree(migrated_template, directory, ignore=shutil.ignore_patterns("*.lock"))
    core = directory / "bowhead.db"
    users = UserStore(str(core))
    await users.initialize()
    sessions = SessionStore(str(core))
    await sessions.initialize()
    team = await users.create_team(name="Trial", max_seats=10)
    owner = await users.create_user(team_id=team.id, email="trial-owner@example.com", password="Passw0rd!")
    member = await users.create_user(team_id=team.id, email="trial-member@example.com", password="Passw0rd!")
    owner_ws = await users.create_local_workspace(team_id=team.id, name="Owner", creator_user_id=owner.id)
    member_ws = await users.create_local_workspace(team_id=team.id, name="Member", creator_user_id=member.id)
    text = _document(_trial_resource()).display_text()
    for session_id, workspace_id, user_id, turn_id in (
        ("s_owner_trial", owner_ws, owner.id, OWNER_TURN),
        ("s_owner_other", owner_ws, owner.id, "t_owner_other"),
        ("s_member", member_ws, member.id, MEMBER_TURN),
    ):
        await sessions.ensure_session(session_id=session_id, workspace_id=workspace_id, user_id=user_id)
        await sessions.record_turn(turn_id=turn_id, session_id=session_id, user_text=text)
    await users.create_team_subagent(
        TeamSubagent(
            id=AGENT,
            team_id=team.id,
            name=PROFILE,
            display_name="Trial Agent",
            description="Safe trial execution",
            system_prompt="legacy prompt",
            tool_refs=["bi_query"],
            visibility="team_wide",
            created_by=owner.id,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    async with open_shared_ctx(str(core)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        live = await _insert_revision(db, team_id=team.id, owner_id=owner.id, sequence=1, prompt="released prompt", capability="sha256:cap1")
        await _insert_revision(db, team_id=team.id, owner_id=owner.id, sequence=2, prompt="draft prompt", capability=DRAFT_CAPABILITY)
        await db.execute(
            "INSERT INTO team_subagent_release(id,team_id,subagent_id,release_sequence,revision_id,audience_snapshot_json,audience_hash,"
            "capability_fingerprint,dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at) "
            "VALUES (?,?,?,1,?,'{}','sha256:aud1',?,?,'{}',?,?,?)",
            (RELEASE, team.id, AGENT, live["id"], live["capability"], live["dependency"], live["safety"], owner.id, _NOW),
        )
        await db.execute(
            "UPDATE team_subagent SET live_revision_id=?,live_release_id=?,release_sequence=1,definition_mode='revisioned',"
            "owner_user_id=?,row_version=row_version+1,updated_at=? WHERE team_id=? AND id=?",
            (LIVE_REVISION, RELEASE, owner.id, _NOW, team.id, AGENT),
        )
        await db.commit()
    await _insert_candidate(core, team_id=team.id, owner_id=owner.id, candidate_id=CANDIDATE, state="prepared")
    await AgentAssignmentStore(str(core)).hire(team_id=team.id, user_id=member.id, agent_id=AGENT, agent_source_kind="team", live_access=True)
    return SimpleNamespace(
        core=core,
        execution=domain_database_path(core, "execution"),
        team_id=team.id,
        owner=owner.id,
        member=member.id,
        owner_ws=owner_ws,
        member_ws=member_ws,
        store=AgentExecutionBindingStore(core),
    )


async def _in_tx(c, operation):
    """Run ``operation(db)`` in one execution-file writer transaction and commit it."""
    async with open_shared_ctx(str(c.execution)) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            result = await operation(db)
            await db.commit()
            return result
        except BaseException:
            await db.rollback()
            raise


async def _record(c, *, turn_id=OWNER_TURN, user_id=None, candidate_id=CANDIDATE, revision_ref=DRAFT_REVISION):
    return await _in_tx(
        c,
        lambda db: c.store.record_owner_trial_turn_in_connection(
            db, team_id=c.team_id, user_id=user_id or c.owner, turn_id=turn_id, agent_id=AGENT, candidate_id=candidate_id, revision_ref=revision_ref
        ),
    )


async def _admit(c, *, turn_id=OWNER_TURN, user_id=None, candidate_id=CANDIDATE):
    return await _in_tx(
        c,
        lambda db: c.store.admit_owner_trial_execution_in_connection(
            db, team_id=c.team_id, user_id=user_id or c.owner, turn_id=turn_id, agent_id=AGENT, candidate_id=candidate_id
        ),
    )


async def _start_reference_trial(c, *, candidate_id=CANDIDATE):
    """The Turn transaction slice 2 will run: source, trial row, primary execution, plan and root binding."""
    resource = _trial_resource(candidate_id)
    document = _document(resource)
    source = ChatTurnUserSource(
        turn_id=OWNER_TURN,
        session_id="s_owner_trial",
        user_id=c.owner,
        workspace_id=c.owner_ws,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )

    async def write(db):
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=_NOW)
        await c.store.record_owner_trial_turn_in_connection(
            db, team_id=c.team_id, user_id=c.owner, turn_id=OWNER_TURN, agent_id=AGENT, candidate_id=candidate_id, revision_ref=DRAFT_REVISION
        )
        binding = await c.store.admit_owner_trial_execution_in_connection(
            db, team_id=c.team_id, user_id=c.owner, turn_id=OWNER_TURN, agent_id=AGENT, candidate_id=candidate_id
        )
        async with db.execute("SELECT COALESCE((SELECT epoch FROM access_policy_state WHERE team_id=?),0)", (c.team_id,)) as cursor:
            epoch = (await cursor.fetchone())[0]
        plan = ReferencePlan.model_validate(
            {
                "schema_version": 1,
                "source_turn_id": OWNER_TURN,
                "logical_source_turn_id": OWNER_TURN,
                "snapshot_owner_turn_id": OWNER_TURN,
                "source_hash": reference_source_hash(source),
                "admissions": [
                    {
                        "node_id": document.nodes[0].node_id,
                        "resource": resource.model_dump(exclude_none=True),
                        "operation": requested_reference_operation(resource),
                        "policy_digest": binding.policy_fingerprint,
                    }
                ],
            }
        )
        scope = ReferenceScope(team_id=c.team_id, user_id=c.owner, workspace_id=c.owner_ws, session_id="s_owner_trial", policy_epoch=epoch)
        expires_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        await ChatReferenceStore.put_plan_in_connection(db, scope=scope, turn_id=OWNER_TURN, plan=plan, expires_at=expires_at)
        await c.store.insert_root_binding_in_connection(
            db, binding=binding, user_id=c.owner, assignment_id=None, binding_source="mention", bind_primary_execution=True
        )
        return binding

    return resource, source, await _in_tx(c, write)


async def _read_primary(c, resource, binding):
    async with open_shared_ctx(str(c.execution)) as db:
        await db.execute("BEGIN")
        try:
            return await c.store.read_reference_primary_in_connection(
                db, team_id=c.team_id, user_id=c.owner, turn_id=OWNER_TURN, resource=resource, policy_fingerprint=binding.policy_fingerprint
            )
        finally:
            await db.rollback()


def _execution_insert(db, *, turn_id: str, team_id: str, release_id: str | None, revision: str, capability: str) -> None:
    db.execute(
        "INSERT INTO turn_agent_execution(id,team_id,turn_id,agent_id,agent_source_kind,revision_ref,release_id,dispatch_source,dispatch_key,"
        "runtime_profile_name,capability_fingerprint,policy_fingerprint,status,created_at) "
        "VALUES (?,?,?,?,'team',?,?,'explicit',?,?,?,'sha256:policy','admitted',?)",
        (f"aex_{uuid4().hex}", team_id, turn_id, AGENT, revision, release_id, f"probe:{uuid4().hex}", PROFILE, capability, _NOW),
    ).close()


def _binding_insert(
    db, *, turn_id: str, team_id: str, user_id: str, release_id: str | None, revision: str, capability: str, assignment_id=None
) -> None:
    db.execute(
        "INSERT INTO turn_agent_binding(turn_id,team_id,user_id,assignment_id,requested_agent_id,agent_source_kind,revision_ref,release_id,"
        "binding_source,capability_fingerprint,policy_fingerprint,created_at) VALUES (?,?,?,?,?,'team',?,?,'mention',?,'sha256:policy',?)",
        (turn_id, team_id, user_id, assignment_id, AGENT, revision, release_id, capability, _NOW),
    ).close()


def _raw_trial_row(c, *, turn_id: str, session_id: str, owner_user_id: str, candidate_id: str = CANDIDATE) -> None:
    with closing(connect_sync(str(c.execution))) as db:
        db.execute(
            "INSERT INTO agent_owner_trial_turn VALUES (?,?,?,?,?,?,?,?,'draft',?,?)",
            (turn_id, session_id, c.team_id, AGENT, owner_user_id, candidate_id, DRAFT_REVISION, DRAFT_CAPABILITY, _DRAFT_SAVED_AT, _NOW),
        ).close()
        db.commit()


# ── Revision 0234 ────────────────────────────────────────────────────────────────


def test_fresh_upgrade_adds_the_table_to_the_execution_file_only(migrated_template) -> None:
    from bowhead_core.account_erasure import _POLICY

    for domain, filename in DOMAIN_TARGET_FILES.items():
        with closing(sqlite3.connect(migrated_template / filename)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert ("agent_owner_trial_turn" in tables) is (domain == "execution"), domain
    with closing(sqlite3.connect(migrated_template / DOMAIN_TARGET_FILES["execution"])) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(agent_owner_trial_turn)")]
        objects = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE tbl_name='agent_owner_trial_turn'")}
        guards = dict(
            db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('turn_agent_binding','turn_agent_execution')")
        )
    # Account erasure reviews the exact column list.
    assert columns == _POLICY["tables"]["agent_owner_trial_turn"]["columns"]
    assert {
        "idx_agent_owner_trial_turn_owner",
        "trg_agent_owner_trial_turn_immutable",
        "trg_account_retired_agent_owner_trial_turn_insert",
    } <= objects
    for name in _REGISTRY_GUARDS:
        assert guards[name] == _TRIGGERS[name]["compiled"], name
        # Every compiled guard passes exactly as many values as its registered predicate reads.
        assert guards[name].count('NEW."') == len(_TRIGGERS[name]["refs"])
    for name, sql in _MIGRATION.REPLACEMENT_GUARDS.items():
        assert sql == _TRIGGERS[name]["compiled"]


def test_upgrade_replaces_the_0220_binding_guards_and_refuses_drift(migrated_template, tmp_path) -> None:
    path = tmp_path / DOMAIN_TARGET_FILES["execution"]
    shutil.copy2(migrated_template / DOMAIN_TARGET_FILES["execution"], path)
    with closing(sqlite3.connect(path)) as raw:
        for name, previous in _MIGRATION.PREVIOUS_GUARDS.items():
            raw.execute(f"DROP TRIGGER {name}")
            raw.execute(previous)
        raw.commit()
        db = SimpleNamespace(exec_driver_sql=lambda sql, params=(): raw.execute(sql, params))
        assert [_MIGRATION.replace_binding_guard(db, name) for name in _MIGRATION.REPLACEMENT_GUARDS] == [True, True]
        assert [_MIGRATION.replace_binding_guard(db, name) for name in _MIGRATION.REPLACEMENT_GUARDS] == [False, False]
        for name, sql in _MIGRATION.REPLACEMENT_GUARDS.items():
            assert raw.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()[0] == sql
        raw.execute("DROP TRIGGER trg_turn_agent_binding_revision")
        raw.execute("CREATE TRIGGER trg_turn_agent_binding_revision BEFORE INSERT ON turn_agent_binding BEGIN SELECT 1; END")
        with pytest.raises(RuntimeError, match="does not match its reviewed definition"):
            _MIGRATION.replace_binding_guard(db, "trg_turn_agent_binding_revision")


async def test_trial_rows_are_immutable_and_refused_for_a_retired_account(ctx) -> None:
    await _record(ctx)
    with closing(connect_sync(str(ctx.execution))) as db, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE agent_owner_trial_turn SET candidate_kind='submitted' WHERE turn_id=?", (OWNER_TURN,))
    with closing(connect_sync(str(ctx.core))) as db:
        db.execute(
            "INSERT INTO account_deletions(user_id,operation_id,requested_at,purge_after,time_source) VALUES('u_gone','op_1','t','2099','request')"
        )
        db.commit()
    with pytest.raises(sqlite3.DatabaseError, match="account is deleted"):
        _raw_trial_row(ctx, turn_id="t_owner_other", session_id="s_owner_other", owner_user_id="u_gone")


async def test_the_frozen_legacy_layout_has_no_trials(tmp_path) -> None:
    from core_legacy_database import copy_migrated_core

    path = tmp_path / "bowhead.db"
    copy_migrated_core(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN")
        assert await read_owner_trial_turn_in_connection(db, turn_id="t_any") is None
        execution = {"release_id": None, "agent_source_kind": "team", "turn_id": "t_any", "team_id": "team", "agent_id": AGENT}
        execution.update(revision_ref=DRAFT_REVISION, capability_fingerprint=DRAFT_CAPABILITY)
        # A release-less execution without a trial keeps its original refusal; no missing-table error.
        with pytest.raises(AgentExecutionBindingError, match="agent_bound_revision_unavailable"):
            await _team_snapshot_by_execution(db, execution)
        with pytest.raises(AgentExecutionBindingError, match="agent_trial_unavailable"):
            await AgentExecutionBindingStore(path).record_owner_trial_turn_in_connection(
                db, team_id="team", user_id="u", turn_id="t_any", agent_id=AGENT, candidate_id=CANDIDATE, revision_ref=DRAFT_REVISION
            )
        await db.rollback()


# ── Admission and resume ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("state", "kind"), [("prepared", "draft"), ("pending", "submitted")])
async def test_owner_trial_binds_the_candidate_revision(ctx, state, kind) -> None:
    candidate_id = CANDIDATE
    if state == "pending":
        # Submitted for review: the trial runs the version the reviewer sees.
        await _core_write(
            ctx.core,
            "UPDATE team_subagent_release_candidate SET state='pending',row_version=row_version+1,updated_at=? WHERE id=?",
            (_NOW, CANDIDATE),
        )
    trial = await _record(ctx, candidate_id=candidate_id)
    assert (trial.candidate_kind, trial.frozen_at, trial.session_id, trial.owner_user_id) == (kind, _DRAFT_SAVED_AT, "s_owner_trial", ctx.owner)
    # Recording again with the same facts is a no-op.
    assert await _record(ctx, candidate_id=candidate_id) == trial
    binding = await _admit(ctx, candidate_id=candidate_id)
    assert (binding.revision_ref, binding.release_id, binding.capability_fingerprint) == (DRAFT_REVISION, None, DRAFT_CAPABILITY)
    assert binding.trial_candidate_id == candidate_id
    assert binding.definition["system_prompt"] == "draft prompt"
    assert binding.runtime_profile_name == PROFILE and binding.status == "admitted"
    # The release-less root binding passes both binding guards inside the trial.
    await _in_tx(
        ctx,
        lambda db: ctx.store.insert_root_binding_in_connection(db, binding=binding, user_id=ctx.owner, assignment_id=None, binding_source="mention"),
    )
    with closing(sqlite3.connect(ctx.execution)) as db:
        assert db.execute("SELECT release_id,assignment_id,revision_ref FROM turn_agent_binding WHERE turn_id=?", (OWNER_TURN,)).fetchone() == (
            None,
            None,
            DRAFT_REVISION,
        )


async def test_resume_after_the_candidate_is_withdrawn_keeps_the_same_execution(ctx) -> None:
    await _record(ctx)
    first = await _admit(ctx)
    await _core_write(
        ctx.core,
        "UPDATE team_subagent_release_candidate SET state='withdrawn',row_version=row_version+1,updated_at=? WHERE id=?",
        (_NOW, CANDIDATE),
    )
    again = await _admit(ctx)
    assert again.execution_id == first.execution_id
    assert again.definition == first.definition and again.definition["system_prompt"] == "draft prompt"
    assert again.trial_candidate_id == CANDIDATE
    with closing(sqlite3.connect(ctx.core)) as db:
        assert db.execute("SELECT live_release_id,live_revision_id FROM team_subagent WHERE id=?", (AGENT,)).fetchone() == (RELEASE, LIVE_REVISION)
    # A new trial cannot start on a withdrawn candidate.
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_candidate_not_prepared"):
        await _record(ctx, turn_id="t_owner_other")


async def test_member_live_admission_still_binds_the_release(ctx) -> None:
    binding = await _in_tx(
        ctx,
        lambda db: ctx.store.admit_team_execution_in_connection(
            db,
            team_id=ctx.team_id,
            user_id=ctx.member,
            turn_id=MEMBER_TURN,
            profile_name=PROFILE,
            agent_id=AGENT,
            dispatch_source="explicit",
            dispatch_key="reference:primary",
        ),
    )
    assert (binding.release_id, binding.revision_ref, binding.trial_candidate_id) == (RELEASE, LIVE_REVISION, None)
    assert binding.definition["system_prompt"] == "released prompt"


async def test_trial_admission_refusals(ctx) -> None:
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_turn_missing"):
        await _admit(ctx)
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_owner_required"):
        await _record(ctx, turn_id=MEMBER_TURN, user_id=ctx.member)
    with pytest.raises(AgentExecutionBindingError, match="agent_candidate_stale"):
        await _record(ctx, revision_ref=LIVE_REVISION)
    await _insert_candidate(ctx.core, team_id=ctx.team_id, owner_id=ctx.owner, candidate_id="arc_trial_stale", state="stale")
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_candidate_not_prepared"):
        await _record(ctx, candidate_id="arc_trial_stale")
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_candidate_unavailable"):
        await _record(ctx, candidate_id="arc_trial_missing")
    # A trial row naming a member cannot admit them, nor one on a candidate that left review.
    _raw_trial_row(ctx, turn_id=MEMBER_TURN, session_id="s_member", owner_user_id=ctx.member)
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_owner_required"):
        await _admit(ctx, turn_id=MEMBER_TURN, user_id=ctx.member)
    _raw_trial_row(ctx, turn_id="t_owner_other", session_id="s_owner_other", owner_user_id=ctx.owner, candidate_id="arc_trial_stale")
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_candidate_not_prepared"):
        await _admit(ctx, turn_id="t_owner_other", candidate_id="arc_trial_stale")
    # The live projection never admits a trial reference.
    from bowhead_core.chat_reference_scope import ReferenceRequestScope

    scope = ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.owner, workspace_id=ctx.owner_ws)
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_unavailable"):
        await _in_tx(ctx, lambda db: ctx.store.project_reference_team_agent_in_connection(db, scope=scope, resource=_trial_resource()))


# ── Persistent guards: compiled triggers calling registered predicates ───────────


def _predicate(c, name: str, *values) -> int:
    with closing(connect_sync(str(c.execution))) as db:
        placeholders = ",".join("?" for _ in values)
        return db.execute(f"SELECT bowhead_domain_predicate_v1('{name}',{placeholders})", values).fetchone()[0]  # noqa: S608


async def test_guards_refuse_a_release_less_team_row_outside_an_owner_trial(ctx) -> None:
    execution = ("team", None, ctx.team_id, AGENT, DRAFT_REVISION, DRAFT_CAPABILITY, OWNER_TURN)
    assignment = ("team", None, ctx.team_id, ctx.owner, AGENT, OWNER_TURN, None)
    # 1 refuses, 0 accepts.
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *execution) == 1
    assert _predicate(ctx, "trg_turn_agent_binding_revision", *execution) == 1
    assert _predicate(ctx, "trg_turn_agent_binding_assignment", *assignment) == 1
    with closing(connect_sync(str(ctx.execution))) as db:
        with pytest.raises(sqlite3.DatabaseError, match="turn Agent execution revision mismatch"):
            _execution_insert(db, turn_id=OWNER_TURN, team_id=ctx.team_id, release_id=None, revision=DRAFT_REVISION, capability=DRAFT_CAPABILITY)
        with pytest.raises(sqlite3.DatabaseError, match="turn Agent binding (revision|assignment) mismatch"):
            _binding_insert(
                db, turn_id=OWNER_TURN, team_id=ctx.team_id, user_id=ctx.owner, release_id=None, revision=DRAFT_REVISION, capability=DRAFT_CAPABILITY
            )
        db.rollback()

    await _record(ctx)
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *execution) == 0
    assert _predicate(ctx, "trg_turn_agent_binding_revision", *execution) == 0
    assert _predicate(ctx, "trg_turn_agent_binding_assignment", *assignment) == 0
    # Inside the trial, only the trial's own revision and fingerprint are covered.
    assert _predicate(ctx, "trg_turn_agent_execution_revision", "team", None, ctx.team_id, AGENT, LIVE_REVISION, "sha256:cap1", OWNER_TURN) == 1
    # A live release without an assignment is not covered by the trial.
    assert _predicate(ctx, "trg_turn_agent_binding_assignment", "team", None, ctx.team_id, ctx.owner, AGENT, OWNER_TURN, RELEASE) == 1
    with closing(connect_sync(str(ctx.execution))) as db:
        _execution_insert(db, turn_id=OWNER_TURN, team_id=ctx.team_id, release_id=None, revision=DRAFT_REVISION, capability=DRAFT_CAPABILITY)
        with pytest.raises(sqlite3.DatabaseError, match="turn Agent binding assignment mismatch"):
            _binding_insert(
                db, turn_id=OWNER_TURN, team_id=ctx.team_id, user_id=ctx.owner, release_id=RELEASE, revision=LIVE_REVISION, capability="sha256:cap1"
            )
        _binding_insert(
            db, turn_id=OWNER_TURN, team_id=ctx.team_id, user_id=ctx.owner, release_id=None, revision=DRAFT_REVISION, capability=DRAFT_CAPABILITY
        )
        db.commit()


async def test_guards_require_the_owner_in_their_own_session_and_a_candidate_in_review(ctx) -> None:
    # A trial row naming a member: the member is not the Agent's Owner.
    _raw_trial_row(ctx, turn_id=MEMBER_TURN, session_id="s_member", owner_user_id=ctx.member)
    member = ("team", None, ctx.team_id, AGENT, DRAFT_REVISION, DRAFT_CAPABILITY, MEMBER_TURN)
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *member) == 1
    assert _predicate(ctx, "trg_turn_agent_binding_revision", *member) == 1
    assert _predicate(ctx, "trg_turn_agent_binding_assignment", "team", None, ctx.team_id, ctx.member, AGENT, MEMBER_TURN, None) == 1
    # The Owner's row, then the candidate leaves review: new rows are refused again.
    await _record(ctx)
    owner = ("team", None, ctx.team_id, AGENT, DRAFT_REVISION, DRAFT_CAPABILITY, OWNER_TURN)
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *owner) == 0
    await _core_write(
        ctx.core,
        "UPDATE team_subagent_release_candidate SET state='withdrawn',row_version=row_version+1,updated_at=? WHERE id=?",
        (_NOW, CANDIDATE),
    )
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *owner) == 1
    assert _predicate(ctx, "trg_turn_agent_binding_revision", *owner) == 1
    assert _predicate(ctx, "trg_turn_agent_binding_assignment", "team", None, ctx.team_id, ctx.owner, AGENT, OWNER_TURN, None) == 1
    # Ownership moved to the member: the Owner's trial no longer covers new rows.
    await _insert_candidate(ctx.core, team_id=ctx.team_id, owner_id=ctx.owner, candidate_id="arc_trial_review", state="pending")
    await _record(ctx, turn_id="t_owner_other", candidate_id="arc_trial_review")
    other = ("team", None, ctx.team_id, AGENT, DRAFT_REVISION, DRAFT_CAPABILITY, "t_owner_other")
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *other) == 0
    await _core_write(ctx.core, "UPDATE team_subagent SET owner_user_id=?,row_version=row_version+1 WHERE id=?", (ctx.member, AGENT))
    assert _predicate(ctx, "trg_turn_agent_execution_revision", *other) == 1


# ── Readers ───────────────────────────────────────────────────────────────────────


async def test_reference_primary_reader_rechecks_the_trial(ctx) -> None:
    resource, _, binding = await _start_reference_trial(ctx)
    primary = await _read_primary(ctx, resource, binding)
    assert (primary.execution_id, primary.release_id, primary.trial_candidate_id) == (binding.execution_id, None, CANDIDATE)
    assert primary.definition["system_prompt"] == "draft prompt"
    # The candidate's state is not rechecked once the trial runs.
    await _core_write(
        ctx.core,
        "UPDATE team_subagent_release_candidate SET state='withdrawn',row_version=row_version+1,updated_at=? WHERE id=?",
        (_NOW, CANDIDATE),
    )
    assert (await _read_primary(ctx, resource, binding)).execution_id == binding.execution_id
    # A reference naming another candidate is not this trial.
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_turn_missing"):
        await _read_primary(ctx, _trial_resource("arc_trial_other"), binding)
    await _core_write(ctx.core, "UPDATE team_subagent SET owner_user_id=?,row_version=row_version+1 WHERE id=?", (ctx.member, AGENT))
    with pytest.raises(AgentExecutionBindingError, match="agent_trial_owner_required"):
        await _read_primary(ctx, resource, binding)
    await _core_write(ctx.core, "UPDATE team_subagent SET owner_user_id=?,status='disabled',row_version=row_version+1 WHERE id=?", (ctx.owner, AGENT))
    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await _read_primary(ctx, resource, binding)


async def test_skill_receiver_accepts_only_a_trial_binding_without_release() -> None:
    from bowhead_core.skill_read_receiver import SkillReadReceiverError, primary_skill_read_receiver
    from test_skill_read_closure import SCOPE
    from test_skill_read_receiver import primary

    original = primary(())
    trial = replace(original, binding=replace(original.binding, release_id=None, trial_candidate_id=CANDIDATE))
    receiver = primary_skill_read_receiver(trial, SCOPE, "turn")
    assert (receiver.release_id, receiver.revision_ref) == (None, "revision")
    unproven = replace(original, binding=replace(original.binding, release_id=None))
    with pytest.raises(SkillReadReceiverError, match="reference_skill_receiver_unavailable"):
        primary_skill_read_receiver(unproven, SCOPE, "turn")


# ── Blocked features (C4) ────────────────────────────────────────────────────────


def test_every_blocked_feature_has_its_own_registered_code() -> None:
    assert set(AGENT_TRIAL_BLOCKED_FEATURE_CODES) == {
        "plan_mode",
        "mcp_references",
        "extension_skills",
        "steer_follow_ups",
        "same_agent_helper",
        "template_document_tools",
    }
    assert len(AGENT_TRIAL_REFUSAL_CODES) == len(AGENT_TRIAL_BLOCKED_FEATURE_CODES)


async def test_same_agent_helper_is_refused_in_a_trial_turn(ctx) -> None:
    from bowhead_core.subagent import _admission_block_guidance

    await _record(ctx)
    with pytest.raises(AgentExecutionBindingError) as refused:
        await _in_tx(
            ctx,
            lambda db: ctx.store.admit_team_execution_in_connection(
                db, team_id=ctx.team_id, user_id=ctx.owner, turn_id=OWNER_TURN, profile_name=PROFILE, agent_id=AGENT, dispatch_key="call:helper"
            ),
        )
    assert refused.value.code == AGENT_TRIAL_BLOCKED_FEATURE_CODES["same_agent_helper"]
    assert "trial" in _admission_block_guidance(refused.value.code)


async def test_template_document_tools_are_refused_in_a_trial_turn(ctx) -> None:
    from bowhead_core.documents.agent_caller import read_document_caller_in_tx
    from bowhead_core.documents.capability import admit_capability
    from bowhead_core.documents.request import DocumentRequestError, DocumentScope

    await _record(ctx)
    binding = await _admit(ctx)
    scope = DocumentScope(team_id=ctx.team_id, workspace_id=ctx.owner_ws, user_id=ctx.owner)
    code = AGENT_TRIAL_BLOCKED_FEATURE_CODES["template_document_tools"]
    async with open_shared_ctx(str(ctx.execution)) as db:
        await db.execute("BEGIN")
        with pytest.raises(DocumentRequestError, match=code):
            await read_document_caller_in_tx(db, path=ctx.core, scope=scope, turn_id=OWNER_TURN, execution_id=binding.execution_id)
        with pytest.raises(DocumentRequestError, match=code):
            await admit_capability(db, path=ctx.core, runtime=SimpleNamespace(binding=binding), scope=scope, turn_id=OWNER_TURN, inputs=(), now=_NOW)
        await db.rollback()


async def test_plan_steps_are_refused_for_a_trial_source(ctx) -> None:
    from bowhead_core.chat_reference_plan import ReferencePlanRuntime

    await _record(ctx)
    runtime = ReferencePlanRuntime(
        sources=SimpleNamespace(db_path=str(ctx.core)),
        cipher=None,
        scope=None,
        requirements=SimpleNamespace(source_turn_id=OWNER_TURN),
        plan_id="plan_trial",
        turn_id="t_owner_other",
        owner_id="worker",
        generation=1,
        registry=None,
    )
    with pytest.raises(ValueError, match=AGENT_TRIAL_BLOCKED_FEATURE_CODES["plan_mode"]):
        await runtime.admit_step(runner=None, spec=None, ctx=None)


async def test_mcp_and_prompt_references_are_refused_for_a_trial_primary() -> None:
    from bowhead_core import reference_mcp_execution, reference_mcp_prompt_execution

    primary = SimpleNamespace(binding=SimpleNamespace(trial_candidate_id=CANDIDATE))
    for module in (reference_mcp_execution, reference_mcp_prompt_execution):
        with pytest.raises(ValueError, match=AGENT_TRIAL_BLOCKED_FEATURE_CODES["mcp_references"]):
            module._composition(primary, None)


async def test_extension_skills_never_use_the_live_policy_for_a_trial(monkeypatch) -> None:
    from bowhead_core.skill_read_receiver import SkillReadReceiverError, primary_skill_read_receiver
    from test_skill_read_closure import SCOPE
    from test_skill_read_receiver import primary

    from bowhead_core import skill_read_extensions

    definition = {"skill_grants": [], "system_prompt": "draft", "runtime_extensions": {"schema_version": 1}}
    original = primary((), definition=definition)
    trial = replace(original, binding=replace(original.binding, release_id=None, trial_candidate_id=CANDIDATE))
    receiver = primary_skill_read_receiver(trial, SCOPE, "turn")

    async def live_policy(*_args, **_kwargs):
        raise AssertionError("a trial must not read the released version's extension policy")

    async def no_composition(*_args, **_kwargs):
        return None

    monkeypatch.setattr(skill_read_extensions, "published_extension_policy", live_policy)
    monkeypatch.setattr(skill_read_extensions, "_row", no_composition)
    assert await skill_read_extensions.primary_extension_policy(None, trial, receiver) is None
    with pytest.raises(SkillReadReceiverError, match=AGENT_TRIAL_BLOCKED_FEATURE_CODES["extension_skills"]):
        await skill_read_extensions.require_current_extension_policy(
            SimpleNamespace(in_transaction=True), receiver, None, SimpleNamespace(policy=object())
        )


# ── Session origin ───────────────────────────────────────────────────────────────


async def test_a_trial_session_keeps_its_origin(ctx) -> None:
    async def ensure(db, origin):
        return await _ensure_chat_session_in_tx(
            db, session_id="s_trial_origin", workspace_id=ctx.owner_ws, user_id=ctx.owner, now=_NOW, origin=origin
        )

    assert await _in_tx(ctx, lambda db: ensure(db, "agent_trial")) is True
    assert await _in_tx(ctx, lambda db: ensure(db, "agent_trial")) is False
    with pytest.raises(ValueError, match="chat_session_scope_conflict"):
        await _in_tx(ctx, lambda db: ensure(db, "normal"))
    with pytest.raises(ValueError, match="chat_session_origin_invalid"):
        await SessionStore(str(ctx.core)).finalize_started_chat_submission(
            team_id=ctx.team_id,
            user_id=ctx.owner,
            client_submission_id="sub",
            workspace_id=ctx.owner_ws,
            requested_session_id=None,
            actual_session_id="s_new",
            turn_id="t_new",
            user_text="hello",
            attachment_ids=[],
            submission_kind="start",
            submission_contract_version="chat.v1",
            model_control_batch="none",
            request_fingerprint="1" * 64,
            lease_owner="owner",
            lease_version=1,
            response={},
            session_origin="agent_eval",
        )
