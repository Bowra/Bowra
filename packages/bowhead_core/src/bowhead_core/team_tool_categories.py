"""Administrator-confirmed categories for remote MCP team tools (``FEATURE_TEAM_TOOL_CONFIRMATION``).

Stored per source in ``team_settings`` under ``team_tool_categories:<source_id>`` as
``{tool_name: {"risk", "schema_digest", "confirmed_by", "confirmed_at"}}``. Only the category
route writes it; the other source writers never touch it.

A record applies only while the tool's cached server definition still has the confirmed schema
digest, so a Refresh Tool List that changes the definition clears it in effect. A record can
settle an ``unknown`` tool or raise a category; it never lowers what the annotations imply.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from bowhead_core._db import open_pooled_read_ctx, open_shared_ctx
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.team_tool_confirmation import CATEGORY_RANK, team_mcp_category, team_tool_schema_digest

logger = logging.getLogger(__name__)

KEY_PREFIX: Final = "team_tool_categories:"
CONFIRMABLE: Final = ("read", "external_write", "destructive")


def _hints(meta: Mapping[str, Any]) -> dict[str, bool]:
    annotations = meta.get("annotations")
    if not isinstance(annotations, dict):
        return {}
    return {key: annotations[key] for key in ("readOnlyHint", "destructiveHint") if isinstance(annotations.get(key), bool)}


def category_digest(meta: Mapping[str, Any]) -> str:
    """Identity of the cached server definition a confirmation applies to."""
    schema = meta.get("input_schema")
    return team_tool_schema_digest(
        name=str(meta.get("name") or ""),
        description=str(meta.get("description") or ""),
        input_schema=dict(schema) if isinstance(schema, dict) else {},
        annotations=_hints(meta),
    )


def annotation_category(meta: Mapping[str, Any]) -> str:
    hints = _hints(meta)
    return team_mcp_category(read_only=hints.get("readOnlyHint"), destructive=hints.get("destructiveHint"))[0]


def lowers_annotation(category: str, meta: Mapping[str, Any]) -> bool:
    base = annotation_category(meta)
    return base != "unknown" and CATEGORY_RANK[category] < CATEGORY_RANK[base]


def effective_admin_category(meta: Mapping[str, Any], record: object) -> str | None:
    """The confirmed category when the record still applies to this definition, else None."""
    if not isinstance(record, dict):
        return None
    category = record.get("risk")
    if category not in CONFIRMABLE or record.get("schema_digest") != category_digest(meta):
        return None
    if lowers_annotation(str(category), meta):
        return None
    return str(category)


def _core(db_path: str | Path) -> str:
    return str(domain_database_path(db_path, "core"))


def _decode(value: object) -> dict[str, Any]:
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def load_team_tool_categories(db_path: str | Path, team_id: str) -> dict[str, dict[str, Any]]:
    """``{source_id: {tool_name: record}}``. A read failure yields no confirmations (fail closed)."""
    try:
        async with (
            open_pooled_read_ctx(_core(db_path)) as db,
            db.execute(
                "SELECT key, value FROM team_settings WHERE team_id=? AND key LIKE ?",
                (team_id, f"{KEY_PREFIX}%"),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — confirmations only ever add trust; losing them is safe
        logger.warning("team_tool_categories.load_failed team=%s err=%s", team_id, type(exc).__name__)
        return {}
    return {str(key)[len(KEY_PREFIX) :]: _decode(value) for key, value in rows}


async def save_team_tool_categories(
    db_path: str | Path,
    *,
    team_id: str,
    source_id: str,
    changes: Mapping[str, str | None],
    tools: Mapping[str, Mapping[str, Any]],
    actor_user_id: str,
) -> dict[str, Any] | None:
    """Apply ``changes`` (``None`` removes) and bump the source revision in one transaction.

    The caller validated names and categories against ``tools``. Returns the stored records,
    or None when the source no longer exists in this Team.
    """
    now = datetime.now(tz=UTC).isoformat()
    key = f"{KEY_PREFIX}{source_id}"
    async with open_shared_ctx(_core(db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute("SELECT 1 FROM team_tool_source WHERE team_id=? AND id=?", (team_id, source_id)) as cursor:
                exists = await cursor.fetchone()
            if not exists:
                await db.rollback()
                return None
            records = _decode(await TeamSettingsStore.get_with_connection(db, team_id, key) or "{}")
            for name, category in changes.items():
                if category is None:
                    records.pop(name, None)
                else:
                    records[name] = {
                        "risk": category,
                        "schema_digest": category_digest(tools[name]),
                        "confirmed_by": actor_user_id,
                        "confirmed_at": now,
                    }
            if records:
                await TeamSettingsStore.set_with_connection(
                    db, team_id, key, json.dumps(records, ensure_ascii=False, sort_keys=True), actor_user_id, updated_at=now
                )
            else:
                await db.execute("DELETE FROM team_settings WHERE team_id=? AND key=?", (team_id, key))
            # The source revision is each tool's dependency revision, so dependent Agents see the change.
            await db.execute("UPDATE team_tool_source SET updated_at=? WHERE team_id=? AND id=?", (now, team_id, source_id))
            await db.commit()
            return records
        except BaseException:
            await db.rollback()
            raise
