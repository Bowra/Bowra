"""Resolve the closed set of capability roles against the current roster.

Candidates must pass the role's capability predicate. Prefer a candidate declaring
that primary_capability; otherwise use the first candidate in stable roster order.
Return None when no candidate exists.

Capability and responsibility are distinct: a tool whitelist alone cannot uniquely
assign document responsibilities. This is a closed role mapping, not a dynamic
plugin framework. Keep it outside prompts because predicates may recognize
historical hidden tool names. The legacy BI predicate remains BI-specific.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.agent_capability_catalog import CapabilityCatalog

logger = logging.getLogger(__name__)

# ── Closed role set ────────────────────────────────────────────────────────────────
ROLE_DATA = "data"
ROLE_COMPUTE = "compute"
ROLE_DECK = "deck"
ROLE_KNOWLEDGE = "knowledge"
ROLE_IMAGE_GEN = "image_gen"

ROLES: tuple[str, ...] = (
    ROLE_DATA,
    ROLE_COMPUTE,
    ROLE_DECK,
    ROLE_KNOWLEDGE,
    ROLE_IMAGE_GEN,
)

# Role → **tool-inferred** capabilities (any match enters the candidate pool).
#
# `ROLE_IMAGE_GEN` is deliberately empty: `cap_visualize` groups `add_chart` / `add_image` /
# `image_generate` / `canvas_create_design`, coarser than the text-to-image role; inferring from it
# would treat every chart-capable Agent as an image generator. This role uses `_ROLE_TOOLS` instead.
_ROLE_INFERRED_CAPS: Mapping[str, tuple[str, ...]] = {
    ROLE_DATA: ("cap_data_query",),
    ROLE_COMPUTE: ("cap_compute",),
    ROLE_DECK: ("cap_presentation",),
    ROLE_KNOWLEDGE: ("cap_knowledge_search",),
    ROLE_IMAGE_GEN: (),
}

# Role → primary-responsibility preference. Candidates first pass tool or artifact-contract eligibility.
_ROLE_DECLARED_CAPS: Mapping[str, tuple[str, ...]] = {
    **_ROLE_INFERRED_CAPS,
    ROLE_IMAGE_GEN: ("cap_visualize",),
}

# Text-to-image-contract capability ids, read by `lead_orchestrator`'s image_gen gate;
# single source: do not copy the `cap_visualize` literal elsewhere.
IMAGE_GEN_CONTRACTS: frozenset[str] = frozenset({"cap_visualize"})

# Role → tool-level predicates (any tool match enters candidates), only where catalog granularity is insufficient.
_ROLE_TOOLS: Mapping[str, tuple[str, ...]] = {
    ROLE_IMAGE_GEN: ("image_generate",),
}


def _catalog() -> CapabilityCatalog | None:
    """进程级 catalog（唯一加载点在 `agent_capability_catalog.default_catalog`）。

    返 None 时所有岗位的候选集为空，调用方渲染「没有承担者」话术。这比抛异常好：
    最坏情况是 Lead 说「本团队没有能做这件事的 Agent」（保守但自洽），
    而不是整个 turn 挂掉。
    """
    from bowhead_core.agent_capability_catalog import default_catalog

    return default_catalog()


def reset_catalog_cache_for_test() -> None:
    from bowhead_core.agent_capability_catalog import reset_default_catalog_cache_for_test

    reset_default_catalog_cache_for_test()


def active_capability_ids() -> frozenset[str]:
    """Active catalog IDs used to validate admin responsibility overrides."""

    cat = _catalog()
    if cat is None:
        return frozenset()
    return frozenset(definition.id for definition in cat.definitions if definition.lifecycle == "active")


def agent_capabilities(
    tool_names: Collection[str],
    *,
    output_formats: Collection[str] = (),
) -> frozenset[str]:
    """Derive routing evidence from tools and code-owned output contracts.

    Output formats come from the system manifest, never from an Agent's name
    or its chosen responsibility. Exact execution authorization still applies.
    """
    cat = _catalog()
    if cat is None:
        return frozenset()
    from bowhead_core.agent_capability_catalog import infer_capabilities_from_tools

    capabilities = {cid for cid, _ in infer_capabilities_from_tools(tool_names, catalog=cat)}
    if "skill_invoke" in tool_names and "pptx" in output_formats:
        capabilities.add("cap_presentation")
    return frozenset(capabilities)


def supported_primary_capabilities(
    tool_names: Collection[str],
    *,
    output_formats: Collection[str] = (),
) -> frozenset[str]:
    """Responsibilities supported by tools and code-owned output contracts."""
    capabilities = set(agent_capabilities(tool_names, output_formats=output_formats))
    capabilities.add("cap_analysis")
    if "image_generate" not in tool_names:
        capabilities.discard("cap_visualize")
    assignable = {capability for values in _ROLE_DECLARED_CAPS.values() for capability in values} | {"cap_analysis"}
    return frozenset(capabilities & assignable & active_capability_ids())


def agent_fills_role(
    role: str,
    *,
    capabilities: Collection[str],
    tool_names: Collection[str],
    primary_capability: str = "",
) -> bool:
    """Qualification precedes preference; a responsibility grants no ability."""
    inferred = _ROLE_INFERRED_CAPS.get(role, ())
    if inferred and any(c in capabilities for c in inferred):
        return True
    tools = _ROLE_TOOLS.get(role)
    return bool(tools) and any(t in tool_names for t in tools)


@dataclass(frozen=True)
class RoleCandidate:
    """花名册里的一个 Agent，带岗位解析需要的三样东西。"""

    name: str
    capabilities: frozenset[str] = frozenset()
    tool_names: frozenset[str] = frozenset()
    # This Agent's **contract** (`primary_capability`), not incidental abilities.
    # Empty = undeclared; only roster order provides fallback.
    primary_capability: str = ""
    # Whether an Agent with no declared contract can be **drafted** as a role fallback.
    #
    # Platform built-ins always False (caller policy: `peer_block._peer_to_candidate`):
    # built-ins are curated; missing `primary_capability` is **misconfiguration**, not permission to dispatch any work.
    # Without this gate, a newly added built-in lacking a declaration would be drafted for data fetching;
    # `kb-analyst` / `consultant` / all three specialists have **`bi_query` in their allowlists**,
    # routing BI requests to a document-research agent.
    # Team/user custom Agents default True: missing declarations are normal, and they serve as substitutes.
    draftable: bool = True


@dataclass(frozen=True)
class PromptRoster:
    """本轮花名册解析出的岗位承担者 —— 提示词渲染的**唯一**名字来源。

    `names` 是判据 1 的对象：不在这个集合里的 Agent 名，整份提示词里必须出现 0 次。
    """

    holders: Mapping[str, str | None]
    names: frozenset[str]

    def holder(self, role: str) -> str | None:
        """该岗位本轮的承担者名；None = 花名册里没人能做这件事。"""
        return self.holders.get(role)

    def has(self, role: str) -> bool:
        return self.holders.get(role) is not None


_EMPTY_ROSTER = PromptRoster(holders=dict.fromkeys(ROLES), names=frozenset())


def resolve_roles(candidates: Sequence[RoleCandidate]) -> PromptRoster:
    """花名册 → 每个岗位的承担者。

    顺序即优先级：`_gather_lead_peers` 已按 system > team > user 排好，
    system 内部的顺序来自 `lead.yaml` 的 `callable_peers`。
    """
    if not candidates:
        return _EMPTY_ROSTER
    holders: dict[str, str | None] = {}
    for role in ROLES:
        pool = [
            c
            for c in candidates
            if agent_fills_role(
                role,
                capabilities=c.capabilities,
                tool_names=c.tool_names,
                primary_capability=c.primary_capability,
            )
        ]
        wanted = _ROLE_DECLARED_CAPS.get(role, ())
        declared = [c for c in pool if c.primary_capability and c.primary_capability in wanted]
        # ⚠ Fallback must **exclude Agents declaring another role contract**.
        #
        # This generalizes the observation in `pick_data_peer`'s original comment: in production,
        # `kb-analyst` / `consultant` / all three specialists have **`bi_query` in their allowlists**;
        # without exclusion, teams disabling `compute` would render the always-spawn-X-for-data instruction with
        # `kb-analyst` as X, assigning BI requests to a document-research agent.
        # Holding a tool does not make the task its responsibility; an Agent claiming another contract is occupied.
        # Only undeclared Agents (normal for team custom Agents) are truly draftable fallback candidates.
        unclaimed = [c for c in pool if not c.primary_capability and c.draftable]
        chosen = declared[0] if declared else (unclaimed[0] if unclaimed else None)
        holders[role] = chosen.name if chosen else None
    # The built-in compute profile intentionally owns data-query while also
    # carrying verified Python compute. Reuse the already-selected data holder
    # for compute only when its inferred capabilities prove both; a Power
    # BI-only replacement therefore never receives Python/export work.
    if holders.get(ROLE_COMPUTE) is None and holders.get(ROLE_DATA):
        data_candidate = next(
            (candidate for candidate in candidates if candidate.name == holders[ROLE_DATA]),
            None,
        )
        if data_candidate is not None and "cap_compute" in data_candidate.capabilities:
            holders[ROLE_COMPUTE] = data_candidate.name
    return PromptRoster(holders=holders, names=frozenset(c.name for c in candidates))


def build_candidate(
    name: str,
    tool_names: Iterable[str] | None,
    primary_capability: str = "",
    *,
    draftable: bool = True,
    output_formats: Collection[str] = (),
) -> RoleCandidate:
    """从一个 peer 的工具白名单造候选项（能力现算）。"""
    tools = frozenset(str(t) for t in (tool_names or ()) if t)
    return RoleCandidate(
        name=name,
        capabilities=agent_capabilities(tools, output_formats=output_formats),
        tool_names=tools,
        primary_capability=primary_capability or "",
        draftable=draftable,
    )


def manifest_primary_capability(manifest: Any) -> str:
    """读 manifest 的 `primary_capability`，缺失/异常 → ""（靠花名册顺序兜底）。"""
    return str(getattr(manifest, "primary_capability", "") or "")


__all__ = [
    "IMAGE_GEN_CONTRACTS",
    "ROLES",
    "ROLE_COMPUTE",
    "ROLE_DATA",
    "ROLE_DECK",
    "ROLE_IMAGE_GEN",
    "ROLE_KNOWLEDGE",
    "PromptRoster",
    "RoleCandidate",
    "active_capability_ids",
    "agent_capabilities",
    "agent_fills_role",
    "build_candidate",
    "manifest_primary_capability",
    "reset_catalog_cache_for_test",
    "resolve_roles",
    "supported_primary_capabilities",
]
