"""Sprint θ Phase 8b-1: δ active_facts block builder (G3 wire #1).

Per tech-design §0.6 §4.6 + r2.5 R1 + R5:
- Caller: prompts/lead.py:build_lead_system_prompt at top of system prompt
- Returns XML-tagged `<active_facts>...</active_facts>` block (or empty string)
- Top-K cap (10 facts) per workspace per turn build (R5 — write-side cap also
  enforced in fact_store.register, this is read-side defense-in-depth)
- Env flag fence: BOWHEAD_DELTA_INJECT_ENABLED=0 → returns empty (R1 partial-rollback)
- Per-workspace exclude list: BOWHEAD_DELTA_EXCLUDE_WORKSPACES=ws_xxx,ws_yyy

Token budget: 10 facts × 30 tok = 300 tok worst-case (per L1 token table).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bowhead_core.delta.registered_fact_store import RegisteredFact

logger = logging.getLogger(__name__)

# Cap from tech-design §0.6 R5: hard 10 facts (top-K by recency/registered_at desc).
# Both write-side (fact_store.register evicts oldest if at cap) AND read-side here.
_MAX_FACTS_PER_BUILD = 10

# Cap per-fact summary length (~30 tok = ~60 chars Chinese, tiktoken cl100k)
_MAX_FACT_SUMMARY_CHARS = 60


def is_delta_inject_enabled() -> bool:
    """R1 partial-rollback rope: env flag to globally disable δ injection.

    Setting `BOWHEAD_DELTA_INJECT_ENABLED=0` returns empty active_facts block
    on all turns — used in incident triage when δ wiring causes regression.
    """
    return os.environ.get("BOWHEAD_DELTA_INJECT_ENABLED", "1") == "1"


def is_workspace_excluded(workspace_id: str) -> bool:
    """R1 partial-rollback rope: per-workspace exclude list.

    Setting `BOWHEAD_DELTA_EXCLUDE_WORKSPACES=ws_a,ws_b` skips δ injection for
    those workspaces only (used when corrupt fact_store row affects 1 workspace).
    """
    excludes = os.environ.get("BOWHEAD_DELTA_EXCLUDE_WORKSPACES", "")
    if not excludes:
        return False
    return workspace_id in {w.strip() for w in excludes.split(",") if w.strip()}


def build_block(facts: list[RegisteredFact], *, workspace_id: str = "") -> str:
    """Build XML-tagged active_facts block for Lead system prompt header.

    Args:
        facts: list of RegisteredFact (caller is responsible for fetching from store).
            Top 10 by recency are used; rest dropped silently.
        workspace_id: for exclude-list check. Empty string means caller didn't pass
            (or test setup); skip exclude check.

    Returns:
        XML block string, OR empty string if disabled/excluded/empty.

    Format:
        <active_facts>
          <fact id="ebitda_definition" provenance="auto_learner" registered_at="2026-04-22">
            {"formula":"营收 - COGS - 外包"}
          </fact>
          ...
        </active_facts>
    """
    # R1 fences
    if not is_delta_inject_enabled():
        logger.debug("delta_inject_disabled workspace_id=%s", workspace_id)
        return ""
    if workspace_id and is_workspace_excluded(workspace_id):
        logger.debug("workspace_excluded_from_delta workspace_id=%s", workspace_id)
        return ""

    if not facts:
        return ""

    # Top-K cap (read-side defense-in-depth per R5)
    top_facts = facts[:_MAX_FACTS_PER_BUILD]

    lines = ["<active_facts>"]
    for fact in top_facts:
        # RegisteredFact carries fact_key (the [fact:KEY] citation handle) +
        # value_json (the content) — it has NO `.summary`/`.id`. Render the value
        # as the body and the key as the id, so the Lead can cite [fact:fact_key].
        summary = (fact.value_json or "")[:_MAX_FACT_SUMMARY_CHARS]

        # Conservative escape: replace XML special chars in summary
        summary_escaped = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def _attr(v: str) -> str:
            # Escape every value going into an XML attribute. source/created_at are
            # normally controlled, but a stray quote/bracket (migration artifact or
            # adversarial stored fact) must not break out of the attribute (review).
            return (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        prov = _attr(fact.source or "user-confirmed")
        registered_at = _attr(fact.created_at or "")
        key_escaped = _attr(fact.fact_key or "")

        lines.append(f'  <fact id="{key_escaped}" provenance="{prov}" registered_at="{registered_at}">')
        lines.append(f"    {summary_escaped}")
        lines.append("  </fact>")
    lines.append("</active_facts>")

    return "\n".join(lines)


async def build_block_from_store(
    workspace_id: str,
    *,
    project_id: str | None = None,
    store: RegisteredFactStoreProtocol | None = None,
) -> str:
    """Convenience: pull facts from store + build block in one call.

    **ASYNC** per CTO P1 #5 fix — `RegisteredFactStore.list_active` is async,
    so this convenience helper must `await` it. Callers (lead_orchestrator,
    agent_runner_adapter) must `await build_block_from_store(...)`.

    Used by Lead orchestrator + agent_runner_adapter (G3 wire #1 + #2).

    Args:
        workspace_id: for fact_store.list_active filter.
        project_id: optional project scope.
        store: RegisteredFactStore (or None to read from ContextVar).

    Returns:
        XML block string, OR empty string.
    """
    from bowhead_core.agents._lead_context import get_lead_fact_store

    if store is None:
        store = get_lead_fact_store()
    if store is None:
        return ""

    # Fence checks happen in build_block (no point fetching if disabled)
    if not is_delta_inject_enabled() or is_workspace_excluded(workspace_id):
        return ""

    facts = await store.list_active(workspace_id=workspace_id, project_id=project_id)
    return build_block(facts, workspace_id=workspace_id)


# Type protocol stub (avoid circular import; real type from delta.registered_fact_store)
if TYPE_CHECKING:

    class RegisteredFactStoreProtocol:
        async def list_active(
            self,
            *,
            workspace_id: str,
            project_id: str | None = None,
        ) -> list[RegisteredFact]: ...
