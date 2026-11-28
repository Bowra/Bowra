"""Knowledge base grant report (organization phase 1, slice 6), behind ``FEATURE_KNOWLEDGE_BASES``.

A read-only view for the people who manage resource grants on ``/admin/access/``: every unrevoked
``kb_scope`` and ``kb_file`` grant of the Team, who it applies to, its target, what it restricts
today and how it would map to knowledge base members. Phase 1 keeps applying these grants after
the knowledge base rule as extra narrowing (PRD Q10); they never grant access. The mapping is a
suggestion only: nothing here changes a grant, a knowledge base or a member list.

Names follow the caller's own read rights. People, groups and Spaces of the Team are named. A
file and the knowledge base it is in are named only when the caller can open the file through the
current KB check (``kb_file_authorizer``), and a Space's named knowledge bases only when the
caller can use them: Team owners and admins do not automatically read Space knowledge bases
(PRD Q6), and a members-only knowledge base stays undisclosed (C4).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.access_control import permission_satisfies, resource_grants_enforce_enabled
from bowhead_core.kb.knowledge_base_store import DEFAULT_AUDIENCE, KnowledgeBaseRow, KnowledgeBaseStore, default_knowledge_base_id
from pydantic import BaseModel, Field

from bowhead_gateway.kb_authorization import kb_file_authorizer

if TYPE_CHECKING:
    from bowhead_core.access_control import AccessControlStore, ResourceGrantRecord
    from bowhead_core.kb.schemas import KbFileDTO
    from bowhead_core.user_store import User, UserStore

logger = logging.getLogger(__name__)

REPORT_KINDS = ("kb_scope", "kb_file")
#: Knowledge bases listed per grant; ``more`` counts the rest the caller can see.
MAX_LISTED_KNOWLEDGE_BASES = 20
_ACTIONS = ("read", "write")

Home = Literal["personal", "space", "team"]
PrincipalState = Literal["active", "not_in_team", "group_removed", "space_archived", "not_found"]
TargetState = Literal["active", "hidden", "deleted", "not_found", "space_archived", "unrecognized", "unavailable"]
Action = Literal["read", "write"]


class GrantReportKnowledgeBaseDTO(BaseModel):
    """A knowledge base the caller may see; defaults have no name and are named after their home."""

    id: str
    is_default: bool
    home: Home
    home_id: str
    name: str | None = None
    # The Space's name for Space homes, so the page can name a Space's default knowledge base.
    home_name: str | None = None
    audience: str
    audience_level: str


class GrantReportPrincipalDTO(BaseModel):
    """Who the grant applies to, as stored: a person, a group, a Space's members or the whole Team."""

    type: str
    id: str
    # Person, group or Space name; None when it does not resolve (the Team principal has no label).
    label: str | None = None
    state: PrincipalState = "active"
    # Groups only: the Space a Space-bound group belongs to, and its active member count.
    space_name: str | None = None
    member_count: int | None = None


class GrantReportTargetDTO(BaseModel):
    """What the grant is on: a home's knowledge files (``kb_scope``) or one knowledge file (``kb_file``)."""

    kind: Literal["kb_scope", "kb_file"]
    home: Home | None = None
    space_name: str | None = None
    # Files only, and only when the caller can open the file.
    file_name: str | None = None
    state: TargetState = "active"
    # The knowledge base a file is in, when the caller can open the file.
    knowledge_base: GrantReportKnowledgeBaseDTO | None = None


class GrantReportRestrictionDTO(BaseModel):
    """What the grant restricts today, before the enforcement setting is applied.

    ``allow_list``: once any allow grant is on a target, only principals with an allow grant
    covering the request get through; ``actions`` is what this principal keeps. ``deny``: the
    principal loses ``actions``. ``none``: no effect (``reason``).
    """

    kind: Literal["allow_list", "deny", "none"]
    actions: list[Action] = Field(default_factory=list)
    reason: Literal["expired", "permission_not_checked"] | None = None


