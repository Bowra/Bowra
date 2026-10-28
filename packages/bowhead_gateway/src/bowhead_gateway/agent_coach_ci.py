"""Typed internal CI operations; consent comes from the Owner, never from the model.

The Owner consents through the explicit HTTP action, or in advance through the
``agent_coach_after_edit=check`` setting, which covers only a check on Coach's own
draft write in the same Turn, with the fixed budget below.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_comparison import AgentEvalComparisonStore, coach_comparison_intent_hash
from bowhead_core.agent_revision_store import AgentRevisionConflictError
from bowhead_core.coach_ci_delegation import STANDING_REQUEST_PREFIX, CoachCIDelegations, CoachCIError

from bowhead_gateway._entitlement_gate import assert_not_readonly
from bowhead_gateway.agent_coach_after_edit import KEY_AGENT_COACH_AFTER_EDIT, coach_after_edit

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources

# The standing check: one release comparison within the existing per-request limits.
CHECK_AFTER_EDIT_BUDGET = {"max_provider_calls": 200, "max_comparisons": 1, "duration_seconds": 1800}


@dataclass(frozen=True)
class AgentCoachCI:
    resources: AgentCoachResources

    @property
    def store(self) -> CoachCIDelegations:
        return CoachCIDelegations(str(self.resources.store.db_path))

    def owner(self, agent_id: str) -> dict:
        return {"team_id": self.resources.user.team_id, "agent_id": agent_id, "actor_user_id": self.resources.user.id}

    async def authorize(self, db: aiosqlite.Connection) -> None:
        await assert_not_readonly(self.resources.request)
        await self.resources.authoring._authorize_agent(db)

    async def get(self, target: dict) -> dict:
        await self.resources.evaluations.reauthorize(target["agent_id"])
        result = await self.store.get(**self.owner(target["agent_id"]), delegation_id=target["delegation_id"])
        if result["workspace_id"] != self.resources.workspace_id:
            raise CoachCIError("coach_ci_not_found")
        await self.resources.evaluations.reauthorize(target["agent_id"])
        return result

    async def request(self, *, target: dict, budget: dict, request_id: str, turn_id: str) -> dict:
        standing = await self._standing_check(target, turn_id)
        if standing:
            # One setting-authorized check per Coach write: asking again returns the same check, not another budget.
            request_id = f"{STANDING_REQUEST_PREFIX}{target['proposal_id']}"
        result = await self.store.request(
            team_id=self.resources.user.team_id,
            agent_id=target["agent_id"],
            owner_user_id=self.resources.user.id,
            workspace_id=self.resources.workspace_id,
            session_id=self.resources.session_id,
            turn_id=turn_id,
            proposal_id=target["proposal_id"],
            request_id=request_id,
            authorize=self.authorize,
            **(CHECK_AFTER_EDIT_BUDGET if standing else budget),
        )
        review = {"ok": True, "delegation": result, "next_action": "owner_review_ci_budget", "execution_requested": False}
        if not standing:
            return review
        if (result.get("estimate") or {}).get("may_exhaust_call_cap"):
            return {
                **review,
                "check_after_edit": "needs_owner_review",
                "message": "The check would likely need more than 200 model requests, so it was not started. Ask the user to review and run it.",
            }
        owner = self.owner(target["agent_id"])
        for stopped in await self.store.supersede(**owner, keep_delegation_id=result["delegation_id"]):
            await self._withdraw_unsubmitted_candidate(target["agent_id"], stopped)
        if result["state"] == "requested":
            await self.store.authorize(
                **owner, delegation_id=result["delegation_id"], expected_version=result["row_version"], authorize=self.authorize
            )
        try:
            started = await self.start_first(
                {"kind": "agent_ci_delegation", "agent_id": target["agent_id"], "delegation_id": result["delegation_id"]}
            )
        except AgentRevisionConflictError as exc:
            if exc.code != "agent_release_candidate_already_active":
                raise
            return {
                **review,
                "delegation": await self.get({"agent_id": target["agent_id"], "delegation_id": result["delegation_id"]}),
                "check_after_edit": "candidate_awaiting_review",
                "message": "Another candidate version of this Agent is still being checked or waiting for review, so this "
                "check can't start. Ask the user to finish or withdraw it on the Release tab, then run the check.",
            }
        return {**started, "next_action": "check_running", "check_after_edit": "started", "authorized_by": "owner_setting"}

    async def _withdraw_unsubmitted_candidate(self, agent_id: str, delegation_id: str) -> None:
        """A superseded check's candidate is withdrawn while still only prepared, never once submitted for review."""
        from bowhead_gateway.routes.agents import _revision_store

        stopped = await self.store.get(**self.owner(agent_id), delegation_id=delegation_id)
        prepared = stopped["preparation"]
        if prepared is None:
            return
        async with open_shared_ctx(self.store.db_path) as db:
            cursor = await db.execute(
                "SELECT state,row_version FROM team_subagent_release_candidate WHERE team_id=? AND id=?",
                (self.resources.user.team_id, prepared["candidate_id"]),
            )
            row = await cursor.fetchone()
        if row is None or row[0] != "prepared":
            return
        revisions = await _revision_store(self.resources.request, user=self.resources.user, store=self.resources.store)
        await revisions.withdraw_candidate(
            team_id=self.resources.user.team_id,
            candidate_id=prepared["candidate_id"],
            actor_user_id=self.resources.user.id,
            expected_row_version=int(row[1]),
            expected_candidate_hash=prepared["candidate_hash"],
        )

    async def _standing_check(self, target: dict, turn_id: str) -> bool:
        """The setting applies only to a check on the change Coach wrote into the draft in this Turn."""
        from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore

        settings = getattr(self.resources.request.app.state, "user_settings_store", None)
        value = await settings.get(self.resources.user.id, KEY_AGENT_COACH_AFTER_EDIT) if settings is not None else None
        if coach_after_edit(value) != "check":
            return False
        proposal = await AgentChangeProposalStore(self.store.db_path).get(**self.owner(target["agent_id"]), proposal_id=target["proposal_id"])
        return (
            proposal.source_type == "coach"
            and proposal.status == "accepted"
            and proposal.provenance.get("source_turn_id") == turn_id
            and proposal.provenance.get("workspace_id") == self.resources.workspace_id
        )

    async def prepare(self, target: dict) -> dict:
        from bowhead_gateway.routes.agents import _revision_store, record_working_copy_semantic_evidence

        await assert_not_readonly(self.resources.request)
        current = await self.get(target)
        if current["preparation"] is not None:
            return {"ok": True, "delegation": current, "replayed": True, "execution_requested": False}
        context = current["context"]
        await record_working_copy_semantic_evidence(
            self.resources.request,
            user=self.resources.user,
            store=self.resources.store,
            agent_id=target["agent_id"],
            working_copy_id=context["working_copy_id"],
            working_copy_row_version=context["working_copy_version"],
        )
        revisions = await _revision_store(self.resources.request, user=self.resources.user, store=self.resources.store)
        await revisions.prepare_working_copy(
            **self.owner(target["agent_id"]),
            expected_working_copy_id=context["working_copy_id"],
            expected_working_copy_version=context["working_copy_version"],
            expected_eval_policy_revision_id=context["policy"]["id"] if context["policy"] else None,
            workspace_id=current["workspace_id"],
            idempotency_key=f"coach-ci:prepare:{current['delegation_id']}",
            coach_delegation_id=current["delegation_id"],
        )
        return {"ok": True, "delegation": await self.get(target), "replayed": False, "execution_requested": False}

    async def start(
        self,
        *,
        target: dict,
        candidate_row_version: int,
        control_row_version: int,
        run_kind: str,
        selection: list[str],
        request_id: str,
        turn_id: str,
    ) -> dict:
        key = "coach-ci:" + hashlib.sha256(f"{turn_id}:{request_id}".encode()).hexdigest()
        return await self._start(
            target=target,
            candidate_row_version=candidate_row_version,
            control_row_version=control_row_version,
            run_kind=run_kind,
            selection=selection,
            key=key,
        )

    async def _start(
        self, *, target: dict, candidate_row_version: int, control_row_version: int, run_kind: str, selection: list[str], key: str
    ) -> dict:
        from bowhead_gateway.routes.agents import _comparison_dict, _comparison_model_hashes, _require_eval_runtime, _schedule_comparison

        await assert_not_readonly(self.resources.request)
        current = await self.get(target)
        prepared = current["preparation"]
        if prepared is None:
            raise CoachCIError("coach_ci_preparation_required")
        if (run_kind == "release" and selection) or (run_kind == "diagnostic" and not selection):
            raise CoachCIError("coach_ci_case_selection_invalid")
        if run_kind not in {"release", "diagnostic"}:
            raise CoachCIError("coach_ci_run_kind_invalid")
        if run_kind == "diagnostic":
            await self._refuse_held_out(selection)
        comparison = AgentEvalComparisonStore(self.store.db_path)
        identity = dict(
            **self.owner(target["agent_id"]),
            candidate_id=prepared["candidate_id"],
            candidate_hash=prepared["candidate_hash"],
            candidate_row_version=candidate_row_version,
            plan_id=prepared["plan_id"],
            plan_hash=prepared["plan_hash"],
        )
        intent = coach_comparison_intent_hash(
            **identity, delegation_id=current["delegation_id"], control_row_version=control_row_version, run_kind=run_kind, selection=tuple(selection)
        )
        old = await comparison.replay_coach(
            **self.owner(target["agent_id"]), delegation_id=current["delegation_id"], idempotency_key=key, client_intent_hash=intent
        )
        if old is not None:
            # The durable worker already recovers queued jobs. This read cannot
            # restart a terminal/uncertain operation or require a replacement route.
            return {"ok": True, "execution": _comparison_dict(old), "delegation": current, "replayed": True, "execution_requested": False}
        runtime = _require_eval_runtime(self.resources.request)
        model_hashes, judge_hash = await _comparison_model_hashes(
            runtime=runtime,
            db_path=self.store.db_path,
            team_id=self.resources.user.team_id,
            candidate_id=prepared["candidate_id"],
            plan_id=prepared["plan_id"],
        )
        args = {
            **identity,
            "expected_control_row_version": control_row_version,
            "model_config_hashes": model_hashes,
            "judge_model_fingerprint": judge_hash,
            "idempotency_key": key,
            "coach_delegation_id": current["delegation_id"],
        }
        result = (
            await comparison.start_release(**args)
            if run_kind == "release"
            else await comparison.start_diagnostic(**args, selected_case_revision_ids=tuple(selection))
        )
        await _schedule_comparison(runtime, result)
        return {
            "ok": True,
            "execution": _comparison_dict(await comparison.get(result.id)),
            "delegation": await self.get(target),
            "replayed": False,
            "execution_requested": True,
        }

    async def _refuse_held_out(self, selection: list[str]) -> None:
        """Coach can't pick a held-out case for a diagnostic run (alignment loop A6); the release check runs all."""
        async with open_shared_ctx(self.store.db_path) as db:
            cursor = await db.execute(
                """SELECT 1 FROM agent_eval_case_revision WHERE id IN (SELECT value FROM json_each(?))
                   AND json_extract(expected_contract_json,'$.held_out')=1 LIMIT 1""",
                (json.dumps(selection),),
            )
            if await cursor.fetchone() is not None:
                raise CoachCIError("agent_eval_case_held_out")

    async def with_held_out_counts(self, delegation: dict) -> dict:
        """Each execution's held-out results, as counts only."""
        from bowhead_core.agent_coach_evidence import _held_out_results

        async with open_shared_ctx(self.store.db_path) as db:
            db.row_factory = aiosqlite.Row
            for execution in delegation.get("executions") or []:
                cursor = await db.execute(
                    "SELECT run_id FROM agent_eval_comparison_run WHERE execution_id=? AND subject_side='candidate'", (execution["id"],)
                )
                run_ids = [str(row[0]) for row in await cursor.fetchall()]
                execution["held_out"] = await _held_out_results(db, run_ids=run_ids) if run_ids else {"cases": 0, "attempts": 0, "passed_attempts": 0}
        return delegation

    async def start_first(self, target: dict) -> dict:
        """Continue the single explicit Owner start action with a fixed identity."""
        result = await self.prepare(target)
        prepared = result["delegation"]["preparation"]
        return await self._start(
            target=target,
            candidate_row_version=prepared["candidate_row_version"],
            control_row_version=prepared["control_row_version"],
            run_kind="release",
            selection=[],
            key=f"coach-ci:first:{target['delegation_id']}",
        )

    async def call(self, operation: str, args: dict, *, turn_id: str) -> dict:
        if operation == "agent_ci_request":
            return await self.request(**args, turn_id=turn_id)
        if operation == "agent_ci_get":
            return {"ok": True, "delegation": await self.with_held_out_counts(await self.get(args["target"])), "execution_requested": False}
        if operation == "agent_ci_prepare":
            return await self.prepare(args["target"])
        if operation == "agent_ci_start":
            return await self.start(**args, turn_id=turn_id)
        if operation == "agent_ci_cancel":
            await self.get(args["target"])
            async with open_shared_ctx(self.store.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN")
                await self.authorize(db)
            result = await self.store.revoke(**self.owner(args["target"]["agent_id"]), delegation_id=args["target"]["delegation_id"])
            return {"ok": True, "delegation": result, "execution_requested": False}
        raise ValueError("Unsupported Coach CI operation")
