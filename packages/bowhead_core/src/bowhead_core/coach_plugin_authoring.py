"""Focused shared Plugin changes with atomic revision and replay receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx, run_sqlite_authority_write
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.team_plugin_store import TeamPluginStore, _validate_contents, rebase_skill_grants

Authorize = Callable[[aiosqlite.Connection, bool], Awaitable[bool]]
Validate = Callable[[dict, list[dict] | None], Awaitable[dict]]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _target(item: dict) -> dict:
    return {
        "kind": "team_plugin",
        "id": item["id"],
        "version": item["row_version"],
        "revision": item["revision"],
        "content_hash": item["content_hash"],
    }


def _rebase_to_validation(candidate: dict, validation: dict) -> dict:
    """Store the Skill revisions the validation resolved, not the caller's possibly older ones."""
    current = validation.pop("skill_grants", None) or []
    return {**candidate, "skill_grants": rebase_skill_grants(candidate["skill_grants"], current)}


@asynccontextmanager
async def _read_transaction(db_path: str) -> AsyncIterator[aiosqlite.Connection]:
    """One consistent read snapshot that takes no cross-file authority lease.

    Inspect, validate, the receipt read and the save's first check only read. On a write-capable
    connection their scope check pins the execution file's shared lease and fails at once beside
    an authority writer. The save's write re-authorizes and rechecks inside its own transaction.
    """
    async with open_pooled_read_ctx(db_path) as db:
        await db.execute("BEGIN")
        try:
            yield db
        finally:
            await db.rollback()


