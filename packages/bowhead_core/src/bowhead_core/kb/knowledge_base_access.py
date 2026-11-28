"""Which knowledge bases a caller can use in one access context, and at what level (phase 1 §2.1).

:func:`resolve_knowledge_levels` reads ``kb_knowledge_bases`` and the matching
``kb_knowledge_base_members`` rows in one indexed query. It looks only at the caller's My
home, the current Space and the Team, and returns ``{knowledge_base_id: level}`` for every
knowledge base the caller can use there:

- **My:** the owner manages; nobody else gets a level.
- **Space:** only inside that Space's context and only for its members (Q2). The audience
  gives its default level; listed people, groups bound to that Space and Team-wide groups
  give their listed level. Space admins and the owner manage. Viewers are capped at use.
  Team owners/admins who are Space members manage the Space's **default** knowledge base,
  as viewers too (C2: today's rights to add and delete any Space file).
- **Team:** audience "everyone" gives use; listed people, Team-wide groups and listed
  Spaces (their members, C9) give their listed level. Team owners/admins and the owner
  manage.
- The highest matching level wins. Group entries whose group is inactive or deleted never
  match, because ``AccessContext.group_ids`` lists active groups only.

Nothing is cached: a member-list change applies on the next resolution. The core policy
epoch is not involved. Default knowledge bases have fixed audiences in phase 1 (C6), so a
home's default is always reported, also before its row exists (it is created lazily).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bowhead_core.kb.knowledge_base_store import DEFAULT_AUDIENCE, default_for_scope, default_knowledge_base_id

if TYPE_CHECKING:
    import aiosqlite

KNOWLEDGE_USE = 1
KNOWLEDGE_EDIT = 2
KNOWLEDGE_MANAGE = 3
#: The owner or an automatic manager: manages and can also transfer and delete the knowledge base.
KNOWLEDGE_OWNER = 4
LEVEL_RANKS: Mapping[str, int] = {"use": KNOWLEDGE_USE, "edit": KNOWLEDGE_EDIT, "manage": KNOWLEDGE_MANAGE}
LEVEL_NAMES: Mapping[int, str] = {KNOWLEDGE_USE: "use", KNOWLEDGE_EDIT: "edit", KNOWLEDGE_MANAGE: "manage", KNOWLEDGE_OWNER: "manage"}
# The normalized Space roles of `bowhead_core.access_control.normalize_workspace_role`.
_SPACE_ADMIN, _SPACE_VIEWER = "ws_admin", "viewer"


@dataclass(frozen=True, slots=True)
class TeamPrincipals:
    """The caller's principals that only Team knowledge bases need, loaded from core on demand."""

    space_ids: frozenset[str]
    team_group_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class KnowledgeCaller:
    """What an access context knows about the caller, as the resolver needs it."""

    user_id: str
    team_id: str
    #: The current Space; "" when the context has none.
    space_id: str = ""
    #: The normalized Space role in ``space_id`` (ws_admin, member or viewer); "" when not a member.
    space_role: str = ""
    #: Team owner or Team admin.
    is_team_admin: bool = False
    #: ``AccessContext.group_ids``: active groups, Team-wide or bound to the current Space.
    group_ids: frozenset[str] = frozenset()
    #: The Team-wide subset of the caller's active groups; None when not loaded.
    team_group_ids: frozenset[str] | None = None
    #: The caller's active Spaces in this Team (Space entries on Team knowledge bases); None when not loaded.
    space_ids: frozenset[str] | None = None


