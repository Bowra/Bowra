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
