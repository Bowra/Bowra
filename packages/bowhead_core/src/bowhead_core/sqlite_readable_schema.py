"""Read-only business availability across the managed SQLite domain attachments.

These helpers never attest a writer schema or install migrations. Initializers,
DDL checks and persistent guard fingerprints must still inspect their own main.
"""

from __future__ import annotations

from typing import Literal

import aiosqlite

from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES, TABLE_DOMAINS

_READABLE_SCHEMAS = frozenset({"main", *(f"domain_{name}" for name in DOMAIN_TARGET_FILES)})


async def _schemas(db: aiosqlite.Connection) -> tuple[str, ...]:
    async with db.execute("PRAGMA database_list") as cursor:
        return tuple(row[1] for row in await cursor.fetchall() if row[1] in _READABLE_SCHEMAS)


async def readable_schema_objects(db: aiosqlite.Connection, *, kind: Literal["table", "view"] = "table") -> frozenset[str]:
    """List readable business objects; permission/corruption errors propagate."""
    schemas = await _schemas(db)
    query = " UNION ALL ".join(f'SELECT name FROM "{schema}".sqlite_schema WHERE type=?' for schema in schemas)
    async with db.execute(query, (kind,) * len(schemas)) as cursor:
        return frozenset(str(row[0]) for row in await cursor.fetchall())


async def _readable_object_exists(db: aiosqlite.Connection, name: str, *, kind: str, owner_domain: str | None) -> bool:
    schemas = await _schemas(db)
    if owner_domain is not None:
        if owner_domain not in DOMAIN_TARGET_FILES:
            raise ValueError("unknown SQLite object owner domain")
        attached = f"domain_{owner_domain}"
        schemas = (attached,) if attached in schemas else ("main",)
    # A known owner avoids taking unrelated authority leases just to discover
    # one table. Isolated legacy stores continue to inspect their own main.
    query = " UNION ALL ".join(f'SELECT 1 FROM "{schema}".sqlite_schema WHERE type=? AND name=?' for schema in schemas) + " LIMIT 1"
    async with db.execute(query, tuple(item for _ in schemas for item in (kind, name))) as cursor:
        return await cursor.fetchone() is not None


async def readable_table_exists(db: aiosqlite.Connection, table: str) -> bool:
    """Check a readable table without interpreting failed reads as absence."""
    return await _readable_object_exists(db, table, kind="table", owner_domain=TABLE_DOMAINS.get(table))


async def readable_view_exists(db: aiosqlite.Connection, view: str, *, owner_domain: str | None = None) -> bool:
    """Check a readable view without accepting a different object's type."""
    return await _readable_object_exists(db, view, kind="view", owner_domain=owner_domain)
