"""Independent case proposals over the existing proposal and suite writers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import (
    AgentChangeProposal,
    AgentChangeProposalError,
    _canonical_json,
    _hash_json,
    _now,
    _proposal_by_source_conn,
    _proposal_with_case_decisions_conn,
    _require_owner_conn,
    case_text_problem,
    suggested_case_invalid,
)
from bowhead_core.agent_eval_points import case_points, validate_drafted_points
from bowhead_core.agent_eval_store import EvalCaseDraft, _require_suite_manager, _revision_is_complete, _verify_revision_hash

Authorize = Callable[[aiosqlite.Connection], Awaitable[None]]


def validate_case_suggestions(values: Sequence[Mapping[str, Any]], *, originals: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[dict, ...]:
    if not 1 <= len(values) <= 32:
        raise suggested_case_invalid("case_count", f"Suggest between 1 and 32 cases; received {len(values)}.", case_count=len(values))
    cases, identities, keys = [], set(), set()
    allowed_spec_keys = {"case_key", "fixture_ref", "expected_contract", "scorer_config", "effect_policy", "valid_until"}
    expected_fields = {"id", "task", "failure_mode", "case_spec"}
    for index, value in enumerate(values):
        fields = set(value) if isinstance(value, Mapping) else set()
        label = value.get("id") if isinstance(value, Mapping) and isinstance(value.get("id"), str) else None
        if fields != expected_fields:
            raise suggested_case_invalid(
                "case_fields",
                f"Suggested case {index} must have exactly the fields {sorted(expected_fields)}.",
                case_index=index,
                case_id=label,
                missing_fields=sorted(expected_fields - fields),
                unexpected_fields=sorted(str(item) for item in fields - expected_fields)[:16],
            )
        identity = value["id"]
        if not isinstance(identity, str) or not 1 <= len(identity) <= 120:
            raise suggested_case_invalid(
                "id_invalid", f"Suggested case {index} `id` must be a string of 1-120 characters.", case_index=index, field="id"
            )
        if identity in identities:
            raise suggested_case_invalid(
                "duplicate_case_id",
                f"Suggested case id {identity!r} is used more than once.",
                case_index=index,
                case_id=identity,
                field="id",
            )
        problem = case_text_problem(value)
        if problem is not None:
            raise suggested_case_invalid(
                f"{problem[0]}_invalid",
                f"Suggested case {identity!r} `{problem[0]}` {problem[1]}.",
                case_index=index,
                case_id=identity,
                field=problem[0],
            )
        spec = value["case_spec"]
        if not isinstance(spec, dict):
            raise suggested_case_invalid(
                "case_spec_invalid",
                f"Suggested case {identity!r} `case_spec` must be an object.",
                case_index=index,
                case_id=identity,
                field="case_spec",
            )
        if unexpected := sorted(str(item) for item in set(spec) - allowed_spec_keys):
            raise suggested_case_invalid(
                "case_spec_fields",
                f"Suggested case {identity!r} `case_spec` has unsupported fields {unexpected[:16]}; allowed fields are {sorted(allowed_spec_keys)}.",
                case_index=index,
                case_id=identity,
                field="case_spec",
                unexpected_fields=unexpected[:16],
            )
        try:
            case = EvalCaseDraft(input={"task": value["task"]}, **spec)
            # Coach only suggests points; the Owner confirms them (PRD R8–R10).
            original = (originals or {}).get(identity)
            if original is None:
                validate_drafted_points(case.expected_contract)
            else:
                before = original["case_spec"]["expected_contract"]
                if ("held_out" in before) != ("held_out" in case.expected_contract) or before.get("held_out") != case.expected_contract.get(
                    "held_out"
                ):
                    raise ValueError("Only the Owner or platform changes held-out status")
                old_points = {point["id"]: point for point in case_points(before)}
                changed_points = [point for point in case_points(case.expected_contract) if old_points.get(point["id"]) != point]
                validate_drafted_points({"points": changed_points})
        except (TypeError, ValueError) as exc:
            raise suggested_case_invalid(
                "case_spec_invalid",
                f"Suggested case {identity!r} `case_spec` is invalid: {str(exc)[:300]}",
                case_index=index,
                case_id=identity,
                field="case_spec",
            ) from exc
        if case.case_key in keys:
            raise AgentChangeProposalError("agent_eval_case_conflict", details={"case_id": identity, "case_key": case.case_key})
        identities.add(identity)
        keys.add(case.case_key)
        cases.append({**value, "covers_group_ids": []})
    if len(_canonical_json(cases).encode()) > 256 * 1024:
        raise AgentChangeProposalError("agent_change_proposal_too_large")
    return tuple(cases)


def apply_platform_case_updates(cases: Sequence[Mapping[str, Any]], updates: Mapping[str, Mapping[str, Any]]) -> tuple[dict, ...]:
    """Server-side expected-contract keys added after Coach's suggestions are validated.

    The alignment loop adds the Owner-confirmed point to each variant and holds one case out (§7);
    Coach's own input can do neither, so this runs only after `validate_case_suggestions`.
    """
    from bowhead_core.agent_eval_scorers import validate_core_scoring_contract

    if unknown := sorted(set(updates) - {str(case["id"]) for case in cases}):
        raise suggested_case_invalid(
            "platform_update_unknown_case", f"Platform updates name unknown suggested cases {unknown[:16]}.", case_ids=unknown[:16]
        )
    result = []
    for case in cases:
        update = updates.get(str(case["id"]))
        if update is None:
            result.append(dict(case))
            continue
        spec = dict(case["case_spec"])
        expected = {**dict(spec.get("expected_contract") or {}), **dict(update)}
        spec["expected_contract"] = expected
        try:
            validate_core_scoring_contract(expected, spec.get("scorer_config") or {}, allow_schema_ref=False)
            EvalCaseDraft(input={"task": case["task"]}, **spec)
        except (TypeError, ValueError) as exc:
            raise suggested_case_invalid(
                "case_spec_invalid",
                f"Suggested case {case['id']!r} is invalid after the confirmed point was added: {str(exc)[:300]}",
                case_id=str(case["id"]),
                field="case_spec.expected_contract",
            ) from exc
        result.append({**case, "case_spec": spec})
    return tuple(result)


async def require_case_suite_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, suite_id: str, revision_id: str, actor_user_id: str
) -> None:
    """Require the latest complete editable suite under current Owner authority."""
    await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    cursor = await db.execute("SELECT * FROM agent_eval_suite WHERE id=? AND team_id=? AND agent_id=?", (suite_id, team_id, agent_id))
    suite = await cursor.fetchone()
    if suite is None or suite["suite_kind"] != "owner_quality" or suite["state"] == "archived":
        raise AgentChangeProposalError("agent_eval_suite_not_editable")
    await _require_suite_manager(db, row=suite, actor_user_id=actor_user_id)
    cursor = await db.execute("SELECT * FROM agent_eval_suite_revision WHERE suite_id=? ORDER BY sequence DESC LIMIT 1", (suite_id,))
    revision = await cursor.fetchone()
    if revision is None or revision["id"] != revision_id:
        raise AgentChangeProposalError("agent_eval_suite_version_conflict")
    if not await _revision_is_complete(db, revision):
        raise AgentChangeProposalError("agent_eval_suite_revision_incomplete")
    await _verify_revision_hash(db, revision)


async def require_initial_suite_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str, actor_user_id: str) -> None:
    """Recheck absence under the writer lock; never replace an existing suite."""
    await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    cursor = await db.execute(
        "SELECT 1 FROM agent_eval_suite WHERE team_id=? AND agent_id=? AND suite_kind='owner_quality' AND state!='archived' LIMIT 1",
        (team_id, agent_id),
    )
    if await cursor.fetchone():
        raise AgentChangeProposalError("agent_eval_initial_suite_conflict")


EDIT_FIELDS = frozenset({"task", "expected_contract", "scorer_config", "valid_until"})


async def materialize_case_edits_conn(
    db: aiosqlite.Connection, values: Sequence[Mapping[str, Any]], revision_id: str | None
) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    """Resolve edit intents against their original exact visible cases inside the writer lock."""
    from bowhead_core.agent_eval_store import _case_from_row

    expanded, originals, edits = [], {}, {}
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise suggested_case_invalid("case_fields", "Each suggested case must be an object.", case_index=index)
        if value.get("operation") != "edit":
            expanded.append(dict(value))
            continue
        required = {"id", "operation", "case_revision_id", "case_hash", "changes", "failure_mode"}
        if set(value) != required or revision_id is None:
            raise suggested_case_invalid("edit_shape", "An edit requires an existing suite and an exact case target.", case_index=index)
        changes = value["changes"]
        if not isinstance(changes, dict) or not changes or set(changes) - EDIT_FIELDS:
            raise suggested_case_invalid("edit_fields", "Change only task, expected_contract, scorer_config or valid_until.", case_index=index)
        if ("task" in changes and (not isinstance(changes["task"], str) or not changes["task"].strip())) or any(
            key in changes and not isinstance(changes[key], dict) for key in ("expected_contract", "scorer_config")
        ):
            raise suggested_case_invalid("edit_values", "The question must be text and expected/scoring contracts must be objects.", case_index=index)
        cursor = await db.execute(
            "SELECT * FROM agent_eval_case_revision WHERE id=? AND suite_revision_id=?", (value["case_revision_id"], revision_id)
        )
        row = await cursor.fetchone()
        if row is None or row["case_hash"] != value["case_hash"]:
            raise AgentChangeProposalError(
                "agent_eval_case_version_conflict",
                details={
                    "case_index": index,
                    "message": "The saved case does not match the read target. Read the current suite and reconsider this edit.",
                },
            )
        original = _case_from_row(row)
        if original.held_out:
            raise AgentChangeProposalError(
                "agent_eval_case_not_editable",
                details={
                    "case_index": index,
                    "message": "This case is held out from Coach. Choose a visible case; the Owner manages held-out cases in Evaluate.",
                },
            )
        before = {
            "task": original.input.get("task", ""),
            "case_spec": {
                key: getattr(original, key)
                for key in ("case_key", "fixture_ref", "expected_contract", "scorer_config", "effect_policy", "valid_until")
            },
        }
        updated = {**before, "case_spec": {**before["case_spec"]}}
        for key, content in changes.items():
            if key == "task":
                updated["task"] = content
            else:
                updated["case_spec"][key] = content
        changed = [key for key in changes if changes[key] != (before["task"] if key == "task" else before["case_spec"][key])]
        if not changed:
            raise suggested_case_invalid("edit_unchanged", "The requested edit does not change this case.", case_index=index)
        identity = value["id"]
        if not isinstance(identity, str):
            raise suggested_case_invalid("id_invalid", "An edit needs a text suggestion identity.", case_index=index)
        originals[identity] = before
        edits[identity] = {
            "case_revision_id": original.case_revision_id,
            "case_hash": original.case_hash,
            "case_key": original.case_key,
            "before": before,
            "changed_fields": sorted(changed),
        }
        expanded.append({"id": identity, "failure_mode": value["failure_mode"], **updated})
    return expanded, originals, edits


class AgentEvalCaseProposals:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def create(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        suite_id: str | None,
        revision_id: str | None,
        initial_suite_name: str | None = None,
        source_ref: str,
        suggested_cases: Sequence[Mapping[str, Any]],
        rationale: str,
        provenance: Mapping[str, Any],
        authorize: Authorize,
        platform_updates: Mapping[str, Mapping[str, Any]] | None = None,
        validate_product: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
    ) -> AgentChangeProposal:
        if initial_suite_name is not None:
            initial_suite_name = initial_suite_name.strip()
            if not 1 <= len(initial_suite_name) <= 120 or suite_id is not None or revision_id is not None:
                raise AgentChangeProposalError("agent_eval_initial_suite_invalid")
        elif not suite_id or not revision_id:
            raise AgentChangeProposalError("agent_eval_suite_version_required")
        has_edits = any(isinstance(value, Mapping) and value.get("operation") == "edit" for value in suggested_cases)
        if has_edits and (initial_suite_name is not None or platform_updates):
            raise suggested_case_invalid("edit_context", "Edit existing visible cases independently of initial suites and alignment variants.")
        cases = () if has_edits else validate_case_suggestions(suggested_cases)
        if platform_updates:
            cases = apply_platform_case_updates(cases, platform_updates)
        if not source_ref or len(source_ref) > 240 or len(rationale) > 4000:
            raise AgentChangeProposalError("agent_change_proposal_invalid")
        cases_json = _canonical_json(cases)
        provenance_json = _canonical_json(
            {
                **provenance,
                "kind": "coach_case_only",
                "suite_id": suite_id,
                "suite_revision_id": revision_id,
                **({"case_edit_intent_hash": _hash_json(_canonical_json(suggested_cases))} if has_edits else {}),
            }
        )
        if len(provenance_json.encode()) > 256 * 1024:
            raise AgentChangeProposalError("agent_change_proposal_too_large")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await authorize(db)
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            existing = await _proposal_by_source_conn(db, team_id=team_id, agent_id=agent_id, source_type="coach", source_ref=source_ref)
            if existing is not None:
                if (
                    existing["base_suite_revision_id"] != revision_id
                    or existing["initial_suite_name"] != initial_suite_name
                    or (not has_edits and existing["suggested_cases_json"] != cases_json)
                    or existing["rationale"] != rationale
                    or existing["provenance_json"] != provenance_json
                    or existing["created_by_kind"] != "user"
                    or existing["created_by_id"] != actor_user_id
                ):
                    raise AgentChangeProposalError("agent_change_proposal_idempotency_conflict")
                return await _proposal_with_case_decisions_conn(db, existing)
            if initial_suite_name is not None:
                await require_initial_suite_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            else:
                await require_case_suite_conn(
                    db, team_id=team_id, agent_id=agent_id, suite_id=suite_id, revision_id=revision_id, actor_user_id=actor_user_id
                )
            if has_edits:
                cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_coach_case_edit_scope'")
                if await cursor.fetchone() is None:
                    raise AgentChangeProposalError(
                        "agent_eval_case_edit_schema_required", details={"message": "Upgrade the database before proposing saved-case edits."}
                    )
                expanded, originals, edits = await materialize_case_edits_conn(db, suggested_cases, revision_id)
                cases = validate_case_suggestions(expanded, originals=originals)
                if validate_product is not None:
                    validate_product(cases)
                cases = tuple({**case, **({"edit": edits[case["id"]]} if case["id"] in edits else {})} for case in cases)
                cases_json = _canonical_json(cases)
                if len(cases_json.encode()) > 256 * 1024:
                    raise AgentChangeProposalError("agent_change_proposal_too_large")
            proposal_id = "acp_" + uuid4().hex
            await db.execute(
                """INSERT INTO agent_change_proposal(
                   id,team_id,agent_id,source_type,source_ref,base_document_json,base_document_hash,
                   patch_json,patch_hash,atomic_groups_json,suggested_cases_json,rationale,provenance_json,
                   status,row_version,created_by_kind,created_by_id,created_at,base_suite_revision_id,initial_suite_name
                   ) VALUES(?,?,?,'coach',?,'{}',?,'[]',?,'[]',?,?,?,'open',1,'user',?,?,?,?)""",
                (
                    proposal_id,
                    team_id,
                    agent_id,
                    source_ref,
                    _hash_json("{}"),
                    _hash_json("[]"),
                    cases_json,
                    rationale,
                    provenance_json,
                    actor_user_id,
                    _now(),
                    revision_id,
                    initial_suite_name,
                ),
            )
            await authorize(db)
            await db.commit()
            cursor = await db.execute("SELECT * FROM agent_change_proposal WHERE id=?", (proposal_id,))
            return await _proposal_with_case_decisions_conn(db, await cursor.fetchone())
