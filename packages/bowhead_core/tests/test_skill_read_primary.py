"""Canonical mixed source/plan + current source/receiver + parameter rendering.

The fixture uses real storage/admission primitives, without a submission writer,
receipt or dispatch. Stored Skill policy digests never substitute for live access.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection
from bowhead_core.chat_reference_runtime_store import primary_composition
from bowhead_core.chat_reference_storage import reference_source_hash
from bowhead_core.skill_read_primary import prepare_reference_primary_skill_reads
from bowhead_core.skill_read_receiver import SkillReadReceiverError
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case = _receiver_case
start_case = _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


async def read(state):
    async with open_shared_ctx(state.case.fixture.db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        changes = db.total_changes  # Connection-wide cumulative count: reused connections start above 0; compare only this transaction's delta.
        result = await read_reference_skill_primary_in_connection(db, scope=state.case.scope, turn_id="t_reference", bindings=state.case.bindings)
        assert db.in_transaction and db.total_changes == changes and db.row_factory is None
        await db.rollback()
        return result


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_canonical_source_supplies_exact_parameters_with_agent_after_skills(receiver_case):
    state = receiver_case
    original = await read(state)
    prepared, rendered = await prepare_reference_primary_skill_reads(state.sources, scope=state.scope, turn_id="t_reference")
    assert prepared.receiver.source_hash == original.plan.source_hash
    assert prepared.receiver.execution_id == original.binding.execution_id
    assert prepared.closure.roots == (state.selected, state.child)
    assert [item["parameters"] for item in rendered.document()["skills"]] == [{"year": 2025}, {"year": 2026}]
    assert [item["resource"]["id"] for item in rendered.document()["skills"]] == [state.child.id, state.selected.id]
    assert "SAVED_METHOD_LABEL" not in rendered.content.decode()
    original.source.input_document.nodes[0].parameters["year"] = 2001
    _, again = await prepare_reference_primary_skill_reads(state.sources, scope=state.scope, turn_id="t_reference")
    assert again == rendered
    with sqlite3.connect(state.case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (1,)
        for table in ("chat_reference_resolutions", "chat_reference_content_snapshots", "chat_turn_spawn_dispatch", "turn_events"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_initial_worker_reader_still_refuses_a_mixed_source(receiver_case):
    with pytest.raises(ValueError, match="^reference_start_unsupported$"):
        await receiver_case.reader()


@pytest.mark.parametrize("receiver_case", ["source_agent_first"], indirect=True)
async def test_initial_composition_cannot_drop_skill_selections_from_a_preparation_reader(receiver_case):
    primary = await read(receiver_case)
    assert primary.plan.admissions[0].resource.kind == "agent"
    with pytest.raises(ValueError, match="^reference_composition_unavailable$"):
        primary_composition(primary)


@pytest.mark.parametrize("change", ["user", "workspace", "session", "no_session", "turn", "assignment", "expired", "disabled_skill"])
@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_source_and_current_authority_are_rechecked(receiver_case, change, monkeypatch):
    state = receiver_case
    scope = state.scope
    turn_id = "t_reference"
    if change in {"user", "workspace", "session"}:
        scope = scope.model_copy(update={change + "_id": "unrelated"})
    elif change == "no_session":
        scope = scope.model_copy(update={"session_id": None})
    elif change == "turn":
        turn_id = "other"
    elif change == "disabled_skill":
        await state.sources.instances.upsert(
            user_id=scope.user_id, name="method", source_type="builtin", source_path=str(state.root / "method"), enabled=False
        )
    elif change == "expired":
        future = datetime.now(UTC) + timedelta(days=365)
        monkeypatch.setattr("bowhead_core.chat_reference_storage._now", lambda: future)
    else:
        with sqlite3.connect(state.case.fixture.db_path) as db:
            db.execute("UPDATE agent_assignment SET personal_state='removed' WHERE user_id=?", (scope.user_id,))
    with pytest.raises((SkillReadReceiverError, SkillReadResolutionError)):
        await prepare_reference_primary_skill_reads(state.sources, scope=scope, turn_id=turn_id)


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
async def test_revocation_during_material_capture_prevents_a_source_bound_result(receiver_case, monkeypatch):
    state = receiver_case
    original = state.sources.prepare_package
    calls = []

    async def prepare_then_remove(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result.proof.resource)
        with sqlite3.connect(state.case.fixture.db_path) as db:
            db.execute("UPDATE agent_assignment SET personal_state='removed' WHERE user_id=?", (state.scope.user_id,))
        return result

    monkeypatch.setattr(state.sources, "prepare_package", prepare_then_remove)
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_unavailable$"):
        await prepare_reference_primary_skill_reads(state.sources, scope=state.scope, turn_id="t_reference")
    assert calls == [state.selected, state.child]


@pytest.mark.parametrize("receiver_case", ["source_invoke", "source_agent_parameters"], indirect=True)
async def test_preparation_reader_rejects_unsupported_stored_shapes(receiver_case):
    # Stored through the general source/plan contract with all immutable guards
    # enabled; those broader storage records do not imply preparation support.
    # ("Agent + Capability + Skill" has been a supported shortcut-reference shape since dc64d05af and no longer belongs here.)
    with pytest.raises(ValueError, match="^reference_start_unsupported$"):
        await read(receiver_case)


@pytest.mark.parametrize("receiver_case", ["source_no_agent"], indirect=True)
async def test_skill_only_source_without_lead_root_has_no_primary(receiver_case):
    # Skill-only sources have been supported since 5f88f6e67, but primary execution identity must come from the immutable
    # Lead root binding (binding_source='lead'); this case has only a mention root, so readers must not invent one.
    with pytest.raises(ValueError, match="^reference_primary_unavailable$"):
        await read(receiver_case)


@pytest.mark.parametrize("receiver_case", ["source"], indirect=True)
@pytest.mark.parametrize("change", ["source", "plan", "execution"])
async def test_a_later_reader_response_cannot_rebind_the_selected_original(receiver_case, change, monkeypatch):
    state = receiver_case
    calls = []

    async def changing_reader(*args, **kwargs):
        primary = await read_reference_skill_primary_in_connection(*args, **kwargs)
        calls.append(primary)
        if len(calls) == 1:
            return primary
        # A malformed reader response exercises this adapter boundary. SQLite
        # immutable guards remain enabled; this is not a claimed database race.
        if change == "source":
            document = primary.source.input_document.model_copy(deep=True)
            document.nodes[0].parameters["year"] = 2001
            source = replace(primary.source, input_document=document)
            return replace(primary, source=source, plan=primary.plan.model_copy(update={"source_hash": reference_source_hash(source)}))
        if change == "plan":
            plan = primary.plan.model_copy(deep=True)
            plan.admissions[0] = plan.admissions[0].model_copy(update={"policy_digest": "sha256:" + "0" * 64})
            return replace(primary, plan=plan)
        return replace(primary, binding=replace(primary.binding, execution_id="another-execution"))

    monkeypatch.setattr("bowhead_core.skill_read_primary.read_reference_skill_primary_in_connection", changing_reader)
    with pytest.raises(SkillReadReceiverError, match="^reference_skill_receiver_changed$"):
        await prepare_reference_primary_skill_reads(state.sources, scope=state.scope, turn_id="t_reference")
    assert len(calls) == 2
