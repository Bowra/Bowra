"""Backend tool → kind taxonomy (Phase A · sprint chat-v3).

Backend-only taxonomy keyed by **underscore** tool names (post-omicron-12 rename).
The frontend has no mirror of this map.

8 kinds per V6 PRD §3.2:
    lookup / fetch / query / compute / chart / attribute / spawn / write

Used by Phase A `tool_finding.should_skip_for_category` — only ``query`` and
``compute`` get LLM finding emit; others skip (read tools 自身是文本, write
tools 没数字 result 可总结).
"""

from __future__ import annotations

from typing import Any, Final

from bowhead_core.agents._generated.tool_renames import canonical_tool_name

#: Tool name (underscore) → kind.
_TOOL_KIND_MAP: Final[dict[str, str]] = {
    # BI query tools (read-only)
    "bi_query": "query",
    # chat-truth-hotfix-2 (2026-05-15): bi_schema was missing → fell to default
    # "compute" → LLM-eligible → schedule_finding gated on preview_payload but
    # build_preview_for_tool has no bi_schema branch → no finding emit → "—".
    # Map to "lookup" so static_finding template kicks in instead.
    "bi_schema": "lookup",
    # bi-workspace-list review c2: same lesson as bi_schema; unregistered falls back to "compute" →
    # finding-eligible without a preview branch → frontend finding slot renders "—". Read-only metadata
    # lookup, same shape as bi_schema.
    "tool_list_workspaces": "lookup",
    # bi-workspace-tour: register switch from the same family (previous "compute" fallback had the same flaw). Semantically
    # a context action rather than a query, but taxonomy lacks context; use lookup to avoid LLM findings,
    # following the static template like the list tool.
    "tool_switch_context": "lookup",
    "bi_list_measures": "lookup",
    "bi_list_dimensions": "lookup",
    # 2026-09-05 (tool-inventory): remove 10 omicron-sunset BI tool keys along with the retired tools
    # （bi_fetch_rows / bi_attribute / bi_breakdown / bi_get_card / bi_get_cards /
    #  bi_explain / bi_plan_query / bi_fetch_account / bi_call_llm_async /
    # bi_get_llm_result). G9 test_kind_map_has_no_dead_keys guards against their return.
    # BI write (bi_create/update/delete_measures + bi_update_dimensions) was removed as an entire family
    # (2026-07-21, datarc has no write API; see removal notes in tool_registry.py).
    # Skill
    "skill_list": "lookup",
    "skill_read": "lookup",
    "skill_invoke": "compute",
    "skill_save_generated": "write",
    "bi_concept_resolve": "lookup",
    # Knowledge base
    "kb_search": "query",
    "kb_list_tables": "lookup",
    "kb_get_record": "fetch",
    "kb_query_records": "query",
    # Reference Office reads (5ccb0efd9): returns bounded upload text, like kb_get_record; no LLM finding.
    "attachment_read": "fetch",
    # Python / sandbox
    "python_sandbox": "compute",
    # Batch 5ⓑ: flat sandbox entry executes the python_sandbox skill, so kind must have the same value.
    "python_exec": "compute",
    "web_fetch": "lookup",
    # Subagent
    "subagent_spawn": "spawn",
    "subagent_return": "spawn",
    "get_current_user": "lookup",
    "send_email": "write",
    "google_doc_write": "write",
    "google_slides_write": "write",
    # Native PPTX rendering produces a deliverable file, like google_slides_write; no numbers to summarize.
    "render_deck": "write",
    "google_sheet_write": "write",
    # Canvas (write tools)
    "add_chart": "chart",
    "canvas_add_insight": "write",
    "canvas_add_html_artifact": "write",
    "canvas_add_external_iframe": "write",
    "remove_chart": "write",
    # Report writing: reading canvas is read-only metadata (no LLM finding); adding sections is write (section ID result, no summarizable numbers).
    # Previously unregistered ⇒ default compute fallback ⇒ finding-eligible.
    "read_report": "lookup",
    "write_report": "write",
    # Web
    "web_search": "lookup",
    "web_extract": "lookup",
    "web_map": "lookup",
    "web_crawl": "lookup",
    "web_deep_research": "lookup",
    # Agent Coach and configuration specialist. None of these returns numbers worth an LLM finding.
    # lookup = catalog/status/validation reads and view-open actions (like tool_switch_context);
    # fetch = one exact record or file (like kb_get_record); write = persists, starts or cancels work.
    "agent_resource_search": "lookup",
    "agent_resource_get": "fetch",
    # Emits only the Canvas editor-open pointer; nothing is saved.
    "agent_resource_open": "lookup",
    "agent_authoring_inspect": "lookup",
    "agent_authoring_tools": "lookup",
    "agent_authoring_validate": "lookup",
    "agent_authoring_propose": "write",
    "agent_authoring_proposal_get": "fetch",
    "agent_draft_start": "write",
    # Creates the working-copy baseline from the published release when absent.
    "agent_working_copy_open": "write",
    "agent_change_validate": "lookup",
    "agent_change_propose": "write",
    "agent_change_get": "fetch",
    "agent_operation_get": "fetch",
    "agent_operation_open": "lookup",
    "agent_eval_evidence_get": "fetch",
    "agent_eval_suite_list": "lookup",
    "agent_eval_suite_get": "fetch",
    "agent_eval_case_propose": "write",
    "agent_eval_case_proposal_get": "fetch",
    "agent_eval_turns_list": "lookup",
    "agent_eval_alignment_start": "write",
    "agent_eval_alignment_ask": "write",
    "agent_eval_alignment_get": "fetch",
    "agent_eval_alignment_report": "fetch",
    "agent_ci_request": "write",
    "agent_ci_get": "fetch",
    "agent_ci_prepare": "write",
    "agent_ci_start": "write",
    "agent_ci_cancel": "write",
    "agent_plugin_inspect": "lookup",
    "agent_plugin_validate": "lookup",
    "agent_plugin_save": "write",
    "agent_plugin_operation_get": "fetch",
    "agent_skill_inspect": "lookup",
    "agent_skill_read": "fetch",
    "agent_skill_validate": "lookup",
    "agent_skill_save": "write",
    "agent_skill_cancel": "write",
    "agent_skill_operation_get": "fetch",
    "agent_tool_requirement_create": "write",
    "agent_tool_requirement_get": "fetch",
    "agent_tool_requirement_open": "lookup",
    "agent_tool_requirement_reconcile": "write",
    "configuration_list": "fetch",
    "configuration_read": "fetch",
    "configuration_open": "lookup",
    "configuration_propose": "write",
    "configuration_apply": "write",
    "configuration_result": "fetch",
    # Delivers the specialist's final message, like subagent_return.
    "configuration_submit": "spawn",
}