class KnowledgeLevels(Mapping[str, int]):
    """``{knowledge_base_id: level}`` for one caller, plus the homes the resolver saw.

    ``home_of`` answers for every knowledge base the resolver read, usable or not, so a level
    is only ever applied to files of the same home. ``excluded_in`` lists the named knowledge
    bases of a home the caller cannot use: the retrieval pre-filter.
    """

    __slots__ = ("_defaults", "_excluded", "_homes", "_levels")

    def __init__(
        self,
        levels: Mapping[str, int],
        homes: Mapping[str, tuple[str, str]],
        excluded: frozenset[str] = frozenset(),
        defaults: frozenset[str] = frozenset(),
    ) -> None:
        self._levels = {key: int(value) for key, value in levels.items() if value > 0}
        self._homes = dict(homes)
        self._excluded = frozenset(excluded)
        self._defaults = frozenset(defaults)

    def __getitem__(self, knowledge_base_id: str) -> int:
        return self._levels[knowledge_base_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._levels)

    def __len__(self) -> int:
        return len(self._levels)

    def __repr__(self) -> str:
        return f"KnowledgeLevels({self._levels!r}, excluded={sorted(self._excluded)!r})"

    def home_of(self, knowledge_base_id: str) -> tuple[str, str] | None:
        """The (home, home ID) of a knowledge base the resolver read; None when it did not."""
        return self._homes.get(knowledge_base_id)

    def excluded_in(self, home: str, home_id: str) -> tuple[str, ...]:
        """The named knowledge bases of one home the caller cannot use, sorted."""
        return tuple(sorted(key for key in self._excluded if self._homes.get(key) == (home, home_id)))

    def named_in(self, home: str, home_id: str) -> tuple[str, ...]:
        """Every live named knowledge base of one home the resolver read, usable or not, sorted."""
        return tuple(sorted(key for key, value in self._homes.items() if value == (home, home_id) and key not in self._defaults))

    def is_default(self, knowledge_base_id: str) -> bool:
        """Whether the resolver saw this ID as a home's default."""
        return knowledge_base_id in self._defaults


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSearch:
    """Restrict a search to one knowledge base of the caller's context (``ScopeContext.knowledge_base_search``).

    ``scope`` is the file scope of its home. A named knowledge base matches its own chunk key
    (``only``); a home's default matches every chunk of the home except those of its named
    knowledge bases (``excluded``), so chunks written before the key existed still match.
    """

    knowledge_base_id: str
    scope: str
    only: str | None = None
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HomeSelection:
    """One home's part of a :class:`KnowledgeBaseSelection`.

    With the home's default selected (``default``), every chunk of the home matches except those of
    its named knowledge bases that are not selected (``excluded``), so the default's old chunks
    without the key still match. Otherwise only chunks of the selected named knowledge bases
    (``only``) match.
    """

    default: bool = False
    only: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSelection:
    """Restrict a search to selected knowledge bases the caller can use (``ScopeContext.knowledge_base_selection``).

    An Agent's selected-knowledge-base ceiling, per home of the caller's context: My knowledge
    (``personal``), the current Space and the Team. None for a home means nothing of it is selected.
    """

    personal: HomeSelection | None = None
    space: HomeSelection | None = None
    team: HomeSelection | None = None

    def home(self, scope: str) -> HomeSelection | None:
        return self.personal if scope == "personal" else self.team if scope == "team" else self.space


def selected_usable(levels: KnowledgeLevels, selected: Iterable[str]) -> tuple[str, ...]:
    """The selected knowledge bases the caller can use in this context (use or more), sorted.

    The levels cover only the caller's My home, current Space and Team, so a Space knowledge base
    applies only inside its own Space, and someone else's My knowledge never applies.
    """
    return tuple(sorted({identity for identity in selected if levels.get(identity, 0) >= KNOWLEDGE_USE and levels.home_of(identity)}))


def knowledge_base_selection(levels: KnowledgeLevels, usable: Iterable[str], *, user_id: str, space_id: str, team_id: str) -> KnowledgeBaseSelection:
    """Build the per-home search restriction for usable selected knowledge bases."""
    chosen = set(usable)
    parts: dict[str, HomeSelection | None] = {}
    for home, home_id in (("personal", user_id), ("space", space_id), ("team", team_id)):
        if not home_id:
            parts[home] = None
            continue
        in_home = {identity for identity in chosen if levels.home_of(identity) == (home, home_id)}
        default = default_knowledge_base_id(team_id, home, home_id)
        if default in in_home:
            parts[home] = HomeSelection(default=True, excluded=tuple(sorted(set(levels.named_in(home, home_id)) - in_home)))
        elif in_home:
            parts[home] = HomeSelection(only=tuple(sorted(in_home)))
        else:
            parts[home] = None
    return KnowledgeBaseSelection(**parts)


