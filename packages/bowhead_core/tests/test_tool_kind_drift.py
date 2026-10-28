"""G9（收窄版）— 后端 _TOOL_KIND_MAP 与 tool 名宇宙的漂移守卫.

docs/skills-tools-ssot.md §5-G9：tool 展示/kind 元数据多源（codegen + 后端手写
kind map + FE 双代映射）。全量 codegen 收敛因 kind 语义分叉（finding-emit 语义
vs FE 展示语义，见 bi_fetch_rows 注释）列为 follow-up；本守卫先钉住两个不变式：

  1. kind map 不许再积累死键（曾有 bi_profile/kb_read/canvas_add_narrative）。
  2. 新注册的真实 tool 必须显式选 kind，或有意识地列入 default-compute 豁免
     （kind 决定 tool_finding 是否 LLM emit——静默落 default 是行为，不是缺省）。
"""

from __future__ import annotations

from bowhead_core.agents._generated.legacy_trace_names import LEGACY_TRACE_NAMES
from bowhead_core.agents._generated.tool_names import (
    ALL_TOOL_NAMES,
    PSEUDO_TOOL_NAMES,
)
from bowhead_core.tool_category import _TOOL_KIND_MAP

# Actual tools intentionally retaining default("compute"): whether canvas writers and image_generate
# should stop emitting findings (become writes) is a separate product/behavior decision (SSOT §5-G9 note),
# never silently changed in a hygiene batch. New tools using the default must be explicitly added here.
_ALLOWED_DEFAULT_KIND: frozenset[str] = frozenset(
    {
        "add_image",
        "canvas_add_spec_block",
        "canvas_create_design",
        "canvas_flatten_design",
        "canvas_update_design_text",
        "image_generate",
        # Memory tools joined tool_aliases.yaml for display names only; their finding
        # behavior (default kind) is unchanged by that move.
        "recall",
        "remember",
        # Display metadata registration preserves the existing document submission finding policy.
        "template_document_submit",
    }
)


def test_kind_map_has_no_dead_keys() -> None:
    known = set(ALL_TOOL_NAMES) | set(PSEUDO_TOOL_NAMES) | set(LEGACY_TRACE_NAMES)
    dead = sorted(set(_TOOL_KIND_MAP) - known)
    assert dead == [], f"kind map 死键（工具已改名/删除）: {dead}"


def test_real_tools_have_explicit_kind_or_allowed_default() -> None:
    real = set(ALL_TOOL_NAMES) - set(PSEUDO_TOOL_NAMES)
    unmapped = sorted(real - set(_TOOL_KIND_MAP) - _ALLOWED_DEFAULT_KIND)
    assert unmapped == [], (
        f"新 tool 未定 kind（会静默落 default compute=finding-eligible）: {unmapped} — "
        "在 tool_category._TOOL_KIND_MAP 显式选 kind，或有意识加入本测试豁免集"
    )
