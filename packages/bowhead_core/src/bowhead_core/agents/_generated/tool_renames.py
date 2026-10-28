# ⚠️ AUTO-GENERATED — DO NOT EDIT BY HAND ⚠️
#
# Source: packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml
# Regenerate: uv run python -m bowhead_cli.codegen_tool_aliases
# CI check  : uv run python -m bowhead_cli.codegen_tool_aliases --check

"""工具改名的读路径规范化表。**运行时权威**。

旧名 → 现名。客户自建 agent 的 system_prompt / tool_refs 里可能永久留着旧名，
我们看不到也改不了 ⇒ 一个字节的客户数据都不动，只在**解析时**归一。
别名是永久的，不是过渡期。规范见 docs/design/工具改名迁移.md。

⚠ 与 legacy_trace_names.LEGACY_TRACE_NAMES 不同：那是双注册旧名的**展示**归一,
把它接进运行时会改变分发目标 —— 两张表**不合并**。"""

from __future__ import annotations

TOOL_RENAMES: dict[str, str] = {
    "bi_ask_question": "bi_query",
    "canvas_add_chart": "add_chart",
    "canvas_add_image": "add_image",
}


def canonical_tool_name(name: str) -> str:
    """旧名 → 现名；未登记的名字**原样返回**（未知工具照旧走各自的报错路）。"""
    return TOOL_RENAMES.get(name, name)
