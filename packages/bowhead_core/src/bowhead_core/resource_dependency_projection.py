"""Durable, queryable Agent resource dependency projection.

Definitions and the dependency compiler remain authoritative.  This module
only materializes typed reverse edges and a per-subject completeness marker;
it never grants execution access or reconstructs resource identity from a
display name.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_dependency_compiler import AgentDependencyCompilation
from bowhead_core.openapi_tool_source import mcp_entry, openapi_entry, parse_openapi_ops

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensions

SubjectKind = Literal[
    "agent_working_copy",
    "agent_revision",
    "system_agent_revision",
]
ResourceKind = Literal["skill", "tool_operation", "connection"]
EvidenceLevel = Literal["exact", "stable_only"]
ProjectionStatus = Literal["complete", "partial"]

RESOURCE_DEPENDENCY_PROJECTION_VERSION = "resource-dependency-v1"
_UNRESOLVED_REVISIONS = frozenset({"", "unresolved", "unknown"})


class ResourceDependencyProjectionError(RuntimeError):
    """Raised when an existing immutable projection disagrees with truth."""


@dataclass(frozen=True, slots=True)
class ResourceDependencyEdge:
    team_id: str
    subject_kind: SubjectKind
    subject_ref: str
    agent_id: str
    resource_kind: ResourceKind
    resource_ref: str
    resource_parent_ref: str | None
    resource_revision: str | None
    dependency_role: Literal["required", "optional"]
    evidence_level: EvidenceLevel
    subject_fingerprint: str

    @property
    def edge_key(self) -> str:
        return _hash_json(
            {
                "agent_id": self.agent_id,
                "dependency_role": self.dependency_role,
                "evidence_level": self.evidence_level,
                "resource_kind": self.resource_kind,
                "resource_parent_ref": self.resource_parent_ref,
                "resource_ref": self.resource_ref,
                "resource_revision": self.resource_revision,
                "subject_fingerprint": self.subject_fingerprint,
                "subject_kind": self.subject_kind,
                "subject_ref": self.subject_ref,
                "team_id": self.team_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ResourceDependencyProjection:
    team_id: str
    subject_kind: SubjectKind
    subject_ref: str
    agent_id: str
    subject_fingerprint: str
    status: ProjectionStatus
    reason_code: str | None
    compiler_version: str = RESOURCE_DEPENDENCY_PROJECTION_VERSION

    def __post_init__(self) -> None:
        if self.status == "complete" and self.reason_code is not None:
            raise ValueError("complete dependency projection cannot have a reason")
        if self.status == "partial" and not self.reason_code:
            raise ValueError("partial dependency projection requires a reason")


@dataclass(frozen=True, slots=True)
class ResourceDependencyBackfillResult:
    scanned_count: int
    projected_count: int
    complete_count: int
    partial_count: int
    next_cursor: str | None


def edges_from_compilation(
    *,
    team_id: str,
    subject_kind: SubjectKind,
    subject_ref: str,
    agent_id: str,
    subject_fingerprint: str,
    compilation: AgentDependencyCompilation,
) -> tuple[ResourceDependencyProjection, tuple[ResourceDependencyEdge, ...]]:
    """Convert an exact compiler result without parsing stable Tool names."""
    edges: list[ResourceDependencyEdge] = []
    partial_reasons: set[str] = set()
    for skill in compilation.skills:
        revision = skill.grant.revision if skill.available else None
        evidence: EvidenceLevel = "exact" if revision is not None else "stable_only"
        if revision is None:
            partial_reasons.add("skill_revision_unresolved")
        edges.append(
            ResourceDependencyEdge(
                team_id=team_id,
                subject_kind=subject_kind,
                subject_ref=subject_ref,
                agent_id=agent_id,
                resource_kind="skill",
                resource_ref=skill.grant.identity,
                resource_parent_ref=None,
                resource_revision=revision,
                dependency_role="required",
                evidence_level=evidence,
                subject_fingerprint=subject_fingerprint,
            )
        )
    for tool in compilation.tools:
        revision = tool.revision if tool.revision not in _UNRESOLVED_REVISIONS else None
        evidence = "exact" if revision is not None and tool.available else "stable_only"
        if evidence == "stable_only":
            partial_reasons.add("tool_revision_unresolved")
        if tool.source_kind == "team_mcp" and tool.source_id is None:
            partial_reasons.add("tool_source_identity_unresolved")
        edges.append(
            ResourceDependencyEdge(
                team_id=team_id,
                subject_kind=subject_kind,
                subject_ref=subject_ref,
                agent_id=agent_id,
                resource_kind="tool_operation",
                resource_ref=tool.stable_ref,
                resource_parent_ref=tool.source_id,
                resource_revision=revision,
                dependency_role="required",
                evidence_level=evidence,
                subject_fingerprint=subject_fingerprint,
            )
        )
        if tool.connection_ref is not None:
            edges.append(
                ResourceDependencyEdge(
                    team_id=team_id,
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                    agent_id=agent_id,
                    resource_kind="connection",
                    resource_ref=tool.connection_ref,
                    resource_parent_ref=tool.source_id,
                    resource_revision=revision,
                    dependency_role="required",
                    evidence_level=evidence,
                    subject_fingerprint=subject_fingerprint,
                )
            )
    policy = getattr(compilation, "runtime_extension_policy", None)
    if policy is not None:
        if "agent_runtime_extension_source_unavailable" in compilation.reason_codes:
            partial_reasons.add("optional_skill_source_unavailable")
        edges.extend(
            _optional_skill_edges(
                team_id=team_id,
                subject_kind=subject_kind,
                subject_ref=subject_ref,
                agent_id=agent_id,
                subject_fingerprint=subject_fingerprint,
                policy=policy,
            )
        )
    status: ProjectionStatus = "partial" if partial_reasons else "complete"
    projection = ResourceDependencyProjection(
        team_id=team_id,
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        agent_id=agent_id,
        subject_fingerprint=subject_fingerprint,
        status=status,
        reason_code=(sorted(partial_reasons)[0] if partial_reasons else None),
        compiler_version=compilation.compiler_version,
    )
    return projection, _dedupe_edges(edges)


def stable_edges_from_definition(
    *,
    team_id: str,
    subject_kind: SubjectKind,
    subject_ref: str,
    agent_id: str,
    subject_fingerprint: str,
    definition: Mapping[str, Any],
    team_tool_source_ids: Mapping[str, str] | None = None,
) -> tuple[ResourceDependencyProjection, tuple[ResourceDependencyEdge, ...]]:
    """Recover only identities that are explicit in a persisted definition.

    This powers backfill and legacy system snapshots.  It deliberately does
    not reverse-parse ``teammcp.*`` names.  A caller may pass an authoritative
    stable-ref to source-id mapping built from the Team Tool registry.
    """
    edges: list[ResourceDependencyEdge] = []
    reasons: set[str] = set()
    raw_tools: object = definition.get("tool_refs")
    if subject_kind == "system_agent_revision":
        raw_tools = [
            *(_string_list(definition.get("tool_whitelist")) or ()),
            *(_string_list(definition.get("team_tool_bundle")) or ()),
        ]
    tools = _string_list(raw_tools)
    if tools is None:
        reasons.add("tool_dependency_shape_unrecoverable")
        tools = ()
    for tool_ref in tools:
        source_id = (team_tool_source_ids or {}).get(tool_ref)
        if tool_ref.startswith("teammcp.") and source_id is None:
            reasons.add("tool_source_identity_unresolved")
        edges.append(
            ResourceDependencyEdge(
                team_id=team_id,
                subject_kind=subject_kind,
                subject_ref=subject_ref,
                agent_id=agent_id,
                resource_kind="tool_operation",
                resource_ref=tool_ref,
                resource_parent_ref=source_id,
                resource_revision=None,
                dependency_role="required",
                evidence_level="stable_only",
                subject_fingerprint=subject_fingerprint,
            )
        )
        if source_id is not None:
            edges.append(
                ResourceDependencyEdge(
                    team_id=team_id,
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                    agent_id=agent_id,
                    resource_kind="connection",
                    resource_ref=f"team_tool_source:{source_id}",
                    resource_parent_ref=source_id,
                    resource_revision=None,
                    dependency_role="required",
                    evidence_level="stable_only",
                    subject_fingerprint=subject_fingerprint,
                )
            )

    raw_grants = definition.get("skill_grants")
    if subject_kind == "system_agent_revision":
        allowlist = _string_list(definition.get("skill_invoke_allowlist"))
        if allowlist is None:
            reasons.add("skill_dependency_shape_unrecoverable")
            allowlist = ()
        for name in allowlist:
            edges.append(
                ResourceDependencyEdge(
                    team_id=team_id,
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                    agent_id=agent_id,
                    resource_kind="skill",
                    resource_ref=f"builtin:builtin:{name}",
                    resource_parent_ref=None,
                    resource_revision=None,
                    dependency_role="required",
                    evidence_level="stable_only",
                    subject_fingerprint=subject_fingerprint,
                )
            )
    elif raw_grants is None:
        if definition.get("skills"):
            reasons.add("legacy_skill_identity_unrecoverable")
    elif not isinstance(raw_grants, list):
        reasons.add("skill_dependency_shape_unrecoverable")
    else:
        for value in raw_grants:
            parsed = _recover_skill_grant(value)
            if parsed is None:
                reasons.add("skill_dependency_shape_unrecoverable")
                continue
            resource_ref, revision = parsed
            edges.append(
                ResourceDependencyEdge(
                    team_id=team_id,
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                    agent_id=agent_id,
                    resource_kind="skill",
                    resource_ref=resource_ref,
                    resource_parent_ref=None,
                    resource_revision=revision,
                    dependency_role="required",
                    evidence_level="exact",
                    subject_fingerprint=subject_fingerprint,
                )
            )

    from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError, read_agent_runtime_extensions

    try:
        policy = read_agent_runtime_extensions(definition)
    except AgentRuntimeExtensionError:
        reasons.add("optional_skill_policy_unrecoverable")
    else:
        if policy is not None:
            edges.extend(
                _optional_skill_edges(
                    team_id=team_id,
                    subject_kind=subject_kind,
                    subject_ref=subject_ref,
                    agent_id=agent_id,
                    subject_fingerprint=subject_fingerprint,
                    policy=policy,
                )
            )

    # Backfill cannot reconstruct the historical Tool revision even when all
    # stable identities are otherwise recoverable.
    if tools:
        reasons.add("tool_revision_unrecoverable")
    status: ProjectionStatus = "partial" if reasons else "complete"
    projection = ResourceDependencyProjection(
        team_id=team_id,
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        agent_id=agent_id,
        subject_fingerprint=subject_fingerprint,
        status=status,
        reason_code=(sorted(reasons)[0] if reasons else None),
    )
    return projection, _dedupe_edges(edges)


def _optional_skill_edges(
    *, team_id: str, subject_kind: SubjectKind, subject_ref: str, agent_id: str, subject_fingerprint: str, policy: AgentRuntimeExtensions
) -> tuple[ResourceDependencyEdge, ...]:
    if not policy.skill_read.enabled:
        return ()
    return tuple(
        ResourceDependencyEdge(
            team_id=team_id,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            agent_id=agent_id,
            resource_kind="skill",
            resource_ref=f"{item.source_kind}:{item.id}",
            resource_parent_ref=None,
            resource_revision=item.version,
            dependency_role="optional",
            evidence_level="exact" if item.version else "stable_only",
            subject_fingerprint=subject_fingerprint,
        )
        for item in policy.skill_read.resource_allowlist
    )


async def replace_subject_projection_conn(
    db: aiosqlite.Connection,
    *,
    projection: ResourceDependencyProjection,
    edges: Sequence[ResourceDependencyEdge],
    projected_at: str | None = None,
) -> None:
    """Replace a mutable subject or idempotently fill an immutable subject."""
    timestamp = projected_at or datetime.now(UTC).isoformat()
    _validate_edges(projection, edges)
    if projection.subject_kind == "agent_working_copy":
        await db.execute(
            """
            DELETE FROM resource_dependency_edge
            WHERE team_id=? AND subject_kind=? AND subject_ref=?
            """,
            (projection.team_id, projection.subject_kind, projection.subject_ref),
        )
        await db.execute(
            """
            DELETE FROM resource_dependency_subject_projection
            WHERE team_id=? AND subject_kind=? AND subject_ref=?
            """,
            (projection.team_id, projection.subject_kind, projection.subject_ref),
        )
    else:
        existing = await _projection_row(db, projection)
        if existing is not None:
            expected = (
                projection.agent_id,
                projection.subject_fingerprint,
                projection.compiler_version,
            )
            actual = (
                str(existing["agent_id"]),
                str(existing["subject_fingerprint"]),
                str(existing["compiler_version"]),
            )
            if actual != expected:
                raise ResourceDependencyProjectionError("immutable_resource_dependency_projection_conflict")
    await db.execute(
        """
        INSERT OR IGNORE INTO resource_dependency_subject_projection(
          team_id,subject_kind,subject_ref,agent_id,subject_fingerprint,
          status,reason_code,compiler_version,projected_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            projection.team_id,
            projection.subject_kind,
            projection.subject_ref,
            projection.agent_id,
            projection.subject_fingerprint,
            projection.status,
            projection.reason_code,
            projection.compiler_version,
            timestamp,
        ),
    )
    for edge in edges:
        await db.execute(
            """
            INSERT OR IGNORE INTO resource_dependency_edge(
              edge_key,team_id,subject_kind,subject_ref,agent_id,resource_kind,
              resource_ref,resource_parent_ref,resource_revision,dependency_role,
              evidence_level,subject_fingerprint,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                edge.edge_key,
                edge.team_id,
                edge.subject_kind,
                edge.subject_ref,
                edge.agent_id,
                edge.resource_kind,
                edge.resource_ref,
                edge.resource_parent_ref,
                edge.resource_revision,
                edge.dependency_role,
                edge.evidence_level,
                edge.subject_fingerprint,
                timestamp,
            ),
        )
        await _require_edge_matches(db, edge)
    if projection.subject_kind != "agent_working_copy":
        await db.execute(
            """
            UPDATE resource_dependency_subject_projection
            SET status=?,reason_code=?
            WHERE team_id=? AND subject_kind=? AND subject_ref=?
              AND status='partial'
            """,
            (
                projection.status,
                projection.reason_code,
                projection.team_id,
                projection.subject_kind,
                projection.subject_ref,
            ),
        )


async def invalidate_working_copy_projection_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    working_copy_id: str,
) -> None:
    await db.execute(
        """
        DELETE FROM resource_dependency_edge
        WHERE team_id=? AND subject_kind='agent_working_copy' AND subject_ref=?
        """,
        (team_id, working_copy_id),
    )
    await db.execute(
        """
        DELETE FROM resource_dependency_subject_projection
        WHERE team_id=? AND subject_kind='agent_working_copy' AND subject_ref=?
        """,
        (team_id, working_copy_id),
    )


class ResourceDependencyBackfillService:
    """Paged preview/stage/verify service; never runs inside a migration."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)

    async def run_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        apply: bool = False,
    ) -> ResourceDependencyBackfillResult:
        if limit < 1 or limit > 500:
            raise ValueError("backfill limit must be between 1 and 500")
        decoded_cursor = _decode_cursor(cursor)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await _subject_page(db, cursor=decoded_cursor, limit=limit + 1)
            page = rows[:limit]
            source_maps = await _team_tool_source_maps(
                db,
                team_ids={str(row["team_id"]) for row in page},
            )
            projected = complete = partial = 0
            if apply and page:
                await db.execute("BEGIN IMMEDIATE")
            try:
                for row in page:
                    subject_fingerprint = _row_subject_fingerprint(row)
                    if row["projected_fingerprint"] is not None and str(row["projected_fingerprint"]) == subject_fingerprint:
                        continue
                    definition = _decode_definition(row["definition_json"])
                    projection, edges = stable_edges_from_definition(
                        team_id=str(row["team_id"]),
                        subject_kind=str(row["subject_kind"]),  # type: ignore[arg-type]
                        subject_ref=str(row["subject_ref"]),
                        agent_id=str(row["agent_id"]),
                        subject_fingerprint=subject_fingerprint,
                        definition=definition,
                        team_tool_source_ids=source_maps.get(str(row["team_id"])),
                    )
                    if projection.status == "complete":
                        complete += 1
                    else:
                        partial += 1
                    if apply:
                        await replace_subject_projection_conn(
                            db,
                            projection=projection,
                            edges=edges,
                        )
                        projected += 1
                if apply:
                    await db.commit()
            except BaseException:
                if apply:
                    await db.rollback()
                raise
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                str(last["subject_kind"]),
                str(last["subject_ref"]),
            )
        return ResourceDependencyBackfillResult(
            scanned_count=len(page),
            projected_count=projected,
            complete_count=complete,
            partial_count=partial,
            next_cursor=next_cursor,
        )

    async def verify(self) -> dict[str, int]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await _subject_page(db, cursor=None, limit=1_000_000)
            async with db.execute(
                """
                SELECT status,COUNT(*) AS count
                FROM resource_dependency_subject_projection
                GROUP BY status
                """
            ) as result_cursor:
                status_rows = await result_cursor.fetchall()
        missing_or_stale = sum(
            row["projected_fingerprint"] is None or str(row["projected_fingerprint"]) != _row_subject_fingerprint(row) for row in rows
        )
        counts = {"complete": 0, "partial": 0, "missing_or_stale": missing_or_stale}
        for row in status_rows:
            counts[str(row["status"])] = int(row["count"])
        return counts


