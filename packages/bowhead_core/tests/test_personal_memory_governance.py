from __future__ import annotations

import aiosqlite
import pytest
from bowhead_core.personal_memory_governance import (
    PersonalMemoryGovernanceStore,
)
from bowhead_core.user_memory import MemoryScopeConflictError, UserMemoryStore
from bowhead_memory.candidates import MemoryCandidateStore


@pytest.fixture
async def stores(tmp_path):
    db_path = tmp_path / "memory.db"
    memory = UserMemoryStore(db_path)
    await memory.initialize()
    candidates = MemoryCandidateStore(
        db_path=str(db_path),
        fingerprint_key="test-candidate-fingerprint",
    )
    await candidates.initialize()
    governance = PersonalMemoryGovernanceStore(
        db_path,
        root_secret="test-governance-secret",
    )
    return memory, candidates, governance


@pytest.mark.asyncio
async def test_approval_preserves_created_provenance_and_scrubs_candidate(
    stores,
):
    memory, candidates, governance = stores
    existing = await memory.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="weekly",
        source="user_explicit",
    )
    # Since 1e0d7df5f (FR9), user_explicit entries have confidence=high; candidate approval (low) cannot
    # overwrite them by design (MemoryConfidenceConflictError, candidate stays pending, separately tested). This case enforces
    # preservation of creation provenance after overwrite, using pre-FR9 legacy rows with no confidence (treated as low).
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("UPDATE memory_entries SET confidence=NULL WHERE id=?", (existing.id,))
        await db.commit()
    candidate = await candidates.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
        reason="Repeatedly requested monthly reporting",
        source_turn_id="t_source",
    )
    assert candidate is not None

    approved = await governance.approve_candidate(
        user_id="u1",
        candidate_id=candidate.id,
        target="structured",
        decided_by="u1",
    )

    [updated] = await memory.list_product_for_user("u1")
    assert updated.id == existing.id
    assert updated.parsed_value() == "monthly"
    assert updated.created_source_type == "user_explicit"
    assert updated.created_source_candidate_id is None
    assert updated.last_modified_actor_type == "user"
    assert updated.last_modified_actor_id == "u1"
    assert approved.status == "approved"
    assert approved.key == ""
    assert approved.value is None
    assert approved.reason is None
    assert approved.source_turn_id is None
    async with aiosqlite.connect(candidates.db_path) as db:
        [row] = await db.execute_fetchall(
            """
            SELECT content_fingerprint, COUNT(*) OVER ()
            FROM memory_candidates
            JOIN memory_candidate_suppressions
              ON memory_candidate_suppressions.user_id=
                 memory_candidates.user_id
            WHERE memory_candidates.id=?
            """,
            (candidate.id,),
        )
    assert row == (None, 1)


@pytest.mark.asyncio
async def test_approval_rejects_global_candidate_for_space_entry(stores):
    memory, candidates, governance = stores
    await memory.upsert_product_memory(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="weekly",
        workspace_id="ws_retail",
    )
    candidate = await candidates.enqueue_if_new(
        user_id="u1",
        kind="preference",
        key="report_grain",
        value="monthly",
        reason=None,
        source_turn_id="t_source",
        workspace_id=None,
    )
    assert candidate is not None

    with pytest.raises(MemoryScopeConflictError):
        await governance.approve_candidate(
            user_id="u1",
            candidate_id=candidate.id,
            target="structured",
            decided_by="u1",
        )

    pending = await candidates.get(candidate.id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.value == "monthly"
