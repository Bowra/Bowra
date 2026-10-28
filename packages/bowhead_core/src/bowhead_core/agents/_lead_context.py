"""Sprint θ Phase 8a: Lead orchestrator ContextVar plumbing.

A1 fix from tech-design-review-r1.md: fact_store DI undefined. Solution:
ContextVar set in `lead_orchestrator.run_turn` head, read in adapter (G3 wire #2).

Async-safe: ContextVar uses copy-on-task semantics — when dispatcher spawns
parallel peer tasks via asyncio.create_task, each task inherits the current
context snapshot. Reset via token after run_turn completes.

Also exposed:
- `_LEAD_FACT_STORE_CONTEXTVAR`: per-turn fact_store reference for δ wire #2
- `_LEAD_TURN_DEADLINE_CONTEXTVAR`: the turn's hard deadline (the Lead loop timeout), shared with peers
- `_CURRENT_PEER_NAME_CONTEXTVAR`: current peer name for I3 runtime guard (R3 fix)
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.delta.registered_fact_store import RegisteredFactStore


# G3 wire #2 (δ active facts injection in peer dispatch)
_LEAD_FACT_STORE_CONTEXTVAR: contextvars.ContextVar[RegisteredFactStore | None] = contextvars.ContextVar("lead_fact_store", default=None)

# The turn's hard deadline (monotonic), set by run_turn to the same instant its
# asyncio.wait_for enforces. Router retry admission and confirmation windows read it.
_LEAD_TURN_DEADLINE_CONTEXTVAR: contextvars.ContextVar[float | None] = contextvars.ContextVar("lead_turn_deadline", default=None)

# R3 fix (I3 runtime guard — track which peer is currently invoking tools)
# Default "lead" so Lead self-call detected by guard.
_CURRENT_PEER_NAME_CONTEXTVAR: contextvars.ContextVar[str] = contextvars.ContextVar("current_peer_name", default="lead")

# report-engine Phase-2 WP-B: the UNIQUE per-spawn id of the peer currently
# invoking tools (e.g. "sub_a1b2c3d4e5"), used to stamp report-section ownership.
# A ContextVar (NOT parent_ctx._current_subagent_id, which is SHARED across
# parallel sibling tasks → last-wins → two same-profile writers would collide on
# owner). Each spawn dispatches in its own copied context, so this is per-task
# isolated. None for the Lead (no spawn) → owner falls back to the peer name.
_CURRENT_SUBAGENT_ID_CONTEXTVAR: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_subagent_id", default=None)

# Exact correlation for the Lead's currently executing subagent_spawn tool.
# Each parallel sibling runs in its own copied asyncio context, so this avoids
# attaching peer prose/terminal facts to a sibling by arrival order.
_CURRENT_SPAWN_TRACE_CONTEXTVAR: contextvars.ContextVar[tuple[str | None, str | None] | None] = contextvars.ContextVar(
    "current_spawn_trace",
    default=None,
)


def set_lead_fact_store(
    store: RegisteredFactStore | None,
) -> contextvars.Token:
    return _LEAD_FACT_STORE_CONTEXTVAR.set(store)


def get_lead_fact_store() -> RegisteredFactStore | None:
    return _LEAD_FACT_STORE_CONTEXTVAR.get()


def reset_lead_fact_store(token: contextvars.Token) -> None:
    _LEAD_FACT_STORE_CONTEXTVAR.reset(token)


def set_turn_deadline(deadline_monotonic: float | None) -> contextvars.Token:
    return _LEAD_TURN_DEADLINE_CONTEXTVAR.set(deadline_monotonic)


def get_turn_deadline() -> float | None:
    return _LEAD_TURN_DEADLINE_CONTEXTVAR.get()


def reset_turn_deadline(token: contextvars.Token) -> None:
    _LEAD_TURN_DEADLINE_CONTEXTVAR.reset(token)


def set_current_peer_name(peer_name: str) -> contextvars.Token:
    """Dispatcher sets this when invoking peer; reset on peer exit. R3 invariant.

    Default value "lead" represents Lead self-call (no peer invocation in progress).
    """
    return _CURRENT_PEER_NAME_CONTEXTVAR.set(peer_name)


def get_current_peer_name() -> str:
    return _CURRENT_PEER_NAME_CONTEXTVAR.get()


def reset_current_peer_name(token: contextvars.Token) -> None:
    _CURRENT_PEER_NAME_CONTEXTVAR.reset(token)


def set_current_subagent_id(subagent_id: str) -> contextvars.Token:
    """SubagentRunner sets this (unique per-spawn id) around the agent loop."""
    return _CURRENT_SUBAGENT_ID_CONTEXTVAR.set(subagent_id)


def get_current_subagent_id() -> str | None:
    return _CURRENT_SUBAGENT_ID_CONTEXTVAR.get()


def reset_current_subagent_id(token: contextvars.Token) -> None:
    _CURRENT_SUBAGENT_ID_CONTEXTVAR.reset(token)


# i18n: step summaries (`output_preview`) follow the **UI language**. Use ContextVar rather than threading a parameter
# through to `_execute_tool`: like the others here, set once at turn start and read deeper down.
# Missing language contexts use the English product default.
_CURRENT_UI_LANGUAGE_CONTEXTVAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_ui_language",
    default="en-US",
)


def set_current_ui_language(ui_language: str | None) -> contextvars.Token:
    return _CURRENT_UI_LANGUAGE_CONTEXTVAR.set(ui_language or "en-US")


def get_current_ui_language() -> str:
    return _CURRENT_UI_LANGUAGE_CONTEXTVAR.get()


def reset_current_ui_language(token: contextvars.Token) -> None:
    _CURRENT_UI_LANGUAGE_CONTEXTVAR.reset(token)


# Multilingual batch 3 (C2 consistency): deterministic chrome strings follow **this turn's actual effective output language**.
# Use ContextVar as for the UI axis: `report_chrome_text.chrome_lang` has a dozen call sites across canvas_composer /
# report_writer_loop / lead_orchestrator; threading parameters would change a dozen locations.
# More critically, those locations **each choosing their own source** caused G2 mixed languages; consolidate the source to fix it.
# ⚠ Default is an empty string (not zh-CN): empty = never set ⇒ readers fall back to original `language_context`, preserving old paths.
_CURRENT_OUTPUT_LANGUAGE_CONTEXTVAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_output_language",
    default="",
)


def set_current_output_language(output_language: str | None) -> contextvars.Token:
    return _CURRENT_OUTPUT_LANGUAGE_CONTEXTVAR.set(output_language or "")


def get_current_output_language() -> str:
    return _CURRENT_OUTPUT_LANGUAGE_CONTEXTVAR.get()


def reset_current_output_language(token: contextvars.Token) -> None:
    _CURRENT_OUTPUT_LANGUAGE_CONTEXTVAR.reset(token)


def set_current_spawn_trace(
    call_id: str | None,
    parent_step_id: str | None,
) -> contextvars.Token[tuple[str | None, str | None] | None]:
    return _CURRENT_SPAWN_TRACE_CONTEXTVAR.set((call_id, parent_step_id))


def get_current_spawn_trace() -> tuple[str | None, str | None] | None:
    return _CURRENT_SPAWN_TRACE_CONTEXTVAR.get()


def reset_current_spawn_trace(
    token: contextvars.Token[tuple[str | None, str | None] | None],
) -> None:
    _CURRENT_SPAWN_TRACE_CONTEXTVAR.reset(token)


# citation-audit impl-review B-P2-9: `parent_ctx._current_subagent_invocations`
# This was a shared attr with last-wins parallel siblings: after thin-path uses it for value binding/:src: conversion,
# peer A can misbind through B's invocation table. ContextVar gives each execution task its own
# snapshot (asyncio copies context at task creation); retain the attr fallback for old harnesses.
_CURRENT_SUBAGENT_INVOCATIONS_CONTEXTVAR: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "bowhead_current_subagent_invocations",
    default=None,
)


def set_current_subagent_invocations(invocations: list) -> contextvars.Token:
    return _CURRENT_SUBAGENT_INVOCATIONS_CONTEXTVAR.set(invocations)


def get_current_subagent_invocations() -> list | None:
    return _CURRENT_SUBAGENT_INVOCATIONS_CONTEXTVAR.get()


# canvas-update-turn batch B2-6: `parent_ctx._ν15_allowlist` is also a shared attr with parallel-sibling
# last-wins behavior: `write_report` translates A's `[figure:N]` through B's table, binding anchors to someone else's figures.
# Each spawn attaches its own table in its own execution task.
_CURRENT_REF_ALLOWLIST_CONTEXTVAR: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "bowhead_current_ref_allowlist",
    default=None,
)


def set_current_ref_allowlist(allowlist: Any) -> contextvars.Token:
    return _CURRENT_REF_ALLOWLIST_CONTEXTVAR.set(allowlist)


def get_current_ref_allowlist() -> Any:
    return _CURRENT_REF_ALLOWLIST_CONTEXTVAR.get()


def reset_current_ref_allowlist(token: contextvars.Token) -> None:
    _CURRENT_REF_ALLOWLIST_CONTEXTVAR.reset(token)


def run_ref_allowlist(ctx: Any) -> Any:
    """本执行 task（本 run）自己的 ν1.5 表；不在任何 run 里（老 harness / 测试替身）才退回共享 attr。

    ★ 按表翻译 / 核对的写入口一律走这里：共享 `ctx._ν15_allowlist` 在并行 peer 间 last-wins，
    A 的 `[ref:N]` 会被 B 的表翻成 B 的片段（知识库引用审计 #1）。"""
    allowlist = get_current_ref_allowlist()
    return allowlist if allowlist is not None else getattr(ctx, "_ν15_allowlist", None)


