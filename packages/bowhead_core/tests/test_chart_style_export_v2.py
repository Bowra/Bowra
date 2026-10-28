"""Trusted chart-style V2 export freeze and identity contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chart_style_export import (
    CHART_EXPORT_SURFACE_CONTRACT_VERSION,
    ChartStyleExportError,
    build_chart_style_export_theme_v2,
    freeze_chart_presentation_v2,
    parse_chart_style_export_theme,
    parse_frozen_chart_presentation_v2,
    require_current_chart_style_v2,
)
from bowhead_core.chart_style_materializer import (
    DARK_STANDARD_SURFACE,
    actual_surface_projection,
)
from bowhead_core.chart_style_registry_generated import (
    BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
)
from bowhead_core.chart_style_v2 import (
    built_in_style_projection,
    chart_style_policy_etag_v2,
)
from bowhead_core.export_ir import normalize_export_snapshot
from bowhead_core.export_snapshot import ExportSnapshotRequest, ExportSnapshotStore
from bowhead_core.export_visual_identity import (
    compute_screen_visual_input_sha256_v2,
)
from bowhead_core.print_theme import PRINT_PAPER, current_print_theme_snapshot
from bowhead_core.session_store import SessionStore


def _effective_v2() -> dict:
    print_theme = current_print_theme_snapshot()
    style = built_in_style_projection("bowhead-editorial")
    snapshot = style["resolvedPresetSnapshot"]
    return {
        "schemaVersion": 2,
        "registryVersion": 0,
        "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
        "style": style,
        "selection": {
            "source": "product",
            "savedStyleId": None,
            "unavailableReason": None,
        },
        "accessibility": {
            "contrast": "standard",
            "colorVision": "standard",
            "source": {
                "contrast": "product",
                "colorVision": "product",
            },
        },
        "modeColors": {
            "light": actual_surface_projection(
                snapshot,
                mode="light",
                surface=PRINT_PAPER,
                color_scheme_checksum=print_theme["checksum"],
            ),
            "dark": actual_surface_projection(
                snapshot,
                mode="dark",
                surface=DARK_STANDARD_SURFACE,
                color_scheme_checksum=print_theme["checksum"],
            ),
        },
        "teamPolicy": {"etag": chart_style_policy_etag_v2(None)},
        "resolutionAdjustments": [],
    }


def _freshness(effective: dict) -> dict:
    style = effective["style"]
    return {
        "kind": "chart-style-freshness",
        "schemaVersion": 2,
        "styleId": style["id"],
        "revision": style["revision"],
        "snapshotChecksum": style["snapshotChecksum"],
        "teamPolicyEtag": effective["teamPolicy"]["etag"],
        "registryVersion": effective["registryVersion"],
        "builtInRegistryChecksum": effective["builtInRegistryChecksum"],
    }


def _spec() -> dict:
    return {
        "kind": "line",
        "title": "Revenue trend",
        "row_count": 3,
        "columns": [
            {"key": "month", "header": "Month", "type": "date"},
            {"key": "revenue", "header": "Revenue", "type": "numeric"},
        ],
        "rows": [
            {"month": "Jan", "revenue": 100},
            {"month": "Feb", "revenue": 120},
            {"month": "Mar", "revenue": 160},
        ],
    }


def test_v2_theme_is_server_built_and_freshness_is_exact() -> None:
    effective = _effective_v2()
    print_theme = current_print_theme_snapshot()
    freshness = _freshness(effective)

    require_current_chart_style_v2(freshness, effective)
    theme = build_chart_style_export_theme_v2(
        effective,
        print_theme=print_theme,
    )

    assert parse_chart_style_export_theme(theme) == theme
    assert theme["modeColors"] == {"light": effective["modeColors"]["light"]}
    assert theme["structuralThemeChecksum"] == print_theme["checksum"]
    assert theme["rendererContractVersion"] == "2"
    assert "teamPolicy" not in theme

    stale = {**freshness, "registryVersion": 1}
    with pytest.raises(ChartStyleExportError, match="freshness is stale"):
        require_current_chart_style_v2(stale, effective)


def test_v2_custom_theme_preserves_frozen_base_provenance() -> None:
    effective = _effective_v2()
    built_in = effective["style"]
    effective["style"] = {
        **built_in,
        "kind": "teamCustom",
        "id": "tcs_0123456789abcdef01234567",
        "revision": "tcr_0123456789abcdef01234567",
        "basePreset": {
            "id": built_in["id"],
            "revision": built_in["revision"],
            "checksum": built_in["snapshotChecksum"],
        },
    }

    theme = build_chart_style_export_theme_v2(
        effective,
        print_theme=current_print_theme_snapshot(),
    )

    assert theme["style"]["basePreset"] == effective["style"]["basePreset"]

    missing_base = json.loads(json.dumps(theme))
    missing_base["style"].pop("basePreset")
    with pytest.raises(ChartStyleExportError, match="base preset is invalid"):
        parse_chart_style_export_theme(missing_base)

    built_in_with_base = build_chart_style_export_theme_v2(
        _effective_v2(),
        print_theme=current_print_theme_snapshot(),
    )
    built_in_with_base["style"]["basePreset"] = theme["style"]["basePreset"]
    with pytest.raises(ChartStyleExportError, match="base preset is invalid"):
        parse_chart_style_export_theme(built_in_with_base)


def test_frozen_presentation_owns_static_context_and_rejects_tampering() -> None:
    theme = build_chart_style_export_theme_v2(
        _effective_v2(),
        print_theme=current_print_theme_snapshot(),
    )
    frozen = freeze_chart_presentation_v2(
        _spec(),
        theme=theme,
        locale="zh-CN",
    )

    assert frozen["renderContext"] == {
        "placement": "embed",
        "bounds": {
            "widthPx": 1456,
            "heightPx": 772,
            "devicePixelRatio": 1,
        },
        "input": {"hover": False, "pointer": "none", "keyboard": False},
        "output": {"medium": "raster", "static": True},
        "mode": "light",
        "locale": "zh-CN",
        "reducedMotion": True,
        "forcedColors": False,
        "exactDataAlternative": {"kind": "none"},
    }
    assert frozen["requested"]["tooltip"] == "enabled"
    assert frozen["effective"]["tooltip"] == "disabled"
    assert any("context.tooltip_not_applicable_static" in adjustment["reasonCodes"] for adjustment in frozen["adjustments"])

    tampered = json.loads(json.dumps(frozen))
    tampered["effective"]["tooltip"] = "enabled"
    with pytest.raises(ChartStyleExportError, match="presentation is invalid"):
        parse_frozen_chart_presentation_v2(tampered, locale="zh-CN")


async def test_snapshot_freezes_chart_and_export_identity_is_not_screen_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chart-export.db"
    sessions = SessionStore(database)
    canvases = CanvasStore(database)
    snapshots = ExportSnapshotStore(database)
    await sessions.initialize()
    await canvases.initialize()
    await snapshots.initialize()
    await sessions.ensure_session(
        session_id="s_chart_export",
        workspace_id="ws_chart_export",
        user_id="u_owner",
    )
    canvas = await canvases.ensure_canvas(
        session_id="s_chart_export",
        workspace_id="ws_chart_export",
        user_id="u_owner",
        title="Trusted chart export",
    )
    section = await canvases.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": _spec()},
    )
    effective = _effective_v2()
    theme = build_chart_style_export_theme_v2(
        effective,
        print_theme=current_print_theme_snapshot(),
    )
    candidate = await snapshots.build_candidate(
        canvas_id=canvas.id,
        request=ExportSnapshotRequest(
            requested_by="u_owner",
            team_id="team_chart_export",
            fmt="pdf",
            profile="full_report",
            parity_contract="full_content_v1",
            locale="zh-CN",
            timezone="Asia/Singapore",
            client_surface_receipt_sha256=f"sha256:{'a' * 64}",
            theme=theme,
            options={},
            print_theme=current_print_theme_snapshot(),
        ),
    )
    payload = json.loads(candidate.payload_json)
    frozen = payload["sections"][0]["frozen_chart_presentation"]
    assert frozen["normalizedRenderShapeChecksum"].startswith("sha256:")

    saved = await snapshots.commit_candidate(
        candidate,
        expected_projection_token=candidate.content_projection.projection_token,
        expected_surface_token=candidate.surface_projection.surface_token,
    )
    export_ir = normalize_export_snapshot(saved)
    export_hash = export_ir.blocks[0].visual_input_sha256
    screen_hash = compute_screen_visual_input_sha256_v2(
        section_kind="chart",
        payload={"spec": _spec()},
        locale="zh-CN",
        surface_token=candidate.surface_projection.surface_token,
        effective_chart_style=effective,
    )
    assert export_hash is not None
    assert screen_hash is not None
    assert export_hash != screen_hash
    assert export_ir.blocks[0].content["frozen_chart_presentation"] == frozen

    identity = export_ir.blocks[0].visual_input_sha256
    assert CHART_EXPORT_SURFACE_CONTRACT_VERSION == "canvas-export-visual/2"
    await canvases.patch_section(
        section_id=section.id,
        patch={"spec": {**_spec(), "title": "Changed after freeze"}},
        expected_rev=section.rev,
    )
    assert normalize_export_snapshot(saved).blocks[0].visual_input_sha256 == identity


def test_no_chart_infer_emits_can_fail_the_export_freeze() -> None:
    """★ 不变量：`infer_chart_spec` 发出的任何图，导出时都冻结得了样式。

    冻结样式超过 64KB 会让**整份** PDF/PPTX/XLSX 导出失败。以前 spec 只带 50 行，系列数被顺带
    压住；放开之后靠 `_MAX_DRAWN_SERIES` 挡。这里用真冻结函数跑 review 逮到的形状——包括
    「柱状族按列序取 x 轴」那一类：`(门店, 日期, 金额)` 摊开是每天一条系列，第一版的闸数成了 5 条。
    """
    from bowhead_core.chart import ChartColumn, infer_chart_spec, spec_to_dict
    from bowhead_core.chart_style_export import freeze_chart_presentation_v2
    from bowhead_core.print_theme import current_print_theme_snapshot

    theme = build_chart_style_export_theme_v2(_effective_v2(), print_theme=current_print_theme_snapshot())

    def cols(*defs: tuple[str, str]) -> list[ChartColumn]:
        return [ChartColumn(key=k, header=k, type=t) for k, t in defs]  # type: ignore[arg-type]

    store_first = [{"门店": f"S{k}", "日期": f"D{d:03d}", "金额": float(d * 7 + k)} for k in range(5) for d in range(300)]
    sku_daily = [{"日期": f"D{d:03d}", "SKU": f"SKU{k}", "销量": float(d + k)} for d in range(50) for k in range(200)]
    sparse = [{"月": "2025年1月" if i < 100 else "2025年2月", "SKU": f"SKU{i}", "销量": float(i)} for i in range(200)]
    cases = {
        **{
            f"force {kind} (门店, 日期, 金额) 5×300": infer_chart_spec(
                [dict(r) for r in store_first],
                cols(("门店", "categorical"), ("日期", "date"), ("金额", "numeric")),
                hint="raw",
                title="t",
                force_kind=kind,
            )
            for kind in ("bar", "stacked_bar", "hbar")
        },
        "force line (日期, SKU, 销量) 50×200": infer_chart_spec(
            sku_daily, cols(("日期", "date"), ("SKU", "categorical"), ("销量", "numeric")), hint="trend", title="t", force_kind="line"
        ),
        # Stacked-bar structure records stacking boundaries by category, independent of series count (review round 3: freezing failed from ~7000 categories,
        # depending on name length; use 10000 here. Freezing failed without the structural-byte gate, verified as a positive control under mutation).
        "force stacked_bar 10000 个日期 × 1 条": infer_chart_spec(
            [{"日期": f"D{i:05d}", "金额": float(i)} for i in range(10_000)],
            cols(("日期", "date"), ("金额", "numeric")),
            hint="raw",
            title="t",
            force_kind="stacked_bar",
        ),
        "auto 稀疏长表 2 月 × 100 SKU": infer_chart_spec(
            sparse, cols(("月", "date"), ("SKU", "categorical"), ("销量", "numeric")), hint="trend", title="t"
        ),
    }
    failed = {}
    for name, spec in cases.items():
        if spec.kind in {"table", "kpi", "geo"}:
            continue  # These three kinds bypass Chart.js and have no frozen styles.
        try:
            freeze_chart_presentation_v2(spec_to_dict(spec), theme=theme, locale="zh-CN")
        except Exception as exc:  # noqa: BLE001 - This assertion specifically requires no exception.
            failed[name] = f"kind={spec.kind} rows={len(spec.rows or [])}: {exc}"
    assert not failed, failed