#: Kinds that produce a numeric/factual result worth LLM-summarizing.
_FINDING_ALLOW_KINDS: Final[frozenset[str]] = frozenset({"query", "compute"})


#: Renamed canvas write tools **without the `canvas_` prefix**. See docs/design/工具改名迁移.md.
_RENAMED_CANVAS_ADD_TOOLS: Final[frozenset[str]] = frozenset({"add_chart", "add_image"})


def is_canvas_add_tool(tool_name: str) -> bool:
    """「往画布放一件实物」的写工具族判定。

    ⚠ **不要写成 `name.startswith("canvas_add")`**：`add_chart` / `add_image` 改名后
    没有这个前缀，只判前缀会**静默漏掉**它们——批 1 实测踩到两处（chat 行内图整个
    失效、`_drew_canvas` 恒 False 导致「没有产出任何可交付内容」的反向谎报）。

    前缀分支保留给**历史名**：`canvas_add_kpi_row` / `canvas_add_table` /
    `canvas_add_section` 等已退役但仍存在于历史 trace 的名字。
    """
    name = canonical_tool_name(tool_name)
    return name.startswith("canvas_add") or name in _RENAMED_CANVAS_ADD_TOOLS


#: Canvas write tools without `canvas_` prefix that do not add a physical item (canvas-update-turn batch B2-2).
#: Missing these has the same consequence: revision turns only removing charts keep `_drew_canvas` False ⇒ judged as no deliverable content,
#: contradicting `canvas_changes.removed_charts` in the same result.
_UNPREFIXED_CANVAS_EDIT_TOOLS: Final[frozenset[str]] = frozenset({"remove_chart"})