def named_knowledge_base_id(file: Any) -> str | None:
    """The named knowledge base a Space or Team file is in; None when its home's default applies.

    Needs no read: a NULL value and the scope's deterministic default ID both mean the default.
    Personal files always answer None, because they stay owner-only whatever their knowledge base.
    """
    scope = getattr(file, "scope", None)
    if scope not in ("space", "workspace", "team"):
        return None
    stored = getattr(file, "knowledge_base_id", None)
    if not stored:
        return None
    try:
        default = default_for_scope(scope, file.scope_id, file.team_id)
    except (AttributeError, TypeError, ValueError):
        return str(stored)
    return None if stored == default else str(stored)


_RESOLVE_SQL = """
SELECT k.id, k.home, k.home_id, k.is_default, k.audience, k.audience_level, k.owner_user_id,
       MAX(CASE m.level WHEN 'manage' THEN 3 WHEN 'edit' THEN 2 WHEN 'use' THEN 1 END) AS listed,
       CASE WHEN k.home = 'team' AND :pending_check THEN EXISTS(
           SELECT 1 FROM kb_knowledge_base_members x
           WHERE x.knowledge_base_id = k.id AND x.principal_type IN ('group', 'space')) ELSE 0 END AS pending
FROM kb_knowledge_bases k
LEFT JOIN kb_knowledge_base_members m ON m.knowledge_base_id = k.id AND (
     (m.principal_type = 'user' AND m.principal_id = :user)
  OR (m.principal_type = 'group' AND k.home = 'space' AND m.principal_id IN (SELECT value FROM json_each(:groups)))
  OR (m.principal_type = 'group' AND k.home = 'team' AND m.principal_id IN (SELECT value FROM json_each(:team_groups)))
  OR (m.principal_type = 'space' AND k.home = 'team' AND m.principal_id IN (SELECT value FROM json_each(:spaces))))
WHERE k.team_id = :team AND k.deleted_at IS NULL
  AND k.home IN ('personal', 'space', 'team') AND k.home_id IN (:user, :space, :team) AND (
     (k.home = 'personal' AND k.home_id = :user)
  OR (k.home = 'space' AND :space != '' AND k.home_id = :space)
  OR (k.home = 'team' AND k.home_id = :team))
GROUP BY k.id
"""


def knowledge_level(caller: KnowledgeCaller, home: str, is_default: bool, audience: str, audience_level: str, owner: str | None, listed: int) -> int:
    """One person's level on one knowledge base, given the highest level their entries list (0 for none).

    The resolver's rules for one row; member summaries apply them to each person who could match.
    """
    return _level(caller, home, is_default, audience, audience_level, owner, listed)


def _level(caller: KnowledgeCaller, home: str, is_default: bool, audience: str, audience_level: str, owner: str | None, listed: int) -> int:
    if home == "personal":
        return KNOWLEDGE_OWNER
    if home == "space":
        if not caller.space_role:
            return 0
        level = listed
        if audience == "space":
            level = max(level, LEVEL_RANKS.get(audience_level, 0))
        if caller.space_role == _SPACE_ADMIN or owner == caller.user_id:
            level = KNOWLEDGE_OWNER
        if caller.space_role == _SPACE_VIEWER:
            level = min(level, KNOWLEDGE_USE)
        if is_default and caller.is_team_admin:
            level = KNOWLEDGE_OWNER
        return level
    level = listed
    if audience == "team":
        level = max(level, KNOWLEDGE_USE)
    if caller.is_team_admin or owner == caller.user_id:
        level = KNOWLEDGE_OWNER
    return level


