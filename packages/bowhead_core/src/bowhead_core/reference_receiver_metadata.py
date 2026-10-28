"""Background projection of published receivers for metadata-only composition checks.

This cache contains immutable dependency declarations, never current user grants
or execution permission. Discovery must join it to the current exact receiver.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from types import MappingProxyType

import aiosqlite
import anyio

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor
from bowhead_core.agent_dependency_compiler import AGENT_DEPENDENCY_COMPILER_VERSION, ExactSkillGrant
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensions
from bowhead_core.chat_references import AgentResourceRef, decode_reference_json
from bowhead_core.skill_read_extensions import published_extension_policy
from bowhead_core.skill_read_receiver import exact_reference_skill_grants, require_supported_skill_grants

logger = logging.getLogger(__name__)

_MAX_BASELINE_TOOLS = 128
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True, slots=True)
class ReferenceTeamToolBaseline:
    source_id: str
    operation_name: str
    source_revision: str
    risk: str
    approval_required: bool

    def __post_init__(self):
        if (
            not isinstance(self.source_id, str)
            or not _IDENTITY_RE.fullmatch(self.source_id)
            or not isinstance(self.operation_name, str)
            or not self.operation_name.startswith(f"teammcp.{self.source_id}.")
            or len(self.operation_name) > 200
            or not isinstance(self.source_revision, str)
            or not self.source_revision
            or len(self.source_revision) > 200
            or not isinstance(self.risk, str)
            or self.risk not in {"read", "compute", "artifact_write", "external_write", "destructive", "unknown"}
            or type(self.approval_required) is not bool
        ):
            raise ValueError("invalid team Tool baseline")


def _definition_tool_refs(definition: dict) -> frozenset[str]:
    if "tool_refs" in definition and "tool_refs_json" in definition:
        raise ValueError("ambiguous tool refs")
    raw = definition.get("tool_refs")
    if raw is None and "tool_refs_json" in definition:
        raw = decode_reference_json(str(definition["tool_refs_json"]), max_bytes=32_768)
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or len(raw) > _MAX_BASELINE_TOOLS or any(not isinstance(item, str) or not item for item in raw):
        raise ValueError("invalid tool refs")
    values = frozenset(raw)
    if len(values) != len(raw):
        raise ValueError("duplicate tool refs")
    return values


def published_team_tool_baseline(
    definition: dict,
    dependency_json: str | None,
    *,
    expected_fingerprint: str,
    projection_compiler: str | None,
) -> tuple[ReferenceTeamToolBaseline, ...]:
    """Read exact Team Tool baselines from the current release projection.

    This is metadata only. It neither grants the current user access nor makes
    an MCP entry executable. A missing, stale, or contradictory projection
    yields no Team Tool baseline while leaving other receiver metadata usable.
    """
    try:
        refs = _definition_tool_refs(definition)
        if dependency_json is None or projection_compiler is None:
            raise ValueError
        payload = decode_reference_json(dependency_json, max_bytes=262_144)
        if (
            not isinstance(payload, dict)
            or projection_compiler != AGENT_DEPENDENCY_COMPILER_VERSION
            or payload.get("schema_version") != 1
            or payload.get("fingerprint") != expected_fingerprint
            or payload.get("compiler_version") != projection_compiler
            or payload.get("status") not in {"ready", "review_required"}
            or not isinstance(payload.get("tools"), list)
            or len(payload["tools"]) > _MAX_BASELINE_TOOLS
        ):
            raise ValueError
        result = []
        identities: set[str] = set()
        for raw in payload["tools"]:
            if not isinstance(raw, dict) or raw.get("source_kind") != "team_mcp":
                continue
            stable_ref = raw.get("stable_ref")
            connection_ref = raw.get("connection_ref")
            if (
                not isinstance(stable_ref, str)
                or stable_ref not in refs
                or not isinstance(connection_ref, str)
                or not connection_ref.startswith("team_tool_source:")
                or stable_ref in identities
            ):
                raise ValueError
            source_id = connection_ref.removeprefix("team_tool_source:")
            item = ReferenceTeamToolBaseline(
                source_id=source_id,
                operation_name=stable_ref,
                source_revision=raw.get("revision"),
                risk=raw.get("risk"),
                approval_required=raw.get("approval_required"),
            )
            identities.add(stable_ref)
            result.append(item)
        return tuple(sorted(result, key=lambda item: item.operation_name))
    except (TypeError, ValueError, KeyError):
        return ()


@dataclass(frozen=True, slots=True)
class ReferenceReceiverMetadata:
    grants: tuple[ExactSkillGrant, ...]
    policy: AgentRuntimeExtensions | None = None
    behavior: AgentBehaviorDescriptor | None = None
    team_tools: tuple[ReferenceTeamToolBaseline, ...] = ()
    tool_refs: tuple[str, ...] = ()
    ordinary_system_scope: bool = False

    def __post_init__(self):
        if type(self.team_tools) is not tuple or any(not isinstance(item, ReferenceTeamToolBaseline) for item in self.team_tools):
            raise ValueError("invalid receiver Team Tool baseline")
        identities = [item.operation_name for item in self.team_tools]
        if len(set(identities)) != len(identities) or identities != sorted(identities):
            raise ValueError("invalid receiver Team Tool baseline")

    def has_team_tool_baseline(self, *, source_id: str, operation_name: str, source_revision: str) -> bool:
        return (
            self.team_tool_baseline(
                source_id=source_id,
                operation_name=operation_name,
                source_revision=source_revision,
            )
            is not None
        )

    def team_tool_baseline(
        self,
        *,
        source_id: str,
        operation_name: str,
        source_revision: str,
    ) -> ReferenceTeamToolBaseline | None:
        return next(
            (
                item
                for item in self.team_tools
                if item.source_id == source_id and item.operation_name == operation_name and item.source_revision == source_revision
            ),
            None,
        )

    @classmethod
    def from_definition(
        cls,
        definition: dict,
        policy: AgentRuntimeExtensions | None = None,
        behavior: AgentBehaviorDescriptor | None = None,
        team_tools: tuple[ReferenceTeamToolBaseline, ...] = (),
        *,
        system: bool = False,
    ):
        if system:
            from bowhead_core.system_skill_snapshot import has_ordinary_system_skill_scope

            if has_ordinary_system_skill_scope(definition):
                return cls((), behavior=behavior, ordinary_system_scope=True)
        grants = exact_reference_skill_grants(definition)
        require_supported_skill_grants(grants)
        return cls(grants, policy, behavior, team_tools, tuple(sorted(_definition_tool_refs(definition))))


@dataclass(frozen=True, slots=True)
class TeamReferenceReceiverMetadata:
    team_id: str
    agent_id: str
    revision_ref: str
    release_id: str
    receiver: ReferenceReceiverMetadata


@dataclass(frozen=True, slots=True)
class ReferenceReceiverMetadataIndex:
    db_path: Path
    items: tuple[TeamReferenceReceiverMetadata, ...] = ()
    _lookup: Mapping[tuple[str, str, str, str], ReferenceReceiverMetadata] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        values = {(item.team_id, item.agent_id, item.revision_ref, item.release_id): item.receiver for item in self.items}
        if len(values) != len(self.items):
            raise ValueError("ambiguous receiver metadata")
        object.__setattr__(self, "_lookup", MappingProxyType(values))

    def find(self, team_id: str, resource: AgentResourceRef) -> ReferenceReceiverMetadata | None:
        if resource.source_kind != "team":
            return None
        return self._lookup.get((team_id, resource.id, resource.version, resource.release_id))


async def read_reference_team_tools_in_connection(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    resource: AgentResourceRef,
) -> tuple[ReferenceTeamToolBaseline, ...] | None:
    """Read the exact current Team Tool baseline for one published receiver.

    This is an admission-time projection, not a permission check.  It uses the
    caller's transaction so a later Agent admission cannot race a release or
    dependency projection change.  Invalid legacy projections deliberately
    collapse to an empty baseline; a non-current receiver returns ``None``.
    """
    if not db.in_transaction:
        raise ValueError("reference_transaction_required")
    if resource.source_kind != "team":
        return None
    async with db.execute(
        """SELECT r.definition_json,r.dependency_fingerprint,
                  projection.dependency_json,projection.compiler_version AS projection_compiler
           FROM team_subagent a
           JOIN team_subagent_release rel ON rel.team_id=a.team_id AND rel.subagent_id=a.id
             AND rel.id=a.live_release_id AND rel.revision_id=a.live_revision_id
           JOIN team_subagent_revision r ON r.team_id=a.team_id AND r.subagent_id=a.id
             AND r.id=rel.revision_id AND r.dependency_fingerprint=rel.dependency_fingerprint
             AND r.capability_fingerprint=rel.capability_fingerprint
           LEFT JOIN agent_catalog_projection projection ON projection.team_id=a.team_id
             AND projection.agent_id=a.id AND projection.source_kind='team'
             AND projection.source_version=a.live_revision_id
          WHERE a.team_id=? AND a.id=? AND a.live_revision_id=? AND a.live_release_id=?""",
        (team_id, resource.id, resource.version, resource.release_id),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        definition = decode_reference_json(row["definition_json"], max_bytes=1024 * 1024)
        return published_team_tool_baseline(
            definition,
            row["dependency_json"],
            expected_fingerprint=row["dependency_fingerprint"],
            projection_compiler=row["projection_compiler"],
        )
    except (TypeError, ValueError, KeyError):
        return ()


async def build_reference_receiver_metadata(db_path: str | Path) -> ReferenceReceiverMetadataIndex:
    items = []
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        try:
            async with db.execute("""SELECT a.team_id,a.id,a.live_revision_id,a.live_release_id,r.definition_json,
                    r.dependency_fingerprint,behavior.descriptor_json,
                    projection.dependency_json,projection.compiler_version AS projection_compiler
                FROM team_subagent a
                JOIN team_subagent_release rel ON rel.team_id=a.team_id AND rel.subagent_id=a.id
                    AND rel.id=a.live_release_id AND rel.revision_id=a.live_revision_id
                JOIN team_subagent_revision r ON r.team_id=a.team_id AND r.subagent_id=a.id AND r.id=rel.revision_id
                    AND r.dependency_fingerprint=rel.dependency_fingerprint AND r.capability_fingerprint=rel.capability_fingerprint
                JOIN team_subagent_revision_behavior behavior ON behavior.team_id=a.team_id
                    AND behavior.subagent_id=a.id AND behavior.revision_id=r.id
                LEFT JOIN agent_catalog_projection projection ON projection.team_id=a.team_id
                    AND projection.agent_id=a.id AND projection.source_kind='team'
                    AND projection.source_version=a.live_revision_id""") as cursor:
                async for row in cursor:
                    try:
                        definition = await anyio.to_thread.run_sync(
                            lambda raw=row["definition_json"]: decode_reference_json(raw, max_bytes=1024 * 1024)
                        )
                        policy = await published_extension_policy(
                            db,
                            team_id=row["team_id"],
                            agent_id=row["id"],
                            revision_ref=row["live_revision_id"],
                            release_id=row["live_release_id"],
                            definition=definition,
                        )
                        behavior = AgentBehaviorDescriptor.model_validate_json(
                            row["descriptor_json"],
                        )
                        team_tools = published_team_tool_baseline(
                            definition,
                            row["dependency_json"],
                            expected_fingerprint=row["dependency_fingerprint"],
                            projection_compiler=row["projection_compiler"],
                        )
                        receiver = ReferenceReceiverMetadata.from_definition(
                            definition,
                            policy,
                            behavior,
                            team_tools,
                        )
                        items.append(
                            TeamReferenceReceiverMetadata(row["team_id"], row["id"], row["live_revision_id"], row["live_release_id"], receiver)
                        )
                    except (ValueError, TypeError, KeyError):
                        # A legacy/incomplete definition cannot prove a mixed choice.
                        continue
        finally:
            await db.rollback()
    return ReferenceReceiverMetadataIndex(Path(db_path), tuple(items))


class ReferenceReceiverMetadataCache:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._index = ReferenceReceiverMetadataIndex(self.db_path)
        self._expires_at = 0.0
        self._lock = anyio.Lock()

    def current(self) -> ReferenceReceiverMetadataIndex:
        return self._index if monotonic() < self._expires_at else ReferenceReceiverMetadataIndex(self.db_path)

    async def refresh(self) -> None:
        async with self._lock:
            expires_at = monotonic() + 30
            index = await build_reference_receiver_metadata(self.db_path)
            self._index, self._expires_at = index, expires_at

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                logger.warning("reference_receiver_metadata_refresh_failed error=%s", type(exc).__name__)
            await anyio.sleep(15)
