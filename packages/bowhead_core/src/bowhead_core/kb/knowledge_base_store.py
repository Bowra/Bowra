"""Knowledge base containers stored beside ``kb_files`` (revision ``0239_knowledge_bases``).

Every knowledge file belongs to one knowledge base. A knowledge base lives in a home: a
person's My knowledge (``personal``, one per Team and person), a Space (``space``) or the Team
(``team``). Each home has one default knowledge base whose ID is derived from the Team and the
home, so the revision backfill, lazy creation and permission checks name the same row without a
read. Defaults store no name; the interface renders it from the home.

The file's scope columns (``scope``, ``scope_id``, ``workspace_id``, ``team_id``) stay
authoritative for chunk routing. ``kb_files.knowledge_base_id`` must name a knowledge base with
the same Team and home; the read safety rule (:func:`effective_knowledge_base_id`) treats a NULL,
unknown or home-mismatched value as the default of the file's current scope.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

KNOWLEDGE_BASE_ID_PREFIX = "kbase_"
KnowledgeBaseHome = Literal["personal", "space", "team"]
_SCOPE_HOMES: dict[str, KnowledgeBaseHome] = {"personal": "personal", "space": "space", "workspace": "space", "team": "team"}
# Default knowledge bases keep a fixed audience in phase 1: the owner for My knowledge, every
# Space member (who can edit; viewers stay read-only) and everyone in the Team (who can use).
DEFAULT_AUDIENCE: dict[str, tuple[str, str]] = {"personal": ("owner", "use"), "space": ("space", "edit"), "team": ("team", "use")}


@dataclass(frozen=True, slots=True)
class KnowledgeBaseHomeRow:
    """The stored Team and home of one knowledge base ID."""

    id: str
    team_id: str
    home: str
    home_id: str


@dataclass(frozen=True, slots=True)
class KnowledgeBaseRow:
    """One stored knowledge base. Defaults have no name; it is rendered from the home."""

    id: str
    team_id: str
    home: str
    home_id: str
    is_default: bool
    name: str | None
    description: str | None
    audience: str
    audience_level: str
    owner_user_id: str | None
    revision: int
    created_at: str
    updated_at: str
    deleted_at: str | None


_KNOWLEDGE_BASE_COLUMNS = (
    "id,team_id,home,home_id,is_default,name,description,audience,audience_level,owner_user_id,revision,created_at,updated_at,deleted_at"
)


def default_knowledge_base_id(team_id: str, home: str, home_id: str) -> str:
    """The deterministic ID of a home's default knowledge base."""
    if home not in DEFAULT_AUDIENCE:
        raise ValueError("Unknown knowledge base home")
    digest = hashlib.sha256(f"default|{team_id}|{home}|{home_id}".encode()).hexdigest()
    return KNOWLEDGE_BASE_ID_PREFIX + digest[:24]


def home_for_scope(scope: str, scope_id: str) -> tuple[KnowledgeBaseHome, str]:
    """Map a file scope to its home: personal → the owner, space/workspace → the Space, team → the Team."""
    home = _SCOPE_HOMES.get(scope)
    if home is None:
        raise ValueError("Unknown knowledge file scope")
    return home, scope_id


def default_for_scope(scope: str, scope_id: str, team_id: str) -> str:
    """The default knowledge base of a file scope."""
    home, home_id = home_for_scope(scope, scope_id)
    return default_knowledge_base_id(team_id, home, home_id)


def knowledge_base_key(file: Any) -> str:
    """The stored knowledge base ID, or the scope's default when none is stored; '' when the scope is unknown.

    It needs no read, so the worker uses it for chunk metadata and index identity. A stored named
    ID is kept as is; only :func:`effective_knowledge_base_id` can tell whether it is stale.
    """
    stored = getattr(file, "knowledge_base_id", None)
    if stored:
        return str(stored)
    try:
        return default_for_scope(file.scope, file.scope_id, file.team_id)
    except (AttributeError, TypeError, ValueError):
        return ""


