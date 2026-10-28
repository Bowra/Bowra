# ⚠️ AUTO-GENERATED — DO NOT EDIT BY HAND ⚠️
#
# Source: packages/bowhead_core/src/bowhead_core/agents/manifests/tool_aliases.yaml
# Regenerate: uv run python -m bowhead_cli.codegen_tool_aliases
# CI check  : uv run python -m bowhead_cli.codegen_tool_aliases --check

"""Legacy alias → canonical mapping for trace replay UI.

Sprint ω §2.13: runtime dispatch is clean-break (LLM uses canonical names
only). This map is read-only — used by /reports trace renderer to translate
historical tool_name="bi_ask_question" → display as "查询数据"."""

from __future__ import annotations

LEGACY_TRACE_NAMES: dict[str, str] = {
    "bi_list_measures": "bi_schema",
    "bi_list_dimensions": "bi_schema",
}
