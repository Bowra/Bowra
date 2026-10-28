"""report-engine Phase-2 WP-F.3 — runtime trust-class gate for canvas write tools.

Replaces the hard-coded `peer == "canvas-writer"` check (the old I3 guard in
canvas_tools) with a registry-driven `trust_class ∈ WRITE_TRUST_CLASSES` lookup,
and is ALSO applied to `canvas_add_section` / `canvas_compose_final` — which had
no runtime guard at all (re-review §13.4: "trust_class 要运行时门 … 且加到
add_section/compose_final").

Why a runtime gate when manifests already whitelist tools + the CCA fence
(WP-F.1/F.2) strips them from user/team subagents: defense-in-depth. The manifest
whitelist is the primary control, but it's static config that can regress; this
catches a read-mostly peer reaching a write tool at the call site regardless of
how it got there, and it generalizes to the Phase-2 `report-writer` trust class
(WP-D) without re-hardcoding a peer name.

Scope note: the five legacy canvas write tools it was written for (add_section,
compose_final, patch/delete/reorder) are all retired; today it gates
`remove_chart` (canvas_tools), which likewise has no per-section owner model.
It deliberately does NOT gate
`write_report` — that tool's runtime security model is per-section owner
enforcement + the CCA fence + the F.2 holder lint, and the multi-writer model
(WP-D) has read-mostly specialists write their own sections via it (they become
`report-writer` at the manifest layer, enforced by the lint, not here).
"""

from __future__ import annotations

from typing import Any

from ._lead_context import get_current_peer_name

# trust classes permitted to invoke legacy canvas write tools. Mirrors the
# static lint's _WRITE_TRUST_CLASSES (test_report_write_fence): "write-leaf" =
# legacy canvas-writer; "report-writer" = the Phase-2 multi-writer class (WP-D).
WRITE_TRUST_CLASSES = frozenset({"write-leaf", "report-writer"})

_TRUST_BY_PEER: dict[str, str] | None = None


def _trust_class_map() -> dict[str, str]:
    """Lazily cache {system_manifest_name: trust_class}. System manifests are
    static, so build once per process. Custom/team (CCA) subagents are NOT in the
    system registry → a lookup miss → fail-closed (they can never resolve to a
    write trust class), which is the desired security default."""
    global _TRUST_BY_PEER
    if _TRUST_BY_PEER is None:
        from .registry import AgentRegistry

        reg = AgentRegistry.bootstrap()
        _TRUST_BY_PEER = {name: reg.get(name).trust_class for name in reg.list_all()}
    return _TRUST_BY_PEER


def reset_trust_class_cache() -> None:
    """Test hook — drop the cached map so a test that bootstraps a custom
    manifest dir or expects a fresh read isn't served a stale process-wide map."""
    global _TRUST_BY_PEER
    _TRUST_BY_PEER = None


def peer_trust_class(peer_name: str) -> str | None:
    return _trust_class_map().get(peer_name)


def check_write_trust_class() -> dict[str, Any] | None:
    """None if the CURRENT peer (ContextVar) may invoke a canvas write tool; an
    `i3_violation` error dict otherwise. The error code stays `i3_violation` for
    metric (`single_write_leaf_violation`) + existing-test continuity."""
    peer = get_current_peer_name()
    tc = peer_trust_class(peer)
    if tc not in WRITE_TRUST_CLASSES:
        return {
            "error": "i3_violation",
            "reason": (f"canvas write ops require trust_class in {sorted(WRITE_TRUST_CLASSES)}; peer {peer!r} has trust_class={tc!r}"),
        }
    return None
