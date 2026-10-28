"""Plugin versions an Agent release was made with, and which of them changed since.

Capability Center tech design §13 item 3 / PRD decision 17: Plugins float to their latest version
after release, so each release candidate records the exact versions it was validated with. The
Agent details and release pages compare them with the current versions to show "consider
re-running evaluation".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aiosqlite


def _latest_builtin_versions() -> dict[str, int]:
    from bowhead_core.system_plugins import builtin_plugins

    latest: dict[str, int] = {}
    for plugin in builtin_plugins():
        latest[plugin.name] = max(plugin.version, latest.get(plugin.name, 0))
    return latest


def _builtin_names(definition: Mapping[str, Any]) -> list[str]:
    value = definition.get("builtin_plugin_ids")
    if not isinstance(value, list):
        return []
    return [item.removeprefix("builtin:") for item in value if isinstance(item, str) and item.startswith("builtin:")]


async def plugin_versions_conn(db: aiosqlite.Connection, *, team_id: str, definition: Mapping[str, Any]) -> str | None:
    """Canonical JSON of the Plugin versions a definition resolves to now; None without Plugins."""
    from bowhead_core.team_plugin_store import selected_plugin_ids

    entries: list[dict[str, Any]] = []
    for plugin_id in selected_plugin_ids(definition):
        async with db.execute("SELECT current_revision FROM team_capability WHERE team_id=? AND id=?", (team_id, plugin_id)) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            entries.append({"kind": "team", "id": plugin_id, "version": int(row[0])})
    latest = _latest_builtin_versions()
    for name in _builtin_names(definition):
        if name in latest:
            entries.append({"kind": "builtin", "id": f"builtin:{name}", "version": latest[name]})
    if not entries:
        return None
    return json.dumps(sorted(entries, key=lambda item: (item["kind"], item["id"])), separators=(",", ":"), sort_keys=True)


async def plugin_updates_conn(db: aiosqlite.Connection, *, team_id: str, recorded_json: str | None) -> list[dict[str, Any]]:
    """Plugins whose current version is newer than the recorded release-time version."""
    if not recorded_json:
        return []
    try:
        recorded = json.loads(recorded_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(recorded, list):
        return []
    latest = _latest_builtin_versions()
    updates: list[dict[str, Any]] = []
    for entry in recorded:
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), int):
            continue
        kind, ident, version = entry.get("kind"), str(entry.get("id")), int(entry["version"])
        if kind == "team":
            async with db.execute(
                """SELECT c.current_revision, c.updated_at, r.name FROM team_capability c
                   JOIN team_capability_revision r ON r.capability_id=c.id AND r.revision=c.current_revision
                   WHERE c.team_id=? AND c.id=?""",
                (team_id, ident),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and int(row[0]) > version:
                updates.append(
                    {
                        "kind": "team",
                        "id": ident,
                        "name": str(row[2]),
                        "release_version": version,
                        "current_version": int(row[0]),
                        "changed_at": str(row[1]),
                    }
                )
        elif kind == "builtin":
            name = ident.removeprefix("builtin:")
            current = latest.get(name)
            if current is not None and current > version:
                updates.append(
                    {"kind": "builtin", "id": ident, "name": name, "release_version": version, "current_version": current, "changed_at": None}
                )
    return updates