class GrantReportMappingDTO(BaseModel):
    """How the grant would map to knowledge base members. A suggestion; nothing is applied.

    ``needs_level``: the principal would need ``level`` on ``knowledge_bases``. ``keep_out``: member
    lists cannot deny, so keeping the principal out needs a members-only knowledge base that does
    not list it. ``whole_home``: a scope grant covers every knowledge base of its home, including
    any the caller cannot see. ``open_audience``: a listed knowledge base lets its whole Space or
    Team in, so keeping others out would need a members-only knowledge base.
    """

    action: Literal["needs_level", "keep_out", "none"]
    level: Literal["use", "edit"] | None = None
    knowledge_bases: list[GrantReportKnowledgeBaseDTO] = Field(default_factory=list)
    more: int = 0
    whole_home: bool = False
    open_audience: bool = False
    reason: Literal["expired", "permission_not_checked", "target_unavailable", "principal_unavailable", "personal"] | None = None


class GrantReportItemDTO(BaseModel):
    grant_id: str
    effect: str
    permission: str
    expires_at: str | None = None
    expired: bool = False
    created_at: str
    applies_to: GrantReportPrincipalDTO
    target: GrantReportTargetDTO
    restriction: GrantReportRestrictionDTO
    mapping: GrantReportMappingDTO


class GrantReportDTO(BaseModel):
    # BOWHEAD_RESOURCE_GRANTS_ENFORCE=1: refusals apply now; otherwise they are only logged.
    enforced: bool
    items: list[GrantReportItemDTO]
    total: int
    next_cursor: str | None = None


class InvalidCursorError(ValueError):
    """The ``cursor`` query value is not one this report issued."""


