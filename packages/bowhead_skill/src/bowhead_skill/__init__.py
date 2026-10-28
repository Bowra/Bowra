"""Bowhead Skill 子系统.

M2.2 已实现：
    - :class:`SkillSpec`       — SKILL.md 解析后的数据
    - :func:`load_skill`       — 读单个 skill 目录
    - :class:`SkillCatalog`    — 多 root 扫盘 + 按 name 查
    - :class:`SkillRuntime`    — 子进程沙箱跑 skill scripts

历史注：M2.3e 的 SkillDistiller（观察 → 建议）已删除——写侧从未接线，
产品方向由 workflow-solidify 的蒸馏管线取代（见 docs/skills-tools-ssot.md §5-G8）.
"""

from bowhead_skill.catalog import (
    SkillCatalog,
    builtin_catalog_dir,
    builtin_skill_names,
    default_catalog,
    project_catalog_dir,
    user_catalog_dir,
)
from bowhead_skill.index import render_index, substitute_args
from bowhead_skill.loader import SkillSpec, load_skill
from bowhead_skill.runtime import RunLimits, RunResult, SkillRuntime
from bowhead_skill.script_langs import (
    all_entry_extensions,
    entry_extensions,
    guaranteed_cli,
    guaranteed_node_packages,
    guaranteed_runtimes,
    interpreter_for,
    multiruntime_enabled,
)

__all__ = [
    "RunLimits",
    "RunResult",
    "SkillCatalog",
    "SkillRuntime",
    "SkillSpec",
    "all_entry_extensions",
    "builtin_catalog_dir",
    "builtin_skill_names",
    "default_catalog",
    "entry_extensions",
    "guaranteed_cli",
    "guaranteed_node_packages",
    "guaranteed_runtimes",
    "interpreter_for",
    "load_skill",
    "multiruntime_enabled",
    "project_catalog_dir",
    "render_index",
    "substitute_args",
    "user_catalog_dir",
]

__version__ = "0.2.0"