def is_canvas_tool(tool_name: str) -> bool:
    """canvas 命名空间的工具（比 `is_canvas_add_tool` 宽，含 patch/delete/reorder/remove_chart）。

    ⚠ 同样**不要写成 `name.startswith("canvas")`** —— 理由见 `is_canvas_add_tool`。
    """
    name = canonical_tool_name(tool_name)
    return name.startswith("canvas") or name in _RENAMED_CANVAS_ADD_TOOLS or name in _UNPREFIXED_CANVAS_EDIT_TOOLS


def lookup_kind(tool_name: str) -> str:
    """Tool name → kind. Unknown tools fall back to ``compute`` (same as FE)."""
    # C4 consolidation (display): **historical turns retain names from before renaming**; `tool_invocations` /
    # event streams are immutable history. Normalize only when reading; never write back. Change the lookup function,
    # not each table entry. See docs/design/工具改名迁移.md.
    if canonical_tool_name(tool_name).startswith("personalmcp."):
        return "write"
    return _TOOL_KIND_MAP.get(canonical_tool_name(tool_name), "compute")


# Sprint chat-style-fix Phase A4 — 8-kind → 3-display-category (CTO P1-1 unify
# pattern: backend single-source-of-truth, FE reads display_category and uses
# kind only as legacy-trace fallback).
#
#   read   → tool renders as single-line chip (read-only / lookup-style tool)
#   full   → tool renders with finding + preview blocks (query/compute/chart)
#   mutate → tool renders preview only, no finding (write-style)
_DISPLAY_CATEGORY_MAP: Final[dict[str, str]] = {
    "lookup": "read",
    "fetch": "read",
    "attribute": "read",
    "spawn": "read",
    "query": "full",
    "compute": "full",
    "chart": "full",
    "write": "mutate",
}


def lookup_display_category(kind: str) -> str:
    """Map 8-kind to FE display: read (single-line) / full (finding+preview)
    / mutate (preview only). Unknown kinds default to "full" so an unrecognized
    new tool renders the maximum amount of context to the user.
    """
    return _DISPLAY_CATEGORY_MAP.get(kind, "full")


def should_emit_finding_for_kind(kind: str) -> bool:
    """True iff this kind's result is worth a LLM finding summary."""
    return kind in _FINDING_ALLOW_KINDS


def should_emit_finding_for_tool(tool_name: str) -> bool:
    """Shortcut: tool name → bool (combines lookup + kind check)."""
    return should_emit_finding_for_kind(lookup_kind(tool_name))


# ─────────────────────────────────────────────────────────────────────────
# Sprint tool-domain (D1) — tool → SOURCE/DOMAIN axis, orthogonal to the
# rendering-kind axis above. Mirrors MCP ToolAnnotations.openWorldHint:
# web/external tools touch an OPEN world (external entities); dataset/kb/compute
# operate on the CLOSED in-workspace world. Consumed by the cross-turn digest
# (D1 source continuity). lookup_domain is the by-name correctness FLOOR; prod
# prefers the stamped ToolInvocation.source (resolved off the per-turn registry
# view, so teammcp.*/mcp.* self-classify).
# ─────────────────────────────────────────────────────────────────────────

_OPEN_WORLD_SOURCES: Final[frozenset[str]] = frozenset({"web", "external"})

#: turn-level reduction priority. In-workspace data (dataset/kb) wins when a turn
#: touched both it AND web/external — continuing on the dataset is the safe default;
#: the open-world signal fires for turns that went ONLY external.
_SOURCE_PRIORITY: Final[tuple[str, ...]] = ("dataset", "kb", "web", "external", "compute", "model")

