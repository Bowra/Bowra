from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_SOURCE_ROOT = Path(__file__).parents[1] / "src" / "bowhead_core"
_LEGACY_NAMES = {"compute", "consultant", "canvas-writer", "marketing-analyst"}

# This is a shrinking inventory, not an approval list.  M3-13E removes these
# entries as exact behavior contracts replace each branch.  The subset check
# lets deletions pass while rejecting any new spelling or additional copy.
_ALLOWED_DIRECT_COMPARISONS = Counter(
    {
        ("agent_capability_catalog.py", "manifest.name in {'lead', 'canvas-writer'}"): 1,
        ("agents/lead_orchestrator.py", "profile == 'compute'"): 1,
        ("agents/lead_orchestrator.py", "profile == 'consultant'"): 1,
        ("agents/lead_orchestrator.py", "profile == 'marketing-analyst'"): 1,
        ("agents/lead_orchestrator.py", "profile == 'canvas-writer'"): 1,
        ("deck_render_inputs.py", "mode == 'consultant'"): 1,
        ("deck_render_inputs.py", "mode != 'consultant'"): 1,
        ("deck_render_inputs.py", "i.arguments.get('profile') == 'consultant'"): 1,
        ("subagent.py", "profile in ('compute', 'consultant')"): 1,
        ("subagent.py", "getattr(profile, 'name', '') == 'marketing-analyst'"): 1,
        ("subagent.py", "profile_name in ('lead', 'analyst', 'compute')"): 1,
        ("subagent.py", "profile == 'consultant'"): 1,
        ("subagent_capability_resolver.py", "tool.risk in {'read', 'compute', 'artifact_write'}"): 1,
        # As above: compute is a tool risk level, not an Agent name (849cb8df0 reference-receiver metadata validation).
        (
            "reference_receiver_metadata.py",
            "self.risk not in {'read', 'compute', 'artifact_write', 'external_write', 'destructive', 'unknown'}",
        ): 1,
    },
)
_ALLOWED_PROFILE_DISPATCHES = Counter(
    {
        ("agents/lead_orchestrator.py", "compute"): 2,
    },
)
_ALLOWED_INDIRECT_COMPARISONS = Counter(
    {
        ("plan_authoring.py", "a.agent_name in NON_PLANNABLE_AGENTS"): 1,
        ("plan_authoring.py", "a.agent_name not in NON_PLANNABLE_AGENTS"): 1,
        # Sole check that finalizes retired-agent steps in old plans as skipped (replaces the original 9
        # `agent_name == 'canvas-writer'` branches in lead_orchestrator, a net reduction).
        ("plan_store.py", "str(agent_name or '') in RETIRED_PLAN_AGENTS"): 1,
    },
)


def test_runtime_agent_name_gate_inventory_can_only_shrink() -> None:
    direct: Counter[tuple[str, str]] = Counter()
    dispatches: Counter[tuple[str, str]] = Counter()
    indirect: Counter[tuple[str, str]] = Counter()

    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assigned_legacy_collections = _assigned_legacy_collections(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                expression = ast.unparse(node)
                if _string_literals(node) & _LEGACY_NAMES:
                    direct[(relative, expression)] += 1
                elif {item.id for item in ast.walk(node) if isinstance(item, ast.Name)} & assigned_legacy_collections:
                    indirect[(relative, expression)] += 1
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "profile" and isinstance(value, ast.Constant) and value.value in _LEGACY_NAMES:
                        dispatches[(relative, value.value)] += 1

    assert not direct - _ALLOWED_DIRECT_COMPARISONS
    assert not dispatches - _ALLOWED_PROFILE_DISPATCHES
    assert not indirect - _ALLOWED_INDIRECT_COMPARISONS


def _assigned_legacy_collections(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        if not (_string_literals(node.value) & _LEGACY_NAMES):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        names.update(
            item.id
            for target in targets
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and item.id.upper() == item.id and any(token in item.id for token in ("AGENT", "PROFILE", "MANIFEST"))
        )
    return names


def _string_literals(node: ast.AST) -> set[str]:
    return {item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)}