def encode_cursor(grant: ResourceGrantRecord) -> str:
    raw = json.dumps([grant.created_at, grant.id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> tuple[str, str] | None:
    """The ``(created_at, id)`` keyset of a cursor; None for the first page."""
    if not value:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError(value) from exc
    if not (isinstance(decoded, list) and len(decoded) == 2 and all(isinstance(part, str) and part for part in decoded)):
        raise InvalidCursorError(value)
    return decoded[0], decoded[1]


def _actions(permission: str) -> list[Action]:
    """The KB checks (read, write) a stored grant permission covers; ``use`` covers neither."""
    return [action for action in _ACTIONS if permission_satisfies(permission, action)]


def _expired(grant: ResourceGrantRecord, now: str) -> bool:
    # The decision SQL compares the stored text: active while ``expires_at > now``.
    return bool(grant.expires_at) and not str(grant.expires_at) > now


def _scope_home(resource_id: str, team_id: str) -> tuple[Home, str] | None:
    """The home a ``kb_scope`` resource names, as the runtime checks it; None when never checked."""
    prefix, _, value = resource_id.partition(":")
    if prefix == "workspace" and value:
        return "space", value
    if prefix == "team" and value == team_id:
        return "team", value
    return None


def _file_home(file: KbFileDTO) -> Home:
    return "space" if file.scope in ("space", "workspace") else "team" if file.scope == "team" else "personal"


def _default_ref(team_id: str, home: Home, home_id: str, space_names: dict[str, str]) -> GrantReportKnowledgeBaseDTO:
    audience, level = DEFAULT_AUDIENCE[home]
    return GrantReportKnowledgeBaseDTO(
        id=default_knowledge_base_id(team_id, home, home_id),
        is_default=True,
        home=home,
        home_id=home_id,
        home_name=space_names.get(home_id) if home == "space" else None,
        audience=audience,
        audience_level=level,
    )


def _row_ref(row: KnowledgeBaseRow, space_names: dict[str, str]) -> GrantReportKnowledgeBaseDTO:
    return GrantReportKnowledgeBaseDTO(
        id=row.id,
        is_default=row.is_default,
        home=row.home,
        home_id=row.home_id,
        name=None if row.is_default else row.name,
        home_name=space_names.get(row.home_id) if row.home == "space" else None,
        audience=row.audience,
        audience_level=row.audience_level,
    )


class _Resolver:
    """Names and visibility for one report page, with one read per distinct principal, Space and context."""

    def __init__(self, request: Any, user: User, access_store: AccessControlStore, user_store: UserStore) -> None:
        self.request = request
        self.user = user
        self.team_id = user.team_id
        self.access_store = access_store
        self.user_store = user_store
        self.files_store = getattr(request.app.state, "kb_files_store", None)
        db_path = getattr(self.files_store, "_db_path", None)
        self.knowledge_bases = KnowledgeBaseStore(db_path) if db_path else None
        self.authorizer = kb_file_authorizer(user_store, access_store, files_store=self.files_store)
        self.spaces: dict[str, dict[str, Any] | None] = {}
        self.space_names: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.active_users: set[str] = set()
        self.groups: dict[str, Any] = {}
        self.files: dict[str, KbFileDTO] = {}
        self.files_available = self.files_store is not None
        self.readable: set[str] = set()
        self.file_knowledge_bases: dict[str, GrantReportKnowledgeBaseDTO] = {}
        self.home_knowledge_bases: dict[tuple[str, str], list[GrantReportKnowledgeBaseDTO]] = {}

    async def load(self, grants: list[ResourceGrantRecord]) -> None:
        users = {g.principal_id for g in grants if g.principal_type == "user"}
        if users:
            self.labels = await self.user_store.get_team_member_labels(self.team_id, sorted(users))
            self.active_users = await self.user_store.filter_active_team_members(self.team_id, users)
        if any(g.principal_type == "group" for g in grants):
            self.groups = {group.id: group for group in await self.access_store.list_groups(self.team_id)}
        file_ids = sorted({g.resource_id for g in grants if g.resource_kind == "kb_file"})
        if file_ids and self.files_store is not None:
            try:
                rows = await self.files_store.get_many(file_ids)
            except Exception:
                logger.warning("kb.grant_report_files_unavailable team_id=%s", self.team_id, exc_info=True)
                self.files_available = False
                rows = []
            self.files = {row.file_id: row for row in rows if row.team_id == self.team_id}
        scope_homes = {home for g in grants if g.resource_kind == "kb_scope" and (home := _scope_home(g.resource_id, self.team_id))}
        space_ids = {g.principal_id for g in grants if g.principal_type == "workspace"}
        space_ids |= {home_id for home, home_id in scope_homes if home == "space"}
        space_ids |= {group.workspace_id for group in self.groups.values() if group.workspace_id}
        space_ids |= {file.scope_id for file in self.files.values() if _file_home(file) == "space"}
        for space_id in sorted(space_ids):
            row = await self.user_store.get_workspace(space_id)
            self.spaces[space_id] = row if row and row.get("team_id") == self.team_id else None
            if self.spaces[space_id]:
                self.space_names[space_id] = str(self.spaces[space_id].get("name") or "")
        await self._load_files()
        for home, home_id in sorted(scope_homes):
            if home == "team" or self.spaces.get(home_id):
                self.home_knowledge_bases[(home, home_id)] = await self._home_knowledge_bases(home, home_id)

    async def _load_files(self) -> None:
        """Which grant files the caller can open now, and the knowledge base each readable one is in."""
        by_context: dict[str, list[KbFileDTO]] = {}
        for file in self.files.values():
            by_context.setdefault(file.scope_id if _file_home(file) == "space" else "", []).append(file)
        for workspace_id, files in sorted(by_context.items()):
            decisions = await self.authorizer.batch(files, self.user.id, self.team_id, workspace_id)
            self.readable |= {file_id for file_id, allowed in (decisions or {}).items() if allowed}
        readable = [file for file in self.files.values() if file.file_id in self.readable]
        if not readable or self.files_store is None:
            return
        effective = await self.files_store.effective_knowledge_base_ids(readable)
        defaults = {file.file_id: default_knowledge_base_id(file.team_id, _file_home(file), file.scope_id) for file in readable}
        named = {kb_id for file_id, kb_id in effective.items() if kb_id != defaults[file_id]}
        rows = await self.knowledge_bases.get_many(named) if named and self.knowledge_bases else {}
        for file in readable:
            kb_id = effective[file.file_id]
            if kb_id == defaults[file.file_id]:
                self.file_knowledge_bases[file.file_id] = _default_ref(self.team_id, _file_home(file), file.scope_id, self.space_names)
            elif kb_id in rows:
                self.file_knowledge_bases[file.file_id] = _row_ref(rows[kb_id], self.space_names)

    async def _home_knowledge_bases(self, home: Home, home_id: str) -> list[GrantReportKnowledgeBaseDTO]:
        """A home's default, then its named knowledge bases the caller can use there."""
        listed = [_default_ref(self.team_id, home, home_id, self.space_names)]
        levels = await self.authorizer.knowledge_levels(self.user.id, self.team_id, home_id if home == "space" else "")
        if not levels or self.knowledge_bases is None:
            return listed
        default = listed[0].id
        named = [kb_id for kb_id in levels if kb_id != default and levels.home_of(kb_id) == (home, home_id)]
        rows = await self.knowledge_bases.get_many(named) if named else {}
        named_rows = sorted(
            (row for row in rows.values() if not row.is_default and row.deleted_at is None), key=lambda row: ((row.name or "").casefold(), row.id)
        )
        return listed + [_row_ref(row, self.space_names) for row in named_rows]

    def principal(self, grant: ResourceGrantRecord) -> GrantReportPrincipalDTO:
        kind, identity = grant.principal_type, grant.principal_id
        if kind == "user":
            state: PrincipalState = "active" if identity in self.active_users else "not_in_team" if identity in self.labels else "not_found"
            return GrantReportPrincipalDTO(type=kind, id=identity, label=self.labels.get(identity), state=state)
        if kind == "group":
            group = self.groups.get(identity)
            if group is None:
                return GrantReportPrincipalDTO(type=kind, id=identity, state="not_found")
            return GrantReportPrincipalDTO(
                type=kind,
                id=identity,
                label=group.name,
                state="active" if group.status == "active" else "group_removed",
                space_name=self.space_names.get(group.workspace_id) if group.workspace_id else None,
                member_count=group.member_count,
            )
        if kind == "workspace":
            space = self.spaces.get(identity)
            if space is None:
                return GrantReportPrincipalDTO(type=kind, id=identity, state="not_found")
            return GrantReportPrincipalDTO(
                type=kind,
                id=identity,
                label=self.space_names.get(identity),
                state="space_archived" if space.get("status") == "archived" else "active",
            )
        return GrantReportPrincipalDTO(type=kind, id=identity, state="active" if kind == "team" and identity == self.team_id else "not_found")

    def target(self, grant: ResourceGrantRecord) -> GrantReportTargetDTO:
        if grant.resource_kind == "kb_scope":
            home = _scope_home(grant.resource_id, self.team_id)
            if home is None:
                return GrantReportTargetDTO(kind="kb_scope", state="unrecognized")
            if home[0] == "team":
                return GrantReportTargetDTO(kind="kb_scope", home="team")
            space = self.spaces.get(home[1])
            if space is None:
                return GrantReportTargetDTO(kind="kb_scope", home="space", state="not_found")
            state: TargetState = "space_archived" if space.get("status") == "archived" else "active"
            return GrantReportTargetDTO(kind="kb_scope", home="space", space_name=self.space_names.get(home[1]), state=state)
        if not self.files_available:
            return GrantReportTargetDTO(kind="kb_file", state="unavailable")
        file = self.files.get(grant.resource_id)
        if file is None:
            return GrantReportTargetDTO(kind="kb_file", state="not_found")
        home = _file_home(file)
        space_name = self.space_names.get(file.scope_id) if home == "space" else None
        if file.soft_deleted_at:
            return GrantReportTargetDTO(kind="kb_file", home=home, space_name=space_name, state="deleted")
        if file.file_id not in self.readable:
            return GrantReportTargetDTO(kind="kb_file", home=home, space_name=space_name, state="hidden")
        return GrantReportTargetDTO(
            kind="kb_file", home=home, space_name=space_name, file_name=file.filename, knowledge_base=self.file_knowledge_bases.get(file.file_id)
        )

    def knowledge_bases_for(self, grant: ResourceGrantRecord, target: GrantReportTargetDTO) -> tuple[list[GrantReportKnowledgeBaseDTO], bool]:
        if grant.resource_kind == "kb_scope":
            home = _scope_home(grant.resource_id, self.team_id)
            return (list(self.home_knowledge_bases.get(home, [])) if home else []), True
        return ([target.knowledge_base] if target.knowledge_base else []), False


def _restriction(grant: ResourceGrantRecord, *, expired: bool) -> GrantReportRestrictionDTO:
    if expired:
        return GrantReportRestrictionDTO(kind="none", reason="expired")
    actions = _actions(grant.permission)
    if grant.effect == "deny":
        return (
            GrantReportRestrictionDTO(kind="deny", actions=actions)
            if actions
            else GrantReportRestrictionDTO(kind="none", reason="permission_not_checked")
        )
    return GrantReportRestrictionDTO(kind="allow_list", actions=actions)


def _mapping(
    grant: ResourceGrantRecord,
    *,
    restriction: GrantReportRestrictionDTO,
    principal: GrantReportPrincipalDTO,
    target: GrantReportTargetDTO,
    knowledge_bases: list[GrantReportKnowledgeBaseDTO],
    whole_home: bool,
) -> GrantReportMappingDTO:
    if restriction.reason == "expired":
        return GrantReportMappingDTO(action="none", reason="expired")
    if target.state in ("deleted", "not_found", "unrecognized", "unavailable"):
        return GrantReportMappingDTO(action="none", reason="target_unavailable")
    if target.kind == "kb_file" and target.home == "personal":
        return GrantReportMappingDTO(action="none", reason="personal")
    if principal.state not in ("active", "space_archived"):
        return GrantReportMappingDTO(action="none", reason="principal_unavailable")
    if not restriction.actions:
        return GrantReportMappingDTO(action="none", reason="permission_not_checked")
    listed = knowledge_bases[:MAX_LISTED_KNOWLEDGE_BASES]
    common = {
        "knowledge_bases": listed,
        "more": len(knowledge_bases) - len(listed),
        "whole_home": whole_home,
        "open_audience": any(kb.audience in ("space", "team") for kb in knowledge_bases),
    }
    if grant.effect == "deny":
        return GrantReportMappingDTO(action="keep_out", **common)
    return GrantReportMappingDTO(action="needs_level", level="edit" if "write" in restriction.actions else "use", **common)


async def build_grant_report(
    request: Any,
    *,
    user: User,
    access_store: AccessControlStore,
    user_store: UserStore,
    cursor: str | None,
    limit: int,
) -> GrantReportDTO:
    """One page of the report. Raises :class:`InvalidCursorError` for a cursor it did not issue."""
    before = decode_cursor(cursor)
    page, total = await access_store.page_resource_grants(user.team_id, resource_kinds=REPORT_KINDS, limit=limit, before=before)
    grants = page[:limit]
    resolver = _Resolver(request, user, access_store, user_store)
    await resolver.load(grants)
    now = datetime.now(UTC).isoformat()
    items: list[GrantReportItemDTO] = []
    for grant in grants:
        expired = _expired(grant, now)
        principal = resolver.principal(grant)
        target = resolver.target(grant)
        restriction = _restriction(grant, expired=expired)
        knowledge_bases, whole_home = resolver.knowledge_bases_for(grant, target)
        items.append(
            GrantReportItemDTO(
                grant_id=grant.id,
                effect=grant.effect,
                permission=grant.permission,
                expires_at=grant.expires_at,
                expired=expired,
                created_at=grant.created_at,
                applies_to=principal,
                target=target,
                restriction=restriction,
                mapping=_mapping(
                    grant, restriction=restriction, principal=principal, target=target, knowledge_bases=knowledge_bases, whole_home=whole_home
                ),
            )
        )
    return GrantReportDTO(
        enforced=resource_grants_enforce_enabled(),
        items=items,
        total=total,
        next_cursor=encode_cursor(grants[-1]) if len(page) > limit and grants else None,
    )
