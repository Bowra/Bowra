"""Knowledge base API (organization phase 1, slice 3), behind ``FEATURE_KNOWLEDGE_BASES``.

Every route lives under ``/api/v1/kb/knowledge-bases`` on the original KB router and its
authorization helpers. With the switch off each one answers 404 ``knowledge_base_not_found``.
Per-ID routes go through ``_knowledge_base_check``: one 404 for every refusal (unknown,
another Team's, deleted, a Space knowledge base outside the current Space, no level) and 403
``knowledge_base_level_required`` only when the caller can use the knowledge base but needs
more. Names need not be unique (C4); lists show the owner and home to tell them apart.

Levels follow ``resolve_knowledge_levels``: use 1, edit 2, manage 3, and 4 for the owner or an
automatic manager (shown as manage; it also allows transfer and delete).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.access_control import normalize_workspace_role
from bowhead_core.kb.knowledge_base_access import (
    KNOWLEDGE_EDIT,
    KNOWLEDGE_MANAGE,
    KNOWLEDGE_OWNER,
    KNOWLEDGE_USE,
    LEVEL_RANKS,
    KnowledgeCaller,
    knowledge_level,
)
from bowhead_core.kb.knowledge_base_store import (
    DEFAULT_AUDIENCE,
    MAX_MEMBER_CHANGES,
    KnowledgeBaseConflict,
    KnowledgeBaseFileCounts,
    KnowledgeBaseMemberRow,
    KnowledgeBaseRow,
    KnowledgeBaseStore,
    default_knowledge_base_id,
    home_for_scope,
    knowledge_base_key,
    knowledge_bases_enabled,
)
from bowhead_core.kb.permission import KbPermission
from bowhead_core.kb.telemetry import emit_audit
from bowhead_core.space_governance import SpaceOperation
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.kb.permission import ScopeContext
    from bowhead_core.kb.schemas import KbFileDTO
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)


def _require_enabled() -> None:
    """With the switch off every knowledge base route answers like an unknown knowledge base."""
    if not knowledge_bases_enabled():
        from bowhead_gateway.routes.kb import _knowledge_base_not_found

        raise _knowledge_base_not_found()


router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-base"], dependencies=[Depends(_require_enabled)])

_LEVEL_OUT = {KNOWLEDGE_USE: "use", KNOWLEDGE_EDIT: "edit", KNOWLEDGE_MANAGE: "manage", KNOWLEDGE_OWNER: "manage"}
_AUDIENCES = {"personal": ("owner",), "space": ("members", "space"), "team": ("members", "team")}
_SECTIONS = ("personal", "space", "team")
Level = Literal["use", "edit", "manage"]


def _kb():
    from bowhead_gateway.routes import kb

    return kb


def _store(request: Request) -> KnowledgeBaseStore:
    return KnowledgeBaseStore(_kb()._get_files_store(request)._db_path)


async def _context(request: Request, user: User, *, write: bool = False) -> tuple[ScopeContext, str]:
    """The caller's KB context (any Space header is checked for membership) and normalized Space role."""
    ctx = await _kb()._make_scope_context_checked(
        user, request, allow_missing_workspace=True, operation=SpaceOperation.WRITE if write else SpaceOperation.READ_HISTORY
    )
    raw = getattr(request.state, "bowhead_access_context", None) or {}
    role = (normalize_workspace_role(raw.get("workspace_role")) or "") if ctx.workspace_id else ""
    return ctx, role


def _levels(ctx: ScopeContext) -> Any:
    if ctx.knowledge_levels is None:
        raise _kb()._kb_error(
            503,
            "knowledge_base_access_unavailable",
            hint="Knowledge base access could not be checked. Retry; if it keeps failing, ask an administrator to check the knowledge service.",
        )
    return ctx.knowledge_levels


def _default_row(team_id: str, home: str, home_id: str) -> KnowledgeBaseRow:
    """A home's default before its row exists (defaults are created lazily)."""
    audience, audience_level = DEFAULT_AUDIENCE[home]
    return KnowledgeBaseRow(
        id=default_knowledge_base_id(team_id, home, home_id),
        team_id=team_id,
        home=home,
        home_id=home_id,
        is_default=True,
        name=None,
        description=None,
        audience=audience,
        audience_level=audience_level,
        owner_user_id=home_id if home == "personal" else None,
        revision=1,
        created_at="",
        updated_at="",
        deleted_at=None,
    )


def _file_scope(row: KnowledgeBaseRow) -> tuple[str, str]:
    """The file scope of a knowledge base's home; a home's name is its canonical scope."""
    return row.home, row.home_id


async def _labels(request: Request, team_id: str, user_ids: list[str]) -> dict[str, str]:
    users = getattr(request.app.state, "user_store", None)
    if users is None or not user_ids:
        return {}
    try:
        return await users.get_team_member_labels(team_id, [user_id for user_id in user_ids if user_id])
    except Exception:
        logger.warning("kb.knowledge_base_labels_failed team_id=%s", team_id, exc_info=True)
        return {}


