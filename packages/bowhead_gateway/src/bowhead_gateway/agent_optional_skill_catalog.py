"""Owner configuration metadata; never a task authorization or baseline grant."""

from dataclasses import dataclass
from typing import Literal

from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler, SkillReadContext
from bowhead_core.agent_revision_store import AgentRevisionConflictError, AgentRevisionForbiddenError
from bowhead_core.agent_runtime_extensions import SkillExtensionSelector
from bowhead_core.resource_access import builtin_capability_ids_for_skill
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import User, UserStore

from bowhead_gateway.access_context import AccessContextError, build_access_context
from bowhead_skill import SkillCatalog
from bowhead_skill.catalog import builtin_catalog_dir


@dataclass(frozen=True)
class _Candidate:
    source: Literal["builtin", "team", "user"]
    identity: str
    name: str
    label: str
    description: str
    private: bool = False
    owner: str | None = None


async def optional_skill_catalog(
    *,
    store: UserStore,
    user: User,
    agent_id: str | None,
    query: str,
    offset: int,
    limit: int,
    include_source_identity: bool = False,
    baseline_selection: bool = False,
) -> dict:
    access = AccessControlStore(store.db_path)

    async def current_actor():
        # Agent configuration is explicitly team/personal-scoped. It does not
        # select a default workspace or supply a task's later runtime access.
        try:
            return await build_access_context(
                user_store=store,
                access_store=access,
                user_id=user.id,
                expected_team_id=user.team_id,
                workspace_id="",
                allow_missing_workspace=True,
            )
        except AccessContextError:
            raise AgentRevisionForbiddenError("agent_optional_skill_catalog_unavailable") from None

    actor = await current_actor()
    builtin = SkillCatalog(roots=[builtin_catalog_dir()])
    personal = SkillInstanceStore(store.db_path)
    compiler = AgentDependencyCompiler(builtin_skills=builtin, personal_skill_store=personal, team_skill_store=store)
    candidates = [_Candidate("builtin", "builtin:" + spec.name, spec.name, spec.name, spec.description or "") for spec in builtin.list_specs()]
    for record in await store.list_team_skills(user.team_id, only_enabled=True, only_approved=True):
        if record.visibility == "team_wide" or record.created_by == user.id:
            candidates.append(
                _Candidate(
                    "team",
                    record.id,
                    record.name,
                    record.display_name or record.name,
                    record.description or "",
                    record.visibility != "team_wide",
                    record.created_by,
                )
            )
    for record in await personal.list_for_user(user_id=user.id):
        if record.enabled and record.source_type in {"user_upload", "user_generated"} and not record.import_pending:
            candidates.append(_Candidate("user", record.id, record.name, record.name, record.description or "", True, user.id))
    needle = query.strip().casefold()
    candidates = sorted(
        (item for item in candidates if needle in (item.name + " " + item.label + " " + item.description).casefold()),
        key=lambda item: (item.source, item.name, item.identity),
    )
    items = []
    index = offset
    # Bound material eligibility checks per page; no package bytes enter the
    # response. Offset is advisory pagination, never a permission/version token.
    while index < min(len(candidates), offset + 100) and len(items) < limit:
        candidate = candidates[index]
        index += 1
        if candidate.source == "builtin":
            preference = await personal.get(user_id=user.id, name=candidate.name)
            if preference is not None and preference.source_type == "builtin" and not preference.enabled:
                continue
        targets = [("team_skill", candidate.identity)] if candidate.source == "team" else [("skill", candidate.name)]
        targets.extend(("builtin_capability", identity) for identity in builtin_capability_ids_for_skill(candidate.name))
        allowed = True
        for kind, identity in targets:
            decision = await access.explain_decision(actor, kind, identity, "use")
            exempt = kind == "team_skill" and candidate.owner == user.id and decision.get("reason") == "not_in_allowlist"
            if not decision.get("allowed") and not exempt:
                allowed = False
                break
        if not allowed:
            continue
        resolve = compiler.resolve_baseline_skill if baseline_selection else compiler.resolve_optional_skill_read
        resource = await resolve(
            SkillExtensionSelector(source_kind=candidate.source, id=candidate.identity),
            context=SkillReadContext(team_id=user.team_id, owner_user_id=user.id, audience_mode="private" if candidate.private else "teamwide"),
        )
        if resource is None:
            continue
        items.append(
            {
                "resource": resource.model_dump(mode="json"),
                **(
                    {"name": candidate.name, "owned_team_skill": candidate.source == "team" and candidate.owner == user.id}
                    if include_source_identity
                    else {}
                ),
                "label": candidate.label[:120],
                "description": candidate.description[:400],
                "allowed_audiences": ["private"] if candidate.private else ["private", "custom", "teamwide"],
            }
        )
    if await current_actor() != actor:
        raise AgentRevisionConflictError("agent_optional_skill_catalog_changed")
    return {
        "kind": "agent_baseline_skill_catalog" if baseline_selection else "agent_optional_skill_catalog",
        "schema_version": 1,
        "agent_id": agent_id,
        "items": items,
        "next_offset": index if index < len(candidates) else None,
    }
