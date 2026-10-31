"""Compaction completion survives the commit-to-event crash boundary."""

import json

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.context_activity import ContextActivity, recover_context_publications_in_connection
from bowhead_core.context_store import ContextStoreError
from bowhead_core.turn_log_store import TurnLogStore
from test_agent_collaboration import collab as collaboration_fixture
from test_context_execution import owner, prepare
from test_context_maintenance import context_case as maintenance_fixture

collab = collaboration_fixture
context_case = maintenance_fixture
pytestmark = pytest.mark.integration


async def test_committed_event_is_repaired_once_with_original_child_identity(collab, context_case):
    execution = owner(collab, subagent_id="sub_child", profile="compute")
    await prepare(execution, context_case)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert await recover_context_publications_in_connection(db, turn_id=collab.worker.turn_id) == 1
        assert await recover_context_publications_in_connection(db, turn_id=collab.worker.turn_id) == 0
        await db.commit()
        async with db.execute("SELECT id,payload_json FROM turn_events WHERE event_type='agent.context'") as cursor:
            row = await cursor.fetchone()
    payload = json.loads(row[1])
    assert payload["generation_id"] == execution.generation
    assert payload["subagent_id"] == payload["parent_agent_id"] == "sub_child"
    assert payload["phase"] == "committed"
    activity = ContextActivity(
        **execution.activity.await_args_list[-1].args[0],
        execution_id=collab.requester,
        lane="ordinary",
        subagent_id="sub_child",
        profile="compute",
    )
    # A late live retry reads the already persisted fact instead of duplicating it.
    repeated = await TurnLogStore(collab.path).record(
        turn_id=collab.worker.turn_id,
        event_type="agent.context",
        payload=activity.payload(collab.worker.turn_id),
        event_id=activity.event_id(),
    )
    assert repeated is not None and not repeated.inserted


async def test_recovery_refuses_changed_completion_identity(collab, context_case):
    execution = owner(collab)
    await prepare(execution, context_case)
    async with open_shared_ctx(collab.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await recover_context_publications_in_connection(db, turn_id=collab.worker.turn_id)
        await db.execute("UPDATE turn_events SET payload_json='{}' WHERE event_type='agent.context'")
        with pytest.raises(ContextStoreError, match="publication_conflict"):
            await recover_context_publications_in_connection(db, turn_id=collab.worker.turn_id)
        await db.rollback()
