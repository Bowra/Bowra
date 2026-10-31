"""Shared Canvas visual-input identity v1.

The identity is intentionally computable before materialization. Pinned image
and design byte hashes are validated separately and are not folded into this
page/export input hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any

from bowhead_core.print_theme import (
    current_print_theme_snapshot,
    parse_print_theme_snapshot,
)

# "2" (2026-07-31): disable Chart.js export-page animations (first frame=final frame; remove nondeterministic intermediate screenshots,
# confirmed by W2 golden smoke). Parity tests compare with FE CANVAS_VISUAL_RENDERER_VERSION.
# "4" (2026-08-12, chart-table-ux batch 1): export rendering changes: always-on value labels (2.2),
# unit-bearing value-axis titles (2.3), lighter hero background/central primary number (2.1b). Same input no longer yields identical pixels;
# old cached receipts must not be reused.
# "5" (2026-08-12, chart-table-ux batch 3): export all table rows (4.1), pie legends with proportions
# (4.2), theme tokens for grid/axis borders (4.3).
# "6" (2026-08-14, chart-table-ux R8): value-axis ticks follow column-level percent mode; fraction columns
# change ticks from 0/0.1/0.2 to 0/10/20 (labels/tooltips/tables already showed 29.2%, while ticks stayed
# raw with axis title "（%）", causing 100× misreading); scatter axis titles both use axisTitleOf.
# Same input no longer yields identical pixels; old cached receipts must not be reused.
# "7" (2026-08-15, chart-table-ux dogfood): stop rendering chart action bars (including type switcher) in exports.
# On 116, PDF charts displayed "Line ▾ / Open data / Download PNG / Download CSV":
# four buttons unusable on paper. Same input no longer yields identical pixels; old cached receipts must not be reused.
# "8" (2026-08-15, chart-table-ux dogfood): exported charts no longer occupy only the upper half: `.v2-chart-canvas-wrap`
# fills available export height (previous clamp 38vh=342px, available 772px, leaving 52% blank).
# Same input no longer yields identical pixels; old cached receipts must not be reused.
# "9" (2026-08-15): export visual page sets `<html lang>` from **export-request locale**.
# Rendering previously followed the gateway Accept-Language-selected bundle, unrelated to identity locale,
# so one visualInputSha256 could represent differently localized numbers (`20亿` vs `2B`).
# Old cached receipts arose under that faulty behavior and must not be reused.
# "11" (2026-09-05): frozen chart-style preset packages and render-context
# recipes now drive export pixels; the default-theme identity is retired.
# "12" (2026-09-06): stable print chrome is frozen alongside the chart-style
# snapshot and both trusted identities participate in the visual input hash.
# "14" (2026-09-08): edge-label padding changes exported line-chart geometry
# when a first/last data label would otherwise cross the canvas boundary.
# "15" (2026-09-27): top padding changes exported line-chart geometry: add padding for upward labels
# (2231de168), avoid highest-label/top-y-tick collision (4545ede3f), compute padding anchor from current scale
# for first-frame effect (da10a5f9d; export pages previously received no top padding). All three changed real-report
# export pixels without a version bump, leaving old cached receipts with clipped labels; do not reuse.
# "16" (2026-09-29): categorical axes no longer thin labels by `maxXTicks`, a constant set for the 344px chat window's
# **time axis**, which lost category identity on nominal axes (116 observed 465 bar charts with "10 categories
# but only 5 labeled"). Export widthClass=wide was 12; Chart.js now autoSkips by actual label width.
# Same input no longer yields identical pixels; old cached receipts must not be reused.
# "17" (2026-09-29): dual_axis multicolumn titles use tiers (shared dimension→dimension only; incompatible dimensions→none),
# matching FE dualAxisTitle. Same input no longer yields identical pixels; old cached receipts must not be reused.
# "18" (2026-10-01): categorical series get a second channel once color alone stops being distinguishable.
# Built-in base colors were nudged to ΔE2000 ≥10 (normal) / ≥7 (protan/deutan) (e.g. analytical-neutral/dark
# slot 1 #A1B0B7→#808A94), overflow colors 7–16 are chosen by distinctness (`palette-overflow/2`), and series past
# the measured color-only capacity — or every series under color-safe — get fill patterns (bar/area/pie) and
# dashes (lines). Same input no longer yields identical pixels; old cached receipts must not be reused.
# "19" (2026-10-01): chart readability batch 2 changes export pixels for existing charts: KPI values from one
# million show the magnitude as the main line and the exact value below; pie slices keep their full colors (the
# protagonist is offset instead of the others faded); waterfall drops are labelled below their bar with axis headroom;
# scatter name labels no longer cover other points; too-wide tables zoom to fit instead of losing columns; non-flow
# Sankey rows render as a table; long horizontal bars draw the largest rows that fit with a truncation note; KPI
# magnitude lines keep their unit; every waterfall follows the chart style's gridlines; exports honour the exporting
# user's legend-hidden series (`hidden_series`, part of the visual payload).
# Same input no longer yields identical pixels; old cached receipts must not be reused.
# "20" (2026-10-05): the print theme moves to recipe v3. Export chart ink, muted ink and grid come from the
# compiled scheme's cool neutrals (product default: ink #061223, ink3 #4F5F74, grid line #77879F / soft grid line2
# #A8B9D1, matching the on-screen --c-line/--c-line-2) instead of the warm #161310/#56503F/#D9D4CB, and a Business
# team's published brand reaches the snapshot. The export frame and chart surface stay white paper (#FFFFFF).
# Same input no longer yields identical pixels; old cached receipts must not be reused.
# "21" (2026-10-06): Chart Embed Spec. Every chart text is the interface sans (no Geist Mono ticks/labels/axis
# titles), data labels 12/500 without a backing plate and 6px outside bar ends; gridlines are one solid separator
# hairline (ink 12%, base line 20%) instead of the dashed / #77879F lines; bars are sized by band (clamp 24–96px) and horizontal bars
# reserve the widest label; axis titles state only the unit; the chart title row loses its tinted band and rule.
# Same input no longer yields identical pixels; old cached receipts must not be reused.
# Version 22: native name-only line endpoints, scoped month ticks and observation precision.
# Version 23: source-checked fixed numeric domains in native shared scale groups.
# Version 24: source-protected ordered native reading steps.
# Version 25: prepared static frames with stable entities and fixed numeric domains.
# Version 26: source-protected observed annotations and root-local reading targets.
# "29" (2026-10-10): source-bound event/coverage marks and prepared entity trends in native Table.
# "30" (2026-10-11): hide endpoint numeric labels on the initial native line render.
# "31" (2026-10-11): readable per-axis and per-value magnitudes with localized exact observations.
# Version 32: source-bound analytical ranges, explicit series coordinates and static facet/row panels.
# "33" (2026-10-14): native table cells keep only short units inline (two characters or a currency code); longer
# unit text is named once in the column header. Same input no longer yields identical pixels; old cached receipts
# must not be reused.
# "34" (2026-10-15): ordinary numeric precision, base-unit Table readings and unrounded source verification remain distinct.
# Version 36: explicit date patterns affect native chart and Table exports.
# Version 37: Table captions and unit disclosures share resolved column display, including export.
EXPORT_VISUAL_RENDERER_VERSION = "37"
VISUAL_FIELDS: dict[str, tuple[str, ...]] = {
    "image": ("artifact_id", "caption", "alt"),
    "design": (
        "title",
        "size_preset",
        "canvas_width",
        "canvas_height",
        "background",
        "layers",
        "flattened_artifact_id",
        "flattened_source_rev",
        "flattened_at",
    ),
    # `hidden_series`: the exporting user's legend-hidden series, folded in by `export_ir` (never persisted).
    "chart": ("spec", "hidden_series"),
    "spec_block": ("format", "title", "direction", "nodes", "edges"),
}
VISUAL_SECTION_KINDS = frozenset(VISUAL_FIELDS)
# Chart-spec fields **not rendered**, projected per viewer (see `_strip_provenance`).
# Parity-tested against FE `NON_VISUAL_SPEC_FIELDS`; changing either side alone mismatches
# all exact_visual receipts and silently degrades every V2 export.
_NON_VISUAL_SPEC_FIELDS: tuple[str, ...] = ("source_meta", "presentation_plan")


def compute_visual_input_sha256(
    *,
    section_kind: str,
    payload: dict[str, Any],
    locale: str,
    theme: dict[str, Any],
    print_theme: dict[str, Any] | None = None,
    renderer_version: str = EXPORT_VISUAL_RENDERER_VERSION,
) -> str | None:
    visual_theme_hash = compute_visual_theme_hash(
        theme=theme,
        print_theme=print_theme,
    )
    identity = build_visual_input_identity(
        section_kind=section_kind,
        payload=payload,
        locale=locale,
        theme_hash=visual_theme_hash,
        renderer_version=renderer_version,
    )
    return sha256_canonical(identity) if identity is not None else None


def compute_visual_theme_hash(
    *,
    theme: dict[str, Any],
    print_theme: dict[str, Any] | None = None,
) -> str:
    """Compose the trusted chart and stable print snapshot identities."""

    parsed_print = parse_print_theme_snapshot(
        print_theme if print_theme is not None else current_print_theme_snapshot(),
    )
    context_recipe = theme.get("contextRecipeVersion")
    if not isinstance(context_recipe, str) or not context_recipe:
        context_recipe = "legacy"
    return sha256_canonical(
        {
            "chart_style_snapshot_checksum": sha256_canonical(theme),
            "print_theme_checksum": parsed_print["checksum"],
            "chart_context_recipe_version": context_recipe,
        }
    )


def build_visual_input_identity(
    *,
    section_kind: str,
    payload: dict[str, Any],
    locale: str,
    theme_hash: str,
    renderer_version: str = EXPORT_VISUAL_RENDERER_VERSION,
    frozen_chart_presentation: dict[str, Any] | None = None,
    export_surface_contract_version: str | None = None,
) -> dict[str, Any] | None:
    if section_kind not in VISUAL_SECTION_KINDS:
        return None
    visual_payload = {field: payload[field] for field in VISUAL_FIELDS[section_kind] if field in payload}
    visual_payload = _strip_provenance(section_kind, visual_payload)
    typed_data = visual_typed_data(section_kind, visual_payload)
    identity = {
        "effective_kind": _effective_visual_kind(section_kind, visual_payload),
        "locale": locale,
        "payload": visual_payload,
        "renderer_version": renderer_version,
        "section_kind": section_kind,
        "theme_hash": theme_hash,
        "typed_data_sha256": (sha256_canonical(typed_data) if typed_data is not None else None),
    }
    if frozen_chart_presentation is not None:
        identity["frozen_chart_presentation"] = frozen_chart_presentation
    if export_surface_contract_version is not None:
        identity["export_surface_contract_version"] = export_surface_contract_version
    return identity


def screen_chart_style_identity_v2(effective: dict[str, Any]) -> dict[str, Any]:
    """Return the pixel-affecting server/client screen-style identity."""

    style = effective.get("style")
    policy = effective.get("teamPolicy")
    if effective.get("schemaVersion") != 2 or not isinstance(style, dict) or not isinstance(policy, dict):
        raise ValueError("effective chart style V2 is invalid")
    return {
        "schema_version": 2,
        "style": style,
        "accessibility": effective.get("accessibility"),
        "mode_colors": effective.get("modeColors"),
        "team_policy_etag": policy.get("etag"),
        "registry_version": effective.get("registryVersion"),
        "built_in_registry_checksum": effective.get("builtInRegistryChecksum"),
        "context_recipe_version": "2",
    }


def compute_screen_visual_input_sha256_v2(
    *,
    section_kind: str,
    payload: dict[str, Any],
    locale: str,
    surface_token: str,
    effective_chart_style: dict[str, Any],
    renderer_version: str = EXPORT_VISUAL_RENDERER_VERSION,
) -> str | None:
    """Compute a screen admission receipt, intentionally distinct from export."""

    screen_theme_hash = sha256_canonical(
        {
            "screen_chart_style": screen_chart_style_identity_v2(
                effective_chart_style,
            ),
            "surface_token": surface_token,
        }
    )
    identity = build_visual_input_identity(
        section_kind=section_kind,
        payload=payload,
        locale=locale,
        theme_hash=screen_theme_hash,
        renderer_version=renderer_version,
    )
    return sha256_canonical(identity) if identity is not None else None


def _strip_provenance(section_kind: str, visual_payload: dict[str, Any]) -> dict[str, Any]:
    """把**溯源**字段挡在视觉身份之外。

    R8 (2026-08-14, 116 真机)：`spec.source_meta` 是**每个观看者一份**的东西——
    `source_access_projection.project_sources_for_viewer` 会把它改写成 viewer-safe 的
    `source-ref-v1:` 别名对象。而 `visualInputSha256` 在 canvas detail 里是拿**投影前**
    的 payload 算的（`routes/canvas.py` 传 `section_by_id[...].payload`），FE 拿到的却是
    **投影后**的 section ⇒ 同一个响应里公布的 hash 和公布的 section 永远对不上
    ⇒ 全部 exact_visual receipt 失配 ⇒ V2 导出降级 legacy。

    取证（116 v1.1.0，浏览器按 app 同款 `Accept-Language` 拉 `cv_51ac5cfb5a5a`）：
    服务端为 `sec_f57902fc1d2c` 公布的 hash **只有剔除 source_meta 后**才复算得出，
    拿响应里原样的已投影 spec 复算 v1–v6 × 两 locale 共 12 组零命中。
    行为守卫见 `test_export_visual_identity_projection_invariance.py`。

    修法不是「投影后重算」——导出快照渲染的是**未投影**的原文（`export_snapshot.py`
    从 DB section 直接建），投影它等于改导出内容，是安全面的改动。正解是让身份
    **对投影不敏感**：`source_meta` 根本不进画面——导出视觉页
    (`app/(public)/internal/canvas-export/visual/page.tsx`) 只渲 `ChartBlock`，
    来源行 `ChartSourceLine` 不在截图范围内。它是溯源元数据，不是视觉输入。

    于是三处一致：detail 公布的 hash（投影前）== FE 算的（投影后）
    == 导出校验算的（快照原文）。
    """
    if section_kind != "chart":
        return visual_payload
    spec = visual_payload.get("spec")
    if not isinstance(spec, dict) or not any(field in spec for field in _NON_VISUAL_SPEC_FIELDS):
        return visual_payload
    stripped = {key: value for key, value in spec.items() if key not in _NON_VISUAL_SPEC_FIELDS}
    plan = spec.get("presentation_plan")
    if isinstance(plan, dict) and plan.get("scale_groups"):
        # Saved numeric domains render on every surface and survive source projection.
        stripped["_shared_scale_groups"] = plan["scale_groups"]
    if isinstance(plan, dict) and plan.get("guide_steps"):
        stripped["_guide_steps"] = plan["guide_steps"]
    if isinstance(plan, dict) and isinstance(plan.get("analytical_views"), list):
        stripped["_analytical_views"] = [
            {key: value for key, value in item.items() if key != "availability" or value != "available"}
            for item in plan["analytical_views"]
            if isinstance(item, dict)
        ]
    return {
        **visual_payload,
        "spec": stripped,
    }


def visual_typed_data(section_kind: str, payload: dict[str, Any]) -> Any:
    if section_kind == "chart":
        spec = payload.get("spec")
        if not isinstance(spec, dict):
            return None
        if isinstance(spec.get("rows"), list):
            return spec["rows"]
        return spec.get("data")
    if section_kind == "spec_block":
        return {
            "edges": payload.get("edges"),
            "nodes": payload.get("nodes"),
        }
    return None


def _effective_visual_kind(
    section_kind: str,
    payload: dict[str, Any],
) -> str:
    if section_kind == "chart":
        spec = payload.get("spec")
        if isinstance(spec, dict):
            kind = spec.get("kind")
            if isinstance(kind, str) and kind:
                return kind
    if section_kind == "spec_block":
        format_name = payload.get("format")
        if isinstance(format_name, str) and format_name:
            return format_name
    return section_kind


def sha256_canonical(value: Any) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_value(value: Any) -> list[Any]:
    """Return the cross-language visual identity encoding.

    JSON number spelling and object-key collation differ between Python and
    browsers. Tagging every value and encoding numbers as IEEE-754 bytes keeps
    the identity stable without relying on either runtime's JSON formatter.
    """
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("visual identity numbers must be finite")
        if number == 0:
            number = 0.0
        return ["f64", struct.pack(">d", number).hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_value(item) for item in value]]
    if isinstance(value, dict):
        return [
            "object",
            [[str(key), _canonical_value(child)] for key, child in sorted(value.items(), key=lambda item: str(item[0]))],
        ]
    raise TypeError(f"unsupported visual identity value: {type(value).__name__}")


__all__ = [
    "EXPORT_VISUAL_RENDERER_VERSION",
    "VISUAL_FIELDS",
    "VISUAL_SECTION_KINDS",
    "build_visual_input_identity",
    "compute_visual_input_sha256",
    "compute_screen_visual_input_sha256_v2",
    "compute_visual_theme_hash",
    "screen_chart_style_identity_v2",
    "sha256_canonical",
    "visual_typed_data",
]
