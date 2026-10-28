"""Background discovery metadata for the actual registered system profiles."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING

import aiosqlite
import anyio

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import (
    _system_revision_projection_base,
    project_system_agent_revision,
)
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.builtin_subagent_override import effective_description, restrict_override_to_manifest
from bowhead_core.builtin_subagent_override_store import _row_to_override
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata
from bowhead_core.system_agent_marketplace import (
    SYSTEM_AGENT_LISTINGS,
    system_agent_localized_display_name,
    system_agent_localized_summary,
)

if TYPE_CHECKING:
    from bowhead_core.subagent import SubagentRunner

logger = logging.getLogger(__name__)
REFRESH_INTERVAL_SECONDS = 15
MAX_METADATA_AGE_SECONDS = 30


def system_reference_runtime_modes() -> tuple[bool, bool]:
    from bowhead_core.subagent import is_subagent_spec_resolve_enabled

    return is_subagent_spec_resolve_enabled(), True


def system_override_metadata_stamp(row: dict | None) -> tuple[int, bool] | None:
    # The existing override store versions every change, including reverting
    # content. No prompt/Skill body is needed on the discovery request path.
    return (int(row["row_version"]), bool(row["enabled"])) if row is not None else None


@dataclass(frozen=True, slots=True)
class SystemAgentReferenceMetadata:
    team_id: str
    workspace_id: str
    name: str
    revision: str
    override_stamp: tuple[int, bool] | None
    label: str
    summary: str
    label_overridden: bool
    receiver: ReferenceReceiverMetadata | None = field(default=None, repr=False)
    summary_overridden: bool = False

    def localized_label(self, locale: str) -> str:
        return self.label if self.label_overridden else system_agent_localized_display_name(self.name, locale)

    def localized_summary(self, locale: str) -> str:
        return self.summary if self.summary_overridden else system_agent_localized_summary(self.name, locale)[:1024] or self.summary


@dataclass(frozen=True, slots=True)
class SystemAgentReferenceMetadataIndex:
    db_path: Path = field(repr=False)
    modes: tuple[bool, bool]
    scopes: frozenset[tuple[str, str]] = frozenset()
    items: tuple[SystemAgentReferenceMetadata, ...] = ()
    names: frozenset[tuple[str, str, str]] = frozenset()
    _lookup: Mapping[tuple[str, str, str], SystemAgentReferenceMetadata] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        lookup = {(item.team_id, item.workspace_id, item.name): item for item in self.items}
        if len(lookup) != len(self.items):
            raise ValueError("reference_system_metadata_ambiguous")
        object.__setattr__(self, "_lookup", MappingProxyType(lookup))

    def find(self, *, team_id: str, workspace_id: str, name: str, override: dict | None) -> SystemAgentReferenceMetadata | None:
        if self.modes != system_reference_runtime_modes():
            return None
        item = self._lookup.get((team_id, workspace_id, name))
        return item if item is not None and item.override_stamp == system_override_metadata_stamp(override) else None


def _build_scope(
    runner: SubagentRunner, team_id: str, workspace_id: str, overrides: dict[str, dict], modes: tuple[bool, bool]
) -> tuple[tuple[SystemAgentReferenceMetadata, ...], frozenset[tuple[str, str, str]]]:
    from bowhead_core.subagent import _system_profile_snapshot

    runtime = copy.copy(runner)
    runtime._shadow_workspace_id = workspace_id
    result = []
    names = set()
    for name, listing in SYSTEM_AGENT_LISTINGS.items():
        row = overrides.get(name)
        if row is not None and (not row["enabled"] or not modes[1]):
            continue
        try:
            override = restrict_override_to_manifest(name, _row_to_override(row)) if row is not None else None
            known, profile = runtime._resolve_system_profile(name, team_id=team_id, builtin_overrides={name: override} if override else None)
            if not known or profile is None:
                continue
            names.add((team_id, workspace_id, name))
            # Same definition, revision and behavior projection as actual input
            # preparation. No Agent execution or model call is needed.
            definition = _system_profile_snapshot(profile)
            projection = _system_revision_projection_base(team_id=team_id, profile_name=name, definition=definition)
            try:
                prepared = project_system_agent_revision(
                    team_id=team_id,
                    profile_name=name,
                    definition=definition,
                )
                receiver = ReferenceReceiverMetadata.from_definition(
                    definition,
                    behavior=prepared.behavior_descriptor,
                    system=True,
                )
            except (ValueError, TypeError, KeyError):
                receiver = None
            label_overridden = bool(override and override.display_name and override.display_name.strip())
            label = profile.display_name if label_overridden else listing["display_name"]
            summary, _ = effective_description(listing["summary"], override)
            summary_overridden = summary != listing["summary"]
            if scan_agent_listing(name=name, display_name=label, description=summary).blocked:
                label, summary, label_overridden, summary_overridden = listing["display_name"], listing["summary"], False, False
            if scan_agent_listing(name=name, display_name=label, description=summary).blocked:
                continue
            result.append(
                SystemAgentReferenceMetadata(
                    team_id,
                    workspace_id,
                    name,
                    projection[3],
                    system_override_metadata_stamp(row),
                    label[:256],
                    summary[:1024],
                    label_overridden,
                    receiver,
                    summary_overridden,
                )
            )
        except Exception as exc:
            logger.debug("reference_system_metadata_unavailable error=%s", type(exc).__name__)
    return tuple(result), frozenset(names)


async def build_system_agent_reference_metadata(db_path: str | Path, runner: SubagentRunner) -> SystemAgentReferenceMetadataIndex:
    from bowhead_core.subagent import SubagentRunner

    if not isinstance(runner, SubagentRunner) or Path(getattr(runner.team_subagent_store, "db_path", "")) != Path(db_path):
        raise ValueError("reference_system_metadata_unavailable")

    async def source_rows():
        async with open_shared_ctx(db_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                async with db.execute(
                    "SELECT w.team_id,w.id FROM workspaces w JOIN teams t ON t.id=w.team_id WHERE w.status='active' AND t.status='active'"
                ) as cursor:
                    scopes = frozenset(tuple(row) for row in await cursor.fetchall())
                async with db.execute("SELECT * FROM team_builtin_subagent_override") as cursor:
                    cursor.row_factory = aiosqlite.Row
                    overrides = {(row["team_id"], row["name"]): dict(row) for row in await cursor.fetchall()}
                return scopes, overrides
            finally:
                await db.rollback()

    modes = system_reference_runtime_modes()
    scopes, overrides = await source_rows()
    items = []
    names = set()
    team_templates = {}
    shared_base = not modes[0] or getattr(runner, "_registry", None) is None
    for team_id, workspace_id in sorted(scopes):
        team_overrides = {name: row for (team, name), row in overrides.items() if team == team_id}
        if shared_base and team_id in team_templates:
            template_items, template_names = team_templates[team_id]
            scope_items = tuple(replace(item, workspace_id=workspace_id) for item in template_items)
            scope_names = frozenset((team_id, workspace_id, name) for _, _, name in template_names)
        else:
            scope_items, scope_names = await anyio.to_thread.run_sync(_build_scope, runner, team_id, workspace_id, team_overrides, modes)
            if shared_base:
                team_templates[team_id] = scope_items, scope_names
        items.extend(scope_items)
        names.update(scope_names)
    current_scopes, current_overrides = await source_rows()
    if modes != system_reference_runtime_modes():
        return SystemAgentReferenceMetadataIndex(Path(db_path), system_reference_runtime_modes())
    # Compare complete records off the request path. Pending override races
    # cannot publish a new version under a different metadata stamp.
    items = tuple(
        item
        for item in items
        if (item.team_id, item.workspace_id) in current_scopes
        and overrides.get((item.team_id, item.name)) == current_overrides.get((item.team_id, item.name))
    )
    return SystemAgentReferenceMetadataIndex(Path(db_path), modes, scopes & current_scopes, items, frozenset(names))


class SystemAgentReferenceMetadataCache:
    def __init__(self, db_path: str | Path, runner: SubagentRunner):
        self.db_path, self.runner = Path(db_path), runner
        self._index: SystemAgentReferenceMetadataIndex | None = None
        self._expires_at = 0.0
        self._lock = anyio.Lock()

    def current(self) -> SystemAgentReferenceMetadataIndex:
        if self._index is not None and monotonic() < self._expires_at:
            return self._index
        # A wired but cold/expired adapter reports metadata unavailable; it is
        # distinct from an absent adapter and can be retried by the existing UI.
        return SystemAgentReferenceMetadataIndex(self.db_path, system_reference_runtime_modes())

    async def refresh(self) -> None:
        async with self._lock:
            expires_at = monotonic() + MAX_METADATA_AGE_SECONDS
            index = await build_system_agent_reference_metadata(self.db_path, self.runner)
            self._index, self._expires_at = index, expires_at

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                logger.warning("reference_system_metadata_refresh_failed error=%s", type(exc).__name__)
            await anyio.sleep(REFRESH_INTERVAL_SECONDS)
