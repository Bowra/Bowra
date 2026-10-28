"""Explanation of effective authorization: can caller U use resource X, and why.

This module never decides anything on its own. Every verdict comes from a predicate the runtime
already calls (see ``docs/design/有效权限.md`` §2 for the layers); the only thing added here is that
the reason survives instead of being folded into one ``False``.

Nothing here writes: team-default hires are computed, not materialised, and a blocked listing is
not persisted. The assignment rule is ``ADMISSIBLE_ASSIGNMENT_SQL`` — the same one execution admission
applies — and ``test_effective_authorization`` drives the real admission transaction to keep them equal.

Still not represented (recorded in ``docs/sprints/effective-permission-2026-09-19/review-2026-09-20.md`` §B):
space membership and a running Turn, which admission checks against a live Turn; and the frozen
revision's prompt/listing, which admission scans while this module scans the Agent row (equal in
practice for revisioned Agents, but not by construction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

Verdict = Literal["pass", "deny", "skipped", "unknown"]

# Stable layer ids. They name the rows of docs/design/有效权限.md §2 that apply to using a team Agent.
LAYER_IDENTITY = "identity"
LAYER_ASSIGNMENT_SCHEMA = "assignment_schema"
LAYER_ASSIGNMENT = "assignment"
LAYER_VISIBILITY = "visibility"
LAYER_PROMPT_LINT = "prompt_lint"
LAYER_LISTING_SAFETY = "listing_safety"
LAYER_RELEASE_SAFETY = "release_safety"


# Public reason codes — the only reasons the product shows (docs/design/有效权限.md §8).
# Tuple order is display priority: an earlier reason makes a later next step pointless.
PUBLIC_REASON_CODES: tuple[str, ...] = (
    "agent_retired",
    "blocked_by_admin",
    "agent_needs_owner_fix",
    "not_shared_with_you",
    "not_in_space",
    "not_hired",
    "personally_disabled",
    "dependency_blocked_for_you",
    "dependency_broken",
    "missing_access_to_dependency",
    "temporarily_unavailable",
)
FALLBACK_PUBLIC_REASON = "temporarily_unavailable"


@dataclass(frozen=True, slots=True)
class LayerVerdict:
    """One layer's answer. ``reason`` is a finite code, never free text or user content."""

    layer: str
    verdict: Verdict
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LayeredDecision:
    """An ordered set of layer verdicts.

    ``skipped`` means the layer does not apply here and narrows nothing. ``unknown`` means it applies
    but could not be determined from what this caller knows; it never counts as allowed.
    """

    layers: tuple[LayerVerdict, ...]

    @property
    def allowed(self) -> bool:
        return bool(self.layers) and all(layer.verdict in ("pass", "skipped") for layer in self.layers)

    @property
    def determinate(self) -> bool:
        return bool(self.denied_by) or all(layer.verdict != "unknown" for layer in self.layers)

    @property
    def denied_by(self) -> tuple[LayerVerdict, ...]:
        return tuple(layer for layer in self.layers if layer.verdict == "deny")


