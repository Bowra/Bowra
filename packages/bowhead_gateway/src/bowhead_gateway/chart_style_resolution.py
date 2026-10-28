"""Storage-backed chart-style resolution shared by screen and export APIs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.chart_style import (
    KEY_CHART_ACCESSIBILITY_PREFERENCES,
    KEY_TEAM_CHART_STYLE_POLICY,
    ChartStyleError,
    chart_style_policy_etag,
    parse_chart_accessibility_preferences,
    parse_team_chart_style_policy,
    parse_user_team_chart_style_selection,
    resolve_effective_chart_style,
    user_team_chart_style_selection_key,
)

if TYPE_CHECKING:
    from bowhead_core.chart_style_read_service import ChartStyleReadService
    from bowhead_core.team_settings_store import TeamSettingsStore
    from bowhead_core.user_settings_store import UserSettingsStore

logger = logging.getLogger(__name__)


async def resolve_chart_style_for_user(
    *,
    team_store: TeamSettingsStore,
    user_store: UserSettingsStore,
    team_id: str,
    user_id: str,
) -> tuple[dict[str, Any], str]:
    raw_policy = await team_store.get(team_id, KEY_TEAM_CHART_STYLE_POLICY)
    raw_selection, selection_updated_at = await user_store.get_with_updated_at(
        user_id,
        user_team_chart_style_selection_key(team_id),
    )
    raw_accessibility, accessibility_updated_at = await user_store.get_with_updated_at(
        user_id,
        KEY_CHART_ACCESSIBILITY_PREFERENCES,
    )
    policy = _parse(raw_policy, parse_team_chart_style_policy, "team policy", team_id)
    selection = _parse(
        raw_selection,
        parse_user_team_chart_style_selection,
        "user selection",
        user_id,
    )
    accessibility = _parse(
        raw_accessibility,
        parse_chart_accessibility_preferences,
        "accessibility",
        user_id,
    )
    resolved = resolve_effective_chart_style(
        team_policy=policy,
        user_selection=selection,
        accessibility=accessibility,
        policy_etag=chart_style_policy_etag(raw_policy),
    )
    etag_material = ":".join(
        (
            raw_policy or "",
            raw_selection or "",
            selection_updated_at or "",
            raw_accessibility or "",
            accessibility_updated_at or "",
        )
    )
    return resolved, etag_material


async def resolve_chart_style_for_user_v2(
    *,
    service: ChartStyleReadService,
    team_id: str,
    user_id: str,
    surfaces: dict[str, str],
    color_scheme_checksum: str,
) -> tuple[dict[str, Any], str]:
    return await service.effective(
        team_id=team_id,
        user_id=user_id,
        surfaces=surfaces,
        color_scheme_checksum=color_scheme_checksum,
    )


def _parse(
    raw: str | None,
    parser: Any,
    label: str,
    owner_id: str,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        return parser(raw)
    except ChartStyleError as exc:
        logger.warning(
            "chart_style.invalid_setting label=%s owner_id=%s err=%s",
            label,
            owner_id,
            str(exc),
        )
        return None


__all__ = ["resolve_chart_style_for_user", "resolve_chart_style_for_user_v2"]
