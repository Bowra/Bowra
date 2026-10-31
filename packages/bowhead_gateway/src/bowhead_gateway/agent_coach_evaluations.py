"""Owner-scoped evaluation assets and independent reviewable case proposals."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import (
    AgentChangeProposalError,
    AgentChangeProposalStore,
    _require_owner_conn,
    suggested_case_invalid,
)
from bowhead_core.agent_eval_case_proposals import AgentEvalCaseProposals, require_case_suite_conn
from bowhead_core.agent_eval_scorers import core_scoring_contract
from bowhead_core.agent_eval_store import AgentEvalStore, AgentEvalStoreError
from pydantic import ValidationError

from bowhead_gateway._entitlement_gate import assert_not_readonly

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources


def validate_product_case_suggestions(cases: Sequence[Mapping[str, Any]]) -> None:
    """Match Owner adoption modes for case-only and combined Coach proposals."""
    from bowhead_gateway.routes.agents import AgentEvalCaseBody

    for index, item in enumerate(cases):
        if "case_spec" in item:
            case_id = item.get("id") if isinstance(item.get("id"), str) else None
            try:
                AgentEvalCaseBody(task=item["task"], **item["case_spec"]).domain()
            except ValidationError as exc:
                problems = [
                    {"field": ".".join(str(part) for part in error.get("loc", ())), "message": str(error.get("msg", ""))[:200]}
                    for error in exc.errors()[:8]
                ]
                summary = "; ".join(f"{problem['field']}: {problem['message']}" for problem in problems)
                raise suggested_case_invalid(
                    "case_spec_invalid",
                    f"Suggested case {case_id or index!r} is not a valid Owner case: {summary}.",
                    case_index=index,
                    case_id=case_id,
                    problems=problems,
                ) from exc
            except AgentEvalStoreError as exc:
                exc.details = {**exc.details, "case_index": index, **({"case_id": case_id} if case_id else {})}
                raise
            except (KeyError, TypeError) as exc:
                raise suggested_case_invalid(
                    "case_spec_invalid",
                    f"Suggested case {case_id or index!r} needs a `task` string and a `case_spec` object.",
                    case_index=index,
                    case_id=case_id,
                    field="case_spec",
                ) from exc


def redact_held_out_cases(proposal: dict) -> dict:
    """A held-out suggestion reaches Coach as `{id, case_key, held_out: true}` only, as in `agent_eval_suite_get`."""
    cases = []
    for case in proposal.get("suggested_cases") or ():
        spec = case.get("case_spec") if isinstance(case, Mapping) else None
        expected = spec.get("expected_contract") if isinstance(spec, Mapping) else None
        if isinstance(expected, Mapping) and expected.get("held_out") is True:
            cases.append({"id": case.get("id"), "case_key": spec.get("case_key"), "held_out": True})
        else:
            cases.append(case)
    return {**proposal, "suggested_cases": cases}


@dataclass(frozen=True)
class AgentCoachEvaluations:
    resources: AgentCoachResources

    async def authorize_owner(self, db: aiosqlite.Connection, agent_id: str) -> None:
        await self.resources.authoring._authorize_agent(db)
        await _require_owner_conn(db, team_id=self.resources.user.team_id, agent_id=agent_id, actor_user_id=self.resources.user.id)

    async def reauthorize(self, agent_id: str) -> None:
        async with open_shared_ctx(str(self.resources.store.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await self.authorize_owner(db, agent_id)

    async def suites(self, *, agent_id: str, offset: int, limit: int) -> dict:
        async with open_shared_ctx(str(self.resources.store.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await self.authorize_owner(db, agent_id)
            cursor = await db.execute(
                """SELECT s.id,s.name,s.state,r.id AS revision_id,r.definition_hash,r.case_count
                   FROM agent_eval_suite s LEFT JOIN agent_eval_suite_revision r ON r.suite_id=s.id
                     AND r.sequence=(SELECT MAX(sequence) FROM agent_eval_suite_revision WHERE suite_id=s.id)
                   WHERE s.team_id=? AND s.agent_id=? AND s.suite_kind='owner_quality' AND s.state!='archived'
                   ORDER BY s.id LIMIT ? OFFSET ?""",
                (self.resources.user.team_id, agent_id, limit + 1, offset),
            )
            rows = await cursor.fetchall()
            items = [
                {
                    "name": row["name"],
                    "state": row["state"],
                    "case_count": row["case_count"] or 0,
                    "target": {"kind": "agent_eval_suite", "agent_id": agent_id, "suite_id": row["id"], "revision_id": row["revision_id"]}
                    if row["revision_id"]
                    else None,
                    "definition_hash": row["definition_hash"],
                    "requires_suite_setup": row["revision_id"] is None,
                }
                for row in rows[:limit]
            ]
        await self.reauthorize(agent_id)
        return {
            "ok": True,
            "items": items,
            "next_offset": offset + limit if len(rows) > limit else None,
            "executed_by_this_call": False,
            "missing_suite_action": "propose_initial_suite" if not items and offset == 0 else None,
            "scoring_contract": core_scoring_contract(),
        }

    async def suite(self, *, target: dict, offset: int, limit: int) -> dict:
        resource = self.resources
        async with open_shared_ctx(str(resource.store.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await self.authorize_owner(db, target["agent_id"])
            await require_case_suite_conn(
                db,
                team_id=resource.user.team_id,
                agent_id=target["agent_id"],
                suite_id=target["suite_id"],
                revision_id=target["revision_id"],
                actor_user_id=resource.user.id,
            )
        revision = await AgentEvalStore(str(resource.store.db_path)).read_suite_revision(target["revision_id"])
        if revision is None:
            raise AgentChangeProposalError("agent_eval_suite_revision_not_found")
        items, size = [], 0
        for item in revision.cases[offset : offset + limit]:
            if item.held_out:
                # Held out from Coach (alignment loop A6): no task, points or results, only that it exists.
                items.append({"case_key": item.case_key, "held_out": True})
                continue
            payload = asdict(item)
            item_size = len(json.dumps(payload, ensure_ascii=False).encode())
            if item_size > 48 * 1024:
                payload = {"case_key": item.case_key, "case_hash": item.case_hash, "content_status": "too_large_for_coach"}
                item_size = len(json.dumps(payload).encode())
            if items and size + item_size > 64 * 1024:
                break
            items.append(payload)
            size += item_size
        await self.reauthorize(target["agent_id"])
        return {
            "ok": True,
            "target": target,
            "definition_hash": revision.definition_hash,
            "case_count": revision.case_count,
            "held_out_case_count": sum(case.held_out for case in revision.cases),
            "scorer_bundle_ref": revision.scorer_bundle_ref,
            "scoring_contract": core_scoring_contract() if revision.scorer_bundle_ref == "evalkit.core@1" else None,
            "items": items,
            "next_offset": offset + len(items) if offset + len(items) < len(revision.cases) else None,
            "content_role": "untrusted_evaluation_data",
            "independent_quality_proof": False,
            "executed_by_this_call": False,
        }

    async def propose(self, *, target: dict, cases: list[dict], rationale: str, request_id: str, turn_id: str, alignment: dict | None = None) -> dict:
        resource = self.resources
        await assert_not_readonly(resource.request)
        # Do not advertise fixtures/effects that the actual Owner adoption route
        # cannot provision. This is structural validation, not an executor trial.
        validate_product_case_suggestions([case for case in cases if case.get("operation") != "edit"])
        if alignment is not None and any(case.get("operation") == "edit" for case in cases):
            raise suggested_case_invalid("edit_alignment", "Propose saved-case edits separately from new alignment variants.")
        platform_updates = None
        if alignment is not None:
            from bowhead_core.agent_eval_alignment import alignment_enabled

            if not alignment_enabled():
                # FEATURE_AGENT_EVAL_ALIGNMENT off: no loop variants, so no held-out injection.
                raise AgentChangeProposalError("agent_eval_alignment_unavailable")
            platform_updates = await self._alignment_updates(target=target, cases=cases, alignment=alignment)

        async def authorize(db: aiosqlite.Connection) -> None:
            await assert_not_readonly(resource.request)
            await resource.authoring._authorize_agent(db)

        result = await AgentEvalCaseProposals(str(resource.store.db_path)).create(
            team_id=resource.user.team_id,
            agent_id=target["agent_id"],
            actor_user_id=resource.user.id,
            suite_id=target.get("suite_id"),
            revision_id=target.get("revision_id"),
            initial_suite_name=target["name"] if target["kind"] == "initial_agent_eval_suite" else None,
            source_ref=f"coach-cases:{turn_id}:{request_id}",
            suggested_cases=cases,
            rationale=rationale,
            provenance={
                "source_turn_id": turn_id,
                "workspace_id": resource.workspace_id,
                "source_session_id": resource.session_id,
                "target": target,
                **({"alignment": alignment} if alignment is not None else {}),
            },
            authorize=authorize,
            platform_updates=platform_updates,
            validate_product=validate_product_case_suggestions,
        )
        await self.reauthorize(target["agent_id"])
        return {
            "ok": True,
            "proposal": redact_held_out_cases(asdict(result)),
            "applied": False,
            "executed_by_this_call": False,
            "independent_quality_proof": False,
        }

    async def _alignment_updates(self, *, target: dict, cases: list[dict], alignment: dict) -> dict[str, dict]:
        """Variants of a confirmed loop point: each gets the confirmed point, one is held out (§7).

        Coach can't mark a case held out itself; the platform picks the first variant, so the case the
        point came from stays visible to Coach and the variant shows whether the change generalizes.
        """
        from bowhead_core.agent_eval_alignment import AgentEvalAlignmentError, AgentEvalAlignmentStore, _suite_point

        resource = self.resources
        loop, _questions = await AgentEvalAlignmentStore(str(resource.store.db_path)).read(
            team_id=resource.user.team_id, agent_id=target["agent_id"], actor_user_id=resource.user.id, loop_id=alignment["loop_id"]
        )
        point = loop.point(alignment["point_id"])
        if point.get("status") != "confirmed":
            raise AgentEvalAlignmentError("agent_eval_alignment_point_not_confirmed", status=409, details={"point_id": point["id"]})
        if target.get("kind") != "agent_eval_suite" or target.get("suite_id") != loop.suite_id:
            raise AgentEvalAlignmentError("agent_eval_alignment_suite_mismatch", details={"suite_id": loop.suite_id})
        confirmed = _suite_point(point)
        updates: dict[str, dict] = {}
        for index, case in enumerate(cases):
            existing = case["case_spec"].get("expected_contract", {}).get("points") or []
            points = [item for item in existing if item.get("id") != confirmed["id"]][:11] + [confirmed]
            updates[case["id"]] = {"points": points, **({"held_out": True} if index == 0 else {})}
        return updates

    async def get(self, *, agent_id: str, proposal_id: str) -> dict:
        resource = self.resources
        await self.reauthorize(agent_id)
        result = await AgentChangeProposalStore(str(resource.store.db_path)).get(
            team_id=resource.user.team_id,
            agent_id=agent_id,
            proposal_id=proposal_id,
            actor_user_id=resource.user.id,
        )
        if result is None or (result.base_suite_revision_id is None and result.initial_suite_name is None):
            raise AgentChangeProposalError("agent_change_proposal_not_found")
        if result.provenance.get("workspace_id") != resource.workspace_id:
            raise AgentChangeProposalError("agent_change_proposal_not_found")
        await self.reauthorize(agent_id)
        return {"ok": True, "proposal": redact_held_out_cases(asdict(result)), "executed_by_this_call": False, "independent_quality_proof": False}

    async def call(self, operation: str, args: dict, *, turn_id: str) -> dict:
        if operation == "agent_eval_evidence_get":
            from bowhead_core.agent_coach_evidence import AgentCoachEvidence

            return await AgentCoachEvidence(
                str(self.resources.store.db_path),
                self.resources.user.team_id,
                self.resources.user.id,
                self.authorize_owner,
                self.reauthorize,
            ).get(**args)
        if operation == "agent_eval_suite_list":
            return await self.suites(**args)
        if operation == "agent_eval_suite_get":
            return await self.suite(**args)
        if operation == "agent_eval_case_propose":
            return await self.propose(**args, turn_id=turn_id)
        if operation == "agent_eval_case_proposal_get":
            return await self.get(**args)
        raise ValueError("agent_eval_authoring_operation_unsupported")
