"""prod-bug-bash-039 #6 citation reform — turn-scoped citation registry.

Replaces the unreliable `[data:art_xxx]` LLM-emit pattern (per
`[[feedback_llm_internal_syntax_unreliable]]` — ι-15 dogfood proved LLM emit
of 12-char hex artifact IDs is 0~28/49 success rate). LLM now emits simple
`[cite:N]` (consecutive 1-based integers); backend maintains the registry
mapping `N` → `(artifact_id, label, kind)` per turn.

**Lifecycle (per design r2 §5.2 + r2.1 BE-P0-2 lazy-init correction)**:
- Registry instance lazy-init in `_resolve_citations()` via
  `getattr(parent_ctx, "turn_citation_registry", None)` then `setattr` if None.
- Removes init-site dependency entirely — failed-before-Lead paths get empty
  registry that no-ops gracefully.
- Concurrency safety: `register()` is sync (no `await`); asyncio single-thread
  guarantees no preemption between awaits, so dict mutation across sibling
  peer coroutines is safe. Persist via batched flush at turn-end (mirror
  `record_turn_attachments`).

**Dedup**: same `artifact_id` registered twice returns same `N` (not a new
index). LLM may reference the same artifact multiple times in narrative; chip
should consistently link.

**Persistence**: writes to `turn_citations` table (new, separate from
`turn_attachments` which is user-upload only — per design r2.1 P0-NEW-2 —
mixing breaks attachments_injector / artifact_store readers).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

CitationKind = Literal["data_ref", "chart_section", "tool_output", "kb_chunk", "kb_row", "web_clip"]
# Sprint pptx-from-template M1: web_clip = a persisted web_search result page
# (artifact_id = art_<12hex>, source=web_search, trust=unverified). Cited via
# [cite:N] on the CHAT path ONLY (never [ref:S] canvas); FE renders a distinct
# "external / unverified" chip. External numbers are NOT added to the verified
# set (number_verifier ignores web), matching the web_search fail-soft invariant.
# Sprint 25 kb-per-row-extract: kb_row = per-row record from kb_extracted_rows
# (cardinality=many doc_types: subsidiary ledgers/general ledgers/turnover statements). artifact_id = row_id
# (row_<14hex> from kb_extracted_rows store). label = "明细账第 N 行 (凭证号 X)"
# or similar human-friendly hint. LLM emits [cite:N] same as kb_chunk pattern.
# FE renders kb_row chip differently from kb_chunk (e.g. table icon vs document).


@dataclass(frozen=True)
class Citation:
    """One entry in the turn-scoped citation registry."""

    seq: int  # 1-based index — LLM emits [cite:N] where N == seq
    artifact_id: str
    label: str | None  # human-friendly hint (e.g. "2025年客流量"); FE chip tooltip
    kind: CitationKind
    kb_generation_id: str | None = None
    kb_source_check_state: str | None = None


class TurnCitationRegistry:
    """In-memory turn-scoped registry of citations.

    Created lazily per-turn on first `_resolve_citations()` call (no init-site
    dependency). Flushed to `turn_citations` table at turn-end via
    `SessionStore.record_turn_citations()`.

    Dedup by `artifact_id`: re-registering the same artifact returns the
    same `seq` (idempotent).

    Thread-safety: sync `register()` is safe across sibling-peer coroutines
    in asyncio (no preemption between awaits in single-threaded event loop).
    """

    def __init__(self) -> None:
        # Insertion-order dict for monotonic seq.
        self._entries: dict[str, Citation] = {}
        # Reverse lookup for dedup.
        self._by_artifact: dict[str, int] = {}
        # 0.39.3 — O(1) seq → Citation lookup. resolve() is called inside the
        # `[cite:N]` regex sub callback in _resolve_citations, so K markers
        # × N entries was O(K*N) without this. CTO Perf-Review #4 P0-1.
        self._by_seq: dict[int, Citation] = {}

    def register(
        self,
        artifact_id: str,
        label: str | None = None,
        kind: CitationKind = "data_ref",
    ) -> int:
        """Register an artifact citation. Returns 1-based seq for `[cite:N]`.

        If `artifact_id` is already registered, returns the existing seq
        without mutating label/kind (first-write wins).
        """
        existing = self._by_artifact.get(artifact_id)
        if existing is not None:
            return existing
        seq = len(self._entries) + 1
        citation = Citation(
            seq=seq,
            artifact_id=artifact_id,
            label=label,
            kind=kind,
        )
        self._entries[artifact_id] = citation
        self._by_artifact[artifact_id] = seq
        self._by_seq[seq] = citation
        return seq

    def resolve(self, seq: int) -> Citation | None:
        """Look up citation by 1-based seq. None if out of range (orphan)."""
        citation = self._by_seq.get(seq)
        return None if citation and citation.artifact_id in getattr(self, "_suppressed", set()) else citation

    def all_citations(self) -> list[Citation]:
        """Snapshot of all registered citations in seq order."""
        return [c for c in self._entries.values() if c.artifact_id not in getattr(self, "_suppressed", set())]

    def bind_kb_check(self, artifact_id: str, generation_id: str | None, check_state: str, *, allowed: bool) -> None:
        citation = self._entries.get(artifact_id)
        if citation is None:
            return
        if not allowed:
            if not hasattr(self, "_suppressed"):
                self._suppressed: set[str] = set()
            self._suppressed.add(artifact_id)
        updated = replace(citation, kb_generation_id=generation_id, kb_source_check_state=check_state)
        self._entries[artifact_id] = updated
        self._by_seq[citation.seq] = updated

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    def size(self) -> int:
        return len(self._entries)


def ensure_registry(parent_ctx: object) -> TurnCitationRegistry:
    """Lazy-init registry on `parent_ctx.turn_citation_registry`.

    Tolerant of frozen contexts (e.g. immutable test ctxes) — best-effort
    setattr, then return a transient registry if attach failed. Transient
    registries no-op gracefully (text passes through `_resolve_citations`
    unchanged when no rewrites needed).
    """
    existing = getattr(parent_ctx, "turn_citation_registry", None)
    if isinstance(existing, TurnCitationRegistry):
        return existing
    registry = TurnCitationRegistry()
    try:  # noqa: SIM105
        parent_ctx.turn_citation_registry = registry
    except (AttributeError, TypeError):
        pass  # frozen ctx — return transient (no persistence)
    return registry