# Every (layer, reason) the explainer can emit maps to exactly one public code. Kept exhaustive by
# test_every_internal_reason_has_a_public_code.
PUBLIC_REASON_BY_LAYER_REASON: dict[tuple[str, str], str] = {
    (LAYER_IDENTITY, "missing_team_or_agent"): "temporarily_unavailable",
    (LAYER_IDENTITY, "missing_user"): "temporarily_unavailable",
    (LAYER_ASSIGNMENT_SCHEMA, "schema_state_unreadable"): "temporarily_unavailable",
    (LAYER_ASSIGNMENT_SCHEMA, "schema_unavailable"): "temporarily_unavailable",
    (LAYER_ASSIGNMENT_SCHEMA, "schema_not_cutover"): "temporarily_unavailable",
    (LAYER_ASSIGNMENT, "not_hired"): "not_hired",
    # A hire that lost access keeps personal_state='active'; re-hiring is what restores it.
    (LAYER_ASSIGNMENT, "access_revoked"): "not_hired",
    (LAYER_ASSIGNMENT, "personally_disabled"): "personally_disabled",
    (LAYER_ASSIGNMENT, "assignment_unreadable"): "temporarily_unavailable",
    (LAYER_VISIBILITY, "actor_inactive"): "blocked_by_admin",
    (LAYER_VISIBILITY, "lifecycle_not_active"): "agent_retired",
    (LAYER_VISIBILITY, "status_not_enabled"): "agent_retired",
    (LAYER_VISIBILITY, "owner_inactive"): "agent_retired",
    (LAYER_VISIBILITY, "audience_not_allowed"): "not_shared_with_you",
    (LAYER_VISIBILITY, "blocked_by_security_policy"): "blocked_by_admin",
    (LAYER_VISIBILITY, "access_indeterminate"): "temporarily_unavailable",
    (LAYER_VISIBILITY, "access_denied"): "temporarily_unavailable",
    (LAYER_VISIBILITY, "access_unreadable"): "temporarily_unavailable",
    (LAYER_VISIBILITY, "agent_not_found"): "agent_retired",
    (LAYER_PROMPT_LINT, "prompt_rejected"): "agent_needs_owner_fix",
    (LAYER_PROMPT_LINT, "prompt_unreadable"): "temporarily_unavailable",
    (LAYER_PROMPT_LINT, "agent_not_found"): "agent_retired",
    (LAYER_LISTING_SAFETY, "listing_blocked"): "agent_needs_owner_fix",
    (LAYER_LISTING_SAFETY, "listing_safety_unreadable"): "temporarily_unavailable",
    (LAYER_LISTING_SAFETY, "agent_not_found"): "agent_retired",
    (LAYER_RELEASE_SAFETY, "no_live_release_or_safety_not_passed"): "agent_needs_owner_fix",
    (LAYER_RELEASE_SAFETY, "release_gate_unreadable"): "temporarily_unavailable",
}


def public_reason(decision: LayeredDecision) -> str | None:
    """The one reason to show for a refusal, or None when nothing refuses."""
    codes = []
    for layer in decision.denied_by:
        code = PUBLIC_REASON_BY_LAYER_REASON.get((layer.layer, layer.reason or ""))
        if code is None:
            logger.warning("effective_authorization.unmapped_reason layer=%s reason=%s", layer.layer, layer.reason)
            code = FALLBACK_PUBLIC_REASON
        codes.append(code)
    return min(codes, key=PUBLIC_REASON_CODES.index) if codes else None


def passed(layer: str, reason: str | None = None) -> LayerVerdict:
    return LayerVerdict(layer, "pass", reason)


def denied(layer: str, reason: str) -> LayerVerdict:
    return LayerVerdict(layer, "deny", reason)


def skipped(layer: str, reason: str) -> LayerVerdict:
    return LayerVerdict(layer, "skipped", reason)


def unknown(layer: str, reason: str) -> LayerVerdict:
    return LayerVerdict(layer, "unknown", reason)


@dataclass(frozen=True, slots=True)
class TeamAgentUseExplanation:
    """Why user ``user_id`` can or cannot have team Agent ``agent_id`` run for them right now."""

    team_id: str
    user_id: str
    agent_id: str
    decision: LayeredDecision

    @property
    def usable(self) -> bool | None:
        """``None`` when nothing denies but a layer could not be determined (pre-cutover deployments)."""
        return self.decision.allowed if self.decision.determinate else None


_NOT_PREFETCHED: Any = object()


async def explain_team_agent_use(
    db_path: str | Path,
    *,
    team_id: str,
    user_id: str,
    agent_id: str,
    dispatchable_agent_ids: frozenset[str] | None = _NOT_PREFETCHED,
    listing_blocked: bool | None = None,
) -> TeamAgentUseExplanation:
    """Explain every layer, not just the first refusal, without writing anything.

    See the module docstring for what the admission transaction checks beyond this. Composes:

    * ``team_agent_runtime_decision`` — hire, live access/audience, listing safety,
      and the current release/safety gate shared with execution admission.

    Space membership is verified inside the admission transaction against a running Turn and has no
    Turn-free seam yet; it is deliberately not re-implemented here.
    """
    explanations = await explain_team_agents_use(
        db_path,
        team_id=team_id,
        user_id=user_id,
        agent_ids=(agent_id,),
        dispatchable_agent_ids=dispatchable_agent_ids,
        listing_blocked=None if listing_blocked is None else {agent_id: listing_blocked},
    )
    return explanations[agent_id]


