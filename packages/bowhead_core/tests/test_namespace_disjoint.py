"""G5 — tool 名 ∩ skill 名结构性不相交（docs/skills-tools-ssot.md §5-G5）.

ω 事故根因之一：python_sandbox 同时是 skill 名与（幻影）tool 名，诱导 LLM
直发同名 tool call。防线三层：
  1. 静态：内置 catalog ∩ (ALL_TOOL_NAMES − PSEUDO) = ∅（本文件）
  2. boot：gateway 注册链收尾 fail-fast（main.py，内置撞名即炸）
  3. 增量：user skill 的 reserved floor 含全部 tool 名（skill_upload）
"""

from __future__ import annotations

from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP
from bowhead_core.agents._generated.tool_names import (
    ALL_TOOL_NAMES,
    PSEUDO_TOOL_NAMES,
)
from bowhead_core.skill_upload import _reserved_skill_names

from bowhead_skill.catalog import SkillCatalog, builtin_catalog_dir


def _builtin_skill_names() -> set[str]:
    cat = SkillCatalog(roots=[builtin_catalog_dir()])
    cat.refresh()
    return set(cat.list_names())


def test_builtin_catalog_disjoint_from_real_tool_names() -> None:
    real_tools = ALL_TOOL_NAMES - PSEUDO_TOOL_NAMES
    collisions = _builtin_skill_names() & real_tools
    assert collisions == set(), (
        f"tool/skill namespace collision: {sorted(collisions)} — 改名一侧或（仅 display 用途时）在 tool_aliases.yaml 标 pseudo: true"
    )


def test_pseudo_set_is_exactly_python_sandbox() -> None:
    """pseudo 豁免集当前只有 python_sandbox（skill 经 skill_invoke 执行，
    仅 trace 显示需要 display 映射）。扩集合须过 SSOT §5-G5 判据。"""
    assert frozenset({"python_sandbox"}) == PSEUDO_TOOL_NAMES


def test_pseudo_tools_have_no_role_binding() -> None:
    """幻影 binding 已清：pseudo 条目不产 role binding（原恒 inert 且误导审计）。"""
    for name in PSEUDO_TOOL_NAMES:
        assert name not in _PARADIGM_V2_ROLE_MAP


def test_reserved_floor_blocks_tool_names() -> None:
    """user skill 不可再撞任何 tool 名（含 pseudo）——floor 已并入 ALL_TOOL_NAMES。"""
    _reserved_skill_names.cache_clear()
    floor = _reserved_skill_names()
    assert "bi_query" in floor
    assert "subagent_spawn" in floor
    assert "python_sandbox" in floor  # Covers both built-in skill names ∪ pseudo-tool names.
