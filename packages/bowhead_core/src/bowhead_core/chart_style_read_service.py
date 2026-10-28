"""Transactional V2 chart-style reads and safe member projections."""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.chart_style import (
    KEY_CHART_ACCESSIBILITY_PREFERENCES,
    KEY_TEAM_CHART_STYLE_POLICY,
    PRODUCT_DEFAULT_PRESET_ID,
    ChartStyleError,
    parse_chart_accessibility_preferences,
    user_team_chart_style_selection_key,
)
from bowhead_core.chart_style_materializer import (
    CHART_STYLE_KINDS,
    MAX_RESOLVED_SNAPSHOT_BYTES,
    actual_surface_projection,
    revision_checksum,
    validate_resolved_snapshot,
)
from bowhead_core.chart_style_models import (
    RESOLVER_POLICY_VERSION,
    STYLE_SCHEMA_VERSION,
    TOKEN_SCHEMA_VERSION,
    ChartStyleDomainError,
    sha256_json,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.chart_style_store import (
    current_revision,
    registry_version,
    style_by_id,
)
from bowhead_core.chart_style_v2 import (
    built_in_style_catalog,
    chart_style_cache_identity,
    chart_style_policy_etag_v2,
    default_team_chart_style_policy_v2,
    is_custom_chart_style_id,
    parse_team_chart_style_policy_v2,
    parse_user_team_chart_style_selection_v2,
    strong_chart_style_etag,
)
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.theme_compiler import canonical_json

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class _Snapshot:
    raw_policy: str | None
    policy: dict[str, Any]
    policy_syntax_valid: bool
    raw_selection: str | None
    selection: dict[str, Any] | None
    selection_error: bool
    selection_updated_at: str | None
    raw_accessibility: str | None
    accessibility: dict[str, Any] | None
    accessibility_updated_at: str | None
    registry_version: int
    catalog: dict[str, dict[str, Any]]
    failures: dict[str, str]


async def _user_setting(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    key: str,
) -> tuple[str | None, str | None]:
    async with db.execute(
        "SELECT value,updated_at FROM user_settings WHERE user_id=? AND key=?",
        (user_id, key),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None, None
    return str(row["value"]), str(row["updated_at"])


def _parse_accessibility(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        return parse_chart_accessibility_preferences(raw)
    except ChartStyleError:
        return None


def _parse_selection(
    raw: str | None,
) -> tuple[dict[str, Any] | None, bool]:
    if raw is None:
        return None, False
    try:
        return parse_user_team_chart_style_selection_v2(raw), False
    except ChartStyleError:
        return None, True


def _parse_policy(raw: str | None) -> tuple[dict[str, Any], bool]:
    if raw is None:
        return default_team_chart_style_policy_v2(), True
    try:
        return parse_team_chart_style_policy_v2(raw), True
    except ChartStyleError:
        return default_team_chart_style_policy_v2(), False


async def custom_style_projection(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    style_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    style = await style_by_id(db, team_id=team_id, style_id=style_id)
    if style is None:
        return None, "missing"
    if style.lifecycle != "active":
        return None, "inactive"
    revision = await current_revision(db, style=style)
    if revision is None:
        return None, "missing"
    if (
        revision.schema_version != STYLE_SCHEMA_VERSION
        or revision.token_schema_version != TOKEN_SCHEMA_VERSION
        or revision.resolver_policy_version != RESOLVER_POLICY_VERSION
    ):
        return None, "unsupported_schema"
    if len(canonical_json(revision.resolved_snapshot).encode("utf-8")) > MAX_RESOLVED_SNAPSHOT_BYTES or revision.validation.get("blockers"):
        return None, "unsupported_schema"
    if sha256_json(revision.resolved_snapshot) != revision.snapshot_checksum:
        return None, "checksum_mismatch"
    if sha256_json(revision.authoring_base_snapshot) != revision.authoring_base_snapshot_checksum:
        return None, "checksum_mismatch"
    capabilities = revision.resolved_snapshot.get("capabilities")
    colors = revision.resolved_snapshot.get("colors")
    kind_tokens = revision.resolved_snapshot.get("kindTokens")
    if (
        revision.resolved_snapshot.get("schemaVersion") != STYLE_SCHEMA_VERSION
        or revision.resolved_snapshot.get("tokenSchemaVersion") != TOKEN_SCHEMA_VERSION
        or revision.resolved_snapshot.get("resolverPolicyVersion") != RESOLVER_POLICY_VERSION
        or not isinstance(capabilities, dict)
        or capabilities.get("supportedChartKinds") != list(CHART_STYLE_KINDS)
        or capabilities.get("requiredAdapterCapabilities")
        or not isinstance(kind_tokens, dict)
        or set(kind_tokens) != set(CHART_STYLE_KINDS)
        or not isinstance(colors, dict)
        or not isinstance(colors.get("light"), dict)
        or not isinstance(colors.get("dark"), dict)
    ):
        return None, "unsupported_schema"
    try:
        validation = validate_resolved_snapshot(revision.resolved_snapshot)
    except (KeyError, TypeError, ValueError):
        return None, "unsupported_schema"
    if validation["blockers"]:
        return None, "unsupported_schema"
    expected_revision_checksum = revision_checksum(
        source=revision.source,
        base_preset_id=revision.base_preset_id,
        base_preset_revision=revision.base_preset_revision,
        base_preset_checksum=revision.base_preset_checksum,
        authoring_base_snapshot=revision.authoring_base_snapshot,
        authoring_base_snapshot_checksum=revision.authoring_base_snapshot_checksum,
        resolved_snapshot=revision.resolved_snapshot,
        snapshot_checksum=revision.snapshot_checksum,
        validation=revision.validation,
    )
    if expected_revision_checksum != revision.revision_checksum:
        return None, "checksum_mismatch"
    return (
        {
            "kind": "teamCustom",
            "id": style.id,
            "name": revision.name,
            "description": revision.description,
            "revision": revision.id,
            "snapshotChecksum": revision.snapshot_checksum,
            "basePreset": {
                "id": revision.base_preset_id,
                "revision": revision.base_preset_revision,
                "checksum": revision.base_preset_checksum,
            },
            "fallbackBuiltInPresetId": style.fallback_builtin_preset_id,
            "resolvedPresetSnapshot": copy.deepcopy(revision.resolved_snapshot),
        },
        None,
    )


async def _catalog_for_policy(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    policy: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    catalog = built_in_style_catalog()
    failures: dict[str, str] = {}
    for style_id in policy["enabledStyleIds"]:
        if not is_custom_chart_style_id(style_id):
            continue
        projection, reason = await custom_style_projection(
            db,
            team_id=team_id,
            style_id=style_id,
        )
        if projection is None:
            failures[style_id] = reason or "missing"
        else:
            catalog[style_id] = projection
    return catalog, failures


def _accessibility_projection(
    preferences: dict[str, Any] | None,
) -> dict[str, Any]:
    value = preferences or {"version": 1}
    return {
        "contrast": value.get("contrast", "standard"),
        "colorVision": value.get("colorVision", "standard"),
        "source": {
            "contrast": "user" if "contrast" in value else "product",
            "colorVision": "user" if "colorVision" in value else "product",
        },
    }


def _identity(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(projection[key])
        for key in (
            "kind",
            "id",
            "revision",
            "snapshotChecksum",
            "basePreset",
            "fallbackBuiltInPresetId",
            "resolvedPresetSnapshot",
        )
        if key in projection
    }


def _selection_adjustment(
    *,
    requested: str | None,
    effective: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "path": "selection.styleId",
        "requested": requested,
        "effective": effective,
        "reason": f"selection.{reason}",
    }


class ChartStyleReadService:
    """Read policy, head, and exact revision from one SQLite snapshot."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._effective_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._options_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @staticmethod
    def _cache_get(
        cache: OrderedDict[str, dict[str, Any]],
        key: str,
    ) -> dict[str, Any] | None:
        value = cache.get(key)
        if value is None:
            return None
        cache.move_to_end(key)
        return copy.deepcopy(value)

    @staticmethod
    def _cache_put(
        cache: OrderedDict[str, dict[str, Any]],
        key: str,
        value: dict[str, Any],
    ) -> None:
        cache[key] = copy.deepcopy(value)
        cache.move_to_end(key)
        while len(cache) > 256:
            cache.popitem(last=False)

    async def _snapshot(
        self,
        *,
        team_id: str,
        user_id: str | None,
    ) -> _Snapshot:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            raw_policy = await TeamSettingsStore.get_with_connection(
                db,
                team_id,
                KEY_TEAM_CHART_STYLE_POLICY,
            )
            policy, syntax_valid = _parse_policy(raw_policy)
            catalog, failures = await _catalog_for_policy(
                db,
                team_id=team_id,
                policy=policy,
            )
            version = await registry_version(db, team_id=team_id)
            if user_id is None:
                raw_selection = None
                selection_updated_at = None
                raw_accessibility = None
                accessibility_updated_at = None
            else:
                raw_selection, selection_updated_at = await _user_setting(
                    db,
                    user_id=user_id,
                    key=user_team_chart_style_selection_key(team_id),
                )
                raw_accessibility, accessibility_updated_at = await _user_setting(
                    db,
                    user_id=user_id,
                    key=KEY_CHART_ACCESSIBILITY_PREFERENCES,
                )
            selection, selection_error = _parse_selection(raw_selection)
            selected_style_id = selection.get("styleId") if selection else None
            if (
                isinstance(selected_style_id, str)
                and is_custom_chart_style_id(selected_style_id)
                and selected_style_id not in catalog
                and selected_style_id not in failures
            ):
                projection, reason = await custom_style_projection(
                    db,
                    team_id=team_id,
                    style_id=selected_style_id,
                )
                if projection is None:
                    failures[selected_style_id] = reason or "missing"
                else:
                    catalog[selected_style_id] = projection
            return _Snapshot(
                raw_policy=raw_policy,
                policy=policy,
                policy_syntax_valid=syntax_valid,
                raw_selection=raw_selection,
                selection=selection,
                selection_error=selection_error,
                selection_updated_at=selection_updated_at,
                raw_accessibility=raw_accessibility,
                accessibility=_parse_accessibility(raw_accessibility),
                accessibility_updated_at=accessibility_updated_at,
                registry_version=version,
                catalog=catalog,
                failures=failures,
            )

    async def effective(
        self,
        *,
        team_id: str,
        user_id: str,
        surfaces: dict[str, str],
        color_scheme_checksum: str,
    ) -> tuple[dict[str, Any], str]:
        snapshot = await self._snapshot(team_id=team_id, user_id=user_id)
        cache_identity = chart_style_cache_identity(
            team_id=team_id,
            registry_version=snapshot.registry_version,
            raw_policy=snapshot.raw_policy,
            raw_selection=snapshot.raw_selection,
            selection_updated_at=snapshot.selection_updated_at,
            raw_accessibility=snapshot.raw_accessibility,
            accessibility_updated_at=snapshot.accessibility_updated_at,
        )
        effective_cache_key = canonical_json(
            {
                "identity": cache_identity,
                "surfaces": surfaces,
                "colorSchemeChecksum": color_scheme_checksum,
            }
        )
        cached = self._cache_get(self._effective_cache, effective_cache_key)
        if cached is not None:
            return cached, cache_identity
        effective_policy = snapshot.policy if snapshot.policy_syntax_valid else default_team_chart_style_policy_v2()
        catalog = snapshot.catalog
        saved_style_id = snapshot.selection.get("styleId") if snapshot.selection else None
        unavailable_reason: str | None = None
        selected: dict[str, Any] | None = None
        source = "product"

        if snapshot.selection_error:
            unavailable_reason = "unsupported_schema"
        elif saved_style_id is not None:
            if saved_style_id in snapshot.failures:
                unavailable_reason = snapshot.failures[saved_style_id]
            elif saved_style_id not in snapshot.policy["enabledStyleIds"] or not snapshot.policy_syntax_valid:
                unavailable_reason = "not_in_policy"
            else:
                selected = catalog.get(saved_style_id)
                source = "user"

        if selected is None and snapshot.policy_syntax_valid:
            selected = catalog.get(snapshot.policy["defaultStyleId"])
            if selected is not None:
                source = "team" if snapshot.raw_policy is not None else "product"
        if selected is None and snapshot.policy_syntax_valid:
            fallback_id = snapshot.policy.get(
                "fallbackBuiltInPresetId",
                PRODUCT_DEFAULT_PRESET_ID,
            )
            selected = catalog.get(fallback_id)
            if selected is not None and snapshot.raw_policy is not None:
                source = "team"
        if selected is None:  # The generated product catalog is a hard runtime base.
            selected = built_in_style_catalog()[PRODUCT_DEFAULT_PRESET_ID]
            source = "product"

        adjustments: list[dict[str, Any]] = []
        if unavailable_reason is not None:
            adjustments.append(
                _selection_adjustment(
                    requested=saved_style_id,
                    effective=selected["id"],
                    reason=unavailable_reason,
                )
            )
        elif not snapshot.policy_syntax_valid and snapshot.raw_policy is not None:
            adjustments.append(
                _selection_adjustment(
                    requested=snapshot.policy.get("defaultStyleId"),
                    effective=selected["id"],
                    reason="unsupported_schema",
                )
            )

        mode_colors = {
            mode: actual_surface_projection(
                selected["resolvedPresetSnapshot"],
                mode=mode,
                surface=surfaces[mode],
                color_scheme_checksum=color_scheme_checksum,
            )
            for mode in ("light", "dark")
        }
        policy_etag = chart_style_policy_etag_v2(snapshot.raw_policy)
        result = {
            "schemaVersion": 2,
            "registryVersion": snapshot.registry_version,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
            "style": _identity(selected),
            "selection": {
                "source": source,
                "savedStyleId": saved_style_id,
                "unavailableReason": unavailable_reason,
            },
            "accessibility": _accessibility_projection(snapshot.accessibility),
            "modeColors": mode_colors,
            "teamPolicy": {**copy.deepcopy(effective_policy), "etag": policy_etag},
            "resolutionAdjustments": adjustments,
        }
        self._cache_put(self._effective_cache, effective_cache_key, result)
        return result, cache_identity

    async def options(self, *, team_id: str) -> tuple[dict[str, Any], str]:
        snapshot = await self._snapshot(team_id=team_id, user_id=None)
        options_cache_key = canonical_json(
            {
                "teamId": team_id,
                "registryVersion": snapshot.registry_version,
                "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
                "policyRaw": snapshot.raw_policy,
                "contractVersion": 2,
            }
        )
        cached = self._cache_get(self._options_cache, options_cache_key)
        if cached is not None:
            return cached, strong_chart_style_etag(
                "chart-style-options-v2",
                cached,
            )
        policy = snapshot.policy if snapshot.policy_syntax_valid else default_team_chart_style_policy_v2()
        catalog = snapshot.catalog
        items: list[dict[str, Any]] = []
        for style_id in policy["enabledStyleIds"]:
            projection = catalog.get(style_id)
            if projection is None:
                # A corrupt policy is replaced as a whole above.  This guard is
                # only defensive against future registry incompatibility.
                continue
            items.append(
                {
                    key: copy.deepcopy(projection[key])
                    for key in (
                        "id",
                        "kind",
                        "name",
                        "description",
                        "revision",
                        "snapshotChecksum",
                        "resolvedPresetSnapshot",
                    )
                }
            )
        body = {
            "schemaVersion": 2,
            "items": items,
            "teamPolicy": {
                **copy.deepcopy(policy),
                "etag": chart_style_policy_etag_v2(snapshot.raw_policy),
            },
            "registryVersion": snapshot.registry_version,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
        }
        self._cache_put(self._options_cache, options_cache_key, body)
        return body, strong_chart_style_etag("chart-style-options-v2", body)

    async def selection_fallback(
        self,
        *,
        team_id: str,
        style_id: str,
    ) -> str:
        snapshot = await self._snapshot(team_id=team_id, user_id=None)
        if not snapshot.policy_syntax_valid:
            raise ChartStyleDomainError("chart_style_policy_corrupt")
        if style_id not in snapshot.policy["enabledStyleIds"]:
            raise ChartStyleDomainError(
                "chart_style_not_found",
                status_code=404,
            )
        projection = snapshot.catalog.get(style_id)
        if projection is None:
            raise ChartStyleDomainError(
                "chart_style_not_found",
                status_code=404,
            )
        return str(projection["fallbackBuiltInPresetId"])


__all__ = ["ChartStyleReadService", "custom_style_projection"]