def effective_knowledge_base_id(file: Any, stored: KnowledgeBaseHomeRow | None = None) -> str:
    """Apply the read safety rule to one file.

    The stored ID counts only when it is the scope's default or ``stored`` (its looked-up row) has
    the file's Team and home. NULL, unknown and home-mismatched values mean the default of the
    file's current scope.
    """
    default = default_for_scope(file.scope, file.scope_id, file.team_id)
    raw = getattr(file, "knowledge_base_id", None)
    if not raw or raw == default or stored is None or stored.id != raw:
        return default
    if (stored.team_id, stored.home, stored.home_id) != (file.team_id, *home_for_scope(file.scope, file.scope_id)):
        return default
    return str(raw)


async def ensure_default(db: aiosqlite.Connection, team_id: str, home: str, home_id: str, *, now: str | None = None) -> str:
    """Create a home's default knowledge base if it is missing, in the caller's transaction."""
    knowledge_base_id = default_knowledge_base_id(team_id, home, home_id)
    audience, level = DEFAULT_AUDIENCE[home]
    stamp = now or datetime.now(tz=UTC).isoformat(timespec="seconds")
    cursor = await db.execute(
        "INSERT OR IGNORE INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,description,audience,audience_level,"
        "owner_user_id,revision,created_at,updated_at) VALUES (?,?,?,?,1,NULL,NULL,?,?,?,1,?,?)",
        (knowledge_base_id, team_id, home, home_id, audience, level, home_id if home == "personal" else None, stamp, stamp),
    )
    await cursor.close()
    return knowledge_base_id


async def ensure_default_for_scope(db: aiosqlite.Connection, *, scope: str, scope_id: str, team_id: str, now: str | None = None) -> str:
    """The default knowledge base of a file scope, created in the caller's transaction when missing."""
    home, home_id = home_for_scope(scope, scope_id)
    return await ensure_default(db, team_id, home, home_id, now=now)


