"""One evaluation of resource-grant targets, shared by every place that asks whether a caller's grants allow X.

Five call sites used to carry their own copy of this loop: ``ToolRegistry.execute`` (deny the call), the
tool-schema filter (hide the tool), the team-tool overlay build (drop the entry), the last-moment execution
gate (refuse dispatch) and ``skill_visible_by_grants`` (skill lists and skill execution). They keep their own
target sets and their own failure handling; what they share is how a target is resolved and decided, which
is what lives here. See ``docs/design/有效权限.md`` §2 layer 11.

Nothing here logs a call-site message or writes an audit row. Callers that audit denials keep doing so from
the verdicts they receive, which is what lets a read-only explainer call the same code without side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping

GrantTarget = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class GrantVerdict:
    """The decision for one resolved target. ``reason`` is the finite code from ``explain_decision``."""

    resource_kind: str
    resource_id: str
    permission: str
    allowed: bool
    reason: str | None
    # A team skill's author passes a ``not_in_allowlist`` refusal on the skill target itself (never on
    # other targets, never on an explicit deny). ``allowed`` stays False; callers skip exempt verdicts.
    owner_exempt: bool = False
    decision: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def refused(self) -> bool:
        return not self.allowed and not self.owner_exempt


def resolve_contextual_grant_target(ctx: Any, resource_kind: str, resource_id: str) -> tuple[str, str] | None:
    """Turn placeholder targets that depend on the current turn into concrete ones."""
    if resource_kind == "__kb_current_workspace__":
        workspace_id = getattr(ctx, "workspace_id", "") or ""
        return ("kb_scope", f"workspace:{workspace_id}") if workspace_id else None
    if resource_kind == "__kb_current_team__":
        team_id = getattr(ctx, "team_id", "") or ""
        return ("kb_scope", f"team:{team_id}") if team_id else None
    return resource_kind, resource_id


def _owner_exempt(ctx: Any, resource_kind: str, decision: Mapping[str, Any], team_skill_created_by: str | None) -> bool:
    return (
        resource_kind == "team_skill"
        and str(decision.get("reason") or "") == "not_in_allowlist"
        and bool(team_skill_created_by)
        and team_skill_created_by == getattr(ctx, "user_id", None)
    )


async def iter_grant_verdicts(
    ctx: Any,
    access_store: Any,
    access_ctx: Any,
    targets: Iterable[GrantTarget],
    *,
    rekey_skill_targets: bool = False,
    team_skill_created_by: str | None = None,
) -> AsyncIterator[GrantVerdict]:
    """Yield one verdict per target, in order, so a caller may stop at the first refusal.

    ``rekey_skill_targets``: a ``("skill", name, perm)`` target is re-keyed to ``("team_skill", id, perm)``
    when the name resolves to a team skill, and the author is learned from that lookup. Callers that already
    hold the team skill id pass it in the target and the author as ``team_skill_created_by``.

    Targets whose placeholder cannot be resolved, or whose id is empty, are skipped rather than decided.
    Exceptions from the store propagate; each caller keeps its own fail-open / fail-closed handling.
    """
    for resource_kind, resource_id, permission in targets:
        created_by = team_skill_created_by
        if rekey_skill_targets and resource_kind == "skill":
            from bowhead_core.resource_access import resolve_skill_grant_target

            (resource_kind, resource_id, permission), created_by = await resolve_skill_grant_target(ctx, resource_id, permission=permission)
        resolved = resolve_contextual_grant_target(ctx, resource_kind, resource_id)
        if resolved is None:
            continue
        resource_kind, resource_id = resolved
        if not resource_id:
            continue
        decision = await access_store.explain_decision(access_ctx, resource_kind, resource_id, permission)
        allowed = bool(decision.get("allowed"))
        yield GrantVerdict(
            resource_kind=resource_kind,
            resource_id=resource_id,
            permission=permission,
            allowed=allowed,
            reason=(str(decision.get("reason")) if decision.get("reason") is not None else None),
            owner_exempt=(not allowed and _owner_exempt(ctx, resource_kind, decision, created_by)),
            decision=decision,
        )


async def grant_verdicts(
    ctx: Any,
    access_store: Any,
    access_ctx: Any,
    targets: Iterable[GrantTarget],
    *,
    rekey_skill_targets: bool = False,
    team_skill_created_by: str | None = None,
) -> list[GrantVerdict]:
    """All verdicts at once, for callers that need every reason (logging both, or explaining)."""
    return [
        verdict
        async for verdict in iter_grant_verdicts(
            ctx,
            access_store,
            access_ctx,
            targets,
            rekey_skill_targets=rekey_skill_targets,
            team_skill_created_by=team_skill_created_by,
        )
    ]
