"""Lead-orchestration transient state — schema + single-writer init + accessors.

Background (2026-05-19, post 0.26.1)
---
Three same-shape bugs in chat-v4-streaming-rewrite (0.26.0) all stemmed from
`getattr/setattr` proliferation across lead_orchestrator/turn_runner/subagent
(73+ sites). The bugs:

  1. ``_lead_dispatch_window`` — wrong default extraction. Caller did
     ``list(_lead_dispatch_window or [])`` on a dataclass instance →
     ``TypeError: 'LeadDispatchWindow' object is not iterable``. Hidden
     under ``except Exception`` so FE got ``action="noop"`` with no recovery.
  2. ``_v4_phase_plan`` — never WRITTEN. Read site assumed Lead would set it
     before resume_context snapshot; Lead never did → silent ``None`` echo.
  3. ``paradigm_v2`` / ``preferred_provider`` / ``preferred_model`` — never
     SET on ``_peer_ctx`` at turn-runner construction. ``getattr(..., False)``
     default-masked the gap → every resumed turn lost the user's LLM choice.

Per memory ``feedback_no_reactive_patch_system_primitives``: 3rd same-shape
bug = stop patching, open a sprint. This module is that sprint's fix.

What this module gives you
---
- ``LeadTurnTransient`` (TypedDict, total=False): the schema of every
  transient slot stashed on ``ToolExecutionContext`` during a Lead turn.
- ``init_lead_transient(ctx, request)``: SINGLE writer for turn-start
  initialization. Centralizes the wiring that was scattered across 5
  setattr blocks in ``turn_runner.py``. Adding a new transient slot
  requires editing both the TypedDict and this init — the two-place
  edit is the gate that catches Bug 3 ("forgot to wire").
- Typed accessors own the
  extraction logic — Bug 1's wrong-default problem can't recur because
  callers never touch the underlying dataclass directly.

Why not migrate ALL 73 sites in one go?
---
- A pure TypedDict swap is mechanical but doesn't fix Bug 2 (missing
  writers) or Bug 3 (missing constructor wiring).
- Better: lock down the WRITE paths first (one init, typed accessors for
  the high-risk slots), then migrate read sites incrementally as Phase 3.
- Idempotency markers (``_done_emitted`` etc.) and per-subagent locals
  (``_invocations`` etc.) stay raw — they're write-once-or-bookkeeping
  and have never been in the bug class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext
    from bowhead_core.types import TurnRequest


class LeadTurnTransient(TypedDict, total=False):
    """Schema for ctx slots used by Lead orchestration.

    ``total=False`` because some slots are populated lazily (e.g. resume-only
    fields, or terminal-emit flags). Required-at-turn-start slots are all
    written by ``init_lead_transient``.
    """

    # === Request-derived identity / LLM provider hints ===
    # Bug 3 fix surface — these were silently missing from _peer_ctx until
    # 0.26.1 added the setattrs at turn_runner.py:813-815. Now centralized.
    paradigm_v2: bool
    preferred_provider: str | None
    preferred_model: str | None

    # === HITL same-turn-resume signals (Phase 4C) ===
    # Set only when the request is a clarification-resume (resume_turn_id
    # truthy); fresh turns leave these unset.
    _resume_turn_id: str | None
    _resume_glossary: dict[str, str]
    _resume_answer_context: str | None

    # === V4 streaming event surface ===
    # Sprint phase-plan-removal: 6 turn.plan/phase.update fields removed
    # (_v4_phase_plan / _v4_turn_plan_emitted / _v4_phase_seq /
    # _v4_phase_update_emitted_keys / _v4_spawn_id_to_phase_id /
    # _v4_last_peer_dispatched_spawn_id). Only lead.final.payload idempotence
    # flag survives.
    _v4_final_payload_emitted: bool

    # === Eager compose / pinned attachments (Sprint ψ) ===
    _psi_turn_user_text: str
    _psi_pinned_snapshot: list[dict[str, Any]]
    _psi_pinned_total: int

    # === Sprint chat-redesign-2026-05-21 Phase 1.1 — preprocess_attachments ===
    # Populated AFTER ψ-F snapshot by `TurnRunner._preprocess_attachments_with_events`
    # (parallel parse + step_started/step_done emit per attachment). Lead reads:
    # - `_psi_parsed_results`: list of PreparedAttachment with extracted_text/notes
    # - `_psi_failed_metadata`: pre-formatted markdown of failed parses for
    #   "files I couldn't read" section in Lead system prompt (per PRD §3.6.4).
    _psi_parsed_results: list[Any]  # list[PreparedAttachment] — Any to avoid import cycle
    _psi_failed_metadata: str | None
    _chart_selected_facts: list[dict[str, Any]]

    # === Sprint chat-redesign-2026-05-21 Phase 1.1 — L3 picker hint (P0-7) ===
    # Set by `init_lead_transient` from `TurnRequest.l3_picker_hint` (gateway
    # populates this when user message is empty + attachments present).
    # `_lead_loop` reads and injects via prompts/lead.py
    # `_L3_PICKER_HINT_TEMPLATE` so Lead emits clarification_ask immediately.
    l3_picker_hint: list[str] | None

    # === skill-import batch 2: <bundle_import> (FEATURE_BUNDLE_IMPORT_DRAFT flag, default off) ===
    # turn_runner sets this after preprocessing: each item is {filename, inventory}, with inventory from
    # BundleInventory.to_dict() (KB paths also carry landed_skills). Lead uses render_bundle_import_block
    # to render one turn block. This attribute is absent entirely when the flag is off.
    _bundle_import_bundles: list[dict[str, Any]]

    # === web_fetch URL provenance gate ===
    # Per-turn shared set[host] of URLs the user/search surfaced. Seeded from the
    # user message here; web_search adds its result hosts; web_fetch rejects any
    # host not in it (anti-exfil — see url_provenance.py). Shared by reference
    # across Lead/subagent (shallow copy.copy of ctx).
    _seen_url_hosts: set[str] | None


def init_lead_transient(
    ctx: ToolExecutionContext,
    request: TurnRequest,
) -> None:
    """Initialize all turn-start transient slots on ``ctx``.

    Single-writer for turn-start transient state. Called once from
    ``turn_runner.py`` after ``_peer_ctx = ToolExecutionContext(...)``.

    Idempotent: safe to call multiple times (always overwrites). But the
    convention is one call per turn, at peer_ctx construction.
    """
    # Identity + LLM provider hints. Request is a dataclass; getattr is
    # belt-and-suspenders for tests that pass loose stubs.
    ctx.paradigm_v2 = bool(getattr(request, "paradigm_v2", False))  # type: ignore[attr-defined]
    ctx.preferred_provider = getattr(request, "preferred_provider", None)  # type: ignore[attr-defined]
    ctx.preferred_model = getattr(request, "preferred_model", None)  # type: ignore[attr-defined]

    # HITL resume signals — only stash if this turn is a resume.
    _resume_turn_id = getattr(request, "resume_turn_id", None)
    _resume_glossary = getattr(request, "resume_glossary", None)
    if _resume_turn_id:
        ctx._resume_turn_id = _resume_turn_id  # type: ignore[attr-defined]
        ctx._resume_glossary = _resume_glossary or {}  # type: ignore[attr-defined]
        ctx._resume_answer_context = getattr(request, "resume_answer_context", None)  # type: ignore[attr-defined]
        if getattr(request, "ordinary_clarification_id", None):
            from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadRunState

            checkpoint = request.ordinary_lead_checkpoint
            if checkpoint is None:
                raise ValueError("clarification_resume_unavailable")
            ctx.ordinary_lead_state = OrdinaryLeadRunState(card_id=request.ordinary_clarification_id, checkpoint=checkpoint)

    # V4 streaming surface — only the lead.final.payload idempotence flag
    # survives. Sprint phase-plan-removal: removed phase-plan tracking init.
    ctx._v4_final_payload_emitted = False  # type: ignore[attr-defined]

    # Eager compose context — user text stash for subagent_return handler
    # (Sprint ψ-E). Required by composer fallback path.
    ctx._chart_selected_facts = []  # type: ignore[attr-defined]
    ctx._psi_turn_user_text = request.text  # type: ignore[attr-defined]

    # Sprint chat-redesign-2026-05-21 Phase 1.1 — L3 picker hint.
    # Stash request.l3_picker_hint (set by gateway when message="" + attachments)
    # on ctx so _lead_loop can inject it into the system prompt.
    ctx.l3_picker_hint = getattr(request, "l3_picker_hint", None)  # type: ignore[attr-defined]

    # web_fetch URL provenance gate: seed the per-turn seen-host set from the
    # (trusted) user message. web_search adds result hosts; web_fetch gates on it.
    from bowhead_core.url_provenance import seed_seen_hosts

    seed_seen_hosts(
        ctx,
        "\n".join((request.text or "", getattr(request, "provenance_seed_text", "") or "", getattr(ctx, "_resume_answer_context", None) or "")),
    )


def lead_transient(ctx: ToolExecutionContext) -> LeadTurnTransient:
    """Typed dict-view over ``ctx.__dict__`` for callers that prefer ``.get()``.

    Use for read-side migration of ``getattr(ctx, "_attr", default)`` →
    ``lead_transient(ctx).get("_attr", default)`` so callers see the schema.
    """
    return cast("LeadTurnTransient", ctx.__dict__)
