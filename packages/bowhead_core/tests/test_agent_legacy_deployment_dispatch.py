"""Dispatch on a deployment that has not finished the release baseline import.

`test_agent_execution_binding` covers the happy un-migrated dispatch.  This file
pins the parts that are easy to get subtly wrong:

* judgement 6 -- the advertising surface may never name an Agent that admission
  refuses (§5.6.1's sibling criterion);
* the partial-import window -- ``apply`` marks Agents revisioned per batch and
  only sets the deployment marker after the whole inventory reconciles, so a
  deployment-wide fallback would run an already-frozen Agent off its mutable row;
* lifecycle -- the routing-name lookup filters only ``status``, so an archived
  Agent must not reach the compatibility path either;
* the two "not found is a lie" surfaces.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    dispatchable_team_agent_ids,
)
from bowhead_core.agents.plan_admission import list_visible_team_subagents
from bowhead_core.session_store import SessionStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.user_store import TeamSubagent, UserStore

_NOW = "2026-09-06T00:00:00+00:00"


@dataclass(frozen=True, slots=True)
class _Fixture:
    db_path: Path
    users: UserStore
    team_id: str
    owner_id: str
    member_id: str
    turn_id: str


async def _setup(tmp_path: Path) -> _Fixture:
    db_path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    users = UserStore(db_path)
    sessions = SessionStore(db_path)
    await users.initialize()
    await sessions.initialize()
    team = await users.create_team(name="legacy-deployment", max_seats=4)
    owner = await users.create_user(team_id=team.id, email="owner@example.com")
    member = await users.create_user(team_id=team.id, email="member@example.com")
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="Legacy Space",
        creator_user_id=member.id,
    )
    session = await sessions.ensure_session(session_id="s_legacy", workspace_id=workspace_id, user_id=member.id)
    turn = await sessions.record_turn(turn_id="t_legacy", session_id=session.id, user_text="dispatch a team agent")
    return _Fixture(
        db_path=db_path,
        users=users,
        team_id=team.id,
        owner_id=owner.id,
        member_id=member.id,
        turn_id=turn.id,
    )


async def _create_agent(fixture: _Fixture, *, agent_id: str, name: str, display_name: str) -> None:
    """Create an Agent exactly as the product's Creator does: legacy_mutable."""
    await fixture.users.create_team_subagent(
        TeamSubagent(
            id=agent_id,
            team_id=fixture.team_id,
            name=name,
            display_name=display_name,
            description="A team-authored Agent",
            system_prompt="do the job",
            tool_refs=["bi_query"],
            visibility="team_wide",
            created_by=fixture.owner_id,
            created_at=_NOW,
            updated_at=_NOW,
        ),
    )
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        agent_id=agent_id,
        agent_source_kind="team",
        live_access=True,
    )


