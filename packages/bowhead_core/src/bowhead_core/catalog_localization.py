"""Versioned, non-runtime localization for catalog listings.

The store is deliberately scoped to authored display metadata. Runtime prompts,
tool identifiers, permissions, and risk truth never enter this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_listing_safety import scan_agent_listing
from bowhead_core.audit_actions import AuditAction
from bowhead_core.user_store import UserStore

CatalogResourceKind = Literal["agent", "skill", "capability"]
LocalizationGenerationSource = Literal["manual", "llm", "platform"]
LocalizationReviewState = Literal["draft", "confirmed", "stale"]

_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_MAX_FIELDS_BYTES = 32_000
_MAX_SUMMARY_LENGTH = 4_000
_MAX_EXAMPLES = 12
_MAX_EXAMPLE_LENGTH = 1_000
_MAX_KEYWORDS = 40
_MAX_KEYWORD_LENGTH = 80
_MAX_CAPABILITY_EXPLANATIONS = 40


class CatalogLocalizationError(ValueError):
    def __init__(self, code: str, *, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = reason_codes


class CatalogLocalizationForbiddenError(CatalogLocalizationError):
    pass


class CatalogLocalizationConflictError(CatalogLocalizationError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogLocalizedListing:
    id: str
    team_id: str
    resource_kind: CatalogResourceKind
    resource_id: str
    resource_revision: str
    locale: str
    source_locale: str
    fields: dict[str, Any]
    source_hash: str
    generation_source: LocalizationGenerationSource
    review_state: LocalizationReviewState
    row_version: int
    reviewed_by: str | None
    reviewed_at: str | None
    created_by: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "resource_revision": self.resource_revision,
            "locale": self.locale,
            "source_locale": self.source_locale,
            "fields": self.fields,
            "source_hash": self.source_hash,
            "generation_source": self.generation_source,
            "review_state": self.review_state,
            "row_version": self.row_version,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ResolvedLocalizedListing:
    display_name: str
    summary: str
    requested_locale: str
    effective_locale: str
    source_locale: str
    locale_fallback: bool
    generation_source: str


def canonical_locale(value: object) -> str:
    if not isinstance(value, str):
        raise CatalogLocalizationError("catalog_locale_invalid")
    normalized = value.strip().replace("_", "-")
    if _LOCALE_RE.fullmatch(normalized) is None:
        raise CatalogLocalizationError("catalog_locale_invalid")
    parts = normalized.split("-")
    result = [parts[0].lower()]
    for part in parts[1:]:
        result.append(part.upper() if len(part) in {2, 3} and part.isalpha() else part)
    return "-".join(result)


def agent_listing_source_fields(listing: Mapping[str, Any]) -> dict[str, Any]:
    display_name = listing.get("display_name")
    summary = listing.get("description", listing.get("summary", ""))
    if not isinstance(display_name, str) or not display_name.strip():
        raise CatalogLocalizationError("agent_localization_source_invalid")
    if not isinstance(summary, str):
        raise CatalogLocalizationError("agent_localization_source_invalid")
    source: dict[str, Any] = {
        "display_name": display_name.strip(),
        "summary": summary.strip(),
    }
    for key in ("examples", "capability_explanations", "controlled_keywords"):
        value = listing.get(key)
        if value is not None:
            source[key] = value
    return source


def agent_listing_source_hash(listing: Mapping[str, Any]) -> str:
    return _hash_json(agent_listing_source_fields(listing))


def normalize_localized_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "display_name",
        "summary",
        "examples",
        "capability_explanations",
        "controlled_keywords",
    }
    if not isinstance(value, Mapping) or not value or set(value).difference(allowed):
        raise CatalogLocalizationError("catalog_localization_fields_invalid")
    display_name = value.get("display_name")
    summary = value.get("summary")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 120:
        raise CatalogLocalizationError("catalog_localization_name_invalid")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > _MAX_SUMMARY_LENGTH:
        raise CatalogLocalizationError("catalog_localization_summary_invalid")
    normalized: dict[str, Any] = {
        "display_name": display_name.strip(),
        "summary": summary.strip(),
    }
    examples = value.get("examples")
    if examples is not None:
        if (
            not isinstance(examples, list)
            or len(examples) > _MAX_EXAMPLES
            or not all(isinstance(item, str) and 0 < len(item.strip()) <= _MAX_EXAMPLE_LENGTH for item in examples)
        ):
            raise CatalogLocalizationError("catalog_localization_examples_invalid")
        normalized["examples"] = [item.strip() for item in examples]
    keywords = value.get("controlled_keywords")
    if keywords is not None:
        if (
            not isinstance(keywords, list)
            or len(keywords) > _MAX_KEYWORDS
            or not all(isinstance(item, str) and 0 < len(item.strip()) <= _MAX_KEYWORD_LENGTH for item in keywords)
        ):
            raise CatalogLocalizationError("catalog_localization_keywords_invalid")
        normalized["controlled_keywords"] = [item.strip() for item in keywords]
    explanations = value.get("capability_explanations")
    if explanations is not None:
        normalized_explanations: list[dict[str, str]] = []
        if not isinstance(explanations, list) or len(explanations) > _MAX_CAPABILITY_EXPLANATIONS:
            raise CatalogLocalizationError("catalog_localization_capabilities_invalid")
        for item in explanations:
            if not isinstance(item, Mapping) or set(item) != {"capability_id", "description"}:
                raise CatalogLocalizationError("catalog_localization_capabilities_invalid")
            capability_id = item.get("capability_id")
            description = item.get("description")
            if (
                not isinstance(capability_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", capability_id)
                or not isinstance(description, str)
                or not description.strip()
                or len(description.strip()) > _MAX_EXAMPLE_LENGTH
            ):
                raise CatalogLocalizationError("catalog_localization_capabilities_invalid")
            normalized_explanations.append(
                {"capability_id": capability_id, "description": description.strip()},
            )
        normalized["capability_explanations"] = normalized_explanations
    encoded = _canonical_json(normalized)
    if len(encoded.encode("utf-8")) > _MAX_FIELDS_BYTES:
        raise CatalogLocalizationError("catalog_localization_fields_too_large")
    safety = scan_agent_listing(
        name="",
        display_name=normalized["display_name"],
        description=normalized["summary"],
    )
    if safety.blocked:
        raise CatalogLocalizationError(
            "agent_localization_blocked",
            reason_codes=tuple(sorted(safety.reason_codes)),
        )
    return normalized


def agent_listing_display_text(listing: Mapping[str, Any], *, routing_name: str, requested_locale: str) -> tuple[str, str]:
    """The name and summary members see for a published listing.

    A listing whose display name is blank cannot be localized; it is still the same Agent, shown by its
    routing name, as the marketplace already does — never hidden as if it could not be read.
    """
    try:
        localized = resolve_agent_listing(listing, requested_locale=requested_locale)
    except CatalogLocalizationError:
        display_name = listing.get("display_name")
        summary = listing.get("description", listing.get("summary", ""))
        name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else routing_name
        return name, summary if isinstance(summary, str) else ""
    return localized.display_name, localized.summary


def resolve_agent_listing(
    listing: Mapping[str, Any],
    *,
    requested_locale: str,
) -> ResolvedLocalizedListing:
    requested = canonical_locale(requested_locale)
    canonical = agent_listing_source_fields(listing)
    source_locale_value = listing.get("source_locale", "zh-CN")
    try:
        source_locale = canonical_locale(source_locale_value)
    except CatalogLocalizationError:
        source_locale = "zh-CN"
    source_hash = agent_listing_source_hash(listing)
    rows = _published_localizations(listing, source_hash=source_hash)
    effective_locale: str | None = None
    if requested in rows:
        effective_locale = requested
    else:
        language = requested.split("-", 1)[0]
        effective_locale = next(
            (locale for locale in sorted(rows) if locale.split("-", 1)[0] == language),
            None,
        )
    if effective_locale is None and source_locale in rows:
        effective_locale = source_locale
    if effective_locale is None:
        return ResolvedLocalizedListing(
            display_name=str(canonical["display_name"]),
            summary=str(canonical["summary"]),
            requested_locale=requested,
            effective_locale=source_locale,
            source_locale=source_locale,
            locale_fallback=requested != source_locale,
            generation_source="canonical",
        )
    fields, generation_source = rows[effective_locale]
    return ResolvedLocalizedListing(
        display_name=str(fields["display_name"]),
        summary=str(fields["summary"]),
        requested_locale=requested,
        effective_locale=effective_locale,
        source_locale=source_locale,
        locale_fallback=effective_locale != requested,
        generation_source=generation_source,
    )


async def materialize_confirmed_agent_localizations_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    working_copy_id: str,
    listing: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(listing)
    source_hash = agent_listing_source_hash(result)
    rows = _published_localizations(result, source_hash=source_hash)
    async with db.execute(
        """
        SELECT locale,source_locale,fields_json,source_hash,generation_source,reviewed_at
        FROM catalog_localized_listing
        WHERE team_id=? AND resource_kind='agent' AND resource_id=?
          AND resource_revision=? AND review_state='confirmed' AND source_hash=?
        ORDER BY locale
        """,
        (team_id, agent_id, working_copy_id, source_hash),
    ) as cursor:
        staged = await cursor.fetchall()
    source_locales: set[str] = set()
    metadata: dict[str, dict[str, Any]] = {}
    for row in staged:
        locale = canonical_locale(row[0])
        fields = normalize_localized_fields(_decode_object(row[2]))
        rows[locale] = (fields, str(row[4]))
        source_locales.add(canonical_locale(row[1]))
        metadata[locale] = {
            "source_hash": str(row[3]),
            "generation_source": str(row[4]),
            "reviewed_at": str(row[5]) if row[5] else None,
        }
    if not staged:
        previous = result.get("localization_metadata")
        if isinstance(previous, Mapping):
            for locale in rows:
                item = previous.get(locale)
                if isinstance(item, Mapping):
                    metadata[locale] = dict(item)
    source_locale_value = result.get("source_locale", "zh-CN")
    try:
        source_locale = canonical_locale(source_locale_value)
    except CatalogLocalizationError:
        source_locale = "zh-CN"
    if source_locales:
        if len(source_locales) != 1:
            raise CatalogLocalizationConflictError("agent_localization_source_locale_mismatch")
        source_locale = next(iter(source_locales))
    result.pop("localized_listings", None)
    result.pop("localized_display_names", None)
    result.pop("localization_metadata", None)
    result["source_locale"] = source_locale
    if rows:
        result["localized_listings"] = {locale: fields for locale, (fields, _source) in sorted(rows.items())}
        result["localized_display_names"] = {locale: str(fields["display_name"]) for locale, (fields, _source) in sorted(rows.items())}
        result["localization_metadata"] = {
            locale: {
                "source_hash": source_hash,
                "generation_source": source,
                "reviewed_at": metadata.get(locale, {}).get("reviewed_at"),
            }
            for locale, (_fields, source) in sorted(rows.items())
        }
    return result


class CatalogLocalizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def agent_manager_snapshot(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        timestamp = datetime.now(UTC).isoformat()
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
                try:
                    source_locale = canonical_locale(listing.get("source_locale", "zh-CN"))
                except CatalogLocalizationError:
                    source_locale = "zh-CN"
                source_hash = agent_listing_source_hash(listing)
                await db.execute(
                    """
                    UPDATE catalog_localized_listing
                    SET review_state='stale',row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND resource_kind='agent' AND resource_id=?
                      AND resource_revision=? AND review_state IN ('draft','confirmed')
                      AND source_hash<>?
                    """,
                    (timestamp, team_id, agent_id, str(working["id"]), source_hash),
                )
                rows = await _rows_for_revision(
                    db,
                    team_id=team_id,
                    resource_kind="agent",
                    resource_id=agent_id,
                    resource_revision=str(working["id"]),
                )
                row_source_locales = {item.source_locale for item in rows}
                if len(row_source_locales) == 1:
                    source_locale = next(iter(row_source_locales))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return {
            "kind": "catalog_localization_manager",
            "schema_version": 1,
            "resource_kind": "agent",
            "resource_id": agent_id,
            "resource_revision": str(working["id"]),
            "working_copy_row_version": int(working["row_version"]),
            "source_locale": source_locale,
            "source": agent_listing_source_fields(listing),
            "source_hash": source_hash,
            "items": [item.to_dict() for item in rows],
        }

    async def save_agent_draft(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        locale: str,
        source_locale: str,
        fields: Mapping[str, Any],
        generation_source: Literal["manual", "llm"],
        expected_source_hash: str,
        expected_row_version: int | None,
    ) -> CatalogLocalizedListing:
        locale = canonical_locale(locale)
        source_locale = canonical_locale(source_locale)
        if locale == source_locale:
            raise CatalogLocalizationError("catalog_localization_target_matches_source")
        fields_value = normalize_localized_fields(fields)
        fields_json = _canonical_json(fields_value)
        timestamp = datetime.now(UTC).isoformat()
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
                source_hash = agent_listing_source_hash(_decode_object(working["listing_json"]))
                if source_hash != expected_source_hash:
                    raise CatalogLocalizationConflictError("catalog_localization_source_changed")
                async with db.execute(
                    """
                    SELECT DISTINCT source_locale FROM catalog_localized_listing
                    WHERE team_id=? AND resource_kind='agent' AND resource_id=?
                      AND resource_revision=?
                    """,
                    (team_id, agent_id, str(working["id"])),
                ) as cursor:
                    locked_source_locales = {canonical_locale(row[0]) for row in await cursor.fetchall()}
                if locked_source_locales and locked_source_locales != {source_locale}:
                    raise CatalogLocalizationConflictError(
                        "catalog_localization_source_locale_locked",
                    )
                async with db.execute(
                    """
                    SELECT * FROM catalog_localized_listing
                    WHERE team_id=? AND resource_kind='agent' AND resource_id=?
                      AND resource_revision=? AND locale=?
                    """,
                    (team_id, agent_id, str(working["id"]), locale),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    if expected_row_version is not None:
                        raise CatalogLocalizationConflictError("catalog_localization_version_conflict")
                    row_id = f"cll_{uuid4().hex}"
                    await db.execute(
                        """
                        INSERT INTO catalog_localized_listing(
                          id,team_id,resource_kind,resource_id,resource_revision,locale,
                          source_locale,fields_json,source_hash,generation_source,review_state,
                          row_version,reviewed_by,reviewed_at,created_by,created_at,updated_at
                        ) VALUES (?,?, 'agent',?,?,?,?,?,?,?,'draft',1,NULL,NULL,?,?,?)
                        """,
                        (
                            row_id,
                            team_id,
                            agent_id,
                            str(working["id"]),
                            locale,
                            source_locale,
                            fields_json,
                            source_hash,
                            generation_source,
                            actor_user_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    if expected_row_version != int(existing["row_version"]):
                        raise CatalogLocalizationConflictError("catalog_localization_version_conflict")
                    row_id = str(existing["id"])
                    updated = await db.execute(
                        """
                        UPDATE catalog_localized_listing
                        SET source_locale=?,fields_json=?,source_hash=?,generation_source=?,
                            review_state='draft',row_version=row_version+1,
                            reviewed_by=NULL,reviewed_at=NULL,updated_at=?
                        WHERE id=? AND row_version=?
                        """,
                        (
                            source_locale,
                            fields_json,
                            source_hash,
                            generation_source,
                            timestamp,
                            row_id,
                            expected_row_version,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise CatalogLocalizationConflictError("catalog_localization_version_conflict")
                row = await _row_by_id(db, row_id)
                await _write_audit_conn(
                    db,
                    db_path=self.db_path,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    agent_id=agent_id,
                    action=AuditAction.AGENT_LOCALIZATION_DRAFT_SAVED,
                    payload={
                        "locale": locale,
                        "source_locale": source_locale,
                        "generation_source": generation_source,
                        "source_hash": source_hash,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _from_row(row)

    async def confirm_agent_translation(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        locale: str,
        expected_row_version: int,
    ) -> CatalogLocalizedListing:
        locale = canonical_locale(locale)
        timestamp = datetime.now(UTC).isoformat()
        source_changed = False
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
                source_hash = agent_listing_source_hash(_decode_object(working["listing_json"]))
                async with db.execute(
                    """
                    SELECT * FROM catalog_localized_listing
                    WHERE team_id=? AND resource_kind='agent' AND resource_id=?
                      AND resource_revision=? AND locale=?
                    """,
                    (team_id, agent_id, str(working["id"]), locale),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise CatalogLocalizationError("catalog_localization_not_found")
                if int(row["row_version"]) != expected_row_version:
                    raise CatalogLocalizationConflictError("catalog_localization_version_conflict")
                if str(row["source_hash"]) != source_hash:
                    await db.execute(
                        """
                        UPDATE catalog_localized_listing
                        SET review_state='stale',row_version=row_version+1,updated_at=?
                        WHERE id=? AND row_version=?
                        """,
                        (timestamp, str(row["id"]), expected_row_version),
                    )
                    source_changed = True
                if str(row["review_state"]) == "stale":
                    raise CatalogLocalizationConflictError("catalog_localization_source_changed")
                if not source_changed and str(row["review_state"]) == "draft":
                    await db.execute(
                        """
                        UPDATE catalog_localized_listing
                        SET review_state='confirmed',row_version=row_version+1,
                            reviewed_by=?,reviewed_at=?,updated_at=?
                        WHERE id=? AND row_version=?
                        """,
                        (
                            actor_user_id,
                            timestamp,
                            timestamp,
                            str(row["id"]),
                            expected_row_version,
                        ),
                    )
                    row = await _row_by_id(db, str(row["id"]))
                    await _write_audit_conn(
                        db,
                        db_path=self.db_path,
                        team_id=team_id,
                        actor_user_id=actor_user_id,
                        agent_id=agent_id,
                        action=AuditAction.AGENT_LOCALIZATION_CONFIRMED,
                        payload={
                            "locale": locale,
                            "source_locale": str(row["source_locale"]),
                            "generation_source": str(row["generation_source"]),
                            "source_hash": source_hash,
                        },
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if source_changed:
            raise CatalogLocalizationConflictError("catalog_localization_source_changed")
        return _from_row(row)


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
        raise CatalogLocalizationForbiddenError(
            "catalog_localization_release_cutover_required"
            if reason == "release_cutover_required"
            else "catalog_localization_working_copy_unavailable",
        )
    return row


async def _write_audit_conn(
    db: aiosqlite.Connection,
    *,
    db_path: str,
    team_id: str,
    actor_user_id: str,
    agent_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    await UserStore(db_path).write_admin_audit_log(
        actor_user_id=actor_user_id,
        team_id=team_id,
        action=action,
        target_kind="team_subagent",
        target_id=agent_id,
        target_team_id=team_id,
        scope="team",
        payload=payload,
        conn=db,
        audit_id=f"aud_{uuid4().hex}",
    )


async def _rows_for_revision(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    resource_kind: CatalogResourceKind,
    resource_id: str,
    resource_revision: str,
) -> tuple[CatalogLocalizedListing, ...]:
    async with db.execute(
        """
        SELECT * FROM catalog_localized_listing
        WHERE team_id=? AND resource_kind=? AND resource_id=? AND resource_revision=?
        ORDER BY locale,id
        """,
        (team_id, resource_kind, resource_id, resource_revision),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_from_row(row) for row in rows)


async def _row_by_id(db: aiosqlite.Connection, row_id: str) -> aiosqlite.Row:
    async with db.execute(
        "SELECT * FROM catalog_localized_listing WHERE id=?",
        (row_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise CatalogLocalizationError("catalog_localization_not_found")
    return row


# Written by :func:`materialize_confirmed_agent_localizations_conn`, never by the
# Owner: `source_locale` lands on every materialized listing (even for an Agent with
# no translations at all) and `localization_metadata` is source-hash/reviewed-at
# bookkeeping.  Anything that asks "did the Owner change this listing" has to drop
# them first, or it reports an edit nobody made -- and, across a
# frozen before the materializer ran.  The translations themselves are deliberately
# NOT in here: they are consumer-facing copy the Owner confirmed.
SERVER_DERIVED_AGENT_LISTING_KEYS = frozenset({"localization_metadata", "source_locale"})


def without_server_derived_listing_keys(listing: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the materializer's own bookkeeping so two listings can be compared."""
    return {key: value for key, value in listing.items() if key not in SERVER_DERIVED_AGENT_LISTING_KEYS}