async def _subject_page(
    db: aiosqlite.Connection,
    *,
    cursor: tuple[str, str] | None,
    limit: int,
) -> list[aiosqlite.Row]:
    cursor_kind, cursor_ref = cursor or ("", "")
    async with db.execute(
        """
        WITH subjects AS (
          SELECT team_id,'agent_working_copy' AS subject_kind,id AS subject_ref,
                 subagent_id AS agent_id,NULL AS stored_fingerprint,
                 definition_json,listing_json
          FROM team_subagent_working_copy
          UNION ALL
          SELECT team_id,'agent_revision',id,subagent_id,definition_hash,
                 definition_json,listing_json
          FROM team_subagent_revision
          UNION ALL
          SELECT team_id,'system_agent_revision',id,agent_id,definition_hash,
                 definition_json,NULL
          FROM system_agent_effective_revision
        )
        SELECT subjects.*,projection.subject_fingerprint AS projected_fingerprint
        FROM subjects
        LEFT JOIN resource_dependency_subject_projection projection
          ON projection.team_id=subjects.team_id
         AND projection.subject_kind=subjects.subject_kind
         AND projection.subject_ref=subjects.subject_ref
        WHERE subjects.subject_kind>?
           OR (subjects.subject_kind=? AND subjects.subject_ref>?)
        ORDER BY subjects.subject_kind,subjects.subject_ref
        LIMIT ?
        """,
        (cursor_kind, cursor_kind, cursor_ref, limit),
    ) as result_cursor:
        return await result_cursor.fetchall()


