"""V2 导出视觉身份的双端对拍（report-edit-audit dogfood 追加守卫）.

`visual_input_sha256` 由 FE (`lib/canvas-visual-identity.ts`) 与服务端
(`export_visual_identity.py`) 各自独立实现——receipt 成立的前提是两边对同一
section 算出同一 sha。任何一边单独 bump `RENDERER_VERSION` 或改 `VISUAL_FIELDS`
都会让全部 exact_visual entry 失配 → prepare 恒失败 → V2 **静默**降级 legacy
（用户只看到一条"兼容模式" info toast）。与 test_canvas_rank_parity 同款打法：
从 FE 源码把常量抠出来逐项比对，分叉即红。
"""

from __future__ import annotations

import re
from pathlib import Path

from bowhead_core.export_visual_identity import (
    _NON_VISUAL_SPEC_FIELDS,
    EXPORT_VISUAL_RENDERER_VERSION,
    VISUAL_FIELDS,
)
from bowhead_core.print_theme import PRINT_PAPER, PRINT_THEME_SNAPSHOT, PRINT_THEME_TOKEN_FIELDS

_FE_VISUAL_IDENTITY = Path(__file__).resolve().parents[2] / "bowhead_frontend" / "lib" / "canvas-visual-identity.ts"
_FE_PRINT_THEME = Path(__file__).resolve().parents[2] / "bowhead_frontend" / "lib" / "print-theme.ts"


def test_renderer_version_matches_frontend() -> None:
    src = _FE_VISUAL_IDENTITY.read_text(encoding="utf-8")
    m = re.search(r'export const CANVAS_VISUAL_RENDERER_VERSION = "([^"]+)"', src)
    assert m, "FE CANVAS_VISUAL_RENDERER_VERSION 没找到——文件挪了就更新本测试的路径"
    assert m.group(1) == EXPORT_VISUAL_RENDERER_VERSION, (
        f"渲染器版本分叉: FE={m.group(1)!r} server={EXPORT_VISUAL_RENDERER_VERSION!r} —— "
        "两边必须同一次变更一起 bump，否则所有 exact_visual receipt 失配、V2 导出静默全降级"
    )


def test_print_theme_checksum_matches_frontend() -> None:
    src = _FE_PRINT_THEME.read_text(encoding="utf-8")
    match = re.search(r'checksum: "(sha256:[0-9a-f]{64})"', src)
    assert match, "FE print-theme checksum 没找到——源码形态变了就更新本测试的解析器"
    assert match.group(1) == PRINT_THEME_SNAPSHOT["checksum"]


def test_print_theme_recipe_and_tokens_match_frontend() -> None:
    # The export page rejects any snapshot whose recipe or tokens differ from its own
    # constant, so a matching checksum alone is not enough: a stale FE token would fail
    # every visual export even though the checksums agree.
    src = _FE_PRINT_THEME.read_text(encoding="utf-8")
    recipe = re.search(r'export const PRINT_THEME_RECIPE_VERSION = "([^"]+)"', src)
    assert recipe, "FE PRINT_THEME_RECIPE_VERSION not found; update this parser if the source moved"
    assert recipe.group(1) == PRINT_THEME_SNAPSHOT["recipeVersion"]
    body = src.split("export const PRINT_THEME_SNAPSHOT", 1)[1].split("tokens: {", 1)[1].split("}", 1)[0]
    fe_tokens = dict(re.findall(r'(\w+): "(#[0-9A-Fa-f]{6})"', body))
    assert fe_tokens == PRINT_THEME_SNAPSHOT["tokens"]
    keys_body = src.split("export const PRINT_THEME_TOKEN_KEYS = [", 1)[1].split("]", 1)[0]
    assert tuple(re.findall(r'"(\w+)"', keys_body)) == PRINT_THEME_TOKEN_FIELDS
    paper = re.search(r'export const PRINT_PAPER = "(#[0-9A-F]{6})"', src)
    assert paper, "FE PRINT_PAPER not found; update this parser if the source moved"
    assert paper.group(1) == PRINT_PAPER


def _fe_visual_fields() -> dict[str, set[str]]:
    src = _FE_VISUAL_IDENTITY.read_text(encoding="utf-8")
    body = src.split("const VISUAL_FIELDS", 1)[1].split("};", 1)[0]
    table: dict[str, set[str]] = {}
    for kind, fields_src in re.findall(r"(\w+):\s*\[([^\]]*)\]", body, flags=re.DOTALL):
        table[kind] = set(re.findall(r'"([^"]+)"', fields_src))
    assert table, "FE VISUAL_FIELDS 解析为空——源码形态变了就更新解析器"
    return table


def test_visual_fields_match_frontend() -> None:
    fe = _fe_visual_fields()
    server = {kind: set(fields) for kind, fields in VISUAL_FIELDS.items()}
    assert fe == server, (
        "VISUAL_FIELDS 分叉（字段集进 sha，任何差异=receipt 失配）: "
        f"仅 FE 有={ {k: v for k, v in fe.items() if server.get(k) != v} } "
        f"仅 server 有={ {k: v for k, v in server.items() if fe.get(k) != v} }"
    )


def test_non_visual_spec_fields_match_frontend() -> None:
    """R8：「哪些 spec 字段不进视觉身份」两边必须一模一样。

    这张表决定身份对 viewer 投影敏不敏感。任一边单独改 → 全部 exact_visual
    receipt 失配 → V2 导出静默全降级 legacy（2026-08-14 116 prod 实测过一次）。
    真正的行为守卫在 `test_export_visual_identity_projection_invariance.py`；
    这里只挡「双端词表分叉」。
    """
    src = _FE_VISUAL_IDENTITY.read_text(encoding="utf-8")
    body = src.split("const NON_VISUAL_SPEC_FIELDS", 1)[1].split("]", 1)[0]
    fe = set(re.findall(r'"([^"]+)"', body))
    assert fe, "FE NON_VISUAL_SPEC_FIELDS 解析为空——源码形态变了就更新解析器"
    assert fe == set(_NON_VISUAL_SPEC_FIELDS), f"NON_VISUAL_SPEC_FIELDS 分叉: FE={sorted(fe)} server={sorted(_NON_VISUAL_SPEC_FIELDS)}"