def run_invocations(ctx: Any) -> list:
    """本 run 自己的工具调用快照；同上，共享 `ctx._current_subagent_invocations` 仅作退路。"""
    invocations = get_current_subagent_invocations()
    if invocations is not None:
        return invocations
    return getattr(ctx, "_current_subagent_invocations", None) or []


# KB citation audit #2: the spawn handler hands child-run invocations to tool_registry (moved into invocation.nested).
# Previously only shared `ctx._last_nested_invocations` was used: parallel spawn B overwrote it while A's handler was still awaiting,
# so A took B's list, B's spawn record lacked nested, and its kb_search excerpts vanished from this turn's retrieval index.
# Hand off per execution task: the handler and tool_registry finally run in the same task.
def publish_nested_invocations(ctx: Any, invocations: list) -> None:
    import asyncio

    ctx._last_nested_invocations = invocations  # Other readers (whole-turn aggregation) still use this attr.
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is None:
        return
    by_task = getattr(ctx, "_nested_invocations_by_task", None)
    if not isinstance(by_task, dict):
        by_task = {}
        ctx._nested_invocations_by_task = by_task
    by_task[id(task)] = invocations


def take_nested_invocations(ctx: Any) -> list | None:
    """取走本 task 的 handoff；本 task 没有登记时退回共享 attr（老路径 / 非 task 调用）。"""
    import asyncio

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    by_task = getattr(ctx, "_nested_invocations_by_task", None)
    if task is not None and isinstance(by_task, dict):
        # Per-task handoff enabled: no entry for this task = this spawn has no child calls; never pick up a sibling's attr.
        return by_task.pop(id(task), None)
    return getattr(ctx, "_last_nested_invocations", None)