def _out(row: KnowledgeBaseRow, level: int, counts: KnowledgeBaseFileCounts | None, names: dict[str, str]) -> dict[str, Any]:
    files = counts or KnowledgeBaseFileCounts()
    updated = max((value for value in (row.updated_at, files.latest_upload) if value), default=None)
    owner = row.owner_user_id
    return {
        "id": row.id,
        "is_default": row.is_default,
        "home": row.home,
        "home_id": row.home_id,
        # Defaults store no name: the interface renders it from the home.
        "name": row.name,
        "description": row.description,
        "audience": row.audience,
        "audience_level": row.audience_level,
        "level": _LEVEL_OUT.get(min(level, KNOWLEDGE_OWNER), "use"),
        "owner": {"user_id": owner, "name": names.get(owner)} if owner else None,
        "can": {
            "add_files": level >= KNOWLEDGE_EDIT,
            "manage": level >= KNOWLEDGE_MANAGE,
            "transfer": level >= KNOWLEDGE_OWNER and not row.is_default and row.home != "personal",
            "delete": level >= KNOWLEDGE_OWNER and not row.is_default,
        },
        "file_count": files.files,
        "failed_count": files.failed,
        "updated_at": updated,
        "revision": row.revision,
    }


async def _detail(request: Request, ctx: ScopeContext, row: KnowledgeBaseRow, level: int) -> dict[str, Any]:
    counts = await _store(request).file_counts(ctx.team_id, [(row.home, row.home_id)])
    names = await _labels(request, ctx.team_id, [row.owner_user_id or ""])
    return _out(row, level, counts.get(row.id), names)


def _audience(home: str, audience: str | None, level: str | None) -> tuple[str, str]:
    """Validate an audience for a home: My is owner only; Space: members or all Space members; Team: members or everyone (use)."""
    if audience not in _AUDIENCES[home]:
        raise _kb()._kb_error(
            422,
            "knowledge_base_audience_invalid",
            home=home,
            allowed=list(_AUDIENCES[home]),
            hint="Choose who can use it from the options of this place: My knowledge bases are only yours; Space ones are for listed members or all Space members; Team ones for listed members or everyone in the Team.",
        )
    if audience == "team" and (level or "use") != "use":
        raise _kb()._kb_error(
            422,
            "knowledge_base_audience_invalid",
            home=home,
            allowed=list(_AUDIENCES[home]),
            hint="Everyone in the Team can only use a Team knowledge base. Give edit rights by adding people, groups or Spaces to its members.",
        )
    # The default level applies to the "all Space members" audience only.
    return audience, (level or "use") if audience == "space" else "use"


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise _kb()._kb_error(422, "knowledge_base_name_required", hint="Enter a name for the knowledge base.")
    return name


# ─── People, groups and Spaces behind a member list ─────────────────────────


async def _groups(request: Request, row: KnowledgeBaseRow, entries: list[KnowledgeBaseMemberRow]) -> dict[str, tuple[Any, set[str] | None]]:
    """Each group entry's record and, when it applies to this knowledge base, its active members."""
    access = getattr(request.app.state, "access_control_store", None)
    out: dict[str, tuple[Any, set[str] | None]] = {}
    for entry in entries:
        if entry.principal_type != "group" or entry.principal_id in out:
            continue
        group = None
        if access is not None:
            try:
                group = await access.get_group(row.team_id, entry.principal_id)
            except Exception:
                logger.warning("kb.knowledge_base_group_unavailable team_id=%s", row.team_id, exc_info=True)
        members: set[str] | None = None
        if group is not None and _group_applies(group, row):
            try:
                members = {
                    str(member.principal_id) for member in await access.list_group_members(row.team_id, group.id) if member.principal_type == "user"
                }
            except Exception:
                logger.warning("kb.knowledge_base_group_members_unavailable team_id=%s", row.team_id, exc_info=True)
                members = set()
        out[entry.principal_id] = (group, members)
    return out


def _group_applies(group: Any, row: KnowledgeBaseRow) -> bool:
    """C8: active groups only; Space knowledge bases take Team-wide and that Space's groups, Team ones Team-wide groups."""
    if group is None or getattr(group, "status", "") != "active":
        return False
    # GroupRecord reads a Team-wide group's empty Space as None.
    bound = getattr(group, "workspace_id", None) or ""
    if row.home == "space":
        return bound in ("", row.home_id)
    return row.home == "team" and bound == ""


async def _space_members(request: Request, space_id: str) -> list[dict[str, Any]]:
    users = request.app.state.user_store
    try:
        return list(await users.list_members(space_id))
    except Exception:
        logger.warning("kb.knowledge_base_space_members_unavailable", exc_info=True)
        return []