async def stored_homes(db: aiosqlite.Connection, knowledge_base_ids: set[str]) -> dict[str, KnowledgeBaseHomeRow]:
    """Look up the Team and home of stored knowledge base IDs in one query."""
    if not knowledge_base_ids:
        return {}
    ids = sorted(knowledge_base_ids)
    async with db.execute(
        "SELECT id,team_id,home,home_id FROM kb_knowledge_bases WHERE id IN (" + ",".join("?" for _ in ids) + ")",  # noqa: S608 -- Placeholders only.
        ids,
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(row[0]): KnowledgeBaseHomeRow(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows}


async def get_knowledge_base(db: aiosqlite.Connection, knowledge_base_id: str) -> KnowledgeBaseRow | None:
    """One stored knowledge base, deleted ones included; None when the ID is unknown."""
    async with db.execute(
        f"SELECT {_KNOWLEDGE_BASE_COLUMNS} FROM kb_knowledge_bases WHERE id=?",  # noqa: S608 -- Fixed column list.
        (knowledge_base_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _knowledge_base_row(row)


def _knowledge_base_row(row: Any) -> KnowledgeBaseRow:
    return KnowledgeBaseRow(
        id=str(row[0]),
        team_id=str(row[1]),
        home=str(row[2]),
        home_id=str(row[3]),
        is_default=bool(row[4]),
        name=row[5],
        description=row[6],
        audience=str(row[7]),
        audience_level=str(row[8]),
        owner_user_id=row[9],
        revision=int(row[10]),
        created_at=str(row[11]),
        updated_at=str(row[12]),
        deleted_at=row[13],
    )


# ─── Knowledge base management (phase 1 slice 3, behind FEATURE_KNOWLEDGE_BASES) ──────────

#: Upper bounds that keep one knowledge base's member list and one request bounded.
MAX_MEMBER_ENTRIES = 1000
MAX_MEMBER_CHANGES = 100
_HOME_SQL = "CASE scope WHEN 'workspace' THEN 'space' ELSE scope END"
#: The message of the 0238 retention guard on ``kb_files`` (a trashed or claimed original).
RETENTION_ABORT = "Restore the file before adding this reference"


def knowledge_bases_enabled() -> bool:
    """``FEATURE_KNOWLEDGE_BASES`` (default off): named knowledge bases, their API and every new target field.

    Off, the knowledge base routes answer 404, upload and chat tool targets are ignored or refused as
    before, and named knowledge bases are hidden: their files are refused instead of falling back to
    their scope (C3), and member entries raise nobody's level.
    """
    return os.environ.get("FEATURE_KNOWLEDGE_BASES", "0").strip().lower() in {"1", "true", "yes", "on"}


def new_knowledge_base_id() -> str:
    """A random ID for a named knowledge base (defaults use :func:`default_knowledge_base_id`)."""
    return KNOWLEDGE_BASE_ID_PREFIX + uuid4().hex[:24]


def is_retention_abort(exc: BaseException) -> bool:
    """Whether a write failed on the 0238 retention guard: the original is in the automatic trash or being purged."""
    return isinstance(exc, sqlite3.IntegrityError) and RETENTION_ABORT in str(exc)


async def require_live_target(db: aiosqlite.Connection, knowledge_base_id: str, *, team_id: str, home: str, home_id: str) -> None:
    """Inside a ``kb_files`` write transaction: a named target must be live in exactly this Team and home.

    Deleting a knowledge base re-checks for active files in its own transaction, so a file is never
    added to or moved into a knowledge base that is being deleted.
    """
    async with db.execute("SELECT team_id, home, home_id, is_default, deleted_at FROM kb_knowledge_bases WHERE id=?", (knowledge_base_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None or row[4] is not None or bool(row[3]) or (str(row[0]), str(row[1]), str(row[2])) != (team_id, home, home_id):
        from bowhead_core.kb.ingestion import KnowledgeWriteError

        raise KnowledgeWriteError("knowledge_base_unavailable")


@dataclass(frozen=True, slots=True)
class KnowledgeBaseMemberRow:
    """One member-list entry: a person, a group or (Team knowledge bases only) a Space, with a level."""

    knowledge_base_id: str
    principal_type: str
    principal_id: str
    level: str
    added_by: str
    created_at: str


@dataclass(frozen=True, slots=True)
class KnowledgeBaseFileCounts:
    """Active files of one knowledge base: all of them, the failed ones and the newest upload."""

    files: int = 0
    failed: int = 0
    latest_upload: str | None = None


class KnowledgeBaseConflict(Exception):
    """A conditional knowledge base write met another state; ``code`` names it, ``files`` counts active files."""

    def __init__(self, code: str, *, files: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.files = files


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class KnowledgeBaseStore:
    """Reads and writes of ``kb_knowledge_bases`` and its member rows in ``kb_files.db``.

    Every write is one ``BEGIN IMMEDIATE`` transaction with its checks inside, so it serializes
    with the ``kb_files`` writers that add or move files into a named knowledge base and re-check
    its row in their own transaction (:func:`require_live_target`).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)

    async def get(self, knowledge_base_id: str) -> KnowledgeBaseRow | None:
        async with open_pooled_read_ctx(self._db_path) as db:
            return await get_knowledge_base(db, knowledge_base_id)

    async def get_many(self, knowledge_base_ids: Iterable[str]) -> dict[str, KnowledgeBaseRow]:
        ids = sorted(set(knowledge_base_ids))
        out: dict[str, KnowledgeBaseRow] = {}
        if not ids:
            return out
        async with open_pooled_read_ctx(self._db_path) as db:
            for offset in range(0, len(ids), 500):
                batch = ids[offset : offset + 500]
                async with db.execute(
                    f"SELECT {_KNOWLEDGE_BASE_COLUMNS} FROM kb_knowledge_bases WHERE id IN ({','.join('?' for _ in batch)})",  # noqa: S608 -- Fixed columns, placeholders only.
                    batch,
                ) as cursor:
                    out.update({str(row[0]): _knowledge_base_row(row) for row in await cursor.fetchall()})
        return out

    async def file_counts(self, team_id: str, homes: Iterable[tuple[str, str]]) -> dict[str, KnowledgeBaseFileCounts]:
        """Active file counts per knowledge base in the given homes, keyed by knowledge base ID.

        A file counts for the knowledge base it names when that one is live in the file's own home,
        and for its home's default when it names none or the default. A file naming a knowledge base
        of another home is not counted: access refuses it (fail-closed).
        """
        wanted = [(home, home_id) for home, home_id in dict.fromkeys(homes) if home in DEFAULT_AUDIENCE and home_id]
        if not team_id or not wanted:
            return {}
        branches: list[str] = []
        params: list[str] = [team_id]
        for home, home_id in wanted:
            branches.append("(scope IN ('space','workspace') AND scope_id=?)" if home == "space" else f"(scope='{home}' AND scope_id=?)")
            params.append(home_id)
        async with open_pooled_read_ctx(self._db_path) as db:
            async with db.execute("SELECT 1 FROM pragma_table_info('kb_files') WHERE name='knowledge_base_id'") as cursor:
                keyed = await cursor.fetchone() is not None
            async with db.execute(
                f"SELECT {_HOME_SQL}, scope_id, {'knowledge_base_id' if keyed else 'NULL'}, COUNT(*), "  # noqa: S608 -- Fixed SQL, bound values.
                "SUM(CASE WHEN parse_status='failed' THEN 1 ELSE 0 END), MAX(uploaded_at) FROM kb_files "
                f"WHERE team_id=? AND soft_deleted_at IS NULL AND ({' OR '.join(branches)}) GROUP BY 1, 2, 3",
                params,
            ) as cursor:
                groups = await cursor.fetchall()
            named = sorted({str(row[2]) for row in groups if row[2]})
            live: dict[str, tuple[str, str]] = {}
            if named:
                async with db.execute(
                    "SELECT id, home, home_id FROM kb_knowledge_bases WHERE team_id=? AND deleted_at IS NULL AND is_default=0 "  # noqa: S608 -- Placeholders only.
                    f"AND id IN ({','.join('?' for _ in named)})",
                    [team_id, *named],
                ) as cursor:
                    live = {str(row[0]): (str(row[1]), str(row[2])) for row in await cursor.fetchall()}
        counts: dict[str, KnowledgeBaseFileCounts] = {}
        for home, home_id, stored, total, failed, latest in groups:
            default = default_knowledge_base_id(team_id, str(home), str(home_id))
            if not stored or stored == default:
                key = default
            elif live.get(str(stored)) == (str(home), str(home_id)):
                key = str(stored)
            else:
                continue
            before = counts.get(key, KnowledgeBaseFileCounts())
            newest = max((value for value in (before.latest_upload, latest) if value), default=None)
            counts[key] = KnowledgeBaseFileCounts(before.files + int(total or 0), before.failed + int(failed or 0), newest)
        return counts

    async def active_file_ids(self, knowledge_base_id: str) -> list[str]:
        """The active files that name one (named) knowledge base, oldest first."""
        async with (
            open_pooled_read_ctx(self._db_path) as db,
            db.execute(
                "SELECT file_id FROM kb_files WHERE knowledge_base_id=? AND soft_deleted_at IS NULL ORDER BY uploaded_at, file_id",
                (knowledge_base_id,),
            ) as cursor,
        ):
            return [str(row[0]) for row in await cursor.fetchall()]

    async def create(
        self,
        *,
        team_id: str,
        home: str,
        home_id: str,
        name: str,
        description: str | None,
        audience: str,
        audience_level: str,
        owner_user_id: str,
    ) -> KnowledgeBaseRow:
        """Create a named knowledge base. Names need not be unique (C4)."""
        knowledge_base_id, now = new_knowledge_base_id(), _now()
        async with diagnostic_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO kb_knowledge_bases(id,team_id,home,home_id,is_default,name,description,audience,audience_level,"
                    "owner_user_id,revision,created_at,updated_at) VALUES (?,?,?,?,0,?,?,?,?,?,1,?,?)",
                    (knowledge_base_id, team_id, home, home_id, name, description, audience, audience_level, owner_user_id, now, now),
                )
                row = await get_knowledge_base(db, knowledge_base_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        assert row is not None
        return row

    async def update(self, knowledge_base: KnowledgeBaseRow, *, expected_revision: int, changes: Mapping[str, Any]) -> KnowledgeBaseRow:
        """Apply settings changes while the stored revision is still ``expected_revision``.

        A home's default is created first when it has no row yet (revision 1). Raises
        :class:`KnowledgeBaseConflict` ``knowledge_base_revision_conflict`` otherwise.
        """
        if set(changes) - {"name", "description", "audience", "audience_level"}:
            raise ValueError("Unknown knowledge base field")
        now = _now()
        async with diagnostic_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if knowledge_base.is_default:
                    await ensure_default(db, knowledge_base.team_id, knowledge_base.home, knowledge_base.home_id, now=now)
                assignments = "".join(f"{column}=?, " for column in changes)
                cursor = await db.execute(
                    f"UPDATE kb_knowledge_bases SET {assignments}revision=revision+1, updated_at=? "  # noqa: S608 -- Whitelisted columns.
                    "WHERE id=? AND team_id=? AND deleted_at IS NULL AND revision=?",
                    (*changes.values(), now, knowledge_base.id, knowledge_base.team_id, expected_revision),
                )
                changed = cursor.rowcount
                await cursor.close()
                if not changed:
                    raise KnowledgeBaseConflict("knowledge_base_revision_conflict")
                row = await get_knowledge_base(db, knowledge_base.id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        assert row is not None
        return row

    async def delete(self, knowledge_base_id: str, *, expected_revision: int | None) -> None:
        """Mark a named knowledge base deleted once no active file names it; its member rows go with it.

        Raises :class:`KnowledgeBaseConflict`: ``knowledge_base_files_changed`` (with the current
        count) while files remain, ``knowledge_base_revision_conflict`` when it changed meanwhile.
        """
        now = _now()
        async with diagnostic_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT revision FROM kb_knowledge_bases WHERE id=? AND is_default=0 AND deleted_at IS NULL", (knowledge_base_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                async with db.execute(
                    "SELECT COUNT(*) FROM kb_files WHERE knowledge_base_id=? AND soft_deleted_at IS NULL", (knowledge_base_id,)
                ) as cursor:
                    remaining = int((await cursor.fetchone())[0])
                if row is None or (expected_revision is not None and int(row[0]) != expected_revision):
                    raise KnowledgeBaseConflict("knowledge_base_revision_conflict", files=remaining)
                if remaining:
                    raise KnowledgeBaseConflict("knowledge_base_files_changed", files=remaining)
                await db.execute(
                    "UPDATE kb_knowledge_bases SET deleted_at=?, updated_at=?, revision=revision+1 WHERE id=?", (now, now, knowledge_base_id)
                )
                await db.execute("DELETE FROM kb_knowledge_base_members WHERE knowledge_base_id=?", (knowledge_base_id,))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def members(self, knowledge_base_id: str) -> list[KnowledgeBaseMemberRow]:
        """One knowledge base's entries; none before revision 0239 (a home's default without a list)."""
        async with open_pooled_read_ctx(self._db_path) as db:
            async with db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='kb_knowledge_base_members'") as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute(
                "SELECT knowledge_base_id,principal_type,principal_id,level,added_by,created_at FROM kb_knowledge_base_members "
                "WHERE knowledge_base_id=? ORDER BY principal_type, created_at, principal_id",
                (knowledge_base_id,),
            ) as cursor:
                return [KnowledgeBaseMemberRow(*(str(value) for value in row)) for row in await cursor.fetchall()]

    async def apply_member_changes(
        self, knowledge_base: KnowledgeBaseRow, changes: Iterable[tuple[str, str, str | None]], *, added_by: str
    ) -> list[KnowledgeBaseMemberRow]:
        """Add, change (a level) or remove (``None``) entries of one live knowledge base; returns the new list.

        Raises :class:`KnowledgeBaseConflict` ``knowledge_base_not_found`` when it was deleted meanwhile
        and ``knowledge_base_member_limit`` past :data:`MAX_MEMBER_ENTRIES` entries.
        """
        now = _now()
        async with diagnostic_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if knowledge_base.is_default:
                    await ensure_default(db, knowledge_base.team_id, knowledge_base.home, knowledge_base.home_id, now=now)
                async with db.execute("SELECT 1 FROM kb_knowledge_bases WHERE id=? AND deleted_at IS NULL", (knowledge_base.id,)) as cursor:
                    if await cursor.fetchone() is None:
                        raise KnowledgeBaseConflict("knowledge_base_not_found")
                for principal_type, principal_id, level in changes:
                    if level is None:
                        await db.execute(
                            "DELETE FROM kb_knowledge_base_members WHERE knowledge_base_id=? AND principal_type=? AND principal_id=?",
                            (knowledge_base.id, principal_type, principal_id),
                        )
                        continue
                    await db.execute(
                        "INSERT INTO kb_knowledge_base_members(knowledge_base_id,principal_type,principal_id,level,added_by,created_at) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(knowledge_base_id,principal_type,principal_id) DO UPDATE SET level=excluded.level",
                        (knowledge_base.id, principal_type, principal_id, level, added_by, now),
                    )
                async with db.execute("SELECT COUNT(*) FROM kb_knowledge_base_members WHERE knowledge_base_id=?", (knowledge_base.id,)) as cursor:
                    if int((await cursor.fetchone())[0]) > MAX_MEMBER_ENTRIES:
                        raise KnowledgeBaseConflict("knowledge_base_member_limit")
                await db.execute("UPDATE kb_knowledge_bases SET updated_at=? WHERE id=?", (now, knowledge_base.id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.members(knowledge_base.id)

    async def transfer(
        self, knowledge_base: KnowledgeBaseRow, *, new_owner: str, expected_revision: int | None, keep_previous_owner: bool, added_by: str
    ) -> KnowledgeBaseRow:
        """Make ``new_owner`` the owner: their own entry goes, and the previous owner may stay as a manager.

        Raises :class:`KnowledgeBaseConflict` ``knowledge_base_revision_conflict`` when the knowledge
        base, its owner or its revision changed meanwhile.
        """
        now = _now()
        async with diagnostic_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE kb_knowledge_bases SET owner_user_id=?, revision=revision+1, updated_at=? WHERE id=? AND is_default=0 "
                    "AND home!='personal' AND deleted_at IS NULL AND owner_user_id IS ? AND (? IS NULL OR revision=?)",
                    (new_owner, now, knowledge_base.id, knowledge_base.owner_user_id, expected_revision, expected_revision),
                )
                changed = cursor.rowcount
                await cursor.close()
                if not changed:
                    raise KnowledgeBaseConflict("knowledge_base_revision_conflict")
                await db.execute(
                    "DELETE FROM kb_knowledge_base_members WHERE knowledge_base_id=? AND principal_type='user' AND principal_id=?",
                    (knowledge_base.id, new_owner),
                )
                if keep_previous_owner and knowledge_base.owner_user_id:
                    await db.execute(
                        "INSERT INTO kb_knowledge_base_members(knowledge_base_id,principal_type,principal_id,level,added_by,created_at) "
                        "VALUES (?,'user',?,'manage',?,?) ON CONFLICT(knowledge_base_id,principal_type,principal_id) DO UPDATE SET level='manage'",
                        (knowledge_base.id, knowledge_base.owner_user_id, added_by, now),
                    )
                row = await get_knowledge_base(db, knowledge_base.id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        assert row is not None
        return row
