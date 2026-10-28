"""Manifest boundaries for Lead-owned specialist dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFESTS_SYSTEM_DIR = _REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "manifests" / "system"


def _load_manifest(name: str) -> dict:
    p = _MANIFESTS_SYSTEM_DIR / f"{name}.yaml"
    if not p.is_file():
        pytest.skip(f"manifest not found: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def test_financial_analyst_no_peer_callable() -> None:
    """I-W2-1 (CHANGE-W2.1): financial-analyst.callable_peers must be []."""
    manifest = _load_manifest("financial-analyst")
    peers = manifest.get("callable_peers", []) or []
    assert peers == [], (
        f"financial-analyst.callable_peers must be [] post-chat-v4 W2.1 migration "
        f"(I2 1-hop invariant). Got: {peers!r}. "
        f"Cross-domain analysis must route via Lead (parallel spawn), not peer-to-peer."
    )


def test_all_lead_callable_peers_have_empty_callable_peers() -> None:
    """I2 sweep: every manifest Lead can call must itself have callable_peers: []."""
    lead = _load_manifest("lead")
    lead_peers = lead.get("callable_peers", []) or []
    assert lead_peers, "lead.yaml missing callable_peers"

    violations: list[str] = []
    for peer_name in lead_peers:
        peer_path = _MANIFESTS_SYSTEM_DIR / f"{peer_name}.yaml"
        if not peer_path.is_file():
            violations.append(f"missing manifest: {peer_path}")
            continue
        peer = yaml.safe_load(peer_path.read_text(encoding="utf-8"))
        peer_peers = peer.get("callable_peers", []) or []
        if peer_peers:
            violations.append(f"{peer_name}.callable_peers={peer_peers} (must be [] per I2 1-hop)")

    assert not violations, "I2 1-hop violations:\n" + "\n".join(violations)