async def _people_levels(request: Request, row: KnowledgeBaseRow, entries: list[KnowledgeBaseMemberRow]) -> dict[str, int]:
    """Everyone who can use a knowledge base, with their level, by the resolver's own rules.

    Group members count only while they are active members of an applicable group, and on a
    Space knowledge base only while they are members of that Space (Q2). A Space entry on a
    Team knowledge base stands for that Space's members (C9).
    """
    if row.home == "personal":
        return {row.home_id: KNOWLEDGE_OWNER}
    users = request.app.state.user_store
    team = row.team_id
    active = {str(member["user_id"]) for member in await users.list_active_team_members_for_governance(team)}
    admins = set(await users.list_team_users_by_roles(team, {"owner", "team_admin"}))
    by_user = {entry.principal_id: LEVEL_RANKS[entry.level] for entry in entries if entry.principal_type == "user"}
    groups = await _groups(request, row, entries)
    grouped = [(LEVEL_RANKS[entry.level], groups[entry.principal_id][1] or set()) for entry in entries if entry.principal_type == "group"]
    spaced: list[tuple[int, set[str]]] = []
    if row.home == "team":
        for entry in entries:
            if entry.principal_type == "space" and await _space_in_team(request, entry.principal_id, team):
                spaced.append((LEVEL_RANKS[entry.level], {str(member["user_id"]) for member in await _space_members(request, entry.principal_id)}))

    def listed(user_id: str) -> int:
        return max([by_user.get(user_id, 0), *(level for level, members in (*grouped, *spaced) if user_id in members)])

    out: dict[str, int] = {}
    if row.home == "space":
        for member in await _space_members(request, row.home_id):
            user_id = str(member["user_id"])
            if user_id not in active:
                continue
            caller = KnowledgeCaller(
                user_id=user_id,
                team_id=team,
                space_id=row.home_id,
                space_role=normalize_workspace_role(member.get("role")) or "",
                is_team_admin=user_id in admins,
            )
            level = knowledge_level(caller, "space", row.is_default, row.audience, row.audience_level, row.owner_user_id, listed(user_id))
            if level > 0:
                out[user_id] = level
        return out
    if row.audience == "team":
        candidates = active
    else:
        candidates = set(by_user) | admins | {row.owner_user_id or ""}
        for _, members in (*grouped, *spaced):
            candidates |= members
        candidates &= active
    for user_id in candidates:
        caller = KnowledgeCaller(user_id=user_id, team_id=team, is_team_admin=user_id in admins)
        level = knowledge_level(caller, "team", row.is_default, row.audience, row.audience_level, row.owner_user_id, listed(user_id))
        if level > 0:
            out[user_id] = level
    return out


async def _space_in_team(request: Request, space_id: str, team_id: str) -> dict[str, Any] | None:
    users = request.app.state.user_store
    try:
        space = await users.get_workspace(space_id)
    except Exception:
        return None
    return space if space and space.get("team_id") == team_id and space.get("status") == "active" else None


def _summary(people: dict[str, int]) -> dict[str, int]:
    """ "N can use, M can edit, K can manage": each person once, at their highest level."""
    counts = {"use": 0, "edit": 0, "manage": 0}
    for level in people.values():
        counts[_LEVEL_OUT[min(level, KNOWLEDGE_OWNER)]] += 1
    return {**counts, "people": len(people)}


async def _members_out(
    request: Request, ctx: ScopeContext, row: KnowledgeBaseRow, level: int, entries: list[KnowledgeBaseMemberRow]
) -> dict[str, Any]:
    users = request.app.state.user_store
    groups = await _groups(request, row, entries)
    people = await _people_levels(request, row, entries)
    space_roles: dict[str, str] = {}
    if row.home == "space":
        space_roles = {
            str(member["user_id"]): normalize_workspace_role(member.get("role")) or "" for member in await _space_members(request, row.home_id)
        }
    automatic: list[tuple[str, str]] = []
    if row.home == "space":
        admins = set(await users.list_team_users_by_roles(row.team_id, {"owner", "team_admin"})) if row.is_default else set()
        automatic = [(user_id, "space_admin") for user_id, role in space_roles.items() if role == "ws_admin"]
        automatic += [(user_id, "team_admin") for user_id in sorted(admins) if user_id in space_roles and space_roles[user_id] != "ws_admin"]
    elif row.home == "team":
        automatic = [(user_id, "team_admin") for user_id in sorted(await users.list_team_users_by_roles(row.team_id, {"owner", "team_admin"}))]
    automatic = [(user_id, reason) for user_id, reason in automatic if user_id != row.owner_user_id and user_id in people]
    user_ids = [row.owner_user_id or "", *(user_id for user_id, _ in automatic), *(e.principal_id for e in entries if e.principal_type == "user")]
    names = await _labels(request, row.team_id, user_ids)
    out_entries = []
    for entry in entries:
        label, state = None, "active"
        if entry.principal_type == "user":
            label = names.get(entry.principal_id)
            if label is None:
                state = "not_in_team"
            elif row.home == "space" and entry.principal_id not in space_roles:
                # Q2: the entry applies again when the person rejoins the Space.
                state = "not_in_space"
        elif entry.principal_type == "group":
            group, members = groups.get(entry.principal_id, (None, None))
            label = getattr(group, "name", None)
            state = (
                "active"
                if members is not None
                else "group_removed"
                if group is None or getattr(group, "status", "") != "active"
                else "not_applicable"
            )
        else:
            space = await _space_in_team(request, entry.principal_id, row.team_id)
            label = space.get("name") if space else None
            state = "active" if space and row.home == "team" else "space_removed"
        out_entries.append(
            {
                "principal_type": entry.principal_type,
                "principal_id": entry.principal_id,
                "label": label,
                "level": entry.level,
                "state": state,
                "added_by": entry.added_by,
                "created_at": entry.created_at,
            }
        )
    return {
        "knowledge_base_id": row.id,
        "audience": row.audience,
        "audience_level": row.audience_level,
        "owner": {"user_id": row.owner_user_id, "name": names.get(row.owner_user_id or "")} if row.owner_user_id else None,
        "automatic_managers": [{"user_id": user_id, "name": names.get(user_id), "reason": reason} for user_id, reason in automatic],
        "entries": out_entries,
        "summary": _summary(people),
        "can_manage": level >= KNOWLEDGE_MANAGE,
    }


