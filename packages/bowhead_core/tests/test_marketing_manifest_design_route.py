"""Marketing analyst prompt/manifest prefers editable design for copy-heavy assets."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import yaml


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_marketing_manifest_allows_design_tools_and_no_old_canvas_ban() -> None:
    path = _root() / "packages/bowhead_core/src/bowhead_core/agents/manifests/system/marketing-analyst.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    allow = set(manifest["tools"]["allow"])
    description = str(manifest["description"])

    assert "canvas_create_design" in allow
    assert "canvas_update_design_text" in allow
    assert "可编辑设计稿" in description
    assert "不写 canvas section" not in description
    assert "文生图直出含中文文案" not in description

    # multimodal-V2A follow-up fix: the Lead routes purely by the FIRST 200 chars
    # of this description (peer_block._MAX_SYSTEM_DESC_CHARS, after NFKC + whitespace
    # collapse). All routing-critical signals must land inside that window:
    #   - "edit/download an EXISTING poster" → so the Lead routes follow-up edits
    #     here instead of refusing them as a non-editable raster;
    #   - the "generic analysis is not mine" boundary → so it does NOT over-route
    #     generic funnel/cohort analysis to itself.
    #
    # WP M3-12 batch 2: this boundary formerly named another peer literally: "use business-analyst; do not take its work".
    # That name may be absent from the roster if disabled by the team, while the description is the roster row's
    # default copy, copied verbatim into Lead prompts, creating a dangling reference. Use unnamed "outside my scope" wording;
    # **preserve every boundary condition** (Lead still does not dispatch general analysis here), without claiming work for another peer.
    head = " ".join(unicodedata.normalize("NFKC", description).split())[:200]
    assert "改已有海报文字" in head
    assert "下载/导出已有海报" in head
    assert "通用客群/漏斗数据分析不归我" in head
    # ★ Reverse gate: do not express the boundary by naming other peers (the pattern this WP eliminates).
    for peer_name in ("business-analyst", "compute", "consultant", "kb-analyst", "canvas-writer"):
        assert peer_name not in description, f"description 里不该点名 {peer_name}"


def test_marketing_prompt_routes_copy_heavy_assets_to_design() -> None:
    path = _root() / "packages/bowhead_core/src/bowhead_core/agents/prompts/system/marketing-analyst.md"
    prompt = path.read_text(encoding="utf-8")

    assert 'purpose="design_background"' in prompt
    assert "canvas_create_design" in prompt
    assert "canvas_update_design_text" in prompt
    assert "不要把用户必要文案原文放进背景 prompt" in prompt
    assert "你不写 canvas section" not in prompt
    assert "图内文字逐行用引号逐字给出" not in prompt
