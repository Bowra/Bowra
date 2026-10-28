"""chat-v4 W2 — Lead.yaml callable_peers must include 3 specialists.

Per docs/chat-v4-prd.md §6 invariant I-W2-2 + tech-design CHANGE-W2.2:
- lead.yaml callable_peers MUST include financial-analyst, business-analyst,
  compliance-analyst (3 specialist peers wire-up).
- Each must have a corresponding .md system file in agents/manifests/system/.

Also asserts tools.allow contains web_search per CHANGE-W1.2.

See:
- packages/bowhead_core/src/bowhead_core/agents/manifests/system/lead.yaml
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFESTS_SYSTEM_DIR = _REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "manifests" / "system"
# .md system prompts live alongside the prompt builders, not next to the yaml
# manifests (see prompts/system/*.md). This matches the manifest loader pattern.
_PROMPTS_SYSTEM_DIR = _REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core" / "agents" / "prompts" / "system"


def _load_lead() -> dict:
    p = _MANIFESTS_SYSTEM_DIR / "lead.yaml"
    if not p.is_file():
        pytest.skip(f"lead.yaml not found: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def test_v4_specialist_peers_in_lead_yaml() -> None:
    """CHANGE-W2.2: lead.yaml callable_peers must include 3 specialist peers."""
    lead = _load_lead()
    peers = lead.get("callable_peers", []) or []
    required = {"financial-analyst", "business-analyst", "compliance-analyst"}
    missing = required - set(peers)
    assert not missing, f"lead.yaml callable_peers missing chat-v4 W2 specialists: {sorted(missing)}. Current: {peers!r}"


def test_specialist_md_files_exist() -> None:
    """I-W2-2: every specialist must have a .md system file (run-time load)."""
    missing: list[str] = []
    for name in ("financial-analyst", "business-analyst", "compliance-analyst"):
        md_path = _PROMPTS_SYSTEM_DIR / f"{name}.md"
        if not md_path.is_file():
            missing.append(str(md_path))
    assert not missing, "chat-v4 W2 specialist .md system file(s) missing — manifests cannot load:\n" + "\n".join(missing)


def test_web_search_in_lead_tools_allow() -> None:
    """CHANGE-W1.2: lead.yaml tools.allow must include web_search."""
    lead = _load_lead()
    allow = (lead.get("tools") or {}).get("allow", []) or []
    assert "web_search" in allow, f"lead.yaml tools.allow missing web_search (chat-v4 W1.2). Current: {allow!r}"