async def _publish_release(fixture: _Fixture, *, agent_id: str, name: str, display_name: str) -> None:
    revision_id = f"arv_{agent_id}"
    release_id = f"arl_{agent_id}"
    safety_id = f"asr_{agent_id}"
    definition = {
        "max_iterations": 8,
        "model": "analyst",
        "output_schema": None,
        "skill_grants": [],
        "system_prompt": "frozen revision prompt",
        "tool_refs": ["bi_query"],
    }
    listing = {"description": "A team-authored Agent", "display_name": display_name, "name": name}
    definition_json = json.dumps(definition, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    definition_hash = f"sha256:{hashlib.sha256(definition_json.encode()).hexdigest()}"
    content_json = json.dumps(
        {"definition": definition, "listing": listing},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    content_hash = f"sha256:{hashlib.sha256(content_json.encode()).hexdigest()}"
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (?,?,?,1,'authored',?,?,?,'sha256:dep','sha256:cap','risk-v1','{}',?,?)
            """,
            (revision_id, fixture.team_id, agent_id, definition_json, json.dumps(listing, sort_keys=True), definition_hash, fixture.owner_id, _NOW),
        )
        await db.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES (?,?,?,'revision',?,?,'sha256:dep','safety-v1','none','pass','{}','{}','{}',?,?)
            """,
            (safety_id, fixture.team_id, agent_id, revision_id, content_hash, fixture.owner_id, _NOW),
        )
        await db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            ) VALUES (?,?,?,1,?,NULL,'{}','sha256:aud','sha256:cap','sha256:dep','{}',?,?,?)
            """,
            (release_id, fixture.team_id, agent_id, revision_id, safety_id, fixture.owner_id, _NOW),
        )
        await db.execute(
            """
            UPDATE team_subagent
            SET live_revision_id=?,live_release_id=?,release_sequence=1,
                definition_mode='revisioned',row_version=row_version+1,updated_at=?
            WHERE team_id=? AND id=?
            """,
            (revision_id, release_id, _NOW, fixture.team_id, agent_id),
        )
        await db.commit()


async def _complete_release_cutover(fixture: _Fixture) -> None:
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            "UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1",
            (_NOW,),
        )
        await db.commit()


def _runner(fixture: _Fixture) -> SubagentRunner:
    return SubagentRunner(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        team_subagent_store=fixture.users,
    )


def _ctx(fixture: _Fixture) -> SimpleNamespace:
    return SimpleNamespace(
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        turn_id=fixture.turn_id,
        granted_team_subagent_ids=None,
        builtin_subagent_overrides=None,
    )


async def _admit(fixture: _Fixture, *, name: str, dispatch_key: str, agent_id: str | None = None):
    return await _runner(fixture).admit_execution_binding(
        profile_name=name,
        parent_turn_id=fixture.turn_id,
        parent_ctx=_ctx(fixture),
        dispatch_key=dispatch_key,
        agent_id=agent_id,
    )


@pytest.mark.parametrize("agent_id", [None, "system:compute", "compute"])
@pytest.mark.anyio
async def test_a_builtin_dispatches_however_the_lead_spells_its_identity(
    tmp_path: Path,
    agent_id: str | None,
) -> None:
    """Builtin dispatch accepts its display name, canonical ID, or an omitted ID.

    The roster may expose a name without its stable ID; normalization must stay
    restricted to actual builtin profiles.
    """
    fixture = await _setup(tmp_path)

    binding = await _admit(fixture, name="compute", dispatch_key=f"call:builtin:{agent_id}", agent_id=agent_id)

    assert binding is not None, f"内置 compute 必须能派（agent_id={agent_id!r}）"


@pytest.mark.anyio
async def test_partial_import_still_binds_a_revisioned_agent_exactly(tmp_path: Path) -> None:
    """An Agent already imported must not fall back to its mutable row.

    ``apply`` sets ``definition_mode='revisioned'`` per batch and only writes
    ``release_semantics_activated_at`` after the whole inventory reconciles, so an
    interrupted or abandoned import leaves exactly this state.  A deployment-wide
    fallback would execute the mutable ``team_subagent`` row instead of the frozen
    revision -- the "cutover is silently reversible" hazard the design rules out.
    """
    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_pub", name="published-agent", display_name="Published Agent")
    await _publish_release(fixture, agent_id="osa_pub", name="published-agent", display_name="Published Agent")
    # deliberately NOT calling _complete_release_cutover: the marker is still NULL

    binding = await _admit(fixture, name="published-agent", dispatch_key="call:partial")

    assert binding is not None, "a revisioned Agent must bind exactly even before the marker is set"
    assert binding.revision_ref == "arv_osa_pub"
    assert binding.definition["system_prompt"] == "frozen revision prompt"


@pytest.mark.anyio
async def test_archived_agent_does_not_reach_the_compatibility_path(tmp_path: Path) -> None:
    """The routing-name lookup filters only ``status``; lifecycle must still hold."""
    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_arch", name="archived-agent", display_name="Archived Agent")
    async with aiosqlite.connect(fixture.db_path) as db:
        await db.execute(
            "UPDATE team_subagent SET lifecycle_state='archived',archived_at=?,archived_by=? WHERE id='osa_arch'",
            (_NOW, fixture.owner_id),
        )
        await db.commit()

    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await _admit(fixture, name="archived-agent", dispatch_key="call:archived")


@pytest.mark.anyio
async def test_judgement_6_roster_never_names_an_agent_admission_refuses(tmp_path: Path) -> None:
    """Advertising surface ⊆ what admission accepts, in both cutover states.

    Sibling criterion to §5.6.1's "0 mentions": a name may only reach the Lead if
    the Lead can actually dispatch it.  Before this held, a team could watch the
    Lead pick its own Agent and be silently answered by a built-in one instead.
    """
    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_legacy", name="legacy-agent", display_name="Legacy Agent")
    await _create_agent(fixture, agent_id="osa_pub", name="published-agent", display_name="Published Agent")
    await _publish_release(fixture, agent_id="osa_pub", name="published-agent", display_name="Published Agent")

    await AgentAssignmentStore(fixture.db_path).backfill_existing_access(apply=True, source_build="test")
    advertised: set[str] = set()
    for phase, cutover in (("before", False), ("after", True)):
        if cutover:
            await _complete_release_cutover(fixture)
        roster = await list_visible_team_subagents(fixture.users, team_id=fixture.team_id, user_id=fixture.member_id)
        advertised = {name for name, _description, _tools in roster}
        assert advertised == {"published-agent"}, f"{phase}: only a released Agent may be advertised"
        for name in advertised:
            try:
                await _admit(fixture, name=name, dispatch_key=f"call:{phase}:{name}")
            except AgentExecutionBindingError as exc:  # noqa: PERF203 -- one assertion per advertised name
                assert exc.code != "agent_not_runnable", f"{phase}: roster advertises {name}, admission refuses it"

    # ...and after cutover the un-migrated Agent is really gone from the roster,
    # rather than merely being tolerated by the assertion above.
    assert "legacy-agent" not in advertised
    assert "published-agent" in advertised


@pytest.mark.anyio
async def test_dispatchable_gate_requires_a_live_release_before_cutover(tmp_path: Path) -> None:
    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_legacy", name="legacy-agent", display_name="Legacy Agent")

    assert await dispatchable_team_agent_ids(fixture.db_path, team_id=fixture.team_id) == frozenset()

    await _complete_release_cutover(fixture)
    assert await dispatchable_team_agent_ids(fixture.db_path, team_id=fixture.team_id) == frozenset()


@pytest.mark.anyio
async def test_release_manager_reports_cutover_not_missing_agent(tmp_path: Path) -> None:
    """An Agent the owner can see in the catalog must never be reported as absent."""
    from bowhead_core.agent_release_read_store import AgentReleaseReadStore
    from bowhead_core.agent_revision_store import (
        AgentReleaseCutoverRequiredError,
        AgentRevisionForbiddenError,
    )

    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_legacy", name="legacy-agent", display_name="Legacy Agent")
    store = AgentReleaseReadStore(str(fixture.db_path))

    with pytest.raises(AgentReleaseCutoverRequiredError) as legacy:
        await store.working_copy(team_id=fixture.team_id, agent_id="osa_legacy", actor_user_id=fixture.owner_id)
    assert legacy.value.code == "agent_release_cutover_required"

    with pytest.raises(AgentRevisionForbiddenError) as missing:
        await store.working_copy(team_id=fixture.team_id, agent_id="osa_does_not_exist", actor_user_id=fixture.owner_id)
    assert missing.value.code == "agent_not_found"


@pytest.mark.anyio
async def test_m4_panels_report_cutover_not_a_missing_agent(tmp_path: Path) -> None:
    """Localization and taxonomy used to answer "not found" for un-migrated Agents.

    Same shape as the release manager: an Agent the Owner just saw in the catalog
    has no working copy purely because it was never imported into release history.
    """
    from bowhead_core.agent_taxonomy import AgentTaxonomyForbiddenError, AgentTaxonomyStore
    from bowhead_core.catalog_localization import (
        CatalogLocalizationForbiddenError,
        CatalogLocalizationStore,
    )

    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_legacy", name="legacy-agent", display_name="Legacy Agent")

    with pytest.raises(CatalogLocalizationForbiddenError) as loc:
        await CatalogLocalizationStore(str(fixture.db_path)).agent_manager_snapshot(
            team_id=fixture.team_id,
            agent_id="osa_legacy",
            actor_user_id=fixture.owner_id,
        )
    assert loc.value.code == "catalog_localization_release_cutover_required"

    with pytest.raises(AgentTaxonomyForbiddenError) as tax:
        await AgentTaxonomyStore(str(fixture.db_path)).agent_manager_snapshot(
            team_id=fixture.team_id,
            agent_id="osa_legacy",
            actor_user_id=fixture.owner_id,
        )
    assert tax.value.code == "agent_taxonomy_release_cutover_required"

    # Control: an Agent that really is not the caller's still reports unavailable.
    with pytest.raises(CatalogLocalizationForbiddenError) as missing:
        await CatalogLocalizationStore(str(fixture.db_path)).agent_manager_snapshot(
            team_id=fixture.team_id,
            agent_id="osa_nope",
            actor_user_id=fixture.owner_id,
        )
    assert missing.value.code == "catalog_localization_working_copy_unavailable"


@pytest.mark.anyio
async def test_localization_and_taxonomy_agree_on_archived_agents(tmp_path: Path) -> None:
    """The two working-copy lookups had drifted: one filtered lifecycle, one did not.

    An archived Agent could have its localizations edited but not its taxonomy.
    """
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_revision_store import owned_agent_working_copy_conn

    fixture = await _setup(fixture_path := tmp_path)
    assert fixture_path is not None
    await _create_agent(fixture, agent_id="osa_arch", name="archived-agent", display_name="Archived Agent")
    await _publish_release(fixture, agent_id="osa_arch", name="archived-agent", display_name="Archived Agent")
    async with aiosqlite.connect(fixture.db_path) as db:
        # A working copy DOES exist -- otherwise the lookup would miss for the
        # trivial reason and the lifecycle filter would never be exercised.
        await db.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,base_release_id,definition_json,listing_json,
              audience_json,validation_json,row_version,updated_by,created_at,updated_at
            ) VALUES ('awc_arch',?,?,?,'{"system_prompt":"x"}','{"name":"archived-agent"}',
                      '{"mode":"private"}','{}',1,?,?,?)
            """,
            (fixture.team_id, "osa_arch", "arl_osa_arch", fixture.owner_id, _NOW, _NOW),
        )
        await db.execute(
            "UPDATE team_subagent SET status='disabled',lifecycle_state='archived',archived_at=?,archived_by=? WHERE id='osa_arch'",
            (_NOW, fixture.owner_id),
        )
        await db.commit()

    async with open_shared_ctx(str(fixture.db_path)) as db:
        db.row_factory = aiosqlite.Row
        row, reason = await owned_agent_working_copy_conn(
            db,
            team_id=fixture.team_id,
            agent_id="osa_arch",
            actor_user_id=fixture.owner_id,
        )
    # Both panels must refuse an archived Agent, even though its working copy row
    # is still there.  Localization used to accept it and taxonomy used to refuse.
    assert row is None
    assert reason == "unavailable"