async def explain_team_agents_use(
    db_path: str | Path,
    *,
    team_id: str,
    user_id: str,
    agent_ids: Sequence[str],
    dispatchable_agent_ids: frozenset[str] | None = _NOT_PREFETCHED,
    listing_blocked: Mapping[str, bool] | None = None,
) -> dict[str, TeamAgentUseExplanation]:
    """``explain_team_agent_use`` for a page of Agents: every shared layer is read once, not once per Agent."""
    from types import SimpleNamespace

    from bowhead_core.agents.plan_admission import team_agent_runtime_decisions

    runtimes = await team_agent_runtime_decisions(
        SimpleNamespace(db_path=Path(db_path)),
        team_id=team_id,
        user_id=user_id,
        agent_ids=agent_ids,
        explain_all=True,
        persist_blocked_listing=False,
        materialize_team_defaults=False,
        listing_blocked=listing_blocked,
        dispatchable_agent_ids=None if dispatchable_agent_ids is _NOT_PREFETCHED else dispatchable_agent_ids,
    )
    return {
        agent_id: TeamAgentUseExplanation(
            team_id=team_id,
            user_id=user_id,
            agent_id=agent_id,
            decision=runtime,
        )
        for agent_id, runtime in runtimes.items()
    }


def explanation_to_dict(explanation: TeamAgentUseExplanation) -> dict[str, Any]:
    return {
        "team_id": explanation.team_id,
        "user_id": explanation.user_id,
        "agent_id": explanation.agent_id,
        "usable": explanation.usable,
        "public_reason": public_reason(explanation.decision),
        "layers": [{"layer": layer.layer, "verdict": layer.verdict, "reason": layer.reason} for layer in explanation.decision.layers],
    }


# --- Dependencies: which of an Agent's declared tools and skills the caller cannot use ------------------------------


@dataclass(frozen=True, slots=True)
class DependencyVerdict:
    """One declared dependency, decided by the shared grant evaluation (``grant_evaluation``)."""

    kind: Literal["tool", "skill"]
    name: str
    # Whether the member may be told this name (docs/design/有效权限.md §8.2 rule 3): team-owned resources yes,
    # Other users' private skills are shown only as "另有受限依赖".
    nameable: bool
    verdicts: tuple[Any, ...]

    @property
    def allowed(self) -> bool:
        return all(verdict.allowed or verdict.owner_exempt for verdict in self.verdicts)

    @property
    def public_reason(self) -> str | None:
        if self.allowed:
            return None
        refused = [verdict for verdict in self.verdicts if verdict.refused]
        if any(verdict.reason == "denied_by_grant" for verdict in refused):
            return "dependency_blocked_for_you"
        return "missing_access_to_dependency"


@dataclass(frozen=True, slots=True)
class AgentDependencyExplanation:
    team_id: str
    user_id: str
    workspace_id: str
    agent_id: str
    in_space: bool
    # False on shadow deployments (BOWHEAD_RESOURCE_GRANTS_ENFORCE unset): the refusals below are only logged
    # at runtime, so surfaces must say "当前未强制", not "暂不可用".
    enforced: bool
    dependencies: tuple[DependencyVerdict, ...]

    @property
    def lacking(self) -> tuple[DependencyVerdict, ...]:
        return tuple(dependency for dependency in self.dependencies if not dependency.allowed)

    @property
    def public_reason(self) -> str | None:
        if not self.in_space:
            return "not_in_space"
        codes = [dependency.public_reason for dependency in self.lacking if dependency.public_reason]
        return min(codes, key=PUBLIC_REASON_CODES.index) if codes else None


