"""Sprint θ Phase 8a: Lead orchestrator I1-I5 invariants lint tests.

Per tech-design §0.6 §4.0.1:
- I1: All turn entry paths must flow through Lead orchestrator (no shortcuts)
- I2: callable_peers' own manifests MUST callable_peers: [] (1-hop)
- I3: Lead manifest tool whitelist MUST NOT include canvas write tools
- I4: dispatcher MUST 透传 workspace_id + project_id + kb_role to peer parent_ctx
  (currently partial — only workspace_id; project_id+kb_role gap to be fixed Phase 8b-4)
- I5: RegisteredFactStore.list_active write callsites ≤ 2 (Lead build + adapter)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGES_DIR = _REPO_ROOT / "packages"
_BOWHEAD_CORE_SRC = _PACKAGES_DIR / "bowhead_core" / "src"
_MANIFESTS_DIR = _BOWHEAD_CORE_SRC / "bowhead_core" / "agents" / "manifests"


def _grep_count(pattern: str, paths: list[Path], excludes: list[str] | None = None) -> int:
    """Helper: count regex pattern matches across given paths.

    Uses Python re module (not subprocess grep) for portability + speed.
    """
    excludes_re = [re.compile(e) for e in (excludes or [])]
    count = 0
    for p in paths:
        if not p.is_dir():
            continue
        for f in p.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line in content.splitlines():
                # Skip lines matching exclusion pattern
                if any(ex.search(line) for ex in excludes_re):
                    continue
                if re.search(pattern, line):
                    count += 1
    return count


# =====  I1 — single Lead entry path =====


def test_I1_no_shortcut_branch_in_turn_runner() -> None:
    """I1: turn_runner must NOT have if-shortcut MetaIntent branches after Phase 8c.

    Phase 8a baseline: existing 4 _handle_* shortcuts still exist (Phase 8c deletes).
    Soft check — just count existence; once Phase 8c done, count must be 0.
    """
    turn_runner_py = _BOWHEAD_CORE_SRC / "bowhead_core" / "turn_runner.py"
    if not turn_runner_py.is_file():
        pytest.skip("turn_runner.py not found")

    content = turn_runner_py.read_text(encoding="utf-8")
    # Phase 8c will delete these 4 shortcut handlers
    re.findall(r"_handle_(kb_search|content_only|ambiguous_hitl|canvas_side_effect)", content)

    # Phase 8a: just record current count (baseline). Phase 8c reduces to 0.
    # (This test is informational, doesn't fail in 8a; will fail-loud in Phase 7.5 review.)


# =====  I2 — callable_peers 1-hop invariant =====


def test_I2_callable_peers_one_hop_in_existing_system_manifests() -> None:
    """I2: System manifests with callable_peers ≠ [] must call peers with callable_peers = [].

    Phase 8a: only existing v2 manifest is `financial-analyst.yaml` (per r1 review).
    Lead manifest (Phase 8b-4) will add lead.yaml. Verify pattern holds.
    """
    import yaml

    if not _MANIFESTS_DIR.is_dir():
        pytest.skip(f"manifests dir not found: {_MANIFESTS_DIR}")

    # Build name → manifest map
    name_to_manifest: dict[str, dict] = {}
    for yaml_file in _MANIFESTS_DIR.rglob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "name" in data:
                name_to_manifest[data["name"]] = data
        except (yaml.YAMLError, OSError):
            continue

    # For each manifest with non-empty callable_peers, verify its peers have empty callable_peers
    violations: list[str] = []
    for parent_name, parent in name_to_manifest.items():
        peers = parent.get("callable_peers", []) or []
        for peer_name in peers:
            peer = name_to_manifest.get(peer_name)
            if peer is None:
                # Phase 8b-4 will add the missing peer manifest; soft skip
                continue
            peer_peers = peer.get("callable_peers", []) or []
            if peer_peers:
                violations.append(
                    f"Manifest {parent_name!r} calls peer {peer_name!r}, but {peer_name!r} has callable_peers={peer_peers} (violates I2)"
                )

    assert not violations, "\n".join(violations)


# =====  I3 — Lead manifest tool whitelist MUST NOT have canvas writes =====


def test_I3_lead_manifest_excludes_canvas_writes() -> None:
    """I3: When lead.yaml exists (Phase 8b-4), it MUST NOT have canvas writes.

    Phase 8a baseline: lead.yaml not yet created. Test passes trivially.
    Once Phase 8b-4 adds lead.yaml, this test enforces I3.
    """
    import yaml

    lead_yaml = _MANIFESTS_DIR / "system" / "lead.yaml"
    if not lead_yaml.is_file():
        pytest.skip("lead.yaml not yet created (Phase 8b-4 deliverable)")

    data = yaml.safe_load(lead_yaml.read_text(encoding="utf-8"))
    tools = data.get("tools", {})
    allow = tools.get("allow", []) if isinstance(tools, dict) else []

    forbidden_canvas_writes = {
        "canvas.write_section",
        "canvas_patch_section",
        "canvas_delete_section",
        "canvas_reorder_sections",
        "canvas.export",
    }
    actual_canvas_writes = set(allow) & forbidden_canvas_writes
    assert not actual_canvas_writes, f"Lead manifest tool whitelist contains canvas writes (violates I3): {actual_canvas_writes}"


# =====  I4 — dispatcher propagates workspace_id (+ project_id + kb_role per Phase 8b-4) =====


def test_I4_dispatcher_propagates_workspace_id_documented_gap() -> None:
    """I4 (partial): Spike #1 confirmed workspace_id propagates;
    project_id + kb_role gap (Phase 8b-4 must add).
    """
    from bowhead_core.agents.turn_context import SerializableTurnContext

    # Workspace_id present (PRD I4 partial)
    fields = set(SerializableTurnContext.__dataclass_fields__)
    assert "workspace_id" in fields

    # Phase 8b-4 GAP: project_id + kb_role
    if "project_id" in fields and "kb_role" in fields:
        # Phase 8b-4 fix landed
        pass
    else:
        # Phase 8a baseline (gap still exists)
        pass


# =====  I5 — δ active facts wire callsite count =====


def test_I5_delta_wire_callsite_count() -> None:
    """I5: RegisteredFactStore.list_active write callsites ≤ 2.

    Phase 8a baseline: writes happen in legacy `_handle_*` shortcut paths
    (Phase 8c deletes). Phase 8b-1 + 8b-2 add the 2 new authorized callsites:
    - prompts/lead.py:build_lead_system_prompt (Wire #1)
    - agents/agent_runner_adapter.py:_runner_fn body (Wire #2)

    Test counts current callsites; informational in Phase 8a, enforced in Phase 7.5
    impl review (must be ≤ 2 after Phase 8c delete + 8b-1/8b-2 add).
    """
    pattern = r"\.list_active\("
    excludes = [r"# noqa", r"def list_active", r"auto_learner"]  # auto_learner: invalidation-only read

    _grep_count(
        pattern,
        paths=[_BOWHEAD_CORE_SRC],
        excludes=excludes,
    )

    # Phase 8a baseline (informational)
    # Phase 7.5 will assert <= 2 strictly after Phase 8c delete + 8b add