@pytest.mark.anyio
async def test_baseline_import_produces_a_dispatchable_agent(tmp_path: Path) -> None:
    """The migration must leave Agents runnable -- nobody had ever checked end to end.

    Every admission test hand-seeds a release whose safety report satisfies the
    join.  No test ever ran the real importer and then tried to dispatch, so two
    writer/reader disagreements survived:

    * the report was written ``subject_kind='legacy_row'`` / ``subject_ref=agent_id``
      while admission demands a revision-scoped report;
    * ``safety.content_hash`` is ``hash({definition, listing})`` on every writer,
      while admission compared it to ``revision.definition_hash`` -- two different
      digests that can never be equal.

    Together they meant a *successful* baseline import left every team Agent
    revisioned, cut over, and permanently undispatchable -- with the compatibility
    path now disabled and the compatibility floor raised so it could not be rolled
    back.  This test is the gate that would have caught it.
    """
    from bowhead_core.agent_release_baseline import AgentReleaseBaselineStore

    fixture = await _setup(tmp_path)
    await _create_agent(fixture, agent_id="osa_legacy", name="legacy-agent", display_name="Legacy Agent")

    store = AgentReleaseBaselineStore(fixture.db_path)
    preview = await store.preview()
    assert preview.blocked == 0, "a plain Agent must not be blocked from its own migration"
    staged = await store.stage()
    applied = await store.apply(
        run_id=staged.run_id,
        expected_digest=staged.digest,
        writer_stopped=True,
    )
    assert applied.state == "completed" and applied.imported == 1

    async with aiosqlite.connect(fixture.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT definition_mode FROM team_subagent WHERE id='osa_legacy'",
        ) as cursor:
            assert str((await cursor.fetchone())["definition_mode"]) == "revisioned"
        async with db.execute(
            "SELECT release_semantics_activated_at FROM team_subagent_schema_meta WHERE singleton=1",
        ) as cursor:
            assert (await cursor.fetchone())[0] is not None, "apply must activate the runtime gate"

    binding = await _admit(fixture, name="legacy-agent", dispatch_key="call:post-import")
    assert binding is not None, "a migrated Agent must bind exactly, not fall back"
    assert binding.agent_id == "osa_legacy"


def test_admission_refusal_never_hands_the_model_an_internal_code() -> None:
    """The Lead quoted `agent_not_runnable` to a user verbatim; keep codes out."""
    from bowhead_core.subagent import _admission_block_guidance

    guidance = _admission_block_guidance("agent_not_runnable")

    assert "agent_not_runnable" not in guidance
    assert "configuration" in guidance
    assert "try again later" in guidance  # present only as an explicit prohibition
    assert guidance.count("Do not") >= 1
