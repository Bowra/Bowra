"""Frozen release policies preserve exact identity and authorization across upgrades."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import AgentAudience, AgentPublicationBlockedError, AgentRevisionForbiddenError, AgentRevisionStore
from bowhead_core.agent_runtime_extension_store import read_published_extension_policy_in_connection
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError, read_agent_runtime_extensions
from legacy_agent_release_fixture import seed_legacy_publication
from test_agent_revision_store import _Clock, _db, _definition, _listing, _seed, _Validator
from test_agent_runtime_extensions import policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def setup(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    return SimpleNamespace(path=path, store=AgentRevisionStore(str(path), validator=_Validator(), clock=_Clock()))


def row_version(case):
    with sqlite3.connect(case.path) as db:
        row = db.execute("SELECT row_version FROM team_subagent_working_copy WHERE subagent_id='osa_release'").fetchone()
    return int(row[0]) if row else None


async def save(case, definition, *, audience="private", actor="user_owner"):
    return await case.store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id=actor,
        definition=definition,
        listing=_listing(),
        audience=AgentAudience(audience, user_ids=("user_owner",) if audience == "custom" else ()),
        expected_row_version=row_version(case),
    )


async def seed_release(case, working):
    return await seed_legacy_publication(
        case.store, team_id="team_release", agent_id="osa_release", actor_user_id="user_owner", working_copy_id=working.id
    )


async def test_policy_has_independent_digest_without_adding_baseline_grants(tmp_path):
    case = setup(tmp_path)
    value = {**_definition(), **policy()}
    working = await save(case, value)
    result = await seed_release(case, working)
    assert result.state == "activated"
    with sqlite3.connect(case.path) as db:
        db.row_factory = sqlite3.Row
        revision = dict(db.execute("SELECT * FROM team_subagent_revision WHERE id=?", (result.revision_id,)).fetchone())
        retained = dict(db.execute("SELECT * FROM team_subagent_revision_extensions WHERE revision_ref=?", (result.revision_id,)).fetchone())
        assert json.loads(revision["definition_json"]) == value
        assert retained["definition_hash"] == revision["definition_hash"]
        assert retained["extension_policy_digest"] == read_agent_runtime_extensions(value).digest
        assert db.execute("SELECT COUNT(*) FROM team_subagent_revision_extensions").fetchone()[0] == 1
        for sql in (
            "UPDATE team_subagent_revision_extensions SET extension_policy_digest='sha256:' || printf('%064d',1)",
            "DELETE FROM team_subagent_revision_extensions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        restored = await read_published_extension_policy_in_connection(
            db, team_id="team_release", agent_id="osa_release", revision_ref=result.revision_id, definition=value
        )
        assert restored.digest == retained["extension_policy_digest"]
        with pytest.raises(AgentRuntimeExtensionError, match="unproven"):
            await read_published_extension_policy_in_connection(
                db, team_id="another_team", agent_id="osa_release", revision_ref=result.revision_id, definition=value
            )
        changed = json.loads(json.dumps(value))
        changed["runtime_extensions"]["skill_read"]["max_selected"] = 1
        with pytest.raises(AgentRuntimeExtensionError, match="unproven"):
            await read_published_extension_policy_in_connection(
                db, team_id="team_release", agent_id="osa_release", revision_ref=result.revision_id, definition=changed
            )


@pytest.mark.parametrize("published", [False, True])
async def test_old_editor_cannot_silently_drop_policy_but_owner_can_explicitly_clear(tmp_path, published):
    case = setup(tmp_path)
    working = await save(case, {**_definition(), **policy()})
    if published:
        await seed_release(case, working)
        # Activation may retain the working copy; test the live-only read path too.
        with sqlite3.connect(case.path) as db:
            db.execute("DELETE FROM team_subagent_working_copy WHERE subagent_id='osa_release'")
    version = row_version(case)
    with pytest.raises(AgentPublicationBlockedError, match="preservation_required"):
        await save(case, _definition("Edit wording only."))
    assert row_version(case) == version
    cleared = await save(case, {**_definition("Clear optional methods."), "runtime_extensions": None})
    result = await seed_release(case, cleared)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_revision_extensions WHERE revision_ref=?", (result.revision_id,)).fetchone() == (0,)
        assert (
            json.loads(db.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (result.revision_id,)).fetchone()[0])[
                "runtime_extensions"
            ]
            is None
        )


@pytest.mark.parametrize("audience", ["custom", "teamwide"])
async def test_personal_selector_draft_cannot_become_shared(tmp_path, audience):
    case = setup(tmp_path)
    with pytest.raises(AgentRuntimeExtensionError, match="private_source"):
        await save(case, {**_definition(), **policy(kind="user")}, audience=audience)
    assert row_version(case) is None


async def test_policy_writer_requires_current_owner_and_migration(tmp_path):
    case = setup(tmp_path)
    with pytest.raises(AgentRevisionForbiddenError):
        await save(case, {**_definition(), **policy()}, actor="user_admin")
    with sqlite3.connect(case.path) as db:
        db.execute("DROP TRIGGER trg_agent_extensions_candidate_insert")
    with pytest.raises(AgentRuntimeExtensionError, match="migration_required"):
        await save(case, {**_definition(), **policy()})
    assert row_version(case) is None


async def test_review_reports_optional_scope_change_without_exposing_selectors(tmp_path):
    from bowhead_core.agent_release_read_store import AgentReleaseReadStore

    case = setup(tmp_path)
    value = {**_definition(), **policy(kind="team")}
    value["runtime_extensions"]["skill_read"]["resource_allowlist"][0]["id"] = "private-selector-must-not-leak"
    working = await save(case, value, audience="teamwide")
    result = await seed_release(case, working)
    detail = await AgentReleaseReadStore(str(case.path)).candidate_detail(
        team_id="team_release", candidate_id=result.candidate_id, actor_user_id="user_admin"
    )
    assert detail["diff"]["runtime_extensions_changed"] is True
    assert detail["diff"]["skill_set_changed"] is False and detail["diff"]["tool_set_changed"] is False
    assert detail["candidate"]["behavior"]["runtime_extensions"] == {
        "state": "supported",
        "enabled": True,
        "source_kinds": ["team"],
        "selector_count": 1,
        "pinned_count": 0,
        "max_selected": 3,
        "read_only": True,
    }
    assert "private-selector-must-not-leak" not in json.dumps(detail)


async def native_binding(case):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.session_store import SessionStore
    from test_agent_revision_store import _seed_eval_workspace

    _seed_eval_workspace(case.path)
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE agent_assignment SET user_id='user_owner' WHERE id='asa_release'")
    sessions = SessionStore(case.path)
    await sessions.initialize()
    session = await sessions.ensure_session(session_id="s_extensions", workspace_id="ws_eval", user_id="user_owner")
    await sessions.record_turn(turn_id="t_extensions", session_id=session.id, user_text="Use my selected profitability method.")
    return await AgentExecutionBindingStore(case.path).admit_team_execution(
        team_id="team_release",
        user_id="user_owner",
        turn_id="t_extensions",
        profile_name="release-agent",
        agent_id="osa_release",
        dispatch_key="extensions-binding",
    )


async def test_real_dispatch_restores_proven_policy_with_original_baseline_unchanged(tmp_path):
    case = setup(tmp_path)
    value = {**_definition(), **policy()}
    working = await save(case, value)
    result = await seed_release(case, working)
    binding = await native_binding(case)
    assert binding.revision_ref == result.revision_id
    assert binding.definition["runtime_extensions"] == value["runtime_extensions"]
    assert binding.definition["skill_grants"] == [] and binding.definition["tool_refs"] == []


async def test_change_proposal_preserves_policy_and_only_changes_the_accepted_working_copy(tmp_path):
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore

    case = setup(tmp_path)
    working = await save(case, _definition())
    proposals = AgentChangeProposalStore(str(case.path), revision_store=case.store)
    value = policy()["runtime_extensions"]
    proposal = await proposals.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="coach",
        source_ref="feedback:optional-method",
        operations=[{"op": "add", "path": "/definition/runtime_extensions", "value": value}],
        atomic_groups=[{"id": "optional-method", "operation_indexes": [0], "depends_on": []}],
    )
    with sqlite3.connect(case.path) as db:
        assert "runtime_extensions" not in json.loads(
            db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE id=?", (working.id,)).fetchone()[0]
        )
    accepted = await proposals.apply(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        selected_group_ids=["optional-method"],
        expected_proposal_row_version=proposal.row_version,
        expected_working_copy_row_version=working.row_version,
    )
    assert accepted["working_copy"].definition == {**_definition(), "runtime_extensions": value}
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_revision_extensions").fetchone() == (0,)
        assert db.execute("SELECT live_revision_id FROM team_subagent WHERE id='osa_release'").fetchone() == ("arv_initial",)
    with pytest.raises(AgentRuntimeExtensionError, match="preservation_required"):
        await proposals.create(
            team_id="team_release",
            agent_id="osa_release",
            effective_owner_user_id="user_owner",
            source_type="regression",
            source_ref="regression:old-editor",
            operations=[{"op": "replace", "path": "/definition", "value": _definition("Only adjust the wording.")}],
            atomic_groups=[{"id": "wording", "operation_indexes": [0], "depends_on": []}],
        )


async def test_real_publication_validator_can_check_optional_sources_in_read_only_previews(tmp_path, monkeypatch):
    from bowhead_core.agent_revision_store import DeterministicAgentPublicationValidator
    from bowhead_core.user_store import UserStore
    from test_agent_runtime_extension_dependencies import case as dependency_case

    current = setup(tmp_path)
    dependency = dependency_case(tmp_path, monkeypatch, "builtin")
    validator = DeterministicAgentPublicationValidator(catalog_store=UserStore(current.path), dependency_compiler=dependency.compiler)
    async with open_shared_ctx(current.path) as db:
        assert not db.in_transaction  # Safety and fork previews use this path.
        for available in (True, False):
            if not available:
                (dependency.root / "references/method.md").unlink()
            result = await validator.validate(
                db,
                team_id="team_release",
                agent_id="osa_release",
                owner_user_id="user_owner",
                routing_name="release-agent",
                definition={**_definition(), **dependency.value},
                listing=_listing(),
                audience=AgentAudience("private"),
            )
            assert result.compilation.status == ("ready" if available else "blocked")
            assert result.compilation.skills == result.compilation.tools == ()