async def explain_agent_dependencies(
    db_path: str | Path,
    *,
    team_id: str,
    user_id: str,
    workspace_id: str,
    agent_id: str,
    access_store: Any | None = None,
) -> AgentDependencyExplanation:
    """Which of team Agent ``agent_id``'s declared tools and skills this caller lacks in this space. Writes nothing.

    Dependencies come from the live revision's definition, or the Agent row for legacy Agents. Each is decided by
    ``grant_evaluation`` exactly as execution decides it, with no audit. Covers the resource-grant layer
    (有效权限 §2 layer 11); whether a connection or credential is currently broken is not decided here yet.
    """
    import json
    from types import SimpleNamespace

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.access_control import AccessControlStore, resource_grants_enforce_enabled
    from bowhead_core.agent_dependency_compiler import declared_dependencies
    from bowhead_core.chat_reference_scope import ReferenceRequestScope, read_reference_access_context_in_connection
    from bowhead_core.grant_evaluation import grant_verdicts
    from bowhead_core.resource_access import builtin_capability_ids_for_skill, resource_grant_targets_for_tool

    db_path = Path(db_path)
    store = access_store if access_store is not None else AccessControlStore(db_path)
    enforced = resource_grants_enforce_enabled()

    def _empty(in_space: bool) -> AgentDependencyExplanation:
        return AgentDependencyExplanation(team_id, user_id, workspace_id, agent_id, in_space, enforced, ())

    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN")
        try:
            try:
                access_ctx = await read_reference_access_context_in_connection(
                    db,
                    scope=ReferenceRequestScope(team_id=team_id, user_id=user_id, workspace_id=workspace_id),
                    access_store=store,
                )
            except ValueError:
                return _empty(False)
            async with db.execute(
                "SELECT s.tool_refs_json, s.skills_json, r.definition_json FROM team_subagent s "
                "LEFT JOIN team_subagent_revision r ON r.id=s.live_revision_id WHERE s.team_id=? AND s.id=?",
                (team_id, agent_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return _empty(True)
            definition = json.loads(row[2]) if row[2] else {"tool_refs_json": row[0] or "[]", "skills_json": row[1] or "[]"}
            tool_refs, skill_grants, legacy_skills = declared_dependencies(definition)
            team_skills: dict[str, tuple[str, str | None]] = {}
            team_ids = [grant.id for grant in skill_grants if grant.kind == "team"]
            if team_ids:
                marks = ",".join("?" for _ in team_ids)
                async with db.execute(
                    f"SELECT id, visibility, created_by FROM team_skill WHERE team_id=? AND id IN ({marks})",  # noqa: S608 -- placeholders only
                    (team_id, *team_ids),
                ) as cursor:
                    team_skills = {str(r[0]): (str(r[1] or ""), r[2]) for r in await cursor.fetchall()}
        finally:
            await db.rollback()

    ctx = SimpleNamespace(user_id=user_id, team_id=team_id, workspace_id=workspace_id)
    access = access_ctx.as_dict()
    dependencies: list[DependencyVerdict] = []
    for tool in tool_refs:
        targets = resource_grant_targets_for_tool(tool, {}, include_argument_targets=False)
        dependencies.append(DependencyVerdict("tool", tool, True, tuple(await grant_verdicts(ctx, store, access, targets))))
    for grant in skill_grants:
        created_by: str | None = None
        if grant.kind == "team":
            visibility, created_by = team_skills.get(grant.id, ("", None))
            skill_target = ("team_skill", grant.id)
            nameable = visibility == "team_wide" or created_by == user_id
        else:
            # Built-in skills are platform-owned. A personal skill may only sit in its author's private Agent
            # (the compiler refuses wider audiences), so it is never named to anyone else.
            skill_target = ("skill", grant.name)
            nameable = grant.kind == "builtin"
        targets = [(*skill_target, permission) for permission in sorted(grant.permissions)]
        targets += [("builtin_capability", capability, "use") for capability in builtin_capability_ids_for_skill(grant.name)]
        verdicts = await grant_verdicts(ctx, store, access, targets, team_skill_created_by=created_by)
        dependencies.append(DependencyVerdict("skill", grant.name, nameable, tuple(verdicts)))
    for name in legacy_skills:
        targets = [("skill", name, "read")] + [("builtin_capability", c, "use") for c in builtin_capability_ids_for_skill(name)]
        dependencies.append(DependencyVerdict("skill", name, False, tuple(await grant_verdicts(ctx, store, access, targets))))
    return AgentDependencyExplanation(team_id, user_id, workspace_id, agent_id, True, enforced, tuple(dependencies))
