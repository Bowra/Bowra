"""Consumer-safe Agent Marketplace projection builder.

The source query deliberately excludes prompts, output schemas, Audience rosters,
tool endpoint paths and credentials. Raw stable tool/skill references are only
used server-side to compile finite public capability and risk summaries.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_capability_catalog import (
    capability_prompt_fingerprint,
    detect_prompt_protocols,
)
from bowhead_core.agent_listing_safety import (
    BLOCKED_LISTING_DISPLAY_NAME,
    BLOCKED_LISTING_SUMMARY,
    ListingSafetyResult,
    scan_agent_listing,
)
from bowhead_core.agent_marketplace_search_text import capability_search_values
from bowhead_core.agent_taxonomy import (
    AgentTeamTag,
    safe_taxonomy_selection_from_listing,
)
from bowhead_core.catalog_localization import (
    CatalogLocalizationError,
    ResolvedLocalizedListing,
    resolve_agent_listing,
)
from bowhead_core.subagent_authoring_catalog import AuthoringTool, runtime_tool_ref
from bowhead_core.tool_name_canon import TOOL_RENAMES

CATALOG_COMPILER_VERSION = "agent-catalog-v7"

_CAPABILITY_LABELS: dict[str, tuple[str, str, str, str]] = {
    "data_discovery": ("cap_data_discovery", "cap_data_discovery", "发现数据", "data"),
    "data_query": ("cap_data_query", "cap_data_query", "查询与分析数据", "data"),
    "knowledge_search": ("cap_knowledge_search", "cap_knowledge_search", "检索团队知识", "knowledge"),
    "public_web_research": ("cap_web_research", "cap_web_research", "研究公开网络", "research"),
    "public_web_fetch": ("cap_web_fetch", "cap_web_fetch", "读取指定网页", "research"),
    "isolated_compute": ("cap_compute", "cap_compute", "运行隔离计算", "analysis"),
    "artifact_chart": ("cap_visualize", "cap_visualize", "制作图表", "visual"),
    "artifact_report": ("cap_report", "cap_report", "撰写报告", "document"),
    "team_domain_read": ("cap_team_read", "cap_team_read", "查询团队系统", "team-system"),
    "team_domain_write": ("cap_team_write", "cap_team_write", "操作团队系统", "team-system"),
}

_RISK_ORDER = {
    "read": 0,
    "compute": 1,
    "artifact_write": 2,
    "external_write": 3,
    "destructive": 4,
    "unknown": 5,
}


@dataclass(frozen=True, slots=True)
class AgentCatalogSource:
    id: str
    name: str
    display_name: str
    description: str
    model: str | None
    visibility: str
    created_by: str | None
    status: str
    lifecycle_state: str
    definition_mode: str
    updated_at: str
    row_version: int
    tool_refs: tuple[str, ...]
    skills: tuple[str, ...]
    listing: dict[str, Any]
    listing_safety: ListingSafetyResult
    prompt_safety_reasons: tuple[str, ...] = ()
    description_safety_reasons: tuple[str, ...] = ()
    prompt_protocols: tuple[str, ...] = ()
    prompt_fingerprint: str | None = None
    live_revision_id: str | None = None
    # The live revision names its Skills by exact published version (``skill_grants``), as execution reads them.
    precise_skill_grants: bool = False


@dataclass(frozen=True, slots=True)
class AgentCapability:
    id: str
    label_key: str
    label: str
    category: str


@dataclass(frozen=True, slots=True)
class AgentCatalogProjection:
    agent_id: str
    display_name: str
    summary: str
    owner_label: str | None
    capabilities: tuple[AgentCapability, ...]
    domains: tuple[str, ...]
    jobs: tuple[str, ...]
    outputs: tuple[str, ...]
    team_tags: tuple[AgentTeamTag, ...]
    risk_level: str
    risk_reasons: tuple[str, ...]
    safety_status: str
    safety_reasons: tuple[str, ...]
    listing_safety_status: str
    listing_safety_reasons: tuple[str, ...]
    listing_hash: str
    dependency_ready: bool
    dependency_reasons: tuple[str, ...]
    search_text: str
    source_version: str
    projection_hash: str
    requested_locale: str | None = None
    effective_locale: str | None = None
    source_locale: str | None = None
    locale_fallback: bool = False
    localization_generation_source: str | None = None


@dataclass(frozen=True, slots=True)
class AgentCatalogIndex:
    capabilities: tuple[AgentCapability, ...]
    domains: tuple[str, ...]
    jobs: tuple[str, ...]
    outputs: tuple[str, ...]
    team_tags: tuple[AgentTeamTag, ...]
    search_text: str

    @property
    def capability_values(self) -> frozenset[str]:
        return frozenset(
            {
                *(item.id.casefold() for item in self.capabilities),
                *(item.category.casefold() for item in self.capabilities),
                *(value.casefold() for value in (*self.domains, *self.jobs, *self.outputs)),
                *(item.id.casefold() for item in self.team_tags),
                *(item.display_name.casefold() for item in self.team_tags),
            },
        )


def catalog_tool(tools_by_ref: Mapping[str, AuthoringTool], ref: str) -> AuthoringTool | None:
    """The catalog entry for a stored tool reference, named the way the runtime names it.

    Stored references keep old names (``canvas_add_chart``) that the runtime renames before use, and the
    authoring list folds ``python_exec`` into ``python_sandbox``. Looking the raw reference up marked tools
    the runtime runs as missing (116 dogfood 2026-09-25).
    """
    return tools_by_ref.get(TOOL_RENAMES.get(ref, ref)) or tools_by_ref.get(runtime_tool_ref(ref))


def tool_unavailable(tool: AuthoringTool | None) -> bool:
    """A referenced tool this Agent cannot use when it runs — not merely one a new Agent may not pick."""
    return tool is None or not tool.selectable


def _derive_capability_surface(
    source: AgentCatalogSource,
    tools_by_ref: dict[str, AuthoringTool],
) -> tuple[tuple[AgentCapability, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    capability_rows: dict[str, AgentCapability] = {}
    for tool_ref in source.tool_refs:
        tool = catalog_tool(tools_by_ref, tool_ref)
        if tool is None:
            continue
        if tool.stable_ref == "web_fetch":
            # Present the reader's job accurately without changing the stable
            # authoring-catalog class used by existing dependency fingerprints.
            capability_rows["cap_web_fetch"] = AgentCapability(*_CAPABILITY_LABELS["public_web_fetch"])
            continue
        for capability_class in tool.capability_classes:
            mapped = _CAPABILITY_LABELS.get(capability_class)
            if mapped:
                capability_rows[mapped[0]] = AgentCapability(*mapped)

    outputs: set[str] = set()
    for value in (*source.tool_refs, *source.skills):
        normalized = value.casefold()
        if any(token in normalized for token in ("ppt", "slide", "deck", "presentation")):
            outputs.add("presentation")
            capability_rows["cap_presentation"] = AgentCapability(
                "cap_presentation",
                "cap_presentation",
                "制作演示文稿",
                "presentation",
            )
        if any(token in normalized for token in ("word", "docx", "document")):
            outputs.add("document")
            capability_rows["cap_document"] = AgentCapability(
                "cap_document",
                "cap_document",
                "制作文档",
                "document",
            )
        if any(token in normalized for token in ("xlsx", "excel", "spreadsheet", "sheet")):
            outputs.add("spreadsheet")
            capability_rows["cap_spreadsheet"] = AgentCapability(
                "cap_spreadsheet",
                "cap_spreadsheet",
                "制作电子表格",
                "spreadsheet",
            )
        if "report" in normalized:
            outputs.add("report")
            capability_rows["cap_report"] = AgentCapability(
                "cap_report",
                "cap_report",
                "撰写报告",
                "document",
            )
    if source.skills:
        capability_rows["cap_skill"] = AgentCapability(
            "cap_skill",
            "cap_skill",
            "使用专业工作方法",
            "workflow",
        )
    if not capability_rows:
        capability_rows["cap_analysis"] = AgentCapability(
            "cap_analysis",
            "cap_analysis",
            "分析与业务处理",
            "analysis",
        )
    capabilities = tuple(sorted(capability_rows.values(), key=lambda item: item.id))
    job_map = {
        "analysis": "analysis",
        "research": "research",
        "document": "writing",
        "presentation": "presentation",
        "spreadsheet": "spreadsheet",
        "visual": "visualization",
        "workflow": "automation",
    }
    jobs = tuple(sorted({job_map[item.category] for item in capabilities if item.category in job_map}))
    domains = tuple(sorted({"it_data" if tool.startswith("teammcp.") else "general" for tool in source.tool_refs}))
    if not domains:
        domains = ("general",)
    return capabilities, domains, jobs, tuple(sorted(outputs))


def _safe_json_list(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str))


def _safe_listing(value: object, *, display_name: str, description: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value)) if value is not None else None
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        parsed = {}
    parsed.setdefault("display_name", display_name)
    parsed.setdefault("description", description)
    return parsed


def _listing_text(
    source: AgentCatalogSource,
    *,
    locale: str | None,
) -> tuple[str, str, ResolvedLocalizedListing | None]:
    display_name = source.display_name.strip() or source.name
    summary = source.description.strip() or "可加入个人 Agent 团队的专业助手。"
    if locale is None:
        return display_name, summary, None
    try:
        localized = resolve_agent_listing(source.listing, requested_locale=locale)
    except CatalogLocalizationError:
        return display_name, summary, None
    return localized.display_name, localized.summary, localized


def _source_from_row(row: aiosqlite.Row) -> AgentCatalogSource:
    from bowhead_core.agents.exceptions import ManifestSecurityError
    from bowhead_core.agents.manifest_security import lint_user_append

    prompt_safety_reasons: tuple[str, ...] = ()
    try:
        lint_user_append(str(row["system_prompt"] or ""))
    except ManifestSecurityError:
        prompt_safety_reasons = ("prompt_injection_risk",)
    except Exception:
        prompt_safety_reasons = ("prompt_safety_check_failed",)
    listing_safety = scan_agent_listing(
        name=row["name"],
        display_name=row["display_name"],
        description=row["description"],
    )
    listing = _safe_listing(
        row["live_listing_json"],
        display_name=str(row["display_name"] or ""),
        description=listing_safety.normalized_description,
    )
    return AgentCatalogSource(
        id=str(row["id"]),
        name=str(row["name"]),
        display_name=str(row["display_name"] or ""),
        description=listing_safety.normalized_description,
        model=(str(row["model"]) if row["model"] is not None else None),
        visibility=str(row["visibility"]),
        created_by=(str(row["created_by"]) if row["created_by"] is not None else None),
        status=str(row["status"]),
        lifecycle_state=str(row["lifecycle_state"] or "active"),
        definition_mode=str(row["definition_mode"] or "legacy_mutable"),
        updated_at=str(row["updated_at"]),
        row_version=int(row["row_version"]),
        tool_refs=_safe_json_list(row["tool_refs_json"]),
        skills=_safe_json_list(row["skills_json"]),
        listing=listing,
        listing_safety=listing_safety,
        prompt_safety_reasons=prompt_safety_reasons,
        description_safety_reasons=((listing_safety.routing_safety_reason,) if listing_safety.routing_safety_reason else ()),
        prompt_protocols=detect_prompt_protocols(str(row["system_prompt"] or "")),
        prompt_fingerprint=capability_prompt_fingerprint(str(row["system_prompt"] or "")),
        live_revision_id=(str(row["live_revision_id"]) if row["live_revision_id"] else None),
        precise_skill_grants=bool(row["live_has_skill_grants"]),
    )


class AgentCatalogStore:
    """Build and persist finite consumer projections from current Agent rows."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def list_team_sources(self, *, team_id: str) -> tuple[AgentCatalogSource, ...]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT agent.id,agent.name,agent.display_name,agent.description,agent.model,
                       agent.visibility,agent.created_by,agent.status,agent.lifecycle_state,
                       agent.definition_mode,
                       agent.updated_at,agent.row_version,agent.tool_refs_json,agent.skills_json,
                       agent.system_prompt,agent.live_revision_id,
                       revision.listing_json AS live_listing_json,
                       CASE WHEN json_valid(revision.definition_json) THEN json_type(revision.definition_json,'$.skill_grants') IS NOT NULL ELSE 0 END AS live_has_skill_grants
                FROM team_subagent agent
                LEFT JOIN team_subagent_release release
                  ON release.team_id=agent.team_id AND release.subagent_id=agent.id
                 AND release.id=agent.live_release_id
                LEFT JOIN team_subagent_revision revision
                  ON revision.team_id=agent.team_id AND revision.subagent_id=agent.id
                 AND revision.id=release.revision_id
                WHERE agent.team_id=?
                ORDER BY agent.updated_at DESC,agent.id
                """,
                (team_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return tuple(_source_from_row(row) for row in rows)

    async def get_team_source(self, *, team_id: str, agent_id: str) -> AgentCatalogSource | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT agent.id,agent.name,agent.display_name,agent.description,agent.model,
                       agent.visibility,agent.created_by,agent.status,agent.lifecycle_state,
                       agent.definition_mode,
                       agent.updated_at,agent.row_version,agent.tool_refs_json,agent.skills_json,
                       agent.system_prompt,agent.live_revision_id,
                       revision.listing_json AS live_listing_json,
                       CASE WHEN json_valid(revision.definition_json) THEN json_type(revision.definition_json,'$.skill_grants') IS NOT NULL ELSE 0 END AS live_has_skill_grants
                FROM team_subagent agent
                LEFT JOIN team_subagent_release release
                  ON release.team_id=agent.team_id AND release.subagent_id=agent.id
                 AND release.id=agent.live_release_id
                LEFT JOIN team_subagent_revision revision
                  ON revision.team_id=agent.team_id AND revision.subagent_id=agent.id
                 AND revision.id=release.revision_id
                WHERE agent.team_id=? AND agent.id=?
                """,
                (team_id, agent_id),
            ) as cursor:
                row = await cursor.fetchone()
        return _source_from_row(row) if row else None

    async def owner_labels(self, owner_ids: set[str]) -> dict[str, str]:
        if not owner_ids:
            return {}
        placeholders = ",".join("?" for _ in owner_ids)
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                f"SELECT id,COALESCE(NULLIF(display_name,''),email,id) FROM users WHERE id IN ({placeholders})",  # noqa: S608
                tuple(sorted(owner_ids)),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    async def team_tag_resolution(self, *, team_id: str) -> dict[str, AgentTeamTag]:
        from bowhead_core.agent_taxonomy import AgentTaxonomyStore

        return await AgentTaxonomyStore(self.db_path).tag_resolution(team_id=team_id)

    def index(
        self,
        source: AgentCatalogSource,
        *,
        owner_label: str | None,
        tools_by_ref: dict[str, AuthoringTool],
        team_tags_by_id: Mapping[str, AgentTeamTag] | None = None,
        locale: str | None = None,
    ) -> AgentCatalogIndex:
        """Build the lightweight filter/facet surface without a persisted projection."""
        capabilities, domains, jobs, outputs = _derive_capability_surface(source, tools_by_ref)
        # Publication validation rejects malformed taxonomy. Keep the catalog
        # readable if an older or manually altered live snapshot predates it.
        selection = safe_taxonomy_selection_from_listing(source.listing)
        selected_domains = set(selection["domains"])
        derived_domains = set(domains)
        if selected_domains.difference({"general"}):
            derived_domains.discard("general")
        domains = tuple(sorted(derived_domains | selected_domains))
        jobs = tuple(sorted({*jobs, *selection["jobs"]}))
        tag_resolution = team_tags_by_id or {}
        team_tags = tuple(
            sorted(
                {tag_resolution[tag_id].id: tag_resolution[tag_id] for tag_id in selection["team_tag_ids"] if tag_id in tag_resolution}.values(),
                key=lambda item: (item.display_name.casefold(), item.id),
            ),
        )
        listing_blocked = source.listing_safety.blocked
        display_name, summary, _localization = _listing_text(source, locale=locale)
        if listing_blocked:
            display_name = BLOCKED_LISTING_DISPLAY_NAME
            summary = BLOCKED_LISTING_SUMMARY
        search_text = ""
        if not listing_blocked:
            search_text = " ".join(
                [
                    source.name,
                    source.display_name,
                    source.description,
                    display_name,
                    summary,
                    owner_label or "",
                    *(
                        term
                        for item in capabilities
                        for term in (
                            item.id,
                            item.label_key,
                            item.label,
                            item.category,
                            *capability_search_values(item.id),
                        )
                    ),
                    *(tag.display_name for tag in team_tags),
                ],
            ).casefold()
        return AgentCatalogIndex(
            capabilities=capabilities,
            domains=domains,
            jobs=jobs,
            outputs=outputs,
            team_tags=team_tags,
            search_text=search_text,
        )

    def compile(
        self,
        source: AgentCatalogSource,
        *,
        owner_label: str | None,
        tools_by_ref: dict[str, AuthoringTool],
        team_tags_by_id: Mapping[str, AgentTeamTag] | None = None,
        locale: str | None = None,
    ) -> AgentCatalogProjection:
        risk_values: list[str] = []
        risk_reasons: set[str] = set()
        safety_reasons: set[str] = set()
        dependency_reasons: set[str] = set()
        safety_reasons.update(source.prompt_safety_reasons)
        safety_reasons.update(source.description_safety_reasons)

        for tool_ref in source.tool_refs:
            tool = catalog_tool(tools_by_ref, tool_ref)
            if tool is None:
                risk_values.append("unknown")
                safety_reasons.add("tool_risk_metadata_unknown")
                dependency_reasons.add("tool_unavailable")
                continue
            risk_values.append(tool.risk)
            if tool.risk in {"external_write", "destructive"}:
                risk_reasons.add(tool.risk)
            if tool.risk == "unknown":
                safety_reasons.add("tool_risk_metadata_unknown")
            if tool_unavailable(tool):
                dependency_reasons.add(tool.locked_reason or "tool_unavailable")
        if source.skills and not source.precise_skill_grants:
            # Only Skills named without an exact published version lack a precise scope; a live revision
            # with ``skill_grants`` pins each one the way execution reads it.
            safety_reasons.add("skill_scope_not_precise")
        highest_risk = max(risk_values or ["read"], key=lambda value: _RISK_ORDER.get(value, 5))
        risk_level = {
            "read": "low",
            "compute": "low",
            "artifact_write": "medium",
            "external_write": "high",
            "destructive": "high",
            "unknown": "unknown",
        }.get(highest_risk, "unknown")
        safety_status = (
            "fail"
            if safety_reasons.intersection({"prompt_injection_risk", "description_routing_hijack"})
            else ("unknown" if safety_reasons else "pass")
        )
        dependency_ready = not dependency_reasons and source.status == "enabled" and source.lifecycle_state == "active"
        if source.status != "enabled":
            dependency_reasons.add("agent_disabled")
        if source.lifecycle_state != "active":
            dependency_reasons.add("agent_quarantined")

        index = self.index(
            source,
            owner_label=owner_label,
            tools_by_ref=tools_by_ref,
            team_tags_by_id=team_tags_by_id,
            locale=locale,
        )
        capabilities = index.capabilities
        jobs = index.jobs
        domains = index.domains
        outputs = index.outputs
        team_tags = index.team_tags
        listing_blocked = source.listing_safety.blocked
        display_name, summary, localization = _listing_text(source, locale=locale)
        if listing_blocked:
            display_name = BLOCKED_LISTING_DISPLAY_NAME
            summary = BLOCKED_LISTING_SUMMARY
        source_version = str(source.row_version)
        public_payload = {
            "agent_id": source.id,
            "source_version": source_version,
            "display_name": display_name,
            "summary": summary,
            "owner_label": owner_label,
            "capabilities": [{"id": item.id, "label": item.label, "category": item.category} for item in capabilities],
            "domains": domains,
            "jobs": jobs,
            "outputs": list(outputs),
            "team_tags": [{"id": item.id, "label": item.display_name} for item in team_tags],
            "risk_level": risk_level,
            "risk_reasons": sorted(risk_reasons),
            "safety_status": safety_status,
            "safety_reasons": sorted(safety_reasons),
            "listing_safety_status": source.listing_safety.effective_status,
            "listing_safety_reasons": source.listing_safety.reason_codes,
            "listing_hash": source.listing_safety.listing_hash,
            "dependency_ready": dependency_ready,
            "dependency_reasons": sorted(dependency_reasons),
        }
        projection_hash = hashlib.sha256(
            json.dumps(public_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(),
        ).hexdigest()
        search_text = index.search_text
        return AgentCatalogProjection(
            agent_id=source.id,
            display_name=display_name,
            summary=summary,
            owner_label=owner_label,
            capabilities=capabilities,
            domains=domains,
            jobs=jobs,
            outputs=outputs,
            team_tags=team_tags,
            risk_level=risk_level,
            risk_reasons=tuple(sorted(risk_reasons)),
            safety_status=safety_status,
            safety_reasons=tuple(sorted(safety_reasons)),
            listing_safety_status=source.listing_safety.effective_status,
            listing_safety_reasons=source.listing_safety.reason_codes,
            listing_hash=source.listing_safety.listing_hash,
            dependency_ready=dependency_ready,
            dependency_reasons=tuple(sorted(dependency_reasons)),
            search_text=search_text,
            source_version=source_version,
            projection_hash=projection_hash,
            requested_locale=(localization.requested_locale if localization else None),
            effective_locale=(localization.effective_locale if localization else None),
            source_locale=(localization.source_locale if localization else None),
            locale_fallback=(localization.locale_fallback if localization else False),
            localization_generation_source=(localization.generation_source if localization else None),
        )

    async def persist(
        self,
        *,
        team_id: str,
        projection: AgentCatalogProjection,
    ) -> None:
        await self.persist_many(team_id=team_id, projections=(projection,))

    async def persist_many(
        self,
        *,
        team_id: str,
        projections: tuple[AgentCatalogProjection, ...],
    ) -> None:
        """Upsert one catalog response in a single transaction."""
        if not projections:
            return
        timestamp = datetime.now(UTC).isoformat()
        rows = [
            self._projection_row(
                team_id=team_id,
                projection=projection,
                timestamp=timestamp,
            )
            for projection in projections
        ]
        async with open_shared_ctx(self.db_path) as db:
            await db.executemany(
                """
                INSERT INTO agent_catalog_projection(
                  team_id,agent_id,source_kind,source_version,compiler_version,canonical_name,
                  canonical_summary,owner_label,capability_summary_json,facet_json,risk_summary_json,
                  safety_summary_json,dependency_json,search_text,projection_hash,built_at
                ) VALUES (?,?, 'team',?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(team_id,agent_id) DO UPDATE SET
                  source_kind=excluded.source_kind,source_version=excluded.source_version,
                  compiler_version=excluded.compiler_version,canonical_name=excluded.canonical_name,
                  canonical_summary=excluded.canonical_summary,owner_label=excluded.owner_label,
                  capability_summary_json=excluded.capability_summary_json,facet_json=excluded.facet_json,
                  risk_summary_json=excluded.risk_summary_json,safety_summary_json=excluded.safety_summary_json,
                  dependency_json=excluded.dependency_json,search_text=excluded.search_text,
                  projection_hash=excluded.projection_hash,built_at=excluded.built_at
                WHERE agent_catalog_projection.projection_hash<>excluded.projection_hash
                   OR agent_catalog_projection.compiler_version<>excluded.compiler_version
                """,
                rows,
            )
            await db.commit()

    @staticmethod
    def _projection_row(
        *,
        team_id: str,
        projection: AgentCatalogProjection,
        timestamp: str,
    ) -> tuple[object, ...]:
        capabilities = [{"id": item.id, "label": item.label, "category": item.category} for item in projection.capabilities]
        facets = {
            "domains": list(projection.domains),
            "jobs": list(projection.jobs),
            "outputs": list(projection.outputs),
            "team_tags": [{"id": item.id, "label": item.display_name} for item in projection.team_tags],
        }
        return (
            team_id,
            projection.agent_id,
            projection.source_version,
            CATALOG_COMPILER_VERSION,
            projection.display_name,
            projection.summary,
            projection.owner_label,
            json.dumps(capabilities, ensure_ascii=False),
            json.dumps(facets, ensure_ascii=False),
            json.dumps(
                {"level": projection.risk_level, "reasons": projection.risk_reasons},
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "status": projection.safety_status,
                    "reasons": projection.safety_reasons,
                    "listing_status": projection.listing_safety_status,
                    "listing_reasons": projection.listing_safety_reasons,
                    "listing_hash": projection.listing_hash,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {"ready": projection.dependency_ready, "reasons": projection.dependency_reasons},
                ensure_ascii=False,
            ),
            projection.search_text,
            projection.projection_hash,
            timestamp,
        )


__all__ = [
    "AgentCapability",
    "AgentCatalogProjection",
    "AgentCatalogSource",
    "AgentCatalogStore",
    "CATALOG_COMPILER_VERSION",
]
