"""Localize display-only metric and dimension names from configured metadata.

Query identifiers remain unchanged. Replace longer names first and read only the
cache on the step-preview path, avoiding network latency. Missing cached labels
leave the original preview intact.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Skip replacement for short names: one/two-character metric names (if any) match unrelated natural-language
# fragments such as dates/conjunctions; false replacements far outweigh benefits.
_MIN_REPLACEABLE_NAME = 3

# Clarification-card schema fetch limit for display names. datarc normally takes ~500ms (two concurrent lists);
# allow 3 seconds, then send without labels: **cards must not wait for labels**.
_SCHEMA_FETCH_TIMEOUT_SEC = 3.0


def _display_for(meta: Any, locale: str) -> str:
    """该 locale 的展示名；与规范名相同或缺失 → ""（不换）。"""
    names = getattr(meta, "display_names", None) or {}
    if not isinstance(names, dict):
        return ""
    display = str(names.get(locale) or "").strip()
    if not display:
        return ""
    primary = str((getattr(meta, "names", None) or [""])[0] or "").strip()
    return "" if display == primary else display


def build_locale_relabel_map(schema: Any, locale: str) -> list[tuple[str, str]]:
    """`_CachedSchema` → [(规范名, 展示名), …]，**按规范名长度降序**。

    降序是正确性要求，不是优化：见模块 docstring 第 1 条。
    """
    pairs: list[tuple[str, str]] = []
    for bucket in ("measures", "dimensions"):
        for meta in getattr(schema, bucket, None) or []:
            names = getattr(meta, "names", None) or []
            canonical = str(names[0] if names else "").strip()
            if len(canonical) < _MIN_REPLACEABLE_NAME:
                continue
            display = _display_for(meta, locale)
            if display:
                pairs.append((canonical, display))
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    return pairs


def relabel_query_preview(text: str, pairs: list[tuple[str, str]]) -> str:
    """把 `text` 里出现的规范名换成展示名；`pairs` 必须已按长度降序。

    单趟扫描 + 交替正则：一个位置只会被替换一次，换出来的英文名不会再被后续
    规则二次匹配（先换 `内销销售金额`→`In-mkt Sales Value`，再拿 `销售金额`
    去匹配已经换好的英文串是没有意义的）。
    """
    if not text or not pairs:
        return text
    mapping = dict(pairs)
    pattern = "|".join(re.escape(name) for name, _ in pairs)
    try:
        return re.sub(pattern, lambda m: mapping.get(m.group(0), m.group(0)), text)
    except re.error as exc:  # pragma: no cover — unusual name characters are escaped; fallback only
        logger.debug("step_preview.relabel_failed err=%s", exc)
        return text


async def localize_option_labels(
    options: list[str],
    *,
    schema_cache: Any | None,
    datarc_workspace_id: str | None,
    locale: str,
    datarc_client: Any | None = None,
) -> list[str] | None:
    """Create option labels without changing option values, order, or list length.

    Use exact configured-name matches and return None when none match. Clarification
    may perform one bounded metadata fetch per turn because it can precede any data
    call; the hot step-preview path remains cache-only.
    """
    if not options or not locale or locale.startswith("zh"):
        return None
    if not datarc_workspace_id:
        return None
    try:
        schema = None
        if schema_cache is not None:
            schema = schema_cache.get(datarc_workspace_id, count_stats=False)
        if schema is None:
            if datarc_client is None:
                return None
            import anyio

            from bowhead_core.datarc_schema_cache import get_schema_with_cache

            # Use the existing cache-hit read / cache-miss fetch-and-fill boundary: fetched schema also populates cache,
            # avoiding another fetch for later steps this turn (actual data retrieval).
            #
            # ⚠ **Must cap the wait**: this await blocks clarification-card emission, while DatarcClient's
            # default timeout is **60 seconds** (used by `list_measures`/`list_dimensions`).
            # Without a cap, a few translated labels risk leaving users without a card for 60 seconds;
            # the card matters far more. Timeout sends it unlabeled via the existing fallback.
            with anyio.move_on_after(_SCHEMA_FETCH_TIMEOUT_SEC):
                schema = await get_schema_with_cache(
                    datarc_client=datarc_client,
                    datarc_workspace_id=datarc_workspace_id,
                    schema_cache=schema_cache,
                )
        if schema is None:
            return None
        mapping = dict(build_locale_relabel_map(schema, locale))
        labels = [mapping.get(o.strip(), o) for o in options]
        return labels if any(a != b for a, b in zip(labels, options, strict=True)) else None
    except Exception as exc:  # noqa: BLE001 — display enhancement must never break clarification cards
        logger.debug("clarification.localize_options_failed err=%s", exc)
        return None


def localize_step_preview(
    text: str,
    *,
    schema_cache: Any | None,
    datarc_workspace_id: str | None,
    locale: str,
) -> str:
    """步骤卡预览串的 locale 化入口；任何一步不满足就原样返回。

    绝不抛：换名是增强项，出问题就退回原串，不许影响 step 的发出。
    """
    if not text or not locale or locale.startswith("zh"):
        # Chinese locales already use Chinese canonical names; replacing them loses verifiability against original queries.
        return text
    if schema_cache is None or not datarc_workspace_id:
        return text
    try:
        # count_stats=False: incidental display-layer reads must not pollute cache hit-rate metrics
        # (used to tune TTL for data fetching).
        schema = schema_cache.get(datarc_workspace_id, count_stats=False)
        if schema is None:
            return text  # Cache miss → no display-only request (module docstring item 2)
        return relabel_query_preview(text, build_locale_relabel_map(schema, locale))
    except Exception as exc:  # noqa: BLE001 — display enhancement must never break step emission
        logger.debug("step_preview.localize_failed err=%s", exc)
        return text