def _published_localizations(
    listing: Mapping[str, Any],
    *,
    source_hash: str,
) -> dict[str, tuple[dict[str, Any], str]]:
    result: dict[str, tuple[dict[str, Any], str]] = {}
    raw_rows = listing.get("localized_listings")
    raw_metadata = listing.get("localization_metadata")
    metadata: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_metadata, Mapping):
        for raw_locale, item in raw_metadata.items():
            if not isinstance(item, Mapping):
                continue
            try:
                metadata[canonical_locale(raw_locale)] = item
            except CatalogLocalizationError:
                continue
    if isinstance(raw_rows, Mapping):
        for raw_locale, raw_fields in raw_rows.items():
            try:
                locale = canonical_locale(raw_locale)
                fields = normalize_localized_fields(raw_fields) if isinstance(raw_fields, Mapping) else None
            except CatalogLocalizationError:
                continue
            meta = metadata.get(locale)
            if fields is None or not isinstance(meta, Mapping) or meta.get("source_hash") != source_hash:
                continue
            raw_generation_source = meta.get("generation_source")
            generation_source = raw_generation_source if raw_generation_source in {"manual", "llm", "platform"} else "manual"
            result[locale] = (fields, generation_source)
    legacy = listing.get("localized_display_names") if raw_rows is None and raw_metadata is None else None
    canonical = agent_listing_source_fields(listing)
    if isinstance(legacy, Mapping):
        for raw_locale, raw_name in legacy.items():
            if not isinstance(raw_name, str):
                continue
            try:
                locale = canonical_locale(raw_locale)
                fields = normalize_localized_fields(
                    {
                        "display_name": raw_name,
                        "summary": canonical["summary"],
                    },
                )
            except CatalogLocalizationError:
                continue
            result.setdefault(locale, (fields, "manual"))
    return result


