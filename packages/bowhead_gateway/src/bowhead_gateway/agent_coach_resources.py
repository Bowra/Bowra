"""Read-only Coach discovery over current domain catalogs, without a draft."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.resource_access import builtin_capability_ids_for_skill, resource_grant_targets_for_tool
from bowhead_core.skill_read_resolution import build_reference_skill_sources
from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogService
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.team_tool_loader import load_team_tool_entries
from bowhead_core.user_store import User, UserStore
from fastapi import Request

from bowhead_gateway._entitlement_gate import is_feature_licensed_request

# Upper bound for one exact Skill file returned inline to the Coach. It bounds the
# tool result (prompt and memory), not the package: ordinary skill_read allows
# 256 KiB per text file. 64 KiB (about 16-20k tokens) leaves headroom over the
# Coach's own built-in authoring Skill, which outgrew the original 32 KiB.
MAX_EXACT_SKILL_FILE_BYTES = 64 * 1024


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _runnable_package(files: dict[str, bytes]) -> bool:
    """A ``kind: tool`` Skill package with an entry script in any supported language."""
    from bowhead_core.skill_upload import _split_frontmatter
    from bowhead_skill.script_langs import all_entry_extensions

    try:
        frontmatter, _ = _split_frontmatter(files.get("SKILL.md", b"").decode("utf-8"))
    except UnicodeDecodeError:
        return False
    extensions = all_entry_extensions()
    return frontmatter.get("kind") == "tool" and any(path.startswith("scripts/") and Path(path).suffix in extensions for path in files)


@dataclass(frozen=True)
class AuthoringResourceCatalog:
    request: Request
    user: User
    workspace_id: str
    session_id: str

    @property
    def caller(self) -> tuple[str, str, str]:
        return self.user.id, self.user.team_id, self.workspace_id

    @property
    def store(self) -> UserStore:
        return self.request.app.state.user_store

    @property
    def scope(self) -> ReferenceRequestScope:
        return ReferenceRequestScope(
            team_id=self.user.team_id, user_id=self.user.id, workspace_id=self.workspace_id, session_id=self.session_id or None
        )

    async def _access(self) -> AccessContext:
        async with open_shared_ctx(str(self.store.db_path)) as db:
            await db.execute("BEGIN")
            return await read_reference_access_context_in_connection(db, scope=self.scope, access_store=AccessControlStore(self.store.db_path))

    async def inspect(self) -> dict[str, Any]:
        await self._access()
        return {
            "ok": True,
            "target_kind": "resource_discovery",
            "supported_actions": ["resource_search", "resource_get"],
            "writable": False,
            "writable_scope": "current_editor_only",
            "message": "No current editor is bound. This is not an authorization decision for other authoring Tools. Discover and read an exact owned Agent reference to obtain its working-copy target; agent_change_* rechecks that target independently. New drafts use agent_draft_start.",
        }

    async def _allowed(self, actor: AccessContext, targets: list[tuple[str, str, str]], *, owned_team_skill: bool = False) -> bool:
        access = AccessControlStore(self.store.db_path)
        for kind, identity, permission in targets:
            decision = await access.explain_decision(actor, kind, identity, permission)
            exempt = kind == "team_skill" and owned_team_skill and decision.get("reason") == "not_in_allowlist"
            if not decision.get("allowed") and not exempt:
                return False
        return True

    async def _tool_schemas(self) -> dict[str, dict]:
        registry = getattr(getattr(self.request.app.state, "orchestrator", None), "tool_registry", None)
        schemas = {schema.name: schema.input_schema for schema in registry.to_schemas()} if registry is not None else {}
        entries, _ = await load_team_tool_entries(self.user.team_id, self.store)
        schemas.update({entry.advertised_name: entry.input_schema for entry in entries})
        return schemas

    async def _items(self, kind: str) -> tuple[list[dict], AccessContext]:
        actor = await self._access()
        items = []
        if kind == "tool":
            catalog = await SubagentAuthoringCatalogService().build(
                store=self.store,
                team_id=self.user.team_id,
                actor_user_id=self.user.id,
                actor_active=True,
                mcp_entitled=await is_feature_licensed_request(self.request, "mcp"),
            )
            schemas = await self._tool_schemas()
            for tool in (*catalog.native_tools, *catalog.team_tools):
                if not tool.discoverable:
                    continue
                row = tool.to_public_dict()
                identity = row["stable_ref"]
                if not await self._allowed(actor, resource_grant_targets_for_tool(identity, include_argument_targets=False)):
                    continue
                items.append(
                    {
                        "reference": {
                            "kind": kind,
                            "id": identity,
                            "revision": _digest({"source": row["dependency_revision"], "schema": schemas.get(identity)}),
                        },
                        "name": row["display_name"],
                        "description": row["description"],
                        "source_kind": row["source_kind"],
                        "risk": row["risk"],
                        "can_select": row["selectable"],
                        "restriction": row["locked_reason"],
                    }
                )
        elif kind == "skill":
            from bowhead_gateway.agent_optional_skill_catalog import optional_skill_catalog

            offset = 0
            while True:
                page = await optional_skill_catalog(
                    store=self.store,
                    user=self.user,
                    agent_id=None,
                    query="",
                    offset=offset,
                    limit=50,
                    include_source_identity=True,
                    baseline_selection=True,
                )
                for row in page["items"]:
                    resource = row["resource"]
                    targets = [("team_skill", resource["id"], "use")] if resource["source_kind"] == "team" else [("skill", row["name"], "use")]
                    targets.extend(("builtin_capability", identity, "use") for identity in builtin_capability_ids_for_skill(row["name"]))
                    if not await self._allowed(actor, targets, owned_team_skill=row["owned_team_skill"]):
                        continue
                    items.append(
                        {
                            "reference": {
                                "kind": "skill",
                                "id": resource["id"],
                                "source_kind": resource["source_kind"],
                                "revision": resource["version"],
                            },
                            "name": row["label"],
                            "skill_name": row["name"],
                            "description": row["description"],
                            "allowed_audiences": row["allowed_audiences"],
                            "can_select": True,
                        }
                    )
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
        elif kind == "capability":
            for row in await TeamPluginStore(self.store.db_path).list(self.user.team_id):
                if row["status"] != "active":
                    continue
                items.append(
                    {
                        "reference": {"kind": kind, "id": row["id"], "revision": str(row["revision"])},
                        "name": row["name"],
                        "description": row["description"],
                        "can_select": None,
                        "restriction": "dependency_validation_required",
                        "can_author": actor.is_team_admin,
                        "tool_count": len(row["tool_refs"]),
                        "skill_count": len(row["skill_grants"]),
                    }
                )
        else:
            from bowhead_core.agent_release_read_store import AgentReleaseReadStore
            from bowhead_core.agent_revision_store import AgentReleaseCutoverRequiredError

            owned = await AgentReleaseReadStore(self.store.db_path).owned_agent_ids(team_id=self.user.team_id, actor_user_id=self.user.id)
            for row in await self.store.list_team_subagent_summaries(self.user.team_id):
                if row.id not in owned:
                    continue
                try:
                    working = await AgentReleaseReadStore(self.store.db_path).working_copy(
                        team_id=self.user.team_id, agent_id=row.id, actor_user_id=self.user.id
                    )
                except AgentReleaseCutoverRequiredError:
                    working = None
                items.append(
                    {
                        "reference": {"kind": "agent", "id": row.id, "revision": _digest({"version": row.row_version, "working": working})},
                        "name": row.display_name or row.name,
                        "description": row.description,
                        "status": row.status,
                        "can_select": None,
                        "can_author": working is not None and working["aggregate"]["lifecycle_state"] == "active",
                        "restriction": "baseline_import_required" if working is None else None,
                    }
                )
        if actor != await self._access():
            raise ValueError("resource_catalog_changed")
        for item in items:
            item.update(can_discover=True, can_read=True, coach_can_execute=False)
        return sorted(items, key=lambda item: (item["reference"]["id"], item["reference"].get("source_kind", ""))), actor

    async def search(self, *, kind: str, query: str, offset: int, limit: int, expected_fingerprint: str | None = None) -> dict[str, Any]:
        items, actor = await self._items(kind)
        fingerprint = _digest({"items": items, "scope": actor.as_dict()})
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            return {"ok": False, "error": "resource_catalog_changed"}
        needle = query.strip().casefold()
        items = [item for item in items if needle in f"{item['reference']['id']} {item['name']} {item['description']}".casefold()]
        return {
            "ok": True,
            "purpose": "author_target_agent",
            "catalog_fingerprint": fingerprint,
            "items": items[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(items) else None,
            "complete": offset + limit >= len(items),
        }

    async def get(self, *, reference: dict, file: str | None = None) -> dict[str, Any]:
        items, actor = await self._items(reference["kind"])
        item = next((item for item in items if item["reference"] == {key: value for key, value in reference.items() if value is not None}), None)
        if item is None:
            return {"ok": False, "error": "resource_unavailable_or_changed"}
        kind, identity = reference["kind"], reference["id"]
        detail: dict[str, Any] = {
            **item,
            "operation_effect": "read_only",
            "resource_effect": item.get("risk"),
            "validation_evidence": None,
            "execution_cost": None,
        }
        if kind == "tool":
            detail["input_schema"] = (await self._tool_schemas()).get(identity)
            detail["output_schema"] = None
        elif kind == "skill":
            scope = self.scope
            source = build_reference_skill_sources(
                self.store.db_path, scope=scope, builtin_index=lambda: getattr(self.request.app.state, "builtin_skill_reference_metadata", None)
            )
            resource = SkillResourceRef(kind="skill", source_kind=reference["source_kind"], id=identity, version=reference["revision"])
            prepared = await source.prepare_runtime_package(resource, scope=scope)
            files = prepared.material.files()
            selected = file or "SKILL.md"
            if selected not in files or len(files[selected]) > MAX_EXACT_SKILL_FILE_BYTES:
                return {"ok": False, "error": "resource_file_unavailable_or_too_large"}
            detail["exact_read_grant"] = {
                "skill_ref": {"kind": reference["source_kind"], "id": identity, "name": item["skill_name"]},
                "revision": reference["revision"],
                # Same rule as the authoring catalog: a Skill with scripts can also run them (agent-skill-gates D1).
                "permissions": ["invoke", "read"] if _runnable_package(files) else ["read"],
                "distribution": "reference",
            }
            detail.update(files=sorted(files), file=selected, content=files[selected].decode("utf-8"), content_role="untrusted_resource_data")
            await source.revalidate(prepared.proof)
        elif kind == "capability":
            bundle = await TeamPluginStore(self.store.db_path).get(self.user.team_id, identity)
            if bundle is None or str(bundle["revision"]) != reference["revision"]:
                return {"ok": False, "error": "resource_unavailable_or_changed"}
            tools, _ = await self._items("tool")
            skills, _ = await self._items("skill")
            visible_tools = {item["reference"]["id"] for item in tools}
            visible_skills = {(item["reference"]["id"], item["reference"]["revision"]) for item in skills}
            if not set(bundle["tool_refs"]) <= visible_tools or any(
                (grant["skill_ref"]["id"], grant["revision"]) not in visible_skills for grant in bundle["skill_grants"]
            ):
                return {"ok": False, "error": "resource_dependencies_unavailable"}
            detail.update(tool_refs=bundle["tool_refs"], skill_grants=bundle["skill_grants"], affected_agent_count=bundle["agent_count"])
        else:
            from bowhead_core.agent_release_read_store import AgentReleaseReadStore

            detail["working_copy"] = (
                None
                if item.get("restriction") == "baseline_import_required"
                else await AgentReleaseReadStore(self.store.db_path).working_copy(
                    team_id=self.user.team_id, agent_id=identity, actor_user_id=self.user.id
                )
            )
            working = (detail["working_copy"] or {}).get("working_copy")
            detail["authoring_target"] = (
                {
                    "kind": "agent_working_copy",
                    "agent_id": identity,
                    "working_copy_id": working["id"],
                    "version": working["row_version"],
                    "base_release_id": None if working["base_release_id"] in {None, "", "None"} else working["base_release_id"],
                }
                if working is not None
                else None
            )
        refreshed, current_actor = await self._items(kind)
        if actor != current_actor or item not in refreshed:
            return {"ok": False, "error": "resource_unavailable_or_changed"}
        return {"ok": True, "resource": detail}


@dataclass(frozen=True)
class AgentCoachResources(AuthoringResourceCatalog):
    """Coach admission around the shared caller-safe authoring catalog."""

    canvas_target: object | None = None

    async def inspect(self) -> dict[str, Any]:
        result = await super().inspect()
        if self.canvas_target is not None:
            from bowhead_core.coach_canvas_target import AdmittedCoachCanvasTarget

            if type(self.canvas_target) is not AdmittedCoachCanvasTarget:
                raise ValueError("coach_canvas_target_not_admitted")
            result["selected_editor"] = await self.canvas_target.recheck()
        return result

    @property
    def editors(self):
        from bowhead_gateway.agent_coach_editors import AgentCoachEditors

        return AgentCoachEditors(self)

    @property
    def operations(self):
        from bowhead_gateway.agent_coach_operations import AgentCoachOperations

        return AgentCoachOperations(self)

    @property
    def evaluations(self):
        from bowhead_gateway.agent_coach_evaluations import AgentCoachEvaluations

        return AgentCoachEvaluations(self)

    @property
    def alignment(self):
        from bowhead_gateway.agent_coach_alignment import AgentCoachAlignment

        return AgentCoachAlignment(self)

    @property
    def ci(self):
        from bowhead_gateway.agent_coach_ci import AgentCoachCI

        return AgentCoachCI(self)

    @property
    def authoring(self):
        from bowhead_gateway.agent_coach_authoring import AgentCoachAuthoring

        return AgentCoachAuthoring(self)

    @property
    def tool_handoff(self):
        from bowhead_gateway.agent_coach_tool_handoff import AgentCoachToolHandoff

        return AgentCoachToolHandoff(self)

    @property
    def plugins(self):
        from bowhead_gateway.agent_coach_plugins import AgentCoachPlugins

        return AgentCoachPlugins(self)

    @property
    def skills(self):
        from bowhead_gateway.agent_coach_skills import AgentCoachSkills

        return AgentCoachSkills(self)
