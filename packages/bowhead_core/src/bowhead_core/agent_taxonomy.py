"""Versioned Agent taxonomy and governed team tags.

Confirmed domain/job/tag selections live in the Agent listing snapshot. Team tag
rows provide stable IDs and governance aliases. Risk, resource, output, Tool,
and permission truth deliberately do not enter this owner-editable module.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.audit_actions import AuditAction
from bowhead_core.resource_dependency_projection import invalidate_working_copy_projection_conn
from bowhead_core.secret_redaction import extract_secrets
from bowhead_core.user_store import UserStore

TAXONOMY_VERSION = "agent-taxonomy-v1"
MAX_AGENT_TEAM_TAGS = 12
MAX_TEAM_TAGS = 200
_TAG_ID_PATTERN = re.compile(r"^att_[0-9a-f]{32}$")

_DOMAIN_LABELS: dict[str, tuple[str, str]] = {
    "finance": ("财务", "Finance"),
    "marketing": ("市场", "Marketing"),
    "sales": ("销售", "Sales"),
    "operations": ("运营", "Operations"),
    "people": ("人力与组织", "People & organization"),
    "it_data": ("IT 与数据", "IT & data"),
    "general": ("通用", "General"),
}
_JOB_LABELS: dict[str, tuple[str, str]] = {
    "research": ("研究", "Research"),
    "analysis": ("分析", "Analysis"),
    "writing": ("撰写", "Writing"),
    "presentation": ("演示文稿", "Presentations"),
    "spreadsheet": ("电子表格", "Spreadsheets"),
    "visualization": ("图表", "Visualization"),
    "automation": ("自动化", "Automation"),
    "external_action": ("外部操作", "External actions"),
}
_RESERVED_RISK_TAGS = frozenset(
    {
        "risk",
        "safe",
        "no risk",
        "low risk",
        "medium risk",
        "high risk",
        "read only",
        "read-only",
        "external write",
        "destructive",
        "风险",
        "安全",
        "无风险",
        "低风险",
        "中风险",
        "高风险",
        "只读",
        "外部写入",
        "破坏性操作",
    }
)


class AgentTaxonomyError(ValueError):
    def __init__(self, code: str, *, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = reason_codes


class AgentTaxonomyForbiddenError(AgentTaxonomyError):
    pass


class AgentTaxonomyConflictError(AgentTaxonomyError):
    pass


@dataclass(frozen=True, slots=True)
class AgentTeamTag:
    id: str
    display_name: str
    normalized_key: str
    status: Literal["active", "inactive", "merged"]
    merged_into_id: str | None
    row_version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "status": self.status,
            "merged_into_id": self.merged_into_id,
            "row_version": self.row_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class AgentTaxonomySuggestion:
    id: str
    source_hash: str
    taxonomy_version: str
    suggestion: dict[str, Any]
    confidence: float
    state: Literal["pending", "accepted", "dismissed", "stale"]
    row_version: int
    reviewed_by: str | None
    reviewed_at: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_hash": self.source_hash,
            "taxonomy_version": self.taxonomy_version,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "state": self.state,
            "row_version": self.row_version,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def platform_taxonomy(locale: str | None = None) -> dict[str, Any]:
    # Only an explicit Chinese locale selects Chinese taxonomy labels.
    labels_index = 0 if str(locale or "").lower().startswith("zh") else 1
    return {
        "version": TAXONOMY_VERSION,
        "domains": [{"id": term_id, "label": labels[labels_index]} for term_id, labels in _DOMAIN_LABELS.items()],
        "jobs": [{"id": term_id, "label": labels[labels_index]} for term_id, labels in _JOB_LABELS.items()],
    }


def normalize_team_tag(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise AgentTaxonomyError("agent_team_tag_invalid")
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in normalized):
        raise AgentTaxonomyError("agent_team_tag_invalid")
    display_name = " ".join(normalized.split()).strip()
    if not display_name or len(display_name) > 40 or len(display_name.encode("utf-8")) > 120:
        raise AgentTaxonomyError("agent_team_tag_invalid")
    normalized_key = display_name.casefold()
    reserved_probe = " ".join(re.sub(r"[^\w]+", " ", normalized_key).split())
    if normalized_key in _RESERVED_RISK_TAGS or reserved_probe in _RESERVED_RISK_TAGS:
        raise AgentTaxonomyError("agent_team_tag_reserved")
    _redacted, secret_hits = extract_secrets(display_name)
    safety = scan_agent_listing(name="", display_name=display_name, description="")
    if secret_hits or safety.blocked:
        raise AgentTaxonomyError(
            "agent_team_tag_blocked",
            reason_codes=tuple(sorted(safety.reason_codes or ("credential_or_secret",))),
        )
    return display_name, normalized_key


def taxonomy_selection_from_listing(listing: Mapping[str, Any]) -> dict[str, Any]:
    raw = listing.get("taxonomy")
    if not isinstance(raw, Mapping) or raw.get("version") != TAXONOMY_VERSION:
        return {"version": TAXONOMY_VERSION, "domains": [], "jobs": [], "team_tag_ids": []}
    domains = _known_values(raw.get("domains"), allowed=_DOMAIN_LABELS, limit=3)
    jobs = _known_values(raw.get("jobs"), allowed=_JOB_LABELS, limit=5)
    tag_ids = _tag_ids(raw.get("team_tag_ids"))
    return {
        "version": TAXONOMY_VERSION,
        "domains": list(domains),
        "jobs": list(jobs),
        "team_tag_ids": list(tag_ids),
    }


def safe_taxonomy_selection_from_listing(listing: Mapping[str, Any]) -> dict[str, Any]:
    """Read a display selection without making legacy malformed data fatal."""
    try:
        return taxonomy_selection_from_listing(listing)
    except AgentTaxonomyError:
        return {"version": TAXONOMY_VERSION, "domains": [], "jobs": [], "team_tag_ids": []}


async def validate_taxonomy_listing_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    listing: Mapping[str, Any],
) -> None:
    raw = listing.get("taxonomy")
    if raw is None:
        return
    required_fields = {"version", "domains", "jobs", "team_tag_ids", "origin"}
    if (
        not isinstance(raw, Mapping)
        or not required_fields.issubset(raw)
        or set(raw).difference({"version", "domains", "jobs", "team_tag_ids", "origin"})
        or raw.get("version") != TAXONOMY_VERSION
        or raw.get("origin") not in {"manual", "llm"}
        or not isinstance(raw.get("domains"), list)
        or not isinstance(raw.get("jobs"), list)
        or not isinstance(raw.get("team_tag_ids"), list)
    ):
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    selection = taxonomy_selection_from_listing(listing)
    async with db.execute(
        "SELECT id,status,merged_into_id FROM agent_team_tag WHERE team_id=?",
        (team_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    active_ids = {str(row[0]) for row in rows if str(row[1]) == "active"}
    resolvable_ids = active_ids | {str(row[0]) for row in rows if str(row[1]) == "merged" and str(row[2]) in active_ids}
    if any(tag_id not in resolvable_ids for tag_id in selection["team_tag_ids"]):
        raise AgentTaxonomyError("agent_team_tag_unavailable")


def agent_taxonomy_source_hash(
    *,
    listing: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> str:
    source = {
        "display_name": str(listing.get("display_name", "")).strip(),
        "summary": str(listing.get("description", listing.get("summary", ""))).strip(),
        "definition_hash": _hash_json(definition),
        # Bind suggestions to the exact stored value, including malformed legacy
        # shapes that the manager can subsequently repair.
        "taxonomy": listing.get("taxonomy"),
    }
    return _hash_json(source)


def normalize_taxonomy_suggestion(
    value: Mapping[str, Any],
    *,
    allowed_team_tag_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentTaxonomyError("agent_taxonomy_suggestion_invalid")
    allowed_fields = {"domains", "jobs", "team_tag_ids", "new_team_tags", "confidence", "reasons"}
    if set(value).difference(allowed_fields):
        raise AgentTaxonomyError("agent_taxonomy_suggestion_invalid")
    domains = _known_values(value.get("domains"), allowed=_DOMAIN_LABELS, limit=3)
    jobs = _known_values(value.get("jobs"), allowed=_JOB_LABELS, limit=5)
    tag_ids = _tag_ids(value.get("team_tag_ids"))
    if len(tag_ids) > 5 or any(tag_id not in allowed_team_tag_ids for tag_id in tag_ids):
        raise AgentTaxonomyError("agent_taxonomy_suggestion_invalid")
    raw_new = value.get("new_team_tags", [])
    if not isinstance(raw_new, Sequence) or isinstance(raw_new, (str, bytes)) or len(raw_new) > 5:
        raise AgentTaxonomyError("agent_taxonomy_suggestion_invalid")
    new_team_tags: list[str] = []
    new_keys: set[str] = set()
    for item in raw_new:
        display_name, normalized_key = normalize_team_tag(item)
        if normalized_key not in new_keys:
            new_keys.add(normalized_key)
            new_team_tags.append(display_name)
    confidence_raw = value.get("confidence", 0)
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise AgentTaxonomyError("agent_taxonomy_suggestion_invalid")
    confidence = min(1.0, max(0.0, float(confidence_raw)))
    raw_reasons = value.get("reasons", [])
    if (
        not isinstance(raw_reasons, Sequence)
        or isinstance(raw_reasons, (str, bytes))
        or len(raw_reasons) > 6
        or not all(isinstance(item, str) and 0 < len(item.strip()) <= 240 for item in raw_reasons)
    ):
        raise AgentTaxonomyError("agent_taxonomy_suggestion_invalid")
    return {
        "domains": list(domains),
        "jobs": list(jobs),
        "team_tag_ids": list(tag_ids),
        "new_team_tags": new_team_tags,
        "confidence": confidence,
        "reasons": [item.strip() for item in raw_reasons],
    }


class AgentTaxonomyStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def agent_manager_snapshot(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        locale: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                working = await _owned_working_copy(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                listing = _decode_object(working["listing_json"])
                definition = _decode_object(working["definition_json"])
                source_hash = agent_taxonomy_source_hash(listing=listing, definition=definition)
                await db.execute(
                    """
                    UPDATE agent_taxonomy_suggestion
                    SET state='stale',row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND agent_id=? AND working_copy_id=?
                      AND state='pending' AND source_hash<>?
                    """,
                    (timestamp, team_id, agent_id, str(working["id"]), source_hash),
                )
                tags = await _team_tag_rows(db, team_id=team_id)
                resolution = _tag_resolution(tags)
                selection = safe_taxonomy_selection_from_listing(listing)
                selection["team_tag_ids"] = sorted(
                    {resolution[tag_id].id for tag_id in selection["team_tag_ids"] if tag_id in resolution},
                )
                suggestion = await _latest_suggestion(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    working_copy_id=str(working["id"]),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        active_tags = [tag.to_dict() for tag in tags if tag.status == "active"]
        return {
            "kind": "agent_taxonomy_manager",
            "schema_version": 1,
            "agent_id": agent_id,
            "working_copy_id": str(working["id"]),
            "working_copy_row_version": int(working["row_version"]),
            "source_hash": source_hash,
            "source": {
                "display_name": str(listing.get("display_name", "")),
                "summary": str(listing.get("description", listing.get("summary", ""))),
            },
            "platform": platform_taxonomy(locale),
            "selection": selection,
            "team_tags": active_tags,
            "suggestion": suggestion.to_dict() if suggestion else None,
        }

    async def save_suggestion(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        suggestion: Mapping[str, Any],
        expected_source_hash: str,
    ) -> AgentTaxonomySuggestion:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                working = await _owned_working_copy(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                listing = _decode_object(working["listing_json"])
                definition = _decode_object(working["definition_json"])
                source_hash = agent_taxonomy_source_hash(listing=listing, definition=definition)
                if source_hash != expected_source_hash:
                    raise AgentTaxonomyConflictError("agent_taxonomy_source_changed")
                tags = await _team_tag_rows(db, team_id=team_id)
                resolution = _tag_resolution(tags)
                normalized = normalize_taxonomy_suggestion(
                    suggestion,
                    allowed_team_tag_ids=set(resolution),
                )
                await db.execute(
                    """
                    UPDATE agent_taxonomy_suggestion
                    SET state='dismissed',row_version=row_version+1,reviewed_by=?,reviewed_at=?,updated_at=?
                    WHERE team_id=? AND agent_id=? AND working_copy_id=? AND state='pending'
                    """,
                    (
                        actor_user_id,
                        timestamp,
                        timestamp,
                        team_id,
                        agent_id,
                        str(working["id"]),
                    ),
                )
                suggestion_id = f"ats_{uuid4().hex}"
                await db.execute(
                    """
                    INSERT INTO agent_taxonomy_suggestion(
                      id,team_id,agent_id,working_copy_id,source_hash,taxonomy_version,
                      suggestion_json,confidence,state,row_version,created_by,reviewed_by,
                      reviewed_at,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,'pending',1,?,NULL,NULL,?,?)
                    """,
                    (
                        suggestion_id,
                        team_id,
                        agent_id,
                        str(working["id"]),
                        source_hash,
                        TAXONOMY_VERSION,
                        _canonical_json(normalized),
                        normalized["confidence"],
                        actor_user_id,
                        timestamp,
                        timestamp,
                    ),
                )
                row = await _suggestion_row(db, suggestion_id=suggestion_id)
                await _write_audit_conn(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    target_kind="team_subagent",
                    target_id=agent_id,
                    action=AuditAction.AGENT_TAXONOMY_SUGGESTED,
                    payload={
                        "suggestion_id": suggestion_id,
                        "source_hash": source_hash,
                        "taxonomy_version": TAXONOMY_VERSION,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _suggestion_from_row(row)

    async def save_agent_taxonomy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        domains: Sequence[str],
        jobs: Sequence[str],
        team_tag_ids: Sequence[str],
        new_team_tags: Sequence[str],
        expected_working_copy_row_version: int,
        suggestion_id: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        selected_domains = _known_values(domains, allowed=_DOMAIN_LABELS, limit=3)
        selected_jobs = _known_values(jobs, allowed=_JOB_LABELS, limit=5)
        selected_tag_ids = _tag_ids(team_tag_ids)
        if len(selected_tag_ids) + len(new_team_tags) > MAX_AGENT_TEAM_TAGS:
            raise AgentTaxonomyError("agent_taxonomy_too_many_tags")
        new_labels = [normalize_team_tag(value) for value in new_team_tags]
        if len({key for _label, key in new_labels}) != len(new_labels):
            raise AgentTaxonomyError("agent_team_tag_duplicate")
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                working = await _owned_working_copy(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                if int(working["row_version"]) != expected_working_copy_row_version:
                    raise AgentTaxonomyConflictError("agent_working_copy_version_conflict")
                listing = _decode_object(working["listing_json"])
                definition = _decode_object(working["definition_json"])
                source_hash = agent_taxonomy_source_hash(listing=listing, definition=definition)
                tags = list(await _team_tag_rows(db, team_id=team_id))
                by_key = {tag.normalized_key: tag for tag in tags}
                if len(tags) + sum(1 for _label, key in new_labels if key not in by_key) > MAX_TEAM_TAGS:
                    raise AgentTaxonomyError("agent_team_tag_limit_reached")
                created_tag_ids: list[str] = []
                for display_name, normalized_key in new_labels:
                    existing = by_key.get(normalized_key)
                    if existing is not None:
                        resolution = _tag_resolution(tuple(tags))
                        if existing.id not in resolution:
                            raise AgentTaxonomyConflictError("agent_team_tag_inactive")
                        created_tag_ids.append(resolution[existing.id].id)
                        continue
                    tag = await _insert_tag_conn(
                        db,
                        db_path=self.db_path,
                        team_id=team_id,
                        actor_user_id=actor_user_id,
                        display_name=display_name,
                        normalized_key=normalized_key,
                        timestamp=timestamp,
                    )
                    tags.append(tag)
                    by_key[normalized_key] = tag
                    created_tag_ids.append(tag.id)
                resolution = _tag_resolution(tuple(tags))
                canonical_tag_ids = sorted(
                    {resolution[tag_id].id for tag_id in (*selected_tag_ids, *created_tag_ids) if tag_id in resolution},
                )
                unresolved = [tag_id for tag_id in selected_tag_ids if tag_id not in resolution]
                if unresolved:
                    raise AgentTaxonomyError("agent_team_tag_unavailable")
                accepted_suggestion: AgentTaxonomySuggestion | None = None
                if suggestion_id is not None:
                    suggestion_row = await _suggestion_row(db, suggestion_id=suggestion_id)
                    if (
                        str(suggestion_row["team_id"]) != team_id
                        or str(suggestion_row["agent_id"]) != agent_id
                        or str(suggestion_row["working_copy_id"]) != str(working["id"])
                        or str(suggestion_row["state"]) != "pending"
                        or str(suggestion_row["source_hash"]) != source_hash
                    ):
                        raise AgentTaxonomyConflictError("agent_taxonomy_suggestion_stale")
                    accepted_suggestion = _suggestion_from_row(suggestion_row)
                listing["taxonomy"] = {
                    "version": TAXONOMY_VERSION,
                    "domains": list(selected_domains),
                    "jobs": list(selected_jobs),
                    "team_tag_ids": canonical_tag_ids,
                    "origin": "llm" if accepted_suggestion else "manual",
                }
                updated = await db.execute(
                    """
                    UPDATE team_subagent_working_copy
                    SET listing_json=?,validation_json='{"status":"not_validated"}',
                        row_version=row_version+1,updated_by=?,updated_at=?
                    WHERE id=? AND row_version=?
                    """,
                    (
                        _canonical_json(listing),
                        actor_user_id,
                        timestamp,
                        str(working["id"]),
                        expected_working_copy_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentTaxonomyConflictError("agent_working_copy_version_conflict")
                await invalidate_working_copy_projection_conn(
                    db,
                    team_id=team_id,
                    working_copy_id=str(working["id"]),
                )
                if accepted_suggestion:
                    await db.execute(
                        """
                        UPDATE agent_taxonomy_suggestion
                        SET state='accepted',row_version=row_version+1,reviewed_by=?,reviewed_at=?,updated_at=?
                        WHERE id=? AND row_version=? AND state='pending'
                        """,
                        (
                            actor_user_id,
                            timestamp,
                            timestamp,
                            accepted_suggestion.id,
                            accepted_suggestion.row_version,
                        ),
                    )
                await db.execute(
                    """
                    UPDATE agent_taxonomy_suggestion
                    SET state='dismissed',row_version=row_version+1,reviewed_by=?,reviewed_at=?,updated_at=?
                    WHERE team_id=? AND agent_id=? AND working_copy_id=? AND state='pending'
                    """,
                    (
                        actor_user_id,
                        timestamp,
                        timestamp,
                        team_id,
                        agent_id,
                        str(working["id"]),
                    ),
                )
                await _write_audit_conn(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    target_kind="team_subagent",
                    target_id=agent_id,
                    action=AuditAction.AGENT_TAXONOMY_CONFIRMED,
                    payload={
                        "taxonomy_version": TAXONOMY_VERSION,
                        "domains": list(selected_domains),
                        "jobs": list(selected_jobs),
                        "team_tag_ids": canonical_tag_ids,
                        "suggestion_id": suggestion_id,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self.agent_manager_snapshot(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            locale=locale,
        )

    async def team_tag_manager_snapshot(
        self,
        *,
        team_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _require_team_admin(db, team_id=team_id, actor_user_id=actor_user_id)
            tags = await _team_tag_rows(db, team_id=team_id)
        return {
            "kind": "agent_team_tag_manager",
            "schema_version": 1,
            "taxonomy_version": TAXONOMY_VERSION,
            "items": [tag.to_dict() for tag in tags],
            "limits": {"team": MAX_TEAM_TAGS, "per_agent": MAX_AGENT_TEAM_TAGS},
        }

    async def create_team_tag(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        display_name: str,
    ) -> AgentTeamTag:
        display_name, normalized_key = normalize_team_tag(display_name)
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_team_admin(db, team_id=team_id, actor_user_id=actor_user_id)
                tags = await _team_tag_rows(db, team_id=team_id)
                if len(tags) >= MAX_TEAM_TAGS:
                    raise AgentTaxonomyError("agent_team_tag_limit_reached")
                if any(tag.normalized_key == normalized_key for tag in tags):
                    raise AgentTaxonomyConflictError("agent_team_tag_duplicate")
                tag = await _insert_tag_conn(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    display_name=display_name,
                    normalized_key=normalized_key,
                    timestamp=timestamp,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return tag

    async def change_team_tag_status(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        tag_id: str,
        action: Literal["deactivate", "reactivate", "merge"],
        expected_row_version: int,
        target_tag_id: str | None = None,
    ) -> AgentTeamTag:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await _require_team_admin(db, team_id=team_id, actor_user_id=actor_user_id)
                row = await _tag_row(db, team_id=team_id, tag_id=tag_id)
                if int(row["row_version"]) != expected_row_version:
                    raise AgentTaxonomyConflictError("agent_team_tag_version_conflict")
                current_status = str(row["status"])
                next_status = {"deactivate": "inactive", "reactivate": "active", "merge": "merged"}[action]
                if current_status == "merged" or current_status == next_status:
                    raise AgentTaxonomyConflictError("agent_team_tag_transition_invalid")
                if action == "reactivate" and current_status != "inactive":
                    raise AgentTaxonomyConflictError("agent_team_tag_transition_invalid")
                if action in {"deactivate", "merge"}:
                    async with db.execute(
                        """
                        SELECT 1 FROM agent_team_tag
                        WHERE team_id=? AND status='merged' AND merged_into_id=? LIMIT 1
                        """,
                        (team_id, tag_id),
                    ) as cursor:
                        if await cursor.fetchone() is not None:
                            raise AgentTaxonomyConflictError("agent_team_tag_has_aliases")
                merged_into_id: str | None = None
                if action == "merge":
                    if target_tag_id is None or target_tag_id == tag_id:
                        raise AgentTaxonomyError("agent_team_tag_merge_target_invalid")
                    target = await _tag_row(db, team_id=team_id, tag_id=target_tag_id)
                    if str(target["status"]) != "active":
                        raise AgentTaxonomyError("agent_team_tag_merge_target_invalid")
                    merged_into_id = target_tag_id
                updated = await db.execute(
                    """
                    UPDATE agent_team_tag
                    SET status=?,merged_into_id=?,row_version=row_version+1,updated_by=?,updated_at=?
                    WHERE team_id=? AND id=? AND row_version=?
                    """,
                    (
                        next_status,
                        merged_into_id,
                        actor_user_id,
                        timestamp,
                        team_id,
                        tag_id,
                        expected_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentTaxonomyConflictError("agent_team_tag_version_conflict")
                result = _tag_from_row(await _tag_row(db, team_id=team_id, tag_id=tag_id))
                await _write_audit_conn(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    target_kind="agent_team_tag",
                    target_id=tag_id,
                    action=AuditAction.AGENT_TEAM_TAG_STATUS_CHANGED,
                    payload={
                        "before": current_status,
                        "after": next_status,
                        "merged_into_id": merged_into_id,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return result

    async def tag_resolution(self, *, team_id: str) -> dict[str, AgentTeamTag]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            tags = await _team_tag_rows(db, team_id=team_id)
        return _tag_resolution(tags)


async def _insert_tag_conn(
    db: aiosqlite.Connection,
    *,
    db_path: str,
    team_id: str,
    actor_user_id: str,
    display_name: str,
    normalized_key: str,
    timestamp: str,
) -> AgentTeamTag:
    tag_id = f"att_{uuid4().hex}"
    await db.execute(
        """
        INSERT INTO agent_team_tag(
          id,team_id,display_name,normalized_key,status,merged_into_id,row_version,
          created_by,updated_by,created_at,updated_at
        ) VALUES (?,?,?,?,'active',NULL,1,?,?,?,?)
        """,
        (
            tag_id,
            team_id,
            display_name,
            normalized_key,
            actor_user_id,
            actor_user_id,
            timestamp,
            timestamp,
        ),
    )
    tag = _tag_from_row(await _tag_row(db, team_id=team_id, tag_id=tag_id))
    await _write_audit_conn(
        db,
        db_path=db_path,
        team_id=team_id,
        actor_user_id=actor_user_id,
        target_kind="agent_team_tag",
        target_id=tag_id,
        action=AuditAction.AGENT_TEAM_TAG_CREATED,
        payload={"normalized_key_hash": hashlib.sha256(normalized_key.encode()).hexdigest()},
    )
    return tag


async def _owned_working_copy(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> aiosqlite.Row:
    """Delegate to the one shared implementation (see agent_revision_store)."""
    from bowhead_core.agent_revision_store import owned_agent_working_copy_conn

    row, reason = await owned_agent_working_copy_conn(
        db,
        team_id=team_id,
        agent_id=agent_id,
        actor_user_id=actor_user_id,
    )
    if row is None:
        # "Not cut over" is not "does not exist": the Owner can see this Agent in
        # the catalog, it simply has no release history to hang a working copy on.
        raise AgentTaxonomyForbiddenError(
            "agent_taxonomy_release_cutover_required" if reason == "release_cutover_required" else "agent_taxonomy_working_copy_unavailable",
        )
    return row


async def _require_team_admin(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_user_id: str,
) -> None:
    async with db.execute(
        """
        SELECT 1
        FROM teams team
        JOIN users actor ON actor.id=? AND actor.status='active'
        LEFT JOIN team_members member
          ON member.team_id=team.id AND member.user_id=actor.id AND member.status='active'
        WHERE team.id=? AND team.status='active'
          AND (team.created_by_user_id=actor.id OR member.role='team_admin')
        """,
        (actor_user_id, team_id),
    ) as cursor:
        allowed = await cursor.fetchone()
    if allowed is None:
        raise AgentTaxonomyForbiddenError("agent_team_tag_admin_required")


async def _team_tag_rows(
    db: aiosqlite.Connection,
    *,
    team_id: str,
) -> tuple[AgentTeamTag, ...]:
    async with db.execute(
        "SELECT * FROM agent_team_tag WHERE team_id=? ORDER BY display_name,id",
        (team_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_tag_from_row(row) for row in rows)


async def _tag_row(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    tag_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        "SELECT * FROM agent_team_tag WHERE team_id=? AND id=?",
        (team_id, tag_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentTaxonomyError("agent_team_tag_not_found")
    return row


def _tag_resolution(tags: Sequence[AgentTeamTag]) -> dict[str, AgentTeamTag]:
    active = {tag.id: tag for tag in tags if tag.status == "active"}
    result = dict(active)
    for tag in tags:
        if tag.status == "merged" and tag.merged_into_id in active:
            result[tag.id] = active[str(tag.merged_into_id)]
    return result


async def _latest_suggestion(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    working_copy_id: str,
) -> AgentTaxonomySuggestion | None:
    async with db.execute(
        """
        SELECT * FROM agent_taxonomy_suggestion
        WHERE team_id=? AND agent_id=? AND working_copy_id=?
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        (team_id, agent_id, working_copy_id),
    ) as cursor:
        row = await cursor.fetchone()
    return _suggestion_from_row(row) if row else None


