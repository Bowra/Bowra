"""导出格式在**多处**各自枚举 —— 这组把它们钉在一起，防止再漏。

为什么需要：canvas-docx 上线过程中，同一个"加一种格式"被拆散在至少 6 处，
每漏一处就是一次真机故障，而且每次都只在 dogfood 才看得见：

1. `bowhead_skill.tools._CANVAS_EXPORT_SKILLS`      —— 漏了 = 服务端不灌 sections
2. `subagent._CANVAS_EXPORT_SKILL_NAMES`            —— 漏了 = 产物不登记（SEC-5 白名单）
3. `subagent._CANVAS_EXPORT_KINDS`                  —— 漏了 = 扩展名→mime 认不出
4. `canvas_export._FMT_SKILL` / `_FMT_MIME`         —— 漏了 = UI 导出路由不到 skill
5. `canvas_export._validate_legacy_output`          —— 漏了 = 产物自检必失败（真机 exp_4e02c4d6896c）
6. `export_copy_contract.CURRENT_OUTPUT_PRODUCERS`  —— 漏了 = "unsupported export copy producer"（真机 exp_a5f21bb74e04）

⇒ 与其在第 7 处再数一遍格式，不如让**任意两处不一致就变红**。
"""

from __future__ import annotations

from bowhead_core.canvas_export import _FMT_MIME, _FMT_SKILL
from bowhead_core.export_copy_contract import _FORMAT_METADATA, CURRENT_OUTPUT_PRODUCERS
from bowhead_core.subagent import _CANVAS_EXPORT_KINDS, _CANVAS_EXPORT_SKILL_NAMES
from bowhead_skill.tools import _CANVAS_EXPORT_SKILLS


def test_skill_whitelists_agree() -> None:
    """SEC-5 白名单（core）与 skill 侧 SSOT 必须一致 —— 不一致=产物登记与灌 sections 走岔。"""
    assert set(_CANVAS_EXPORT_SKILL_NAMES) == set(_CANVAS_EXPORT_SKILLS)


def test_every_export_fmt_has_a_skill_and_mime() -> None:
    assert set(_FMT_SKILL) == set(_FMT_MIME), "fmt→skill 与 fmt→mime 两张表格式集合不一致"


def test_every_export_fmt_maps_to_a_whitelisted_skill() -> None:
    """UI 导出路由到的 skill 必须在 SEC-5 白名单里，否则产物登记不了。"""
    for fmt, skill in _FMT_SKILL.items():
        assert skill in _CANVAS_EXPORT_SKILL_NAMES, f"{fmt} → {skill} 不在登记白名单里"


def test_every_export_fmt_has_a_legacy_output_producer() -> None:
    """★ 真机 exp_a5f21bb74e04 就是漏了这张表：`unsupported export copy producer`。"""
    for fmt in _FMT_SKILL:
        assert ("legacy", fmt) in CURRENT_OUTPUT_PRODUCERS, f"{fmt} 缺 legacy output producer"


def test_every_export_fmt_has_copy_contract_metadata() -> None:
    for fmt in _FMT_SKILL:
        assert fmt in _FORMAT_METADATA, f"{fmt} 缺 export copy 元数据（mime/后缀）"


def test_copy_contract_mime_agrees_with_export_mime() -> None:
    """同一格式的 mime 在两处必须一字不差 —— 不然产物自检/下载 Content-Type 会歪。"""
    for fmt, mime in _FMT_MIME.items():
        assert _FORMAT_METADATA[fmt][0] == mime, f"{fmt} mime 不一致"


def test_every_export_fmt_has_a_deliverable_kind_by_extension() -> None:
    """`_CANVAS_EXPORT_KINDS` 按扩展名索引；每种导出格式都得能被认出来登记。"""
    for fmt in _FMT_SKILL:
        assert f".{fmt}" in _CANVAS_EXPORT_KINDS, f".{fmt} 不在 _CANVAS_EXPORT_KINDS 里"
        assert _CANVAS_EXPORT_KINDS[f".{fmt}"][0] == _FMT_MIME[fmt], f".{fmt} mime 与导出表不一致"


def test_docx_is_wired_everywhere() -> None:
    """把本次真机踩过的六处一次性锁死（回归锚点）。"""
    assert "canvas-docx" in _CANVAS_EXPORT_SKILLS
    assert "canvas-docx" in _CANVAS_EXPORT_SKILL_NAMES
    assert ".docx" in _CANVAS_EXPORT_KINDS
    assert _FMT_SKILL["docx"] == "canvas-docx"
    assert ("legacy", "docx") in CURRENT_OUTPUT_PRODUCERS
    assert "docx" in _FORMAT_METADATA