class CoachPluginAuthoring:
    def __init__(self, db_path: str, scope: ReferenceRequestScope, authorize: Authorize, validate: Validate) -> None:
        self.db_path, self.scope, self.authorize, self.validate_resources = db_path, scope, authorize, validate
        self.plugins = TeamPluginStore(db_path)

    async def _current(self, db: aiosqlite.Connection, identity: str) -> dict:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM team_capability WHERE team_id=? AND id=? AND status='active'", (self.scope.team_id, identity)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ValueError("plugin_unavailable")
        return await self.plugins._read_conn(db, row)

    async def _impact(self, db: aiosqlite.Connection, identity: str | None) -> dict:
        async with db.execute(
            """SELECT 'release' AS kind,a.id,a.live_revision_id AS version FROM team_subagent a
               JOIN team_subagent_capability_binding b ON b.team_id=a.team_id AND b.subagent_id=a.id AND b.revision_id=a.live_revision_id
               WHERE a.team_id=? AND b.capability_id=? AND a.lifecycle_state='active'
               UNION SELECT 'working',w.subagent_id,CAST(w.row_version AS TEXT) FROM team_subagent_working_copy w,
               json_each(w.definition_json,'$.capability_ids') j WHERE w.team_id=? AND j.value=?
               UNION SELECT 'creation',d.id,CAST(d.row_version AS TEXT) FROM team_subagent_draft d,
               json_each(d.definition_json,'$.value.capability_ids') j WHERE d.team_id=? AND j.value=? AND d.status IN ('editing','ready','blocked')
               ORDER BY kind,id,version""",
            (self.scope.team_id, identity, self.scope.team_id, identity, self.scope.team_id, identity),
        ) as cursor:
            consumers = [tuple(row) for row in await cursor.fetchall()]
        return {
            "fingerprint": "sha256:" + _hash(consumers),
            "affected_agent_count": len({row[1] for row in consumers if row[0] != "creation"}),
            "creation_draft_count": sum(row[0] == "creation" for row in consumers),
            "scope": "future_resolution_of_shared_plugin",
            "running_executions_changed": False,
        }

    async def inspect(self, *, plugin_id: str) -> dict:
        async with _read_transaction(self.db_path) as db:
            can_save = await self.authorize(db, False)
            item = await self._current(db, plugin_id)
            validation = await self.validate_resources(item, None)
            validation.pop("skill_grants", None)
            return {
                "ok": True,
                "target": _target(item),
                "plugin": item,
                "impact": await self._impact(db, plugin_id),
                "can_save": can_save,
                "dependency_validation": validation,
                "executed": False,
            }

    async def _candidate(
        self, db: aiosqlite.Connection, target: dict | None, patch: dict, expected_impact: str | None
    ) -> tuple[dict, dict, dict | None]:
        if len(_json(patch).encode()) > 256 * 1024:
            raise ValueError("plugin_patch_too_large")
        current = await self._current(db, target["id"]) if target else None
        if current is not None and _target(current) != target:
            raise ValueError("plugin_version_conflict")
        impact = await self._impact(db, target["id"] if target else None)
        if target and impact["fingerprint"] != expected_impact:
            raise ValueError("plugin_impact_changed")
        if not patch or set(patch) - {"name", "description", "tools", "skill_grants"}:
            raise ValueError("plugin_patch_invalid")
        candidate = (
            {key: current[key] for key in ("name", "description", "tool_refs", "skill_grants")}
            if current
            else {"name": "", "description": "", "tool_refs": [], "skill_grants": []}
        )
        for key in ("name", "description", "skill_grants"):
            if key in patch:
                candidate[key] = patch[key]
        if "tools" in patch:
            candidate["tool_refs"] = [reference["id"] for reference in patch["tools"]]
        name, description, tools, grants = _validate_contents(**candidate)
        candidate = {"name": name, "description": description, "tool_refs": list(tools), "skill_grants": list(grants)}
        added = set(candidate["tool_refs"]) - set(current["tool_refs"] if current else ())
        if added:
            from bowhead_core.resource_disable import runtime_tool_name
            from bowhead_core.subagent_authoring_catalog import TOOL_DISABLED_BY_TEAM, team_turned_off_tools

            # Same rule as the Plugin editor: a Tool the team turned off stays where it was, never a new pick.
            turned_off = await team_turned_off_tools(self.plugins, self.scope.team_id)
            if any(runtime_tool_name(ref) in turned_off for ref in added):
                raise ValueError(TOOL_DISABLED_BY_TEAM)
        return candidate, impact, current

    async def validate(self, *, target: dict | None, patch: dict, expected_impact: str | None) -> dict:
        from bowhead_core.coach_validation import authoring_validation_report, candidate_digest

        async with _read_transaction(self.db_path) as db:
            can_save = await self.authorize(db, False)
            candidate, impact, _ = await self._candidate(db, target, patch, expected_impact)
            validation = await self.validate_resources(candidate, patch.get("tools"))
            candidate = _rebase_to_validation(candidate, validation)
            return {
                "ok": True,
                "validation_report": authoring_validation_report(
                    target=target,
                    candidate_hash=candidate_digest(candidate),
                    structure="passed",
                    dependencies="passed" if validation["status"] == "ready" else "blocked",
                    dependency_fingerprint=validation.get("fingerprint"),
                ),
                "target": target,
                "candidate": candidate,
                "impact": impact,
                "can_save": can_save,
                "dependency_validation": validation,
                "persisted": False,
                "executed": False,
            }

    async def _receipt(self, db: aiosqlite.Connection, operation_id: str) -> aiosqlite.Row | None:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM coach_plugin_operation WHERE id=? AND team_id=? AND user_id=? AND workspace_id=?",
            (operation_id, self.scope.team_id, self.scope.user_id, self.scope.workspace_id),
        ) as cursor:
            return await cursor.fetchone()

    async def save(self, *, target: dict | None, patch: dict, expected_impact: str | None, request_id: str, turn_id: str) -> dict:
        operation_id = "cpw_" + _hash([self.scope.team_id, self.scope.user_id, turn_id, request_id])
        request_hash = _hash([self.scope.workspace_id, target, patch, expected_impact])

        async def recorded(db: aiosqlite.Connection) -> dict | None:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM coach_plugin_operation WHERE id=?", (operation_id,)) as cursor:
                existing = await cursor.fetchone()
            if existing is None:
                return None
            if existing["request_hash"] != request_hash:
                raise ValueError("plugin_idempotency_conflict")
            return json.loads(existing["result_json"])

        # Dependency validation may do I/O, so it runs before the write transaction instead of
        # holding the writer and the authority lease (census T7).
        async with _read_transaction(self.db_path) as db:
            await self.authorize(db, True)
            previous = await recorded(db)
            if previous is not None:
                return previous
            candidate, _, _ = await self._candidate(db, target, patch, expected_impact)
        validation = await self.validate_resources(candidate, patch.get("tools"))
        validated = _rebase_to_validation(candidate, validation)
        if validation["status"] == "blocked":
            return {"ok": False, "error": "plugin_dependencies_blocked", "dependency_validation": validation, "persisted": False}

        # The Plugin row is fenced, so the write repeats as a whole when a running Turn holds the
        # lease. Each attempt rechecks access, the receipt, the target version and the impact.
        async def write(db: aiosqlite.Connection) -> dict:
            await self.authorize(db, True)
            previous = await recorded(db)
            if previous is not None:
                return previous
            current_candidate, impact, current = await self._candidate(db, target, patch, expected_impact)
            if current_candidate != candidate:
                raise ValueError("plugin_version_conflict")
            unchanged = current is not None and all(current[key] == value for key, value in validated.items())
            item = (
                current
                if unchanged
                else await self.plugins.save_in_connection(
                    db,
                    team_id=self.scope.team_id,
                    actor_user_id=self.scope.user_id,
                    plugin_id=target["id"] if target else None,
                    expected_row_version=target["version"] if target else None,
                    **validated,
                )
            )
            result = {
                "ok": True,
                "operation_id": operation_id,
                "state": "succeeded",
                "effect": "unchanged" if unchanged else "plugin_saved",
                "original_target": target,
                "result_target": _target(item),
                "impact": impact,
                "dependency_validation": validation,
                "validation_scope": "original_save_only_revalidate_before_binding",
                "agent_bound": False,
                "executed": False,
            }
            await db.execute(
                "INSERT INTO coach_plugin_operation(id,team_id,user_id,workspace_id,origin_session_id,origin_turn_id,request_hash,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    self.scope.team_id,
                    self.scope.user_id,
                    self.scope.workspace_id,
                    self.scope.session_id or "",
                    turn_id,
                    request_hash,
                    _json(result),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return result

        return await run_sqlite_authority_write(self.db_path, write)

    async def get(self, *, operation_id: str) -> dict:
        async with _read_transaction(self.db_path) as db:
            await self.authorize(db, False)
            receipt = await self._receipt(db, operation_id)
            if receipt is None:
                raise ValueError("plugin_operation_unavailable")
            result = json.loads(receipt["result_json"])
            async with db.execute(
                "SELECT row_version,status FROM team_capability WHERE team_id=? AND id=?", (self.scope.team_id, result["result_target"]["id"])
            ) as cursor:
                current = await cursor.fetchone()
            result["current_result_matches"] = bool(current and current[0] == result["result_target"]["version"] and current[1] == "active")
            result["validation_scope"] = "original_save_only_revalidate_before_binding"
            return result