#: subagent_spawn profile → source, used only when the peer's nested tools aren't
#: readable. A peer is not a tool; its domain is what it does. Only the non-dataset
#: peers are listed — every other (analysis/specialist) peer AND any unknown profile
#: default to 'dataset' (the safe D1 answer: a dataset-defaulted turn simply won't
#: trigger web-continuity — the conservative behaviour).
_PROFILE_DOMAIN: Final[dict[str, str]] = {
    "kb-analyst": "kb",
    "kb_analyst": "kb",
    "canvas-writer": "compute",
    "canvas_writer": "compute",
}


def lookup_domain(tool_name: str) -> str:
    """Tool name → source domain: 'dataset'|'kb'|'web'|'compute'|'external'.

    By-name fallback (correctness floor) for when ToolInvocation.source wasn't stamped
    (approval-gated / not-registered builders that bypass ToolRegistry.execute, or tests).
    Prefix-based on the fixed built-in families: ``bi_*`` == the datarc bridge (mirrors
    tool_registry._tool_requires_datarc, the dataset SSOT); ``teammcp.*``/``mcp.*`` ==
    external team tools (open world).
    """
    if tool_name.startswith(("bi_", "pbi_")):
        return "dataset"
    if tool_name.startswith(("teammcp.", "mcp.", "personalmcp.")):
        return "external"
    if tool_name in {"send_email", "google_doc_write", "google_slides_write", "google_sheet_write"}:
        return "external"
    if tool_name.startswith("kb_"):
        return "kb"
    if tool_name.startswith("web_"):
        return "web"
    # canvas_*/skill_*/python_sandbox + everything else → closed-world compute
    return "compute"


def _profile_source(inv: Any) -> str:
    args = getattr(inv, "arguments", None)
    profile = ""
    if isinstance(args, dict):
        profile = str(args.get("profile") or "").strip().lower()
    return _PROFILE_DOMAIN.get(profile, "dataset")


def _iter_turn_sources(invocations: Any) -> Any:
    """Yield the source of each (possibly nested) invocation. subagent_spawn defers to its
    nested peer tools when present (a compute peer's bi_* makes the turn 'dataset'); only
    when nested is unreadable does it fall back to the spawn profile — so a web-only peer
    turn isn't mis-stamped 'dataset' by its profile default.

    No depth/visited guard: today nesting is ≤1 level (spawned subagents have subagent_spawn
    stripped from their whitelist, subagent.py) and `nested` is built from a distinct peer's
    invocations (never self-referential), so unbounded recursion is structurally impossible.
    If nested spawns are ever re-enabled, add a depth cap here."""
    for inv in invocations or []:
        name = getattr(inv, "tool_name", "") or ""
        nested = getattr(inv, "nested", None)
        if name == "subagent_spawn":
            if nested:
                yield from _iter_turn_sources(nested)
            else:
                yield _profile_source(inv)
            continue
        src = getattr(inv, "source", None)
        yield src if (isinstance(src, str) and src) else lookup_domain(name)
        if nested:
            yield from _iter_turn_sources(nested)


def classify_turn_source(invocations: Any) -> dict[str, Any]:
    """Reduce a turn's tool trace to {source, open_world} for D1 cross-turn continuity.

    source ∈ dataset|kb|web|external|compute|model; open_world True iff the turn answered
    from an OPEN world (web/external), i.e. NOT the connected workspace dataset/KB. A 0-tool
    turn is 'model' (model knowledge, not open_world → the continuity rule won't fire after
    chit-chat).
    """
    seen = {s for s in _iter_turn_sources(invocations) if s}
    if not seen:
        return {"source": "model", "open_world": False}
    source = next((s for s in _SOURCE_PRIORITY if s in seen), "model")
    return {"source": source, "open_world": source in _OPEN_WORLD_SOURCES}