async def _team_tool_source_maps(
    db: aiosqlite.Connection,
    *,
    team_ids: set[str],
) -> dict[str, dict[str, str]]:
    """Build stable operation ref → source id from the authoritative registry."""
    if not team_ids:
        return {}
    placeholders = ",".join("?" for _ in team_ids)
    async with db.execute(
        f"""
        SELECT id,team_id,kind,openapi_spec_json,tools_schema_json
        FROM team_tool_source
        WHERE team_id IN ({placeholders})
        """,  # noqa: S608 -- placeholders are generated from a set's cardinality only.
        tuple(sorted(team_ids)),
    ) as cursor:
        rows = await cursor.fetchall()
    result: dict[str, dict[str, str]] = {team_id: {} for team_id in team_ids}
    for row in rows:
        source_id = str(row["id"])
        team_id = str(row["team_id"])
        try:
            if row["kind"] == "openapi":
                spec = json.loads(str(row["openapi_spec_json"] or "{}"))
                entries = (openapi_entry(source_id, op) for op in parse_openapi_ops(spec))
            elif row["kind"] == "mcp":
                tools = json.loads(str(row["tools_schema_json"] or "[]"))
                if not isinstance(tools, list):
                    continue
                entries = (mcp_entry(source_id, tool) for tool in tools if isinstance(tool, dict) and tool.get("name"))
            else:
                continue
            for entry in entries:
                result[team_id][entry.advertised_name] = source_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            # Missing or stale metadata is an honest partial result, never a
            # reason to guess identity from a string prefix.
            continue
    return result


