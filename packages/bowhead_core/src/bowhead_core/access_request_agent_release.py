"""Access-request approval for release-managed Agents (access-request expansion).

After the release cutover an Agent's Audience is part of its immutable release, so an
approved access request cannot write a grant directly. Instead the Owner's approval goes
through the normal publication path:

1. Base the change on the **live release**: copy its exact definition and listing into the
   working copy and add only the requester to its audience. If the Owner already has a
   working copy that differs from live, or a candidate in flight, the approval is refused
   (``agent_has_unpublished_changes``) so it can never publish unrelated edits.
2. Save the working copy under the Owner identity. The Owner completes evaluation and
   publication in the version workbench, including admin review when required. Nothing
   here writes a release audience or a grant.
3. The request stays ``approved_pending`` until a release that lets the requester use the
   Agent goes live (``approved``); it becomes ``approve_failed`` when its candidate is
   rejected, goes stale or is withdrawn, when a release goes live without the requester, or
   when the saved working copy no longer carries the requester (``reconcile_release_pending``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionConflictError
from bowhead_core.catalog_localization import without_server_derived_listing_keys
from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

if TYPE_CHECKING:
    from bowhead_core.access_request_store import AccessRequest, AccessRequestStore
    from bowhead_core.agent_revision_store import AgentRevisionStore

UNPUBLISHED_CHANGES = "access_request_agent_has_unpublished_changes"
RELEASE_CHANGED = "access_request_agent_release_changed"
LIVE_RELEASE_MISSING = "agent_live_release_missing"
logger = logging.getLogger(__name__)
ReleaseOutcomeState = Literal["awaiting_owner_publish", "noop"]
ReconcileOutcome = Literal["approved", "approve_failed"]


class AgentReleaseApprovalError(RuntimeError):
    """The approval could not be started; the access request stays pending."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentReleaseApprovalOutcome:
    state: ReleaseOutcomeState
    candidate_id: str | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def idempotency_key_for(request_id: str) -> str:
    return f"access-request:{request_id}"