# ─── Requests ────────────────────────────────────────────────────────────


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    home: Literal["personal", "space", "team"]
    audience: Literal["owner", "members", "space", "team"]
    audience_level: Literal["use", "edit"] = "use"


class KnowledgeBasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    audience: Literal["owner", "members", "space", "team"] | None = None
    audience_level: Literal["use", "edit"] | None = None


class MemberChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_type: Literal["user", "group", "space"]
    principal_id: str = Field(min_length=1, max_length=128)
    # None removes the entry.
    level: Level | None


class MembersPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[MemberChange] = Field(min_length=1, max_length=MAX_MEMBER_CHANGES)


class TransferBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    revision: int | None = Field(default=None, ge=1)


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str] = Field(min_length=1, max_length=500)
    target_knowledge_base_id: str = Field(min_length=1, max_length=64)
    dry_run: bool = False
    reason: str | None = Field(default=None, max_length=500)


def _conflict(exc: KnowledgeBaseConflict, row: KnowledgeBaseRow | None = None) -> HTTPException:
    kb = _kb()
    if exc.code == "knowledge_base_not_found":
        return kb._knowledge_base_not_found()
    if exc.code == "knowledge_base_files_changed":
        return kb._kb_error(
            409,
            exc.code,
            file_count=exc.files,
            hint="Files were added to or removed from this knowledge base meanwhile. Check the new file count and confirm again.",
        )
    if exc.code == "knowledge_base_member_limit":
        return kb._kb_error(409, exc.code, hint="This knowledge base has too many member entries. Add a group or a Space instead of single people.")
    return kb._kb_error(
        409,
        "knowledge_base_revision_conflict",
        hint="Someone changed this knowledge base meanwhile. Reload it and make your change again.",
    )


# ─── Directory and creation ──────────────────────────────────────────────