def _from_row(row: aiosqlite.Row) -> CatalogLocalizedListing:
    return CatalogLocalizedListing(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        resource_kind=str(row["resource_kind"]),  # type: ignore[arg-type]
        resource_id=str(row["resource_id"]),
        resource_revision=str(row["resource_revision"]),
        locale=str(row["locale"]),
        source_locale=str(row["source_locale"]),
        fields=normalize_localized_fields(_decode_object(row["fields_json"])),
        source_hash=str(row["source_hash"]),
        generation_source=str(row["generation_source"]),  # type: ignore[arg-type]
        review_state=str(row["review_state"]),  # type: ignore[arg-type]
        row_version=int(row["row_version"]),
        reviewed_by=(str(row["reviewed_by"]) if row["reviewed_by"] else None),
        reviewed_at=(str(row["reviewed_at"]) if row["reviewed_at"] else None),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _decode_object(value: object) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CatalogLocalizationError("catalog_localization_payload_invalid") from exc
    if not isinstance(result, dict):
        raise CatalogLocalizationError("catalog_localization_payload_invalid")
    return result


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CatalogLocalizationError("catalog_localization_payload_invalid") from exc


def _hash_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


__all__ = [
    "CatalogLocalizationConflictError",
    "CatalogLocalizationError",
    "CatalogLocalizationForbiddenError",
    "CatalogLocalizationStore",
    "CatalogLocalizedListing",
    "ResolvedLocalizedListing",
    "agent_listing_source_fields",
    "agent_listing_source_hash",
    "canonical_locale",
    "materialize_confirmed_agent_localizations_conn",
    "normalize_localized_fields",
    "resolve_agent_listing",
]