async def _query(db: aiosqlite.Connection, caller: KnowledgeCaller) -> list[tuple]:
    if caller.team_group_ids is not None:
        team_groups = caller.team_group_ids
    else:
        # Without a current Space, every group in the context is Team-wide.
        team_groups = caller.group_ids if not caller.space_id else frozenset()
    complete = caller.space_ids is not None and (caller.team_group_ids is not None or not caller.space_id or not caller.group_ids)
    params = {
        "user": caller.user_id,
        "team": caller.team_id,
        "space": caller.space_id,
        "groups": json.dumps(sorted(caller.group_ids)),
        "team_groups": json.dumps(sorted(team_groups)),
        "spaces": json.dumps(sorted(caller.space_ids or ())),
        "pending_check": 0 if complete else 1,
    }
    async with db.execute(_RESOLVE_SQL, params) as cursor:
        return list(await cursor.fetchall())


async def resolve_knowledge_levels(
    db: aiosqlite.Connection | None,
    caller: KnowledgeCaller,
    *,
    load_team_principals: Callable[[], Awaitable[TeamPrincipals]] | None = None,
    named: bool = True,
) -> KnowledgeLevels:
    """Resolve the caller's level on every knowledge base it can use in its context.

    One query on ``kb_files.db``. The caller's Spaces and Team-wide groups are needed only for
    Space and group entries on Team knowledge bases. When the caller does not carry them and
    such entries exist, ``load_team_principals`` loads them and the query runs once more;
    without a loader those entries do not apply (narrower, never wider). ``db`` None stands for
    a files database before revision 0239, which can hold no knowledge base rows.

    ``named`` False (the knowledge base switch off, C3) hides every named knowledge base: each is
    reported as excluded, member entries are ignored and core is never read, so defaults keep
    exactly today's levels and named files are refused.
    """
    if not caller.user_id or not caller.team_id:
        return KnowledgeLevels({}, {})
    rows = await _query(db, caller) if db is not None else []
    if not named:
        rows = [(*row[:7], 0, 0) for row in rows]
    if named and db is not None and load_team_principals is not None and any(row[8] for row in rows):
        principals = await load_team_principals()
        caller = KnowledgeCaller(
            user_id=caller.user_id,
            team_id=caller.team_id,
            space_id=caller.space_id,
            space_role=caller.space_role,
            is_team_admin=caller.is_team_admin,
            group_ids=caller.group_ids,
            team_group_ids=principals.team_group_ids,
            space_ids=principals.space_ids,
        )
        rows = await _query(db, caller)
    levels: dict[str, int] = {}
    homes: dict[str, tuple[str, str]] = {}
    excluded: set[str] = set()
    defaults: set[str] = set()
    for knowledge_base_id, home, home_id, is_default, audience, audience_level, owner, listed, _pending in rows:
        identity = str(knowledge_base_id)
        homes[identity] = (str(home), str(home_id))
        if is_default:
            defaults.add(identity)
        elif not named:
            excluded.add(identity)
            continue
        level = _level(caller, str(home), bool(is_default), str(audience), str(audience_level), owner, int(listed or 0))
        if level > 0:
            levels[identity] = level
        elif not is_default:
            excluded.add(identity)
    # Defaults are created lazily; their IDs and fixed audiences are known without a row.
    for home, home_id in (("personal", caller.user_id), ("space", caller.space_id), ("team", caller.team_id)):
        if not home_id:
            continue
        identity = default_knowledge_base_id(caller.team_id, home, home_id)
        defaults.add(identity)
        if identity in homes:
            continue
        homes[identity] = (home, home_id)
        audience, audience_level = DEFAULT_AUDIENCE[home]
        owner = home_id if home == "personal" else None
        level = _level(caller, home, True, audience, audience_level, owner, 0)
        if level > 0:
            levels[identity] = level
    return KnowledgeLevels(levels, homes, frozenset(excluded), frozenset(defaults))