async def _suggestion_row(
    db: aiosqlite.Connection,
    *,
    suggestion_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        "SELECT * FROM agent_taxonomy_suggestion WHERE id=?",
        (suggestion_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentTaxonomyError("agent_taxonomy_suggestion_not_found")
    return row


async def _write_audit_conn(
    db: aiosqlite.Connection,
    *,
    db_path: str,
    team_id: str,
    actor_user_id: str,
    target_kind: str,
    target_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    await UserStore(db_path).write_admin_audit_log(
        actor_user_id=actor_user_id,
        team_id=team_id,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        target_team_id=team_id,
        scope="team",
        payload=payload,
        conn=db,
        audit_id=f"aud_{uuid4().hex}",
    )


def _known_values(
    value: object,
    *,
    allowed: Mapping[str, object],
    limit: int,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        if value is None:
            return ()
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    if not all(isinstance(item, str) for item in value):
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    result = tuple(sorted({item for item in value if isinstance(item, str)}))
    if len(result) > limit or any(item not in allowed for item in result):
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    return result


def _tag_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        if value is None:
            return ()
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    if not all(isinstance(item, str) for item in value):
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    result = tuple(sorted({item for item in value if isinstance(item, str)}))
    if len(result) > MAX_AGENT_TEAM_TAGS or any(_TAG_ID_PATTERN.fullmatch(item) is None for item in result):
        raise AgentTaxonomyError("agent_taxonomy_selection_invalid")
    return result


def _tag_from_row(row: Mapping[str, Any]) -> AgentTeamTag:
    return AgentTeamTag(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        normalized_key=str(row["normalized_key"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        merged_into_id=str(row["merged_into_id"]) if row["merged_into_id"] else None,
        row_version=int(row["row_version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _suggestion_from_row(row: Mapping[str, Any]) -> AgentTaxonomySuggestion:
    return AgentTaxonomySuggestion(
        id=str(row["id"]),
        source_hash=str(row["source_hash"]),
        taxonomy_version=str(row["taxonomy_version"]),
        suggestion=_decode_object(row["suggestion_json"]),
        confidence=float(row["confidence"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        row_version=int(row["row_version"]),
        reviewed_by=str(row["reviewed_by"]) if row["reviewed_by"] else None,
        reviewed_at=str(row["reviewed_at"]) if row["reviewed_at"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _decode_object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentTaxonomyError("agent_taxonomy_document_invalid") from exc
    if not isinstance(decoded, dict):
        raise AgentTaxonomyError("agent_taxonomy_document_invalid")
    return decoded


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_json(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AgentTaxonomyConflictError",
    "AgentTaxonomyError",
    "AgentTaxonomyForbiddenError",
    "AgentTaxonomyStore",
    "AgentTeamTag",
    "MAX_AGENT_TEAM_TAGS",
    "MAX_TEAM_TAGS",
    "TAXONOMY_VERSION",
    "agent_taxonomy_source_hash",
    "normalize_taxonomy_suggestion",
    "normalize_team_tag",
    "platform_taxonomy",
    "safe_taxonomy_selection_from_listing",
    "taxonomy_selection_from_listing",
    "validate_taxonomy_listing_conn",
]
