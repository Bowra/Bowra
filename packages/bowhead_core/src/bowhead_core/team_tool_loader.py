"""cca LOAD POINT — load an team's enabled OpenAPI tool sources into engine entries.

Bridges the data layer (``UserStore.team_tool_source``) to the engine
(``openapi_tool_source``): parse each enabled source's stored OpenAPI spec, filter to the
team admin's selected ops, and return the ``(source_id, OpenApiOp)`` entries that
``build_team_mcp_server`` + ``build_team_specs`` consume — plus a ``source_id → TeamToolSource``
map for the executor's base_url / auth resolution.

Pure + fail-closed: a falsy team_id, no store, or a malformed/oversized spec yields no
entries and NEVER raises into the turn (a broken source must not break the whole turn).
P1 parses on load each turn; per-team caching keyed on (source_id, updated_at) is P2.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from bowhead_core.openapi_tool_source import (
    SourceConfigResolver,
    TeamToolEntry,
    mcp_entry,
    openapi_entry,
    parse_openapi_ops,
)

if TYPE_CHECKING:
    from bowhead_core.user_store import TeamToolSource

logger = logging.getLogger(__name__)


class _SourceStore(Protocol):
    async def list_team_tool_sources(
        self,
        team_id: str,
        *,
        only_enabled: bool = ...,
    ) -> list[TeamToolSource]: ...


class _AuthHeaderProvider(Protocol):
    async def get_headers(self, team_id: str, source_id: str) -> dict[str, str]: ...


async def load_team_tool_entries(
    team_id: str,
    store: _SourceStore | None,
) -> tuple[list[TeamToolEntry], dict[str, TeamToolSource]]:
    """Return ``([TeamToolEntry], {source_id: source})`` for one team's ENABLED sources.
    Fail-closed: falsy team_id / no store ⇒ ``([], {})``; a source that won't load is skipped
    (logged), never breaks the turn. Both kinds are NETWORK-FREE here: OpenAPI parses its frozen
    spec, remote MCP reads its ``tools_schema_json`` enumerate cache. The actual remote connect
    (kind=mcp) happens later in the turn_runner view builder, not here."""
    if not team_id or store is None:
        return [], {}
    sources = await store.list_team_tool_sources(team_id, only_enabled=True)
    entries: list[TeamToolEntry] = []
    by_id: dict[str, TeamToolSource] = {}
    for src in sources:
        if src.kind == "mcp":
            # WP2: build entries from the enumerate cache (no network). dispatch_name = the remote
            # server's own raw tool name; the description override (keyed by tool name) is overlaid.
            selected = set(src.selected_op_ids or [])
            ovr = src.tool_overrides or {}
            added = False
            for tm in src.tools_schema or []:
                raw = str(tm.get("name") or "")
                if not raw or (selected and raw not in selected):
                    continue
                meta = dict(tm)
                desc_ovr = (ovr.get(raw) or {}).get("description")
                if desc_ovr:
                    meta["description"] = desc_ovr
                entries.append(mcp_entry(src.id, meta))
                added = True
            if added:
                by_id[src.id] = src
            continue
        if src.kind != "openapi" or not src.openapi_spec_json:
            continue
        try:
            spec = json.loads(src.openapi_spec_json)
            all_ops = parse_openapi_ops(spec)
        except Exception:
            logger.warning("cca.load_team_sources.parse_failed team=%s src=%s", team_id, src.id)
            continue
        selected = set(src.selected_op_ids or [])
        ops = [op for op in all_ops if not selected or op.operation_id in selected]
        if not ops:
            continue
        # cca P2: overlay the per-op DESCRIPTION override (LLM-proposed / user-edited) onto op.summary
        # so build_team_specs + the picker advertise the better description with NO downstream change.
        # The registered tool NAME (teammcp.<src>.<op>) is derived elsewhere and stays unaffected.
        ovr = src.tool_overrides or {}
        if ovr:
            ops = [
                replace(op, summary=ovr[op.operation_id]["description"]) if op.operation_id in ovr and ovr[op.operation_id].get("description") else op
                for op in ops
            ]
        by_id[src.id] = src
        entries.extend(openapi_entry(src.id, op) for op in ops)
    # Fail closed on tool-name collisions (two operations whose sanitized names coincide, stored
    # before create/patch refused them): dispatch is keyed by name, so either tool could reach
    # the other operation's endpoint. Expose neither; the editor reports the conflict.
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.advertised_name] = counts.get(entry.advertised_name, 0) + 1
    colliding = {name for name, count in counts.items() if count > 1}
    if colliding:
        logger.warning("cca.load_team_sources.tool_name_conflict team=%s names=%s", team_id, sorted(colliding))
        entries = [entry for entry in entries if entry.advertised_name not in colliding]
    return entries, by_id


def make_team_source_resolver(
    team_id: str,
    by_id: dict[str, TeamToolSource],
    auth: _AuthHeaderProvider,
) -> SourceConfigResolver:
    """Build the executor's source-config resolver: ``source_id → (base_url, auth_headers)``.

    Bound to ``team_id`` (the engine's ``make_http_executor`` also re-binds team_id, so this
    is belt-and-suspenders for cca review #3/#4): a source_id not in this team's ``by_id``,
    or a call whose team_id doesn't match, resolves to ``("", {})`` → the executor's
    SSRF guard then blocks the empty host (fail-closed, never leaks another team's config)."""

    async def _resolve(call_team_id: str, source_id: str) -> tuple[str, dict[str, str]]:
        src = by_id.get(source_id)
        if src is None or call_team_id != team_id:
            return "", {}
        headers = await auth.get_headers(team_id, source_id)
        return src.base_url, headers

    return _resolve
