"""Bind optional policy metadata to its immutable, normally published revision."""

from __future__ import annotations

from collections.abc import Mapping

import aiosqlite

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError, AgentRuntimeExtensions, read_agent_runtime_extensions


async def require_extension_policy_write(db: aiosqlite.Connection, definition: Mapping, *, audience_mode: str) -> AgentRuntimeExtensions | None:
    policy = read_agent_runtime_extensions(definition)
    if policy is None:
        return None
    if not db.in_transaction:
        raise AgentRuntimeExtensionError("agent_runtime_extension_transaction_required")
    policy.require_private_audience(audience_mode)
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_agent_extensions_candidate_insert'") as cursor:
        if await cursor.fetchone() is None:
            raise AgentRuntimeExtensionError("agent_runtime_extension_migration_required")
    return policy


async def retain_extension_policy_in_connection(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, revision_ref: str, definition: Mapping, audience_mode: str, created_at: str
) -> None:
    policy = await require_extension_policy_write(db, definition, audience_mode=audience_mode)
    if policy is None:
        return
    await db.execute(
        "INSERT INTO team_subagent_revision_extensions(revision_ref,team_id,agent_id,definition_hash,extension_policy_digest,created_at) VALUES(?,?,?,?,?,?)",
        (revision_ref, team_id, agent_id, canonical_fingerprint(definition), policy.digest, created_at),
    )


async def read_published_extension_policy_in_connection(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, revision_ref: str, definition: Mapping
) -> AgentRuntimeExtensions | None:
    if not db.in_transaction:
        raise AgentRuntimeExtensionError("agent_runtime_extension_transaction_required")
    policy = read_agent_runtime_extensions(definition)
    if policy is None:
        return None
    try:
        async with db.execute(
            "SELECT definition_hash,extension_policy_digest FROM team_subagent_revision_extensions WHERE team_id=? AND agent_id=? AND revision_ref=?",
            (team_id, agent_id, revision_ref),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or tuple(row) != (canonical_fingerprint(definition), policy.digest):
            raise ValueError()
    except (aiosqlite.DatabaseError, ValueError):
        raise AgentRuntimeExtensionError("agent_runtime_extension_unproven") from None
    return policy