# 116 1.3.56: Lead ran kb_search then delegated report writing to compute; peer numbering and report registration only considered
# that peer's calls. Lead-retrieved excerpts had neither peer numbers nor "retrieved this turn" status, so report body
# `[ref:N]` markers were all stripped. Each run records parent-run KB retrieval results at startup (task-local).
_PARENT_KB_INVOCATIONS_CONTEXTVAR: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "bowhead_parent_kb_invocations",
    default=None,
)


def _kb_hits(invocations: list | None) -> list:
    out: list = []
    for inv in invocations or []:
        if getattr(inv, "tool_name", "") == "kb_search":
            result = getattr(inv, "result", None)
            if isinstance(result, dict) and result.get("chunks"):
                out.append(inv)
        nested = getattr(inv, "nested", None)
        if isinstance(nested, list) and nested:
            out.extend(_kb_hits(nested))
    return out


def inherit_parent_kb_invocations() -> contextvars.Token:
    """run 启动时调用：上级 run 的调用快照（此刻还是继承来的 ContextVar）里有结果的 kb_search，
    连同上级自己继承的，一并作为本 run 可引用的知识库片段。"""
    inherited = list(_PARENT_KB_INVOCATIONS_CONTEXTVAR.get() or [])
    parent_hits = _kb_hits(get_current_subagent_invocations())
    seen = {id(inv) for inv in inherited}
    merged = inherited + [inv for inv in parent_hits if id(inv) not in seen]
    return _PARENT_KB_INVOCATIONS_CONTEXTVAR.set(merged)


def reset_parent_kb_invocations(token: contextvars.Token) -> None:
    _PARENT_KB_INVOCATIONS_CONTEXTVAR.reset(token)


def parent_kb_invocations() -> list:
    return list(_PARENT_KB_INVOCATIONS_CONTEXTVAR.get() or [])