async def _live_state(db_path: str, *, team_id: str, agent_id: str) -> dict[str, Any]:
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT rel.id AS release_id, rel.audience_snapshot_json, rev.definition_json, rev.listing_json
            FROM team_subagent sa
            JOIN team_subagent_release rel
              ON rel.team_id=sa.team_id AND rel.subagent_id=sa.id AND rel.id=sa.live_release_id
            JOIN team_subagent_revision rev
              ON rev.team_id=rel.team_id AND rev.subagent_id=rel.subagent_id AND rev.id=rel.revision_id
            WHERE sa.team_id=? AND sa.id=?
            """,
            (team_id, agent_id),
        ) as cursor:
            live = await cursor.fetchone()
        if live is None:
            raise AgentReleaseApprovalError(LIVE_RELEASE_MISSING)
        async with db.execute(
            "SELECT * FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?",
            (team_id, agent_id),
        ) as cursor:
            working = await cursor.fetchone()
        async with db.execute(
            "SELECT 1 FROM team_subagent_release_candidate WHERE team_id=? AND subagent_id=? AND state IN ('prepared','pending','approved') LIMIT 1",
            (team_id, agent_id),
        ) as cursor:
            in_flight = await cursor.fetchone() is not None
    return {
        "release_id": str(live["release_id"]),
        "definition": json.loads(str(live["definition_json"])),
        "listing": json.loads(str(live["listing_json"])),
        "audience": AgentAudience.from_release_snapshot(json.loads(str(live["audience_snapshot_json"]))),
        "working": working,
        "in_flight": in_flight,
    }


def _working_copy_matches_live(working: aiosqlite.Row, live: dict[str, Any]) -> bool:
    if str(working["base_release_id"] or "") != live["release_id"]:
        return False
    return (
        _canonical(json.loads(str(working["definition_json"]))) == _canonical(live["definition"])
        and _canonical(without_server_derived_listing_keys(json.loads(str(working["listing_json"]))))
        == _canonical(without_server_derived_listing_keys(live["listing"]))
        and _canonical(json.loads(str(working["audience_json"]))) == _canonical(live["audience"].to_dict())
    )


def _with_member(audience: AgentAudience, user_id: str) -> AgentAudience | None:
    """The audience plus ``user_id``; None when the audience already covers them."""
    if audience.mode == "teamwide" or user_id in audience.user_ids:
        return None
    if audience.mode == "private":
        return AgentAudience("custom", user_ids=(user_id,))
    return AgentAudience("custom", user_ids=tuple(sorted({*audience.user_ids, user_id})), group_ids=audience.group_ids)


async def request_audience_release(
    revision_store: AgentRevisionStore,
    *,
    team_id: str,
    agent_id: str,
    owner_user_id: str,
    requester_id: str,
) -> AgentReleaseApprovalOutcome:
    """Save the audience change for the Owner to evaluate and publish in the workbench."""
    live = await _live_state(revision_store.db_path, team_id=team_id, agent_id=agent_id)
    working = live["working"]
    if live["in_flight"] or (working is not None and not _working_copy_matches_live(working, live)):
        raise AgentReleaseApprovalError(UNPUBLISHED_CHANGES)
    audience = _with_member(live["audience"], requester_id)
    if audience is None:
        return AgentReleaseApprovalOutcome("noop")
    try:
        await revision_store.save_working_copy(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=owner_user_id,
            definition=live["definition"],
            listing=live["listing"],
            audience=audience,
            expected_row_version=int(working["row_version"]) if working is not None else None,
            # The draft is derived from this exact live release; if another release went live
            # since the check above (e.g. an audience contraction), nothing is written.
            expected_base_release_id=live["release_id"],
        )
    except AgentRevisionConflictError as exc:
        if exc.code == "agent_working_copy_base_stale":
            raise AgentReleaseApprovalError(RELEASE_CHANGED) from exc
        # The Owner edited concurrently: never overwrite their working copy.
        raise AgentReleaseApprovalError(UNPUBLISHED_CHANGES) from exc
    return AgentReleaseApprovalOutcome("awaiting_owner_publish")


async def remove_requester_from_working_copy(revision_store: AgentRevisionStore, row: AccessRequest) -> bool:
    """After a failed approval, take the requester back out of the Owner's working copy so a
    later Owner publish cannot silently include them. Other edits are kept; an exact copy of
    live plus the requester (what the approval wrote) is discarded."""
    owner_user_id = row.decided_by or row.owner_id
    if not owner_user_id:
        return False
    try:
        live = await _live_state(revision_store.db_path, team_id=row.team_id, agent_id=row.resource_id)
    except Exception:
        logger.warning("access_request.release_cleanup_skipped request_id=%s", row.id, exc_info=True)
        return False
    working = live["working"]
    if working is None or row.requester_id in live["audience"].user_ids:
        return False
    audience = AgentAudience.from_mapping(json.loads(str(working["audience_json"])))
    if row.requester_id not in audience.user_ids:
        return False
    remaining = tuple(u for u in audience.user_ids if u != row.requester_id)
    trimmed = (
        AgentAudience("custom", user_ids=remaining, group_ids=audience.group_ids) if remaining or audience.group_ids else AgentAudience("private")
    )
    try:
        if (
            str(working["base_release_id"] or "") == live["release_id"]
            and _canonical(trimmed.to_dict()) == _canonical(live["audience"].to_dict())
            and (
                _canonical(json.loads(str(working["definition_json"]))) == _canonical(live["definition"])
                and _canonical(without_server_derived_listing_keys(json.loads(str(working["listing_json"]))))
                == _canonical(without_server_derived_listing_keys(live["listing"]))
            )
        ):
            return await revision_store.discard_working_copy(
                team_id=row.team_id,
                agent_id=row.resource_id,
                actor_user_id=owner_user_id,
                expected_working_copy_id=str(working["id"]),
                expected_row_version=int(working["row_version"]),
            )
        await revision_store.save_working_copy(
            team_id=row.team_id,
            agent_id=row.resource_id,
            actor_user_id=owner_user_id,
            definition=json.loads(str(working["definition_json"])),
            listing=json.loads(str(working["listing_json"])),
            audience=trimmed,
            expected_row_version=int(working["row_version"]),
        )
        return True
    except Exception:
        logger.warning("access_request.release_cleanup_failed request_id=%s", row.id, exc_info=True)
        return False


async def _release_pending_outcome(db_path: str, row: AccessRequest) -> ReconcileOutcome | None:
    decision = await TeamSubagentAccessResolver(db_path).resolve(team_id=row.team_id, actor_user_id=row.requester_id, subagent_id=row.resource_id)
    if decision is not None and decision.resolution.can_consume:
        return "approved"
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT 1 FROM team_subagent_release_event WHERE team_id=? AND subagent_id=? AND event_type='release.activated' AND created_at>? LIMIT 1",
            (row.team_id, row.resource_id, row.decided_at or ""),
        ) as cursor:
            if await cursor.fetchone() is not None:
                return "approve_failed"  # a release went live without the requester
        if row.release_candidate_id:
            async with db.execute(
                "SELECT state FROM team_subagent_release_candidate WHERE team_id=? AND id=?",
                (row.team_id, row.release_candidate_id),
            ) as cursor:
                candidate = await cursor.fetchone()
            if candidate is None or str(candidate["state"]) in {"rejected", "stale", "withdrawn"}:
                return "approve_failed"
            return None
        # Waiting for the Owner to publish from the version workbench.
        async with db.execute(
            "SELECT audience_json FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?",
            (row.team_id, row.resource_id),
        ) as cursor:
            working = await cursor.fetchone()
        async with db.execute(
            "SELECT target_audience_json FROM team_subagent_release_candidate WHERE team_id=? AND subagent_id=? AND state IN ('prepared','pending','approved')",
            (row.team_id, row.resource_id),
        ) as cursor:
            candidates = await cursor.fetchall()
    audiences = [str(working["audience_json"])] if working is not None else []
    audiences.extend(str(c["target_audience_json"]) for c in candidates)
    if any(row.requester_id in (json.loads(a).get("user_ids") or []) for a in audiences):
        return None
    return "approve_failed"


async def reconcile_release_pending(
    acr_store: AccessRequestStore,
    db_path: str,
    *,
    team_id: str | None = None,
    agent_id: str | None = None,
    requester_id: str | None = None,
    revision_store: AgentRevisionStore | None = None,
    close_pending_with_access: bool = True,
) -> list[tuple[AccessRequest, ReconcileOutcome]]:
    """Settle ``approved_pending`` Agent requests whose release outcome is now known. With a
    ``revision_store``, a failed approval also takes the requester back out of the Owner's
    working copy (``remove_requester_from_working_copy``)."""
    settled: list[tuple[AccessRequest, ReconcileOutcome]] = []
    for row in await acr_store.list_release_pending(team_id=team_id, resource_id=agent_id, requester_id=requester_id):
        outcome = await _release_pending_outcome(db_path, row)
        if outcome is None:
            continue
        finished = await acr_store.finish_release_pending(request_id=row.id, status=outcome)
        if finished is None:
            continue
        if outcome == "approve_failed" and revision_store is not None:
            await remove_requester_from_working_copy(revision_store, finished)
        settled.append((finished, outcome))
    if close_pending_with_access:
        settled.extend(await _close_pending_with_access(acr_store, db_path, team_id=team_id, agent_id=agent_id, requester_id=requester_id))
    return settled


async def _close_pending_with_access(
    acr_store: AccessRequestStore,
    db_path: str,
    *,
    team_id: str | None,
    agent_id: str | None,
    requester_id: str | None,
) -> list[tuple[AccessRequest, ReconcileOutcome]]:
    """A pending Agent request whose requester can already use the Agent (e.g. a claim was
    reverted although its release went live) is closed as ``approved``, like any access
    that arrives out of band (``resolve_pending_for_grant``)."""
    closed: list[tuple[AccessRequest, ReconcileOutcome]] = []
    resolver = TeamSubagentAccessResolver(db_path)
    for row in await acr_store.list_pending_real(kind="team_subagent", team_id=team_id, resource_id=agent_id, requester_id=requester_id):
        decision = await resolver.resolve(team_id=row.team_id, actor_user_id=row.requester_id, subagent_id=row.resource_id)
        if decision is None or not decision.resolution.can_consume:
            continue
        resolved = await acr_store.resolve_pending_for_grant(
            team_id=row.team_id,
            kind=row.kind,
            resource_id=row.resource_id,
            requester_id=row.requester_id,
            decided_by=decision.owner_user_id or row.owner_id or "system",
        )
        if resolved is not None:
            closed.append((resolved, "approved"))
    return closed


async def settle_stale_claim(
    acr_store: AccessRequestStore,
    revision_store: AgentRevisionStore,
    row: AccessRequest,
) -> Literal["reverted", "approved_pending", "approved", "approve_failed"]:
    """A claim older than the timeout: settle it from the publication receipt bound to the
    request (``access-request:{id}``) instead of reverting a publish that did commit."""
    receipt = None
    if row.decided_by:
        receipt = await revision_store.publication_receipt(
            team_id=row.team_id, agent_id=row.resource_id, actor_user_id=row.decided_by, idempotency_key=idempotency_key_for(row.id)
        )
    if receipt is None:
        # The current approval saves only a draft; older approvals can have a publication
        # receipt. Remove an unfinalized draft change first, so the next approval does
        # not hit agent_has_unpublished_changes on our own leftover.
        await remove_requester_from_working_copy(revision_store, row)
        await acr_store.revert_release_claim(request_id=row.id)
        return "reverted"
    if receipt.state == "pending":
        await acr_store.finalize_release_claim(request_id=row.id, status="approved_pending", candidate_id=receipt.candidate_id)
        return "approved_pending"
    outcome: ReconcileOutcome = "approve_failed"
    if receipt.state == "activated":
        decision = await TeamSubagentAccessResolver(revision_store.db_path).resolve(
            team_id=row.team_id, actor_user_id=row.requester_id, subagent_id=row.resource_id
        )
        if decision is not None and decision.resolution.can_consume:
            outcome = "approved"
    await acr_store.finalize_release_claim(request_id=row.id, status=outcome, candidate_id=receipt.candidate_id)
    return outcome
