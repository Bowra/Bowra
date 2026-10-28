"""turn_runner_v2_entry — manifest bootstrap for multi-agent system.

Sprint θ Phase 8c-B-3: trimmed. The original `run_turn_v2` entry (which dispatched
to `orchestrate_v2`) has been removed because Phase 8c-B-1 hard-routes all turns
through `lead_orchestrator.run_turn`. The remaining function `bootstrap_multi_agent`
is still load-bearing — gateway calls it at startup to register manifest-driven
SubagentProfile entries from system manifests into
the global PROFILES dict so SubagentRunner can find them.

Wire-up sequence (gateway lifespan startup):
1. AgentRegistry.bootstrap() — load N system manifests
2. register_all_system_manifests(registry) — inject manifest profiles into PROFILES
3. register_report_tools(tool_registry) — register read_report / write_report (WP-E
   default-ON writer-loop tools; REPLACED the retired canvas_add_section/compose_final)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_runner_adapter import register_all_system_manifests
from .registry import AgentRegistry
from .report_tools import register_report_tools, write_report_enabled

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolRegistry


def bootstrap_multi_agent(tool_registry: ToolRegistry) -> AgentRegistry:
    """Startup-time wire-up. Returns AgentRegistry singleton.

    Caller (gateway lifespan):
        from bowhead_core.agents.turn_runner_v2_entry import bootstrap_multi_agent
        registry = bootstrap_multi_agent(tool_registry)
        orchestrator.multi_agent_registry = registry

    Steps:
    1. AgentRegistry.bootstrap() — load system manifests
    2. register_all_system_manifests(registry) — inject into PROFILES
    3. register_report_tools(tool_registry) — register read_report/write_report
       (flag-gated, WP-E default ON)
    """
    registry = AgentRegistry.bootstrap()
    register_all_system_manifests(registry)
    # report-engine Phase-2 WP-E: register read_report/write_report (default ON).
    # These REPLACE the retired canvas_add_section/canvas_compose_final (the F12
    # double-write path). Set BOWHEAD_WRITE_REPORT=0 to roll back (tools not
    # registered → no manifest can resolve them → legacy deterministic layout).
    if write_report_enabled():
        register_report_tools(tool_registry)
    return registry
