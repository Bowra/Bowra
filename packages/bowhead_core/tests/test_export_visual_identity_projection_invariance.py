"""R8：视觉身份必须**对观看者投影不敏感**——否则 V2 导出在生产上恒降级 legacy。

## 这条闸拦的是什么

`visualInputSha256` 在 `GET /api/v1/canvas/{id}` 里是拿**投影前**的 `section.payload`
算的（`routes/canvas.py` 的 `_canvas_detail`），而同一个响应的 `sections` 随后被
`_project_canvas_detail` → `project_sources_for_viewer` 改写：chart 的
`spec.source_meta` 会换成 viewer-safe 的 `source-ref-v1:` 别名对象。

于是 FE 拿投影后的 section 复算，和响应里公布的 hash **永远对不上** →
`validateAndBuildRenderedSurfaceReceipt` 判 `surface_projection_conflict` →
`prepareForExport` 返回 !ok → 前端静默走 legacy 导出。

2026-08-14 在 116 (v1.1.0，已含本修) 用**产品自己的链路**取证：浏览器按 app 同款
`Accept-Language: en-US` 拉 `cv_51ac5cfb5a5a`，服务端为 `sec_f57902fc1d2c` 公布的
`visualInputSha256` **只有剔除 source_meta 后**才复算得出；拿响应里原样的
（已投影）spec 复算，v1–v6 × 两种 locale 共 12 种组合**零命中**。
⇒ 身份一旦含 `source_meta`，服务端公布的 hash 就对不上它自己公布的 section。

## ⚠ 别拿「FE 发出的 receipt == 公布的 hash」当反证

`validateAndBuildRenderedSurfaceReceipt` (`canvas-export-prepare.ts`) **只在两者相等时
才产出 receipt**，不等时直接返回 `projectionFailure`。所以任何**发得出去**的 receipt
都必然与公布值逐字相同——那是恒真式，不是测量。2026-08-14 我据此"证伪"过本条守卫，
差点把它连同修复一起删掉（见 [[feedback_reconstructed_input_is_not_evidence]]：
要证伪一条判据，取证入口必须在**判据上游**，不能取它筛完的幸存者）。

## 为什么老闸看不见

`test_export_visual_identity_parity.py` 只用正则从 FE 源码抠**常量**（版本号 +
字段名集合）对拍，**从不真算一次 hash**。双端实现确实一致，所以它永远绿；
真正断掉的是「服务端自己公布的 hash vs 自己公布的 section」。
见 [[feedback_gate_credibility]]「扫描≠可达」。

## 修法为什么不是「投影后重算」

导出快照 (`export_snapshot.py`) 是从 DB section **原文**建的，导出校验
(`_validate_candidate_visual_receipts`) 也拿原文算——投影它等于改导出内容，
是安全面的改动。而 `source_meta` **根本不进画面**：导出视觉页
`app/(public)/internal/canvas-export/visual/page.tsx` 只渲 `ChartBlock`，
来源行 `ChartSourceLine` 不在截图范围内。所以正解是把溯源字段挡在身份之外，
让三处（detail 公布 / FE 复算 / 导出校验）自动一致。
"""

from __future__ import annotations

import copy

from bowhead_core.export_visual_identity import compute_visual_input_sha256
from bowhead_core.source_access_projection import (
    ViewerSourceAliasMap,
    project_sources_for_viewer,
)

_THEME = {"kind": "default"}


def _chart_section() -> dict[str, object]:
    """116 `cv_55d680eda3f7` 上真实的形状：chart spec 带 source_meta。"""
    return {
        "id": "sec_chart",
        "kind": "chart",
        "rev": 1,
        "spec": {
            "kind": "bar",
            "title": "头部品牌退款率对比",
            "columns": [
                {"key": "brand", "header": "品牌", "type": "categorical"},
                {"key": "rate", "header": "退款率", "type": "numeric"},
            ],
            "rows": [{"brand": "老庙", "rate": 0.292}],
            "row_count": 1,
            "source_meta": {"id": "art_1", "query": "secret", "ordinal": 3},
        },
        "data_source": {"context_id": "private", "context_name": "Prod", "tool": "datarc"},
    }


