"""Typed Coach target resolution and initial drafts over existing domain stores."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore, AgentProposalBaseline, _value_at, apply_operations
from bowhead_core.agent_revision_store import AgentRevisionForbiddenError
from bowhead_core.chat_reference_scope import read_reference_access_context_in_connection
from bowhead_core.subagent_authoring_rollout import resolve_subagent_authoring_capabilities
from bowhead_core.subagent_draft_store import SubagentDraftIdempotencyConflictError

from bowhead_gateway._entitlement_gate import assert_not_readonly
from bowhead_gateway.agent_coach_draft_adapter import AgentCoachDraftAdapter
from bowhead_gateway.agent_coach_evaluations import validate_product_case_suggestions
from bowhead_gateway.routes.team_subagent_drafts import _draft_store
from bowhead_gateway.subagent_draft_chat_editor import SubagentDraftChatEditor

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources


@dataclass(frozen=True)
class AgentCoachAuthoring:
    resources: AgentCoachResources

    async def _authorize_agent(self, connection: aiosqlite.Connection) -> None:
        resource = self.resources
        await read_reference_access_context_in_connection(connection, scope=resource.scope, access_store=AccessControlStore(resource.store.db_path))

    async def _authorize(self, connection: aiosqlite.Connection) -> None:
        resource = self.resources
        capabilities = resolve_subagent_authoring_capabilities(resource.user.team_id)
        if not capabilities.unified_draft or not capabilities.draft_chat_edit:
            raise ValueError("agent_coach_authoring_unavailable")
        await read_reference_access_context_in_connection(connection, scope=resource.scope, access_store=AccessControlStore(resource.store.db_path))

    async def start_draft(self, *, purpose: str, locale: str, request_id: str, turn_id: str) -> dict[str, Any]:
        resource = self.resources
        await resource._access()
        await assert_not_readonly(resource.request)
        store = await _draft_store(resource.request)
        try:
            draft = await store.create_manual_draft(
                team_id=resource.user.team_id,
                owner_user_id=resource.user.id,
                workspace_id=resource.workspace_id,
                purpose=purpose,
                locale=locale,
                idempotency_key=f"coach-start:{turn_id}:{request_id}",
                source_mode="turn",
                source_turn_id=turn_id,
                authorize=self._authorize,
            )
        except SubagentDraftIdempotencyConflictError:
            return {"ok": False, "error": "idempotency_key_reused"}
        await resource._access()
        return {
            "ok": True,
            "effect": "draft_saved",
            "agent_created": False,
            "target": {"kind": "agent_draft", "id": draft.id, "version": draft.row_version},
            "workspace_id": draft.workspace_id,
            "status": draft.status,
            "expires_at": draft.expires_at,
            "purpose": draft.purpose_text,
            "message": "Use the exact target for inspection and pending suggestions. Review and private creation remain separate user actions.",
        }

    async def draft_adapter(self, target: dict[str, Any]) -> AgentCoachDraftAdapter:
        resource = self.resources
        await resource._access()
        if target.get("kind") != "agent_draft":
            raise ValueError("agent_coach_target_unsupported")
        adapter = AgentCoachDraftAdapter(
            SubagentDraftChatEditor(
                resource.request,
                resource.user,
                target["id"],
                target["version"],
                workspace_id=resource.workspace_id,
            )
        )
        # Do not mutate the active Canvas selection or acquire another object's
        # baseline implicitly. Read/propose rechecks this exact target itself.
        await adapter._current(check_version=False)
        return adapter

    @staticmethod
    def _baseline(target: dict[str, Any]) -> AgentProposalBaseline:
        return AgentProposalBaseline(target["working_copy_id"], target["version"], target["base_release_id"])

    async def open_working_copy(self, *, agent_id: str, expected_live_release_id: str) -> dict:
        from bowhead_gateway.routes.agents import _revision_store

        resource = self.resources

        async def authorize(db: aiosqlite.Connection) -> None:
            await self._authorize_agent(db)
            await assert_not_readonly(resource.request)

        revisions = await _revision_store(resource.request, user=resource.user, store=resource.store)
        working, created = await revisions.open_working_copy(
            team_id=resource.user.team_id,
            agent_id=agent_id,
            actor_user_id=resource.user.id,
            expected_live_release_id=expected_live_release_id,
            authorize=authorize,
        )
        await resource._access()
        return {
            "ok": True,
            "created_baseline": created,
            "existing_edits_preserved": True,
            "executed": False,
            "released": False,
            "target": {
                "kind": "agent_working_copy",
                "agent_id": agent_id,
                "working_copy_id": working.id,
                "version": working.row_version,
                "base_release_id": working.base_release_id,
            },
        }

    async def validate_change(
        self, *, target: dict[str, Any], operations: list[dict], atomic_groups: list[dict], suggested_cases: list[dict]
    ) -> dict:
        resource = self.resources
        await resource._access()
        validate_product_case_suggestions(suggested_cases)
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.coach_validation import authoring_validation_report, candidate_digest
        from bowhead_core.team_plugin_store import TeamPluginError, expand_plugins_conn

        from bowhead_gateway.agent_authoring_dependencies import compile_authoring_resources
        from bowhead_gateway.builtin_plugin_fields import expand_selected_builtin_plugins

        store = AgentChangeProposalStore(resource.store.db_path)
        args = {
            "team_id": resource.user.team_id,
            "agent_id": target["agent_id"],
            "actor_user_id": resource.user.id,
            "expected_baseline": self._baseline(target),
            "operations": operations,
            "atomic_groups": atomic_groups,
            "suggested_cases": suggested_cases,
            "authorize": self._authorize_agent,
            "include_candidate": True,
        }
        result = await store.preview(**args)
        candidate = result.pop("candidate_document")
        try:
            async with open_shared_ctx(str(resource.store.db_path)) as db:
                expanded = expand_selected_builtin_plugins(
                    await expand_plugins_conn(db, team_id=resource.user.team_id, definition=candidate["definition"])
                )
            dependency = await compile_authoring_resources(resource, expanded, audience_mode=candidate["audience"]["effective_mode"])
            async with open_shared_ctx(str(resource.store.db_path)) as db:
                current = expand_selected_builtin_plugins(
                    await expand_plugins_conn(db, team_id=resource.user.team_id, definition=candidate["definition"])
                )
            if current != expanded:
                raise ValueError("agent_dependencies_changed")
            dependency["fingerprint"] = candidate_digest(
                {"compiled": dependency["fingerprint"], "plugins": expanded.get("resolved_capabilities", [])}
            )
        except (TeamPluginError, ValueError) as exc:
            from bowhead_core.system_plugins import BuiltinPluginSelectionError

            # An invalid built-in Plugin selection is a blocked dependency result, not an error.
            if not isinstance(exc, (TeamPluginError, BuiltinPluginSelectionError)) and str(exc) != "plugin_dependencies_unavailable":
                raise
            dependency = {
                "status": "blocked",
                "reason_codes": ["agent_dependencies_unavailable_or_changed"],
                "fingerprint": None,
                # Which Tool or Skill is unavailable, so Coach can name it instead of retrying.
                **({"details": dict(exc.details)} if getattr(exc, "details", None) else {}),
            }
        after = await store.preview(**args)
        if candidate_digest(after["candidate_document"]) != candidate_digest(candidate):
            raise ValueError("agent_working_copy_version_conflict")
        await resource._access()
        return {
            "ok": True,
            "target": target,
            **result,
            "safety_check": "not_run",
            "evaluation": "not_run",
            "dependency_validation": {key: value for key, value in dependency.items() if key not in {"effective_tool_refs", "skill_grants"}},
            "validation_report": authoring_validation_report(
                target=target,
                candidate_hash=candidate_digest(candidate),
                structure="passed",
                dependencies="passed" if dependency["status"] == "ready" else "blocked",
                dependency_fingerprint=dependency["fingerprint"],
                supports_candidate_evaluation=True,
            ),
        }

    async def propose_change(
        self,
        *,
        target: dict[str, Any],
        operations: list[dict],
        atomic_groups: list[dict],
        suggested_cases: list[dict],
        rationale: str,
        request_id: str,
        turn_id: str,
        resource_context: dict | None = None,
    ) -> dict:
        resource = self.resources
        await resource._access()
        await assert_not_readonly(resource.request)
        validate_product_case_suggestions(suggested_cases)

        async def validate_candidate(candidate: dict) -> None:
            from bowhead_gateway.agent_coach_resource_binding import validate_resource_binding

            await validate_resource_binding(resource, target=target, definition=candidate["definition"], context=resource_context)

        try:
            missing = await self._missing_skill_dependencies(target["agent_id"], operations)
            if missing:
                return {
                    "ok": False,
                    "error": "agent_skill_dependency_missing",
                    "missing_skills": missing,
                    "written": False,
                    "message": "Nothing was written: a Skill this change adds needs other Skills the Agent doesn't have. "
                    "Add the missing Skills in the same change, or tell the user which ones are needed.",
                }
            from bowhead_gateway.routes.agents import _revision_store

            revisions = await _revision_store(resource.request, user=resource.user, store=resource.store)
            proposal, version, written = await AgentChangeProposalStore(resource.store.db_path, revision_store=revisions).create_applied(
                team_id=resource.user.team_id,
                agent_id=target["agent_id"],
                effective_owner_user_id=resource.user.id,
                source_ref=f"coach:{turn_id}:{request_id}",
                operations=operations,
                atomic_groups=atomic_groups,
                suggested_cases=suggested_cases,
                rationale=rationale,
                expected_baseline=self._baseline(target),
                authorize=self._authorize_agent,
                provenance={
                    "kind": "coach_authored_patch",
                    "source_turn_id": turn_id,
                    "workspace_id": resource.workspace_id,
                    "target": target,
                    **({"resource_context": resource_context} if resource_context is not None else {}),
                },
                validate_candidate=validate_candidate if resource_context is not None else None,
            )
        except AgentRevisionForbiddenError as exc:
            if exc.code != "agent_owner_required":
                raise
            return {
                "ok": False,
                "error": "agent_edit_permission_required",
                "written": False,
                "message": "You can't edit this Agent's draft, so nothing was written. Give the change to the user as a suggestion in Chat.",
            }
        await resource._access()
        released = await self._released_sequence(target["agent_id"])
        based_on = f"v{released}" if released is not None else None
        release_as = f"v{released + 1}" if released is not None else None
        draft = f"The change is in the draft (based on {based_on}); {based_on} is unchanged." if based_on else "The change is in the draft."
        release = f" To make it available, the user presses Release as {release_as} on the Release tab." if release_as else ""
        return {
            **self._proposal_receipt(proposal),
            "effect": "draft_updated",
            "applied_by_this_call": written,
            "proposal_id": proposal.id,
            "group_ids": [str(group["id"]) for group in proposal.atomic_groups],
            # An internal edit counter for the next write's target, never a version number for the user.
            "working_copy_version": version,
            "target": {**target, "version": version},
            "released_version": based_on,
            "release_as": release_as,
            "test_change": {"kind": "accepted_agent_proposal", "agent_id": target["agent_id"], "proposal_id": proposal.id},
            "message": f"{draft} Nothing is released. The user can undo each changed field in Configure.{release} "
            "Use the returned target for any further edit in this Turn. To test the change, call agent_ci_request once with "
            "test_change as its target after your last write. Never show working_copy_version, IDs or field names to the user.",
        }

    async def _released_sequence(self, agent_id: str) -> int | None:
        """The released version's number, so Coach can say "the draft, based on vN" (beginner experience §2)."""
        from bowhead_core._db import open_shared_ctx

        async with open_shared_ctx(str(self.resources.store.db_path)) as db:
            cursor = await db.execute(
                """SELECT rel.release_sequence FROM team_subagent agent
                   JOIN team_subagent_release rel ON rel.team_id=agent.team_id AND rel.id=agent.live_release_id
                   WHERE agent.team_id=? AND agent.id=?""",
                (self.resources.user.team_id, agent_id),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    async def _missing_skill_dependencies(self, agent_id: str, operations: list[dict]) -> list[str]:
        """The same rule as a manual save: a newly added Skill brings the Skills it requires."""
        from bowhead_core.agent_change_proposal_store import apply_operations
        from bowhead_core.agent_revision_store import AgentPublicationBlockedError

        from bowhead_gateway.routes.agents import _read_store, _require_new_skill_dependency_closure

        resource = self.resources
        try:
            detail = await _read_store(resource.store).working_copy(team_id=resource.user.team_id, agent_id=agent_id, actor_user_id=resource.user.id)
        except AgentRevisionForbiddenError:
            # Not this user's Agent: the write below answers with the suggestion-only result.
            return []
        current = detail["working_copy"] or detail["live"]
        document = {key: dict(current.get(key) or {}) for key in ("definition", "listing", "audience")}
        try:
            after = apply_operations(document, operations)
        except Exception:  # noqa: BLE001 - an invalid patch is rejected with its own error by the write below
            return []
        try:
            await _require_new_skill_dependency_closure(
                resource.request, user=resource.user, store=resource.store, agent_id=agent_id, definition=after["definition"]
            )
        except AgentPublicationBlockedError as exc:
            if exc.code != "agent_skill_dependency_missing":
                raise
            return list(exc.reason_codes)
        return []

    async def get_change(self, *, agent_id: str, proposal_id: str) -> dict:
        resource = self.resources
        await resource._access()
        proposal = await AgentChangeProposalStore(resource.store.db_path).get(
            team_id=resource.user.team_id,
            agent_id=agent_id,
            proposal_id=proposal_id,
            actor_user_id=resource.user.id,
        )
        await resource._access()
        return self._proposal_receipt(proposal)

    @staticmethod
    def _proposal_receipt(proposal) -> dict:
        result = apply_operations(proposal.base_document, proposal.patch)
        changed = sorted(
            {item["path"] for item in proposal.patch if _value_at(proposal.base_document, item["path"]) != _value_at(result, item["path"])}
        )
        in_draft = proposal.source_type == "coach" and proposal.status == "accepted"
        return {
            "ok": True,
            "effect": "draft_updated" if in_draft else "proposal_available",
            "applied_by_this_call": False,
            "proposal": asdict(proposal),
            "changed_paths": changed,
            "released": False,
            "view": {"kind": "agent", "agent_id": proposal.agent_id, "proposal_id": proposal.id},
            "message": (
                "This change was written into the Agent's draft; the user can undo each changed field. No evaluation or release was started."
                if in_draft
                else "Read the current proposal status. Owner review and selected-group acceptance remain separate actions; no evaluation or release was started."
            ),
        }