@router.get("")
async def list_knowledge_bases(request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    """The usable knowledge bases of My, the current Space and the Team, with the caller's level."""
    kb = _kb()
    ctx, role = await _context(request, user)
    levels = _levels(ctx)
    homes = {"personal": user.id, "space": ctx.workspace_id, "team": ctx.team_id}
    # Enforced scope read grants narrow the Space and Team sections, as they do the file table (Q10).
    listable = {
        "personal": True,
        "space": bool(ctx.workspace_id) and await kb._kb_scope_listable(request, ctx, scope="workspace", scope_id=ctx.workspace_id),
        "team": await kb._kb_scope_listable(request, ctx, scope="team", scope_id=ctx.team_id),
    }
    usable = [identity for identity in levels if (home := levels.home_of(identity)) and listable.get(home[0]) and homes.get(home[0]) == home[1]]
    rows = await _store(request).get_many(usable)
    counts = await _store(request).file_counts(ctx.team_id, [(home, home_id) for home, home_id in homes.items() if home_id and listable[home]])
    sections: dict[str, list[KnowledgeBaseRow]] = {section: [] for section in _SECTIONS}
    for identity in usable:
        home, home_id = levels.home_of(identity)
        row = rows.get(identity) or (
            _default_row(ctx.team_id, home, home_id) if identity == default_knowledge_base_id(ctx.team_id, home, home_id) else None
        )
        if row is None or row.deleted_at or row.team_id != ctx.team_id:
            continue
        sections[home].append(row)
    names = await _labels(request, ctx.team_id, [row.owner_user_id or "" for rows_ in sections.values() for row in rows_])
    out = {
        section: [
            _out(row, int(levels[row.id]), counts.get(row.id), names)
            for row in sorted(rows_, key=lambda item: (not item.is_default, (item.name or "").casefold(), item.id))
        ]
        for section, rows_ in sections.items()
    }
    return {
        "space_id": ctx.workspace_id or None,
        "sections": out,
        "can_create": {
            "personal": True,
            "space": bool(ctx.workspace_id) and role in ("ws_admin", "member"),
            "team": ctx.role == "team_admin",
        },
    }


@router.post("", status_code=201, dependencies=[Depends(require_not_readonly)])
async def create_knowledge_base(body: KnowledgeBaseCreate, request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    """Create a knowledge base in My, the current Space or the Team (PRD §3 creation rights)."""
    kb = _kb()
    ctx, role = await _context(request, user, write=True)
    _levels(ctx)
    home_id = {"personal": user.id, "space": ctx.workspace_id, "team": ctx.team_id}[body.home]
    if body.home == "space":
        kb._require_workspace_context(ctx)
        if role not in ("ws_admin", "member"):
            raise kb._kb_error(
                403,
                "knowledge_base_create_forbidden",
                home="space",
                hint="Space viewers can't create Space knowledge bases. Ask a Space administrator to change your role or to create it.",
            )
    elif body.home == "team" and ctx.role != "team_admin":
        raise kb._kb_error(
            403,
            "knowledge_base_create_forbidden",
            home="team",
            hint="Only the Team owner and Team administrators can create Team knowledge bases. Create it in your Space, or ask a Team administrator.",
        )
    if body.home in ("space", "team"):
        await kb._require_kb_scope_grant(request, ctx, scope="workspace" if body.home == "space" else "team", scope_id=home_id, permission="write")
    audience, audience_level = _audience(body.home, body.audience, body.audience_level)
    description = (body.description or "").strip() or None
    row = await _store(request).create(
        team_id=ctx.team_id,
        home=body.home,
        home_id=home_id,
        name=_clean_name(body.name),
        description=description,
        audience=audience,
        audience_level=audience_level,
        owner_user_id=user.id,
    )
    emit_audit("kb.knowledge_base_created", {"knowledge_base_id": row.id, "home": row.home, "audience": row.audience, "user_id": user.id})
    return _out(row, KNOWLEDGE_OWNER, None, await _labels(request, ctx.team_id, [user.id]))


# ─── Moving files between knowledge bases ───────────────────────────────


def _target_scope(target: KnowledgeBaseRow) -> tuple[str, str]:
    return _file_scope(target)


async def _move_precheck(request: Request, ctx: ScopeContext, file: KbFileDTO, target: KnowledgeBaseRow) -> None:
    """Move rights (PRD §3): your own files need edit where they are, others' manage, and the target edit."""
    kb = _kb()
    new_scope, new_scope_id = _target_scope(target)
    if not KbPermission.can_change_scope(file, ctx, new_scope, new_scope_id, target_knowledge_base_id=target.id):
        raise kb._kb_error(403, "kb_file_access_denied")
    await kb._require_kb_scope_grant(request, ctx, scope=file.scope, scope_id=file.scope_id, permission="write")
    await kb._require_kb_file_grant(request, ctx, file, permission="write")
    await kb._require_kb_scope_grant(request, ctx, scope=new_scope, scope_id=new_scope_id, permission="write")


async def _move_one(request: Request, ctx: ScopeContext, user: User, file: KbFileDTO, target: KnowledgeBaseRow, reason: str | None) -> None:
    kb = _kb()
    files_store = kb._get_files_store(request)
    source = knowledge_base_key(file)
    if source == target.id:
        return
    await _move_precheck(request, ctx, file, target)
    new_scope, new_scope_id = _target_scope(target)
    if home_for_scope(file.scope, file.scope_id) != (target.home, target.home_id):
        # Across collections: copy the chunks with their embeddings and compensate on failure.
        body = kb.KbFileScopePatchRequest(new_scope=new_scope, new_scope_id=new_scope_id, reason=reason)
        await kb._change_file_scope_core(file.file_id, body, ctx, request, user, target_knowledge_base_id=target.id, file=file)
        return
    # Within one home: a metadata change on the file and its chunks; nothing is re-embedded.
    if not await files_store.set_knowledge_base(file.file_id, target.id, expected=file.knowledge_base_id):
        raise kb._kb_error(409, "knowledge_base_move_conflict", hint="The file changed meanwhile. Reload the list and move it again.")
    moved = await files_store.get(file.file_id)
    kb_store = await kb._get_effective_kb_store(request, user)
    try:
        if moved is not None:
            await kb_store.set_chunks_knowledge_base(moved, target.id)
    except Exception as exc:
        logger.exception("kb.knowledge_base_move_chunks_failed file_id=%s", file.file_id)
        try:
            await files_store.set_knowledge_base(file.file_id, source, expected=target.id)
        except Exception:
            logger.exception("kb.knowledge_base_move_rollback_failed file_id=%s", file.file_id)
            emit_audit("kb.knowledge_base_move_rollback_failed", {"file_id": file.file_id})
        raise kb._kb_error(500, "kb_scope_change_failed") from exc
    emit_audit(
        "kb.knowledge_base_moved",
        {"file_id": file.file_id, "from": source, "to": target.id, "moved_by": user.id, "original_uploader": file.uploaded_by, "reason": reason},
    )


async def _source_rows(request: Request, ctx: ScopeContext, files: list[KbFileDTO]) -> dict[str, KnowledgeBaseRow]:
    ids = {knowledge_base_key(file) for file in files}
    rows = await _store(request).get_many(ids)
    for file in files:
        identity = knowledge_base_key(file)
        if identity not in rows:
            home, home_id = home_for_scope(file.scope, file.scope_id)
            if identity == default_knowledge_base_id(file.team_id, home, home_id):
                rows[identity] = _default_row(file.team_id, home, home_id)
    return rows


async def _audience_of(request: Request, row: KnowledgeBaseRow, cache: dict[str, set[str]]) -> set[str]:
    """The people who can use a knowledge base now."""
    if row.id not in cache:
        cache[row.id] = set(await _people_levels(request, row, await _store(request).members(row.id)))
    return cache[row.id]


@router.post("/files/move", dependencies=[Depends(require_not_readonly)])
async def move_files(body: MoveBody, request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    """Move files to another knowledge base; ``dry_run`` previews who gains and who loses access.

    Today's ``PATCH /files/{id}/scope`` stays and maps to the target home's default.
    """
    kb = _kb()
    ctx, _ = await _context(request, user, write=True)
    files_store = kb._get_files_store(request)
    target, _ = await kb._knowledge_base_check(body.target_knowledge_base_id, ctx, files_store, level=KNOWLEDGE_EDIT)
    ids = kb._dedupe_ids(body.file_ids)
    results: list[dict[str, Any]] = []
    checked: list[KbFileDTO] = []
    unchanged = 0
    for file_id in ids:
        try:
            file = await kb._kb_scope_check(file_id, ctx, files_store, request)
            if knowledge_base_key(file) == target.id:
                unchanged += 1
                results.append({"file_id": file_id, "ok": True, "unchanged": True})
                continue
            if body.dry_run:
                await _move_precheck(request, ctx, file, target)
            else:
                await _move_one(request, ctx, user, file, target, body.reason)
            checked.append(file)
            results.append({"file_id": file_id, "ok": True})
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.append({"file_id": file_id, "ok": False, "error_code": (detail.get("error") or {}).get("code") or "kb_file_operation_failed"})
    succeeded = sum(1 for item in results if item["ok"])
    out: dict[str, Any] = {
        "dry_run": body.dry_run,
        "target_knowledge_base_id": target.id,
        "results": results,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "unchanged": unchanged,
    }
    if body.dry_run:
        cache: dict[str, set[str]] = {}
        after = await _audience_of(request, target, cache)
        sources = await _source_rows(request, ctx, checked)
        gains: set[str] = set()
        losses: set[str] = set()
        for file in checked:
            row = sources.get(knowledge_base_key(file))
            before = {file.scope_id} if file.scope == "personal" else await _audience_of(request, row, cache) if row is not None else set()
            gains |= after - before
            losses |= before - after
        out.update({"movable": len(checked), "gains": len(gains), "losses": len(losses)})
    return out


# ─── One knowledge base ─────────────────────────────────────────────────


@router.get("/{knowledge_base_id}")
async def get_knowledge_base(knowledge_base_id: str, request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    kb = _kb()
    ctx, _ = await _context(request, user)
    row, level = await kb._knowledge_base_check(knowledge_base_id, ctx, kb._get_files_store(request))
    return await _detail(request, ctx, row, level)


@router.patch("/{knowledge_base_id}", dependencies=[Depends(require_not_readonly)])
async def patch_knowledge_base(
    knowledge_base_id: str, body: KnowledgeBasePatch, request: Request, user: User = Depends(_current_user)
) -> dict[str, Any]:
    """Name, description, audience and default level; a default changes nothing but its fixed level (C6)."""
    kb = _kb()
    ctx, _ = await _context(request, user, write=True)
    row, level = await kb._knowledge_base_check(knowledge_base_id, ctx, kb._get_files_store(request), level=KNOWLEDGE_MANAGE)
    sent = body.model_fields_set - {"revision"}
    if row.is_default and (sent - {"audience_level"} or (body.audience_level is not None and body.audience_level != row.audience_level)):
        raise kb._kb_error(
            422,
            "knowledge_base_default_fixed",
            hint="Default knowledge bases keep their name, audience and default level. Add people, groups or Spaces to give more rights.",
        )
    changes: dict[str, Any] = {}
    if "name" in sent and body.name is not None:
        changes["name"] = _clean_name(body.name)
    if "description" in sent:
        changes["description"] = (body.description or "").strip() or None
    if not row.is_default and ({"audience", "audience_level"} & sent):
        audience, audience_level = _audience(row.home, body.audience or row.audience, body.audience_level or row.audience_level)
        changes.update({"audience": audience, "audience_level": audience_level})
    changes = {key: value for key, value in changes.items() if getattr(row, key) != value}
    if body.revision != row.revision:
        raise _conflict(KnowledgeBaseConflict("knowledge_base_revision_conflict"))
    if not changes:
        return await _detail(request, ctx, row, level)
    try:
        updated = await _store(request).update(row, expected_revision=body.revision, changes=changes)
    except KnowledgeBaseConflict as exc:
        raise _conflict(exc) from None
    emit_audit("kb.knowledge_base_updated", {"knowledge_base_id": row.id, "fields": sorted(changes), "user_id": user.id})
    return await _detail(request, ctx, updated, level)


@router.delete("/{knowledge_base_id}", dependencies=[Depends(require_not_readonly)])
async def delete_knowledge_base(
    knowledge_base_id: str,
    request: Request,
    file_count: int = Query(ge=0),
    revision: int | None = Query(default=None, ge=1),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Delete a knowledge base: its files leave knowledge and their originals stay in Files (Q5).

    The owner or an automatic manager confirms with the current file count; defaults stay.
    """
    kb = _kb()
    ctx, _ = await _context(request, user, write=True)
    row, _ = await kb._knowledge_base_check(knowledge_base_id, ctx, kb._get_files_store(request), level=KNOWLEDGE_OWNER)
    if row.is_default:
        raise kb._kb_error(
            409, "knowledge_base_default_not_deletable", hint="Default knowledge bases can't be deleted. Remove or move their files instead."
        )
    if revision is not None and revision != row.revision:
        raise _conflict(KnowledgeBaseConflict("knowledge_base_revision_conflict"))
    store = _store(request)
    files = await store.active_file_ids(row.id)
    if len(files) != file_count:
        raise _conflict(KnowledgeBaseConflict("knowledge_base_files_changed", files=len(files)))
    removed, failed = 0, []
    for file_id in files:
        try:
            # Today's "Remove from knowledge base": the membership retires, the original stays in Files.
            await kb._delete_file_core(file_id, ctx, request, user)
            removed += 1
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            failed.append({"file_id": file_id, "error_code": (detail.get("error") or {}).get("code") or "kb_file_operation_failed"})
    if failed:
        raise kb._kb_error(
            409,
            "knowledge_base_files_remaining",
            removed=removed,
            remaining=len(failed),
            failures=failed[:20],
            hint="Some files could not leave the knowledge base, so it was kept. Check access to those files and delete it again.",
        )
    try:
        await store.delete(row.id, expected_revision=row.revision)
    except KnowledgeBaseConflict as exc:
        raise _conflict(exc) from None
    emit_audit("kb.knowledge_base_deleted", {"knowledge_base_id": row.id, "files_removed": removed, "user_id": user.id})
    return {"deleted": True, "knowledge_base_id": row.id, "files_removed": removed}


@router.get("/{knowledge_base_id}/members")
async def get_knowledge_base_members(knowledge_base_id: str, request: Request, user: User = Depends(_current_user)) -> dict[str, Any]:
    """Who has access: the audience, the owner, automatic managers, the member list and the summary."""
    kb = _kb()
    ctx, _ = await _context(request, user)
    row, level = await kb._knowledge_base_check(knowledge_base_id, ctx, kb._get_files_store(request))
    entries = await _store(request).members(row.id)
    return await _members_out(request, ctx, row, level, entries)


async def _validate_change(request: Request, row: KnowledgeBaseRow, change: MemberChange, space_members: set[str] | None) -> None:
    """Pickers' rules: Space knowledge bases take that Space's members (Q2) and applicable groups; Team ones people, Team-wide groups and Spaces."""
    kb = _kb()

    def refuse(reason: str, hint: str) -> HTTPException:
        return kb._kb_error(
            422, "knowledge_base_member_invalid", principal_type=change.principal_type, principal_id=change.principal_id, reason=reason, hint=hint
        )

    users = request.app.state.user_store
    if change.principal_type == "user":
        if change.principal_id == row.owner_user_id:
            raise refuse("owner", "The owner already manages this knowledge base.")
        if await users.get_team_role(change.principal_id, row.team_id) is None:
            raise refuse("not_in_team", "Choose a member of this Team.")
        if space_members is not None and change.principal_id not in space_members:
            raise refuse(
                "not_in_space",
                "A Space knowledge base can be shared only with members of that Space. Use a Team knowledge base to share across Spaces.",
            )
        return
    if change.principal_type == "group":
        access = getattr(request.app.state, "access_control_store", None)
        group = await access.get_group(row.team_id, change.principal_id) if access is not None else None
        if group is None or getattr(group, "status", "") != "active":
            raise refuse("group_inactive", "This group is not active. Choose an active group.")
        if not _group_applies(group, row):
            raise refuse(
                "group_not_applicable",
                "Choose a Team-wide group or a group of this Space."
                if row.home == "space"
                else "Choose a Team-wide group; Space groups apply only in their Space.",
            )
        return
    if row.home != "team":
        raise refuse("space_entries_team_only", "Spaces can be added only to Team knowledge bases.")
    if await _space_in_team(request, change.principal_id, row.team_id) is None:
        raise refuse("space_not_found", "Choose an active Space of this Team.")


@router.put("/{knowledge_base_id}/members", dependencies=[Depends(require_not_readonly)])
async def put_knowledge_base_members(
    knowledge_base_id: str, body: MembersPut, request: Request, user: User = Depends(_current_user)
) -> dict[str, Any]:
    """Add, change or remove member entries; a ``null`` level removes one. Changes apply at once."""
    kb = _kb()
    ctx, _ = await _context(request, user, write=True)
    row, level = await kb._knowledge_base_check(knowledge_base_id, ctx, kb._get_files_store(request), level=KNOWLEDGE_MANAGE)
    if row.home == "personal":
        raise kb._kb_error(
            422,
            "knowledge_base_not_shareable",
            hint="My knowledge bases are only yours. To share files, move them to a Space or Team knowledge base.",
        )
    changes: dict[tuple[str, str], MemberChange] = {}
    for change in body.changes:
        changes[(change.principal_type, change.principal_id)] = change
    space_members = {str(member["user_id"]) for member in await _space_members(request, row.home_id)} if row.home == "space" else None
    for change in changes.values():
        if change.level is not None:
            await _validate_change(request, row, change, space_members)
    try:
        entries = await _store(request).apply_member_changes(
            row, [(change.principal_type, change.principal_id, change.level) for change in changes.values()], added_by=user.id
        )
    except KnowledgeBaseConflict as exc:
        raise _conflict(exc) from None
    emit_audit("kb.knowledge_base_members_changed", {"knowledge_base_id": row.id, "changes": len(changes), "user_id": user.id})
    return await _members_out(request, ctx, row, level, entries)


@router.post("/{knowledge_base_id}/transfer", dependencies=[Depends(require_not_readonly)])
async def transfer_knowledge_base(
    knowledge_base_id: str, body: TransferBody, request: Request, user: User = Depends(_current_user)
) -> dict[str, Any]:
    """Make another manager the owner; the previous owner stays as a manager while they still qualify."""
    kb = _kb()
    ctx, _ = await _context(request, user, write=True)
    row, level = await kb._knowledge_base_check(knowledge_base_id, ctx, kb._get_files_store(request), level=KNOWLEDGE_OWNER)
    if row.is_default or row.home == "personal":
        raise kb._kb_error(
            422,
            "knowledge_base_not_transferable",
            hint="Default and My knowledge bases have no owner to transfer.",
        )
    if body.revision is not None and body.revision != row.revision:
        raise _conflict(KnowledgeBaseConflict("knowledge_base_revision_conflict"))
    if body.user_id == row.owner_user_id:
        return await _detail(request, ctx, row, level)
    store = _store(request)
    people = await _people_levels(request, row, await store.members(row.id))
    if people.get(body.user_id, 0) < KNOWLEDGE_MANAGE:
        raise kb._kb_error(
            422,
            "knowledge_base_transfer_target_invalid",
            hint="Ownership can go only to someone who already manages this knowledge base. Give them manage access first.",
        )
    try:
        updated = await store.transfer(
            row,
            new_owner=body.user_id,
            expected_revision=row.revision,
            keep_previous_owner=bool(row.owner_user_id) and row.owner_user_id in people,
            added_by=user.id,
        )
    except KnowledgeBaseConflict as exc:
        raise _conflict(exc) from None
    emit_audit("kb.knowledge_base_transferred", {"knowledge_base_id": row.id, "from": row.owner_user_id, "to": body.user_id, "user_id": user.id})
    # The caller's level changes with ownership (a previous owner who is no automatic manager now manages).
    ctx, _ = await _context(request, user)
    updated, level = await kb._knowledge_base_check(updated.id, ctx, kb._get_files_store(request))
    return await _detail(request, ctx, updated, level)


@router.get("/{knowledge_base_id}/files")
async def list_knowledge_base_files(
    knowledge_base_id: str,
    request: Request,
    limit: int = 200,
    offset: int = 0,
    cursor: str | None = None,
    since: str | None = None,
    file_ids: list[str] | None = Query(default=None, max_length=100),
    status: str | None = None,
    mime: str | None = None,
    doc_type_id: str | None = None,
    uploaded_after: str | None = None,
    uploaded_before: str | None = None,
    q: str | None = None,
    order_by: str = "uploaded_at",
    order_dir: str = "desc",
    user: User = Depends(_current_user),
):
    """One knowledge base's file table with today's filters, sort, paging and flags (deep search: ``POST /kb/search``)."""
    kb = _kb()
    ctx, _ = await _context(request, user)
    files_store = kb._get_files_store(request)
    row, _ = await kb._knowledge_base_check(knowledge_base_id, ctx, files_store)
    scope, scope_id = _file_scope(row)
    if scope in ("space", "team"):
        await kb._require_kb_scope_grant(request, ctx, scope="workspace" if scope == "space" else "team", scope_id=scope_id, permission="read")
    safe_limit = max(1, min(int(limit), 1000))
    safe_offset = max(0, int(offset))
    sort = kb._list_order(order_by, order_dir)
    if cursor is not None and safe_offset:
        raise kb._kb_error(400, "invalid_kb_pagination")
    cursor_key = kb._decode_list_cursor(cursor, *sort) if cursor is not None else None
    filters = kb._file_list_filters(
        status=status, mime=mime, doc_type_id=doc_type_id, uploaded_after=uploaded_after, uploaded_before=uploaded_before, q=q, sort=sort
    )
    rows = await files_store.list_for_scope_filtered(scope, scope_id, **filters, knowledge_base_id=row.id, team_id=ctx.team_id)
    return await kb._file_list_page(
        request,
        ctx,
        rows,
        listed_scopes=[scope],
        file_ids=file_ids,
        since=since,
        cursor_key=cursor_key,
        offset=safe_offset,
        limit=safe_limit,
        sort=sort,
    )
