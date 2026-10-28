"""Persist pre-workbench release shapes for recovery and exact-binding tests.

This is test data construction, not a publication API: callers supply a saved,
valid draft and cannot use it to test authorization or idempotency. Current
Owner admission tests must use prepare_working_copy and AgentVersionService.
"""

from __future__ import annotations

import json

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import AgentAudience, AgentPublicationResult, AgentRevisionStore
from bowhead_core.catalog_localization import materialize_confirmed_agent_localizations_conn


async def seed_legacy_publication(
    store: AgentRevisionStore,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
    working_copy_id: str,
    release_note: str | None = None,
    receipt_key: str | None = None,
) -> AgentPublicationResult:
    """Seed a historical candidate or release using current snapshot primitives."""
    timestamp = store._now()
    async with open_shared_ctx(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        aggregate = await store._aggregate_conn(db, team_id=team_id, agent_id=agent_id)
        working = await store._working_copy_conn(db, team_id=team_id, agent_id=agent_id)
        assert working is not None and working["id"] == working_copy_id
        definition = json.loads(working["definition_json"])
        listing = await materialize_confirmed_agent_localizations_conn(
            db, team_id=team_id, agent_id=agent_id, working_copy_id=working_copy_id, listing=json.loads(working["listing_json"])
        )
        audience = AgentAudience.from_mapping(json.loads(working["audience_json"]))
        validation = await store._validator.validate(
            db,
            team_id=team_id,
            agent_id=agent_id,
            owner_user_id=actor_user_id,
            routing_name=str(aggregate["name"]),
            definition=definition,
            listing=listing,
            audience=audience,
        )
        assert validation.safety_status != "blocked", "Historical fixtures must contain valid frozen input"
        frozen = await store._freeze_candidate_conn(
            db,
            aggregate=aggregate,
            working=working,
            definition=definition,
            listing=listing,
            audience=audience,
            validation=validation,
            actor_user_id=actor_user_id,
            release_note=release_note,
            eval_json=None,
            timestamp=timestamp,
        )
        assert frozen.created, "Seed each historical candidate only once"
        if store._requires_review(validation, audience=audience):
            event_id = await store._insert_event_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                candidate_id=frozen.candidate_id,
                release_id=None,
                event_type="candidate.submitted",
                actor_user_id=actor_user_id,
                payload={
                    "audience_mode": audience.mode,
                    "behavior_fingerprint": frozen.behavior_fingerprint,
                    "candidate_hash": frozen.candidate_hash,
                    "primary_capability_ref": frozen.primary_capability_ref,
                    "risk_level": validation.compilation.risk.level,
                },
                timestamp=timestamp,
            )
            await store._enqueue_reviewers_conn(db, team_id=team_id, event_id=event_id, candidate_id=frozen.candidate_id, timestamp=timestamp)
            result = AgentPublicationResult(
                state="pending",
                agent_id=agent_id,
                candidate_id=frozen.candidate_id,
                revision_id=frozen.revision_id,
                release_id=None,
                candidate_row_version=frozen.candidate_row_version,
                aggregate_row_version=int(aggregate["row_version"]),
            )
        else:
            result = await store._activate_conn(
                db,
                aggregate=aggregate,
                candidate=frozen.candidate,
                definition=definition,
                listing=listing,
                audience=audience,
                validation=validation,
                actor_user_id=actor_user_id,
                self_review=False,
                decision_reason="direct_publish",
                timestamp=timestamp,
            )
        if receipt_key is not None:
            # Historical reconciliation only reads this receipt; it never replays the removed writer.
            await store._store_receipt_conn(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                scope=f"agent.publish:{agent_id}",
                idempotency_key=receipt_key,
                request_hash="sha256:" + "0" * 64,
                result=result,
                timestamp=timestamp,
            )
        await db.commit()
        return result
