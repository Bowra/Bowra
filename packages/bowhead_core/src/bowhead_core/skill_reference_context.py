"""Recognize durable reference turns without granting or reconstructing a lease.

Only a server-supplied core database is consulted. Missing/corrupt source state is
not permission to run a legacy Skill handler, and probing never creates a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from bowhead_core.session_store import _requires_reference_execution_in_tx

if TYPE_CHECKING:
    from bowhead_core.agent_dependency_compiler import ExactSkillGrant
    from bowhead_core.agent_execution_binding import ExactAgentExecutionBinding
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime


class ReferenceContextUnavailable(ValueError):
    """Finite source-recognition failure, without paths or private input."""


@dataclass(frozen=True, slots=True)
class ReferenceHelperSkillScope:
    """Recognize one actual helper without granting any selected material."""

    runtime: ReferencePrimaryRuntime = field(repr=False)
    binding: ExactAgentExecutionBinding = field(repr=False)
    subagent_id: str
    grants: tuple[ExactSkillGrant, ...] = field(repr=False)

    def _require_receiver(self, ctx: Any) -> None:
        from bowhead_core.agents._lead_context import get_current_subagent_id

        source_path = getattr(ctx, "reference_source_db_path", None)
        if (
            get_current_subagent_id() != self.subagent_id
            or getattr(ctx, "reference_helper_skill_scope", None) is not self
            or getattr(ctx, "reference_skill_required", False) is not False
            or getattr(ctx, "reference_skill_access", None) is not None
            or ctx.skill_runtime_grants != self.grants
            or not isinstance(source_path, (str, Path))
            or Path(source_path).resolve() != Path(self.runtime.bindings.db_path).resolve()
            or any(getattr(ctx, key, None) != value for key, value in {**self.runtime.scope.identity(), "turn_id": self.runtime.turn_id}.items())
        ):
            raise ReferenceContextUnavailable("reference_helper_context_unavailable")

    async def require(self, ctx: Any) -> None:
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.chat_reference_runtime_store import require_reference_worker_in_connection
        from bowhead_core.chat_reference_storage import _row

        self._require_receiver(ctx)
        runtime, binding = self.runtime, self.binding
        async with open_shared_ctx(str(runtime.bindings.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                primary = await runtime._read(db, read_output=True)
                execution = await _row(db, "SELECT * FROM turn_agent_execution WHERE id=?", (binding.execution_id,))
                if (
                    primary.plan.orchestration_contract != "lead-delegation.v1"
                    or primary.binding.execution_id == binding.execution_id
                    or execution is None
                    or execution["status"] != "running"
                    or execution["dispatch_source"] != "lead"
                    or binding.turn_id != runtime.turn_id
                    or binding.team_id != runtime.scope.team_id
                    or any(
                        execution[key] != getattr(binding, key)
                        for key in (
                            "turn_id",
                            "team_id",
                            "agent_id",
                            "agent_source_kind",
                            "revision_ref",
                            "release_id",
                            "policy_fingerprint",
                            "capability_fingerprint",
                        )
                    )
                ):
                    raise ReferenceContextUnavailable("reference_helper_context_unavailable")
                await require_reference_worker_in_connection(
                    db, scope=runtime.scope, turn_id=runtime.turn_id, owner_id=runtime.owner_id, generation=runtime.generation
                )
                self._require_receiver(ctx)
            finally:
                await db.rollback()


async def _ordinary_primary_receiver(db: aiosqlite.Connection, *, turn_id: str, subagent_id: str) -> bool:
    """Whether `subagent_id` is the admitted primary of a Skill-free `initial-primary.v1` composition."""
    import hashlib

    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_resolutions'") as cursor:
        if await cursor.fetchone() is None:
            return False
    async with db.execute(
        "SELECT execution_id FROM chat_reference_resolutions WHERE turn_id=? AND kind='composition' "
        "AND json_extract(CASE WHEN json_valid(composition_json) THEN composition_json ELSE '{}' END,'$.runtime_policy')='initial-primary.v1'",
        (turn_id,),
    ) as cursor:
        executions = [row[0] for row in await cursor.fetchall()]
    return any(
        isinstance(execution_id, str) and "sub_" + hashlib.sha256(execution_id.encode()).hexdigest()[:20] == subagent_id
        for execution_id in executions
    )


async def reference_turn_requires_context(db_path: str | Path, *, turn_id: str, subagent_id: str | None = None) -> bool:
    try:
        if not isinstance(db_path, (str, Path)) or not str(db_path) or type(turn_id) is not str or not turn_id:
            raise ValueError()
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                async with db.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)) as cursor:
                    if await cursor.fetchone() is None:
                        raise ValueError()
                # Includes unknown input formats and retained purge tombstones.
                # Do not parse/drop an unfamiliar source into the legacy path.
                if await _requires_reference_execution_in_tx(db, turn_id):
                    # The admitted @Agent of an `initial-primary.v1` composition has no
                    # selected Skill and no exact Skill baseline (e.g. @Coach, which reads
                    # its manifest's built-in package on demand). It keeps its own Tool
                    # and Skill scope, as when Lead delegates to it; Lead and every
                    # helper of the same Turn still require exact reference access.
                    return not (subagent_id and await _ordinary_primary_receiver(db, turn_id=turn_id, subagent_id=subagent_id))
                async with db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='chat_reference_plan_step_scope'") as cursor:
                    if await cursor.fetchone() is None:
                        return False
                async with db.execute(
                    "SELECT p.spec_json FROM plans p WHERE p.execution_turn_id=? "
                    "AND json_extract(CASE WHEN json_valid(p.spec_json) THEN p.spec_json ELSE '{}' END,'$.reference.contract')='reference-plan.v1'",
                    (turn_id,),
                ) as cursor:
                    plan = await cursor.fetchone()
                if plan is None:
                    return False
                if not subagent_id:
                    return True
                import hashlib

                from bowhead_core.chat_reference_plan import read_requirements

                requirements = read_requirements(plan[0])
                async with db.execute(
                    "SELECT id,dispatch_key FROM turn_agent_execution WHERE turn_id=? AND dispatch_source='plan'", (turn_id,)
                ) as cursor:
                    for execution_id, dispatch_key in await cursor.fetchall():
                        if "sub_" + hashlib.sha256(execution_id.encode()).hexdigest()[:20] == subagent_id:
                            return any(dispatch_key.startswith(f"step:{step_id}:rev:") for step_id in requirements.main_steps)
                return False
            finally:
                await db.rollback()
    except Exception:
        raise ReferenceContextUnavailable("reference_context_unavailable") from None


async def refresh_reference_skill_context(ctx: Any) -> None:
    try:
        helper = getattr(ctx, "reference_helper_skill_scope", None)
        paths = []
        explicit = getattr(ctx, "reference_source_db_path", None)
        if explicit is not None:
            paths.append(explicit)
        store = getattr(ctx, "session_store", None)
        if store is not None:
            paths.append(getattr(store, "db_path", None))
        if not paths:
            if helper is not None:
                raise ValueError()
            # Non-chat callers cannot invent a Chat source/owner. Explicit
            # reference markers still require an exact access object downstream.
            return
        if any(not isinstance(path, (str, Path)) or not str(path) for path in paths):
            raise ValueError()
        resolved = {Path(path).resolve() for path in paths}
        if len(resolved) != 1:
            raise ValueError()
        access = getattr(ctx, "reference_skill_access", None)
        if access is not None:
            access_path = getattr(access, "source_db_path", None)
            if not isinstance(access_path, (str, Path)) or not str(access_path) or Path(access_path).resolve() not in resolved:
                raise ValueError()
        from bowhead_core.agents._lead_context import get_current_subagent_id

        required = await reference_turn_requires_context(resolved.pop(), turn_id=getattr(ctx, "turn_id", None), subagent_id=get_current_subagent_id())
        if helper is not None:
            if not required or type(helper) is not ReferenceHelperSkillScope:
                raise ValueError()
            await helper.require(ctx)
            return
        if required:
            if getattr(ctx, "reference_skill_required", False) is False:
                ctx.reference_skill_required = True
        # Never clear a prior requirement because a source was replaced/lost.
        # The access object and receiver/lease are always verified independently.
    except Exception:
        raise ReferenceContextUnavailable("reference_context_unavailable") from None
