"""Skills an Agent release was made with whose approved revision changed since.

Agent Skill gates D2: Agents follow each Skill's latest approved revision at
the next run, while the released definition records the exact revision it was
validated with. The Agent details and release pages compare the two and suggest
re-running evaluation, next to ``plugin_updates``.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

import aiosqlite
import anyio

from bowhead_core.agent_dependency_compiler import approved_revision_token, parse_exact_skill_grants, skill_bundle_revision


async def skill_updates_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    owner_user_id: str,
    definition: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Skills whose current approved revision differs from the recorded grant revision.

    Each entry is ``{name, identity, release_revision, current_revision, changed_at}``.
    A Skill that is gone or unreadable is left out: it is reported by
    publication and run-time checks, not as an update.
    """
    try:
        grants = parse_exact_skill_grants(definition.get("skill_grants", []))
    except ValueError:
        return []
    updates: list[dict[str, Any]] = []
    for grant in grants:
        current = await _current_revision_conn(db, team_id=team_id, owner_user_id=owner_user_id, kind=grant.kind, skill_id=grant.id)
        if current is None:
            continue
        revision, changed_at = current
        if revision != grant.revision:
            updates.append(
                {
                    "name": grant.name,
                    "identity": grant.identity,
                    "release_revision": grant.revision,
                    "current_revision": revision,
                    "changed_at": changed_at,
                },
            )
    return updates


@cache
def _builtin_catalog() -> Any:
    """The code-owned built-in Skill catalog; it does not change while the process runs."""
    from bowhead_skill import SkillCatalog
    from bowhead_skill.catalog import builtin_catalog_dir

    return SkillCatalog(roots=[builtin_catalog_dir()])


async def _current_revision_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    owner_user_id: str,
    kind: str,
    skill_id: str,
) -> tuple[str, str | None] | None:
    if kind == "team":
        async with db.execute(
            "SELECT approved_sha256,approved_at,updated_at FROM team_skill WHERE team_id=? AND id=?",
            (team_id, skill_id),
        ) as cursor:
            row = await cursor.fetchone()
        revision = approved_revision_token(row[0]) if row is not None else None
        if revision is None:
            return None
        return revision, str(row[1] or row[2] or "") or None
    if kind == "user":
        try:
            async with db.execute(
                "SELECT source_path,updated_at FROM skill_instances WHERE id=? AND user_id=?",
                (skill_id, owner_user_id),
            ) as cursor:
                row = await cursor.fetchone()
        except aiosqlite.OperationalError:
            # Personal Skills live in a table created on first use.
            return None
        if row is None:
            return None
        revision = await anyio.to_thread.run_sync(skill_bundle_revision, str(row[0] or ""))
        return (revision, str(row[1] or "") or None) if revision is not None else None
    if kind == "builtin":
        spec = _builtin_catalog().get(skill_id.removeprefix("builtin:"))
        if spec is None:
            return None
        revision = await anyio.to_thread.run_sync(skill_bundle_revision, str(spec.source_dir))
        return (revision, None) if revision is not None else None
    return None
