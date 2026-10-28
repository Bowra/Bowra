"""report-engine Phase-2 WP-F.3 — runtime trust-class gate for canvas write tools.

Asserts the registry-driven gate: write-trust peers pass, every
read-mostly/read-only/unknown peer is rejected (fail-closed). The live tool it
guards (`remove_chart`) is exercised end to end in test_canvas_revise_b2."""

from __future__ import annotations

import pytest
from bowhead_core.agents._lead_context import (
    reset_current_peer_name,
    set_current_peer_name,
)
from bowhead_core.agents._write_guard import (
    WRITE_TRUST_CLASSES,
    check_write_trust_class,
    peer_trust_class,
    reset_trust_class_cache,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_trust_class_cache()
    yield
    reset_trust_class_cache()


def _as(peer: str):
    return set_current_peer_name(peer)


# canvas-writer (the sole write-leaf peer) was removed in c4d50b03b; positive write-entry cases now use an active
# report-writer peer。
_WRITE_PEER = "business-analyst"


# ── trust-class lookup ───────────────────────────────────────
def test_lead_and_consultant_are_not_write_trust() -> None:
    # After delivery-surface M-C, lead / consultant remain read-mostly, outside write trust classes.
    for name in ("lead", "consultant"):
        assert peer_trust_class(name) not in WRITE_TRUST_CLASSES


def test_report_agents_are_report_writer_class() -> None:
    """M-C 发笔：六个报告 agent 落 WP-F 预留的 report-writer 档（trust 门放行）。"""
    for name in ("compute", "kb-analyst", "financial-analyst", "business-analyst", "compliance-analyst", "marketing-analyst"):
        assert peer_trust_class(name) == "report-writer"
        assert "report-writer" in WRITE_TRUST_CLASSES


def test_unknown_peer_fails_closed() -> None:
    # a CCA custom/team subagent name is NOT in the system registry → None →
    # never a write trust class (the security default)
    assert peer_trust_class("sub_made_up_custom") is None


# ── check_write_trust_class ──────────────────────────────────
def test_check_allows_write_trust_peer() -> None:
    tok = _as(_WRITE_PEER)
    try:
        assert check_write_trust_class() is None
    finally:
        reset_current_peer_name(tok)


@pytest.mark.parametrize("peer", ["lead", "consultant", "sub_unknown_custom"])
def test_check_rejects_non_write_peers(peer: str) -> None:
    tok = _as(peer)
    try:
        err = check_write_trust_class()
        assert err is not None and err["error"] == "i3_violation"
    finally:
        reset_current_peer_name(tok)
