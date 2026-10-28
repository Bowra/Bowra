"""Team Plugin catalog: versioned Tool and Skill selections.

The plugin is a reusable selection, never an authorization grant. Agent
publication and execution must compile its expanded resources with the same
guards used for directly selected resources.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_dependency_compiler import parse_exact_skill_grants
from bowhead_core.resource_dependency_projection import invalidate_working_copy_projection_conn


class TeamPluginError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def selected_plugin_ids(definition: Mapping[str, Any]) -> tuple[str, ...]:
    raw = definition.get("capability_ids", [])
    if not isinstance(raw, list) or len(raw) > 32 or any(not isinstance(value, str) or not value.startswith("tcap_") for value in raw):
        raise TeamPluginError("capability_selection_invalid")
    if len(set(raw)) != len(raw):
        raise TeamPluginError("capability_selection_duplicate")
    return tuple(raw)


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def skill_grant_identity(grant: Mapping[str, Any]) -> str:
    """Compare Skill grants without their revision.

    Skills follow their latest approved revision (agent-skill-gates D2): the
    compiler rebases an older revision, so two grants that differ only by
    revision select the same Skill with the same permissions.
    """
    return _canonical({key: value for key, value in grant.items() if key != "revision"})


def rebase_skill_grants(grants: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Replace each grant with the ``current`` grant of the same identity, when there is one."""
    latest = {skill_grant_identity(grant): grant for grant in current}
    return [dict(latest.get(skill_grant_identity(grant), grant)) for grant in grants]


def _validate_contents(
    name: str, description: str, tool_refs: Sequence[str], skill_grants: Sequence[Mapping[str, Any]]
) -> tuple[str, str, tuple[str, ...], tuple[dict[str, Any], ...]]:
    title = name.strip()
    detail = description.strip()
    if not title or len(title) > 80 or len(detail) > 1000:
        raise TeamPluginError("capability_details_invalid")
    if len(tool_refs) > 128 or len(skill_grants) > 64 or not tool_refs and not skill_grants:
        raise TeamPluginError("capability_resources_invalid")
    if any(not isinstance(ref, str) or not ref or len(ref) > 200 for ref in tool_refs) or len(set(tool_refs)) != len(tool_refs):
        raise TeamPluginError("capability_resources_invalid")
    try:
        grants = parse_exact_skill_grants(list(skill_grants))
    except ValueError as exc:
        raise TeamPluginError("capability_skill_invalid") from exc
    if len(grants) != len(skill_grants) or any(grant.kind == "user" or grant.distribution != "reference" for grant in grants):
        raise TeamPluginError("capability_skill_invalid")
    return title, detail, tuple(sorted(tool_refs)), tuple(grant.to_canonical_dict() for grant in grants)


class TeamPluginStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def list(self, team_id: str, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM team_capability WHERE team_id=?"
            if not include_disabled:
                sql += " AND status='active'"
            sql += " ORDER BY updated_at DESC,id"
            async with db.execute(sql, (team_id,)) as cursor:
                rows = await cursor.fetchall()
            return [await self._read_conn(db, row) for row in rows]

    async def get(self, team_id: str, plugin_id: str) -> dict[str, Any] | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id)) as cursor:
                row = await cursor.fetchone()
            return await self._read_conn(db, row) if row is not None else None

    async def create(
        self, *, team_id: str, actor_user_id: str, name: str, description: str, tool_refs: Sequence[str], skill_grants: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await self.save_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                plugin_id=None,
                expected_row_version=None,
                name=name,
                description=description,
                tool_refs=tool_refs,
                skill_grants=skill_grants,
            )
            await db.commit()
            return result

    async def update(
        self,
        *,
        team_id: str,
        plugin_id: str,
        actor_user_id: str,
        expected_row_version: int,
        name: str,
        description: str,
        tool_refs: Sequence[str],
        skill_grants: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await self.save_in_connection(
                db,
                team_id=team_id,
                actor_user_id=actor_user_id,
                plugin_id=plugin_id,
                expected_row_version=expected_row_version,
                name=name,
                description=description,
                tool_refs=tool_refs,
                skill_grants=skill_grants,
            )
            await db.commit()
            return result

    async def save_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        plugin_id: str | None,
        expected_row_version: int | None,
        name: str,
        description: str,
        tool_refs: Sequence[str],
        skill_grants: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Write under the caller's transaction, authorization and receipt boundary."""
        if not db.in_transaction:
            raise TeamPluginError("capability_transaction_required")
        title, detail, tools, grants = _validate_contents(name, description, tool_refs, skill_grants)
        db.row_factory = aiosqlite.Row
        now = _stamp()
        if plugin_id is None:
            if expected_row_version is not None:
                raise TeamPluginError("capability_version_conflict")
            plugin_id = f"tcap_{uuid4().hex}"
            revision = 1
            await db.execute(
                "INSERT INTO team_capability(id,team_id,name_key,status,current_revision,created_by,created_at,updated_at) VALUES (?,?,?,'active',1,?,?,?)",
                (plugin_id, team_id, title.casefold(), actor_user_id, now, now),
            )
        else:
            async with db.execute("SELECT * FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise TeamPluginError("capability_not_found")
            if row["status"] != "active":
                raise TeamPluginError("capability_disabled")
            if int(row["row_version"]) != expected_row_version:
                raise TeamPluginError("capability_version_conflict")
            revision = int(row["current_revision"]) + 1
            await db.execute(
                "UPDATE team_capability SET name_key=?,current_revision=?,row_version=row_version+1,updated_at=? WHERE id=?",
                (title.casefold(), revision, now, plugin_id),
            )
        await self._insert_revision(db, plugin_id, revision, actor_user_id, now, title, detail, tools, grants)
        async with db.execute("SELECT * FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id)) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return await self._read_conn(db, row)

    async def set_enabled(self, *, team_id: str, plugin_id: str, enabled: bool, expected_row_version: int) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await db.execute(
                    "UPDATE team_capability SET status=?,row_version=row_version+1,updated_at=? WHERE team_id=? AND id=? AND row_version=?",
                    ("active" if enabled else "disabled", _stamp(), team_id, plugin_id, expected_row_version),
                )
                if result.rowcount != 1:
                    raise TeamPluginError("capability_version_conflict")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        item = await self.get(team_id, plugin_id)
        assert item is not None
        return item

    async def delete(self, *, team_id: str, plugin_id: str, expected_row_version: int) -> None:
        """Remove a Plugin and current draft selections, preserving immutable Agent releases."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT row_version FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id)) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise TeamPluginError("capability_not_found")
                if int(row["row_version"]) != expected_row_version:
                    raise TeamPluginError("capability_version_conflict")

                async with db.execute(
                    "SELECT id,definition_json FROM team_subagent_working_copy WHERE team_id=?",
                    (team_id,),
                ) as cursor:
                    working_copies = await cursor.fetchall()
                now = _stamp()
                for working in working_copies:
                    definition = json.loads(str(working["definition_json"]))
                    ids = definition.get("capability_ids", [])
                    if not isinstance(ids, list) or plugin_id not in ids:
                        continue
                    definition["capability_ids"] = [item for item in ids if item != plugin_id]
                    await invalidate_working_copy_projection_conn(db, team_id=team_id, working_copy_id=str(working["id"]))
                    await db.execute(
                        """UPDATE team_subagent_working_copy
                           SET definition_json=?,validation_json='{"status":"not_validated"}',
                               row_version=row_version+1,updated_at=? WHERE id=?""",
                        (_canonical(definition), now, working["id"]),
                    )

                await db.execute(
                    "DELETE FROM team_subagent_capability_binding WHERE team_id=? AND capability_id=?",
                    (team_id, plugin_id),
                )
                await db.execute("DELETE FROM team_capability_resource WHERE capability_id=?", (plugin_id,))
                await db.execute("DELETE FROM team_capability_revision WHERE capability_id=?", (plugin_id,))
                await db.execute("DELETE FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    async def _insert_revision(
        db: aiosqlite.Connection,
        plugin_id: str,
        revision: int,
        actor: str,
        now: str,
        name: str,
        description: str,
        tools: tuple[str, ...],
        grants: tuple[dict[str, Any], ...],
    ) -> None:
        digest = (
            "sha256:" + hashlib.sha256(_canonical({"name": name, "description": description, "tools": tools, "skills": grants}).encode()).hexdigest()
        )
        await db.execute(
            "INSERT INTO team_capability_revision(capability_id,revision,name,description,content_hash,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
            (plugin_id, revision, name, description, digest, actor, now),
        )
        await db.executemany(
            "INSERT INTO team_capability_resource(capability_id,revision,kind,resource_ref,grant_json) VALUES (?,?,'tool',?,NULL)",
            [(plugin_id, revision, ref) for ref in tools],
        )
        await db.executemany(
            "INSERT INTO team_capability_resource(capability_id,revision,kind,resource_ref,grant_json) VALUES (?,?,'skill',?,?)",
            [(plugin_id, revision, str(grant["skill_ref"]["id"]), _canonical(grant)) for grant in grants],
        )

    @staticmethod
    async def _read_conn(db: aiosqlite.Connection, row: aiosqlite.Row) -> dict[str, Any]:
        plugin_id = str(row["id"])
        revision = int(row["current_revision"])
        async with db.execute("SELECT * FROM team_capability_revision WHERE capability_id=? AND revision=?", (plugin_id, revision)) as cursor:
            details = await cursor.fetchone()
        assert details is not None
        async with db.execute(
            "SELECT kind,resource_ref,grant_json FROM team_capability_resource WHERE capability_id=? AND revision=? ORDER BY kind,resource_ref",
            (plugin_id, revision),
        ) as cursor:
            resources = await cursor.fetchall()
        async with db.execute(
            """SELECT COUNT(*) FROM (
                 SELECT a.id FROM team_subagent a
                 JOIN team_subagent_capability_binding b
                   ON b.team_id=a.team_id AND b.subagent_id=a.id AND b.revision_id=a.live_revision_id
                 WHERE a.team_id=? AND b.capability_id=?
                 UNION
                 SELECT w.subagent_id FROM team_subagent_working_copy w, json_each(w.definition_json,'$.capability_ids') j
                 WHERE w.team_id=? AND j.value=?
               )""",
            (row["team_id"], plugin_id, row["team_id"], plugin_id),
        ) as cursor:
            agent_count = int((await cursor.fetchone())[0])
        return {
            "id": plugin_id,
            "team_id": str(row["team_id"]),
            "name": str(details["name"]),
            "description": str(details["description"]),
            "status": str(row["status"]),
            "revision": revision,
            "row_version": int(row["row_version"]),
            "content_hash": str(details["content_hash"]),
            "tool_refs": [str(item["resource_ref"]) for item in resources if item["kind"] == "tool"],
            "skill_grants": [json.loads(str(item["grant_json"])) for item in resources if item["kind"] == "skill"],
            "agent_count": agent_count,
        }


async def expand_plugins_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    definition: Mapping[str, Any],
    ignore_deleted: bool = False,
    allow_inactive: bool = False,
) -> dict[str, Any]:
    """Resolve latest selections in the caller's transaction, keeping direct refs.

    ``allow_inactive`` (run admission) expands a disabled Plugin's
    current revision instead of refusing the Agent; each run then removes its contribution
    (Capability Center tech design §8.3).
    """
    ids = selected_plugin_ids(definition)
    expanded = dict(definition)
    if not ids:
        return expanded
    raw_tools = definition.get("tool_refs", [])
    raw_grants = definition.get("skill_grants", [])
    if not isinstance(raw_tools, list) or not isinstance(raw_grants, list):
        raise TeamPluginError("capability_agent_resources_invalid")
    tools = list(raw_tools)
    grants = list(raw_grants)
    seen_grants = {str(grant.get("skill_ref", {}).get("id")): grant for grant in grants if isinstance(grant, dict)}
    resolutions: list[dict[str, Any]] = []
    for plugin_id in ids:
        async with db.execute("SELECT status,current_revision FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id)) as cursor:
            current = await cursor.fetchone()
        if current is None and ignore_deleted:
            continue
        if current is None or (current[0] != "active" and not allow_inactive):
            raise TeamPluginError("capability_unavailable")
        revision = int(current[1])
        async with db.execute(
            "SELECT kind,resource_ref,grant_json FROM team_capability_resource WHERE capability_id=? AND revision=?", (plugin_id, revision)
        ) as cursor:
            resources = await cursor.fetchall()
        if not resources:
            raise TeamPluginError("capability_empty")
        for kind, resource_ref, grant_json in resources:
            if kind == "tool":
                tools.append(str(resource_ref))
            else:
                grant = json.loads(str(grant_json))
                grant_id = str(grant["skill_ref"]["id"])
                previous = seen_grants.get(grant_id)
                if previous is None:
                    seen_grants[grant_id] = grant
                elif skill_grant_identity(previous) != skill_grant_identity(grant):
                    raise TeamPluginError("capability_skill_conflict")
                # Same Skill and permissions at another revision: keep the
                # earlier grant (direct grants first, then Plugins in selection
                # order). Compilation rebases it to the latest approved revision.
        resolutions.append({"id": plugin_id, "revision": revision})
    expanded["tool_refs"] = sorted(set(tools))
    expanded["skill_grants"] = list(seen_grants.values())
    if ignore_deleted:
        expanded["capability_ids"] = [item["id"] for item in resolutions]
    expanded["resolved_capabilities"] = resolutions
    return expanded