def _row_subject_fingerprint(row: Mapping[str, Any]) -> str:
    stored = row["stored_fingerprint"]
    if stored is not None:
        return str(stored)
    return _hash_json(
        {
            "definition": json.loads(str(row["definition_json"])),
            "listing": json.loads(str(row["listing_json"])),
        }
    )


async def _projection_row(
    db: aiosqlite.Connection,
    projection: ResourceDependencyProjection,
) -> aiosqlite.Row | None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT * FROM resource_dependency_subject_projection
        WHERE team_id=? AND subject_kind=? AND subject_ref=?
        """,
        (projection.team_id, projection.subject_kind, projection.subject_ref),
    ) as cursor:
        return await cursor.fetchone()


async def _require_edge_matches(
    db: aiosqlite.Connection,
    edge: ResourceDependencyEdge,
) -> None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM resource_dependency_edge WHERE edge_key=?",
        (edge.edge_key,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise ResourceDependencyProjectionError("resource_dependency_edge_missing")
    expected = (
        edge.team_id,
        edge.subject_kind,
        edge.subject_ref,
        edge.agent_id,
        edge.resource_kind,
        edge.resource_ref,
        edge.resource_parent_ref,
        edge.resource_revision,
        edge.dependency_role,
        edge.evidence_level,
        edge.subject_fingerprint,
    )
    actual = tuple(
        row[key]
        for key in (
            "team_id",
            "subject_kind",
            "subject_ref",
            "agent_id",
            "resource_kind",
            "resource_ref",
            "resource_parent_ref",
            "resource_revision",
            "dependency_role",
            "evidence_level",
            "subject_fingerprint",
        )
    )
    if actual != expected:
        raise ResourceDependencyProjectionError("resource_dependency_edge_conflict")


def _validate_edges(
    projection: ResourceDependencyProjection,
    edges: Sequence[ResourceDependencyEdge],
) -> None:
    expected = (
        projection.team_id,
        projection.subject_kind,
        projection.subject_ref,
        projection.agent_id,
        projection.subject_fingerprint,
    )
    keys: set[str] = set()
    for edge in edges:
        actual = (
            edge.team_id,
            edge.subject_kind,
            edge.subject_ref,
            edge.agent_id,
            edge.subject_fingerprint,
        )
        if actual != expected:
            raise ValueError("resource dependency edge does not match projection")
        if edge.edge_key in keys:
            raise ValueError("duplicate resource dependency edge")
        keys.add(edge.edge_key)


def _dedupe_edges(
    edges: Sequence[ResourceDependencyEdge],
) -> tuple[ResourceDependencyEdge, ...]:
    by_key = {edge.edge_key: edge for edge in edges}
    return tuple(by_key[key] for key in sorted(by_key))


def _recover_skill_grant(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    skill_ref = value.get("skill_ref")
    if not isinstance(skill_ref, dict):
        return None
    kind = skill_ref.get("kind")
    skill_id = skill_ref.get("id")
    revision = value.get("revision")
    if (
        kind not in {"builtin", "team", "user"}
        or not isinstance(skill_id, str)
        or not skill_id
        or not isinstance(revision, str)
        or not revision.startswith("sha256:")
    ):
        return None
    return f"{kind}:{skill_id}", revision


def _string_list(value: object) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return tuple(sorted(set(value)))


def _decode_definition(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResourceDependencyProjectionError("resource_definition_malformed") from exc
    if not isinstance(decoded, dict):
        raise ResourceDependencyProjectionError("resource_definition_malformed")
    return decoded


def _hash_json(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _encode_cursor(kind: str, ref: str) -> str:
    return json.dumps([kind, ref], separators=(",", ":"))


def _decode_cursor(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid backfill cursor") from exc
    if not isinstance(decoded, list) or len(decoded) != 2 or not all(isinstance(item, str) for item in decoded):
        raise ValueError("invalid backfill cursor")
    return decoded[0], decoded[1]


__all__ = [
    "RESOURCE_DEPENDENCY_PROJECTION_VERSION",
    "ResourceDependencyBackfillResult",
    "ResourceDependencyBackfillService",
    "ResourceDependencyEdge",
    "ResourceDependencyProjection",
    "ResourceDependencyProjectionError",
    "edges_from_compilation",
    "invalidate_working_copy_projection_conn",
    "replace_subject_projection_conn",
    "stable_edges_from_definition",
]