def _project(section: dict[str, object]) -> dict[str, object]:
    aliases = ViewerSourceAliasMap.from_root_secret("root-secret", workspace_id="ws_1", report_scope_id="ses_1")
    projected, _ = project_sources_for_viewer(
        [copy.deepcopy(section)],
        [],
        alias_map=aliases,
        access_by_identity={"art_1": True},
    )
    return projected[0]


def _identity(section: dict[str, object]) -> str | None:
    return compute_visual_input_sha256(section_kind="chart", payload=section, locale="zh-CN", theme=_THEME)


def test_viewer_projection_really_rewrites_source_meta() -> None:
    """负对照：投影确实改了 spec.source_meta。

    没有这一条，下面那条「投影前后 hash 相同」可能只是因为投影根本没生效
    ——那样它就是一条死守卫。
    """
    raw = _chart_section()
    projected = _project(raw)
    raw_meta = raw["spec"]["source_meta"]  # type: ignore[index]
    projected_meta = projected["spec"]["source_meta"]  # type: ignore[index]
    assert projected_meta != raw_meta, "投影没改 source_meta——本文件的守卫全部失效"
    # The key rewrite **aliases identity**: withhold the actual artifact ID, replacing it with a scope-local
    # signed `source-ref-v1:` alias. (Accessible sources still retain label/query:
    # this is intentional, not evidence that projection failed.)
    assert str(projected_meta.get("id")).startswith("source-ref-v1:")  # type: ignore[union-attr]
    assert projected_meta.get("id") != "art_1"  # type: ignore[union-attr]


def test_visual_identity_survives_viewer_projection() -> None:
    """★核心：同一个 section 投影前后必须算出**同一个** visual identity。

    这条一红 = V2 导出在生产上全线降级 legacy（图变浮点数表），
    且前端只会弹一句「兼容模式」info toast，没人会注意到。
    """
    raw = _chart_section()
    projected = _project(raw)
    assert _identity(raw) is not None
    assert _identity(projected) == _identity(raw), (
        "投影前后的视觉身份不一致 ⇒ canvas detail 公布的 hash 与它自己公布的 "
        "section 对不上 ⇒ 所有 exact_visual receipt 失配 ⇒ V2 导出恒降级 legacy。"
        "溯源字段不该进视觉身份（见本文件 docstring）。"
    )


def test_identity_still_tracks_everything_that_does_reach_the_pixels() -> None:
    """反向：别为了让上面那条绿就把身份做成常量。

    真正进画面的东西改了，身份**必须**变——否则旧的 cached receipt 会被复用，
    渲出来的是上一版的图。
    """
    base = _chart_section()
    baseline = _identity(base)

    changed_rows = _chart_section()
    changed_rows["spec"]["rows"] = [{"brand": "老庙", "rate": 0.4}]  # type: ignore[index]
    assert _identity(changed_rows) != baseline, "行数据变了，身份必须变"

    changed_kind = _chart_section()
    changed_kind["spec"]["kind"] = "line"  # type: ignore[index]
    assert _identity(changed_kind) != baseline, "图种变了，身份必须变"

    changed_title = _chart_section()
    changed_title["spec"]["title"] = "别的标题"  # type: ignore[index]
    assert _identity(changed_title) != baseline, "标题进画面，身份必须变"

    changed_cols = _chart_section()
    changed_cols["spec"]["columns"][1]["header"] = "退款率（口径二）"  # type: ignore[index]
    assert _identity(changed_cols) != baseline, "列头进轴标题，身份必须变"


def test_data_source_projection_also_leaves_identity_alone() -> None:
    """`data_source` 同样是每观看者一份，且同样不进画面。"""
    raw = _chart_section()
    projected = _project(raw)
    assert projected["data_source"] != raw["data_source"], "投影没改 data_source"
    assert _identity(projected) == _identity(raw)
