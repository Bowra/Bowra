"""V3.5 PRD-C · build_memory_pack — bounded LLM injection block.

Composes markdown system prompt section from:
  - memory_notes (≤ 3000 char, pinned > recent > old)
  - memory_entries (≤ 1500 char, kind 优先级排序)

Total hard cap 6000 char. Warn (log + metrics) when truncation triggers.

功能已正式发布；权限、作用域和存储就绪校验仍须执行。"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.memory_readiness import memory_storage_ready
from bowhead_core.prompt_injection_defense import (
    sanitize_for_injection,
    wrap_user_content,
)
from bowhead_core.user_memory import summarize_for_prompt

logger = logging.getLogger(__name__)

NOTES_BUDGET = 3000
ENTRIES_BUDGET = 1500
TOTAL_BUDGET = 6000

#: Rendering contract version stored in `memory_pack_snapshots.pack_version`. Turn-start snapshots must replay verbatim via
#: `/api/v1/memory/replay`, so rendering-shape changes require a version change (batch 0③ added entry IDs).
MEMORY_PACK_VERSION = "canonical_v1"


@dataclass(frozen=True)
class MemoryPackSnapshot:
    """turn 起点建一次的记忆快照（记忆重做 批0④ / tech-design §3.2）。

    挂在 `ToolExecutionContext.memory_pack` 上，Lead 与（批3 起）每个执行体读同一份；
    同一份也原样落 `memory_pack_snapshots`，所以 live / 落库 / 重放三者不可能不一致。
    """

    content: str
    breakdown: dict[str, int] = field(default_factory=dict)
    version: str = MEMORY_PACK_VERSION
    #: Memory redesign batch 3: memory block for **executors**, rendered **once** at turn start and stored here ⇒
    #: every peer in the same turn receives identical bytes, also present in the persisted snapshot
    #: (`/memory/replay` output necessarily matches live injection).
    #: Rendering per peer instead would let one mid-turn `remember` change `last_confirmed_at` ordering
    #: and make two peers diverge.
    executor_block: str = ""


async def build_memory_pack(
    *,
    user_id: str,
    user_memory_store: Any,
    notes_store: Any | None = None,
    metrics_log: Any | None = None,
    breakdown_out: dict[str, int] | None = None,
    entries_out: list[Any] | None = None,
    workspace_id: str | None = None,
) -> str:
    """Compose memory injection block. Returns empty string when no memory.

    V4 PRD-D-4-4: ``breakdown_out`` (mutable dict; populated if provided) gives
    per-section char counts {"notes": N, "entries": N} after
    truncation. Backward-compatible — PRD-C callers pass nothing, signature
    return type unchanged.
    """
    parts: list[str] = []
    truncated_sections: list[str] = []
    notes_chars = 0
    entries_chars = 0

    # 1. Notes (highest priority, full text, sanitized + wrapped)
    #
    # Memory redesign §5: migrated users **no longer read notes**; migration split six bullets into
    # `memory_entries`, while intentionally leaving notes intact (rollback: disabling gate must restore original reads,
    # §5.0/§7). Ignoring this marker injects both, duplicating the same preference in the prompt.
    # ⚠ Marker is **per-user**; unmigrated users must keep reading notes, or their memory disappears immediately.
    skip_notes = False
    if memory_storage_ready() and user_memory_store is not None:
        try:
            skip_notes = await user_memory_store.is_legacy_notes_migrated(user_id)
        except Exception as e:
            # If marker unreadable, **keep reading notes**: temporary duplication is preferable to disappearing memory.
            logger.warning("memory_pack.migration_flag_failed user=%s err=%s", user_id, e)
            skip_notes = False
    if notes_store is not None and not skip_notes:
        try:
            note = await notes_store.get(user_id=user_id)
        except Exception as e:
            logger.warning(
                "memory_pack.notes_load_failed user=%s err=%s",
                user_id,
                e,
            )
            note = None
        if note and note.content:
            sanitized = sanitize_for_injection(note.content)
            if len(sanitized) > NOTES_BUDGET:
                sanitized = _truncate_by_priority(sanitized, NOTES_BUDGET)
                truncated_sections.append("notes")
            wrapped = wrap_user_content(sanitized, source_type="user_profile")
            parts.append(wrapped)
            notes_chars = len(wrapped)

    # 2. memory_entries (kind priority). None-guard: notes-only callers (Remember-rebuild
    #    item 2) pass user_memory_store=None — match the notes_store
    #    branches instead of relying on the try/except below. Sanitize like the notes
    #    section: entries values are user-controlled and this block now reaches the
    #    LIVE Lead system prompt (build_memory_pack got its first live caller).
    entries_block = ""
    if user_memory_store is not None:
        # Memory redesign batch 0② (tech-design §4.1): consolidate policy. `list_for_user` has **no policy filter**,
        # and `summarize_for_prompt` skips only datarc_binding, allowing operational rows
        # (`kind='tool_context'`, `memory_role=operational`) into user
        # `<remembered_about_user>` (116 observed {"id":"1","name":"商超零售场景"} repeated over five times).
        # Memory V2 explicitly defines policy as the sole policy for UI, **prompt**, export/reset;
        # prompt filtering had never been wired. This connects it.
        #
        # ⚠ Enable the gate together with §5 legacy classification: `list_product_for_user` requires
        # `memory_role=product_memory` **and** `memory_policy_version=CURRENT` **and**
        # policy-compatible `value_schema`; any unclassified genuine legacy memory is **silently discarded**.
        # Regression in `tests/test_memory_pack_batch0.py`: compare product rows by ID + discard unclassified rows.
        canonical = memory_storage_ready()
        try:
            # memory-revive S2: filter by current Space when workspace_id is nonempty (NULL = global fallback).
            if canonical:
                entries = await user_memory_store.list_product_for_user(user_id, workspace_id=workspace_id)
            else:
                entries = await user_memory_store.list_for_user(user_id, workspace_id=workspace_id)
            # Batch 3: pass structured entries for caller-rendered executor block, avoiding a second DB read
            # and ensuring Lead and executor blocks use **the same read** (cannot diverge within the turn).
            if entries_out is not None:
                entries_out.extend(entries)
            # Batch 0③: include short entry IDs (shared handles for supersede / provenance / recall).
            entries_block = sanitize_for_injection(summarize_for_prompt(entries, with_ids=canonical))
        except Exception as e:
            logger.warning("memory_pack.entries_load_failed user=%s err=%s", user_id, e)
            entries_block = ""
    if len(entries_block) > ENTRIES_BUDGET:
        entries_block = entries_block[:ENTRIES_BUDGET] + "\n…(truncated)"
        truncated_sections.append("entries")
    if entries_block:
        parts.append(entries_block)
        entries_chars = len(entries_block)

    pack = "\n\n".join(parts)
    if len(pack) > TOTAL_BUDGET:
        pack = pack[:TOTAL_BUDGET] + "\n…(memory truncated to 6000 char)"
        truncated_sections.append("total")

    if truncated_sections and metrics_log is not None:
        with contextlib.suppress(Exception):
            await metrics_log.track(
                "memory.budget_truncation",
                user_id=user_id,
                attrs={
                    "sections": ",".join(truncated_sections),
                    "len": len(pack),
                },
            )

    if breakdown_out is not None:
        breakdown_out["notes"] = notes_chars
        breakdown_out["entries"] = entries_chars

    return pack


def _truncate_by_priority(content: str, budget: int) -> str:
    """Pinned (📌) > recent freetext > old. CTO r2 P1: prefer later lines within
    each priority bucket (= more recent edit, user added late).

    Returns content with selected lines in original order (preserves visual flow).
    """
    lines = content.split("\n")
    indexed = list(enumerate(lines))
    pinned = [(i, ln) for i, ln in indexed if ln.lstrip().startswith("📌")]
    other = [(i, ln) for i, ln in indexed if not ln.lstrip().startswith("📌")]
    # Within each bucket, prefer later lines (= more recent)
    pinned.sort(key=lambda p: -p[0])
    other.sort(key=lambda p: -p[0])
    selected: set[int] = set()
    used = 0
    for idx, ln in pinned + other:
        if used + len(ln) + 1 > budget:
            break
        selected.add(idx)
        used += len(ln) + 1
    return "\n".join(ln for i, ln in indexed if i in selected)
