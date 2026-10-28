"""Agent manifest 系统 (sprint multi-agent-v1, PR-#1).

新拓扑核心: Orchestrator + N parallel TaskAgent, 由 manifest 声明替 Python enum
SubagentProfile. 详见 docs/sprints/intellivault-comparison/sprint-multi-agent.md §5.3.

公开接口:
- `AgentManifest` — Pydantic schema (含 trust_class / tools / skills / output_schema)
- `AgentRegistry` — system + user 双源 loader (PR-#1 仅 system, PR-#2 加 user)
- `ManifestSecurityError` / `ManifestValidationError`
"""

from .exceptions import ManifestSecurityError, ManifestValidationError
from .manifest import (
    AgentManifest,
    Example,
    SkillsSpec,
    SystemSpec,
    ToolsSpec,
    TrustClass,
)
from .registry import AgentRegistry

__all__ = [
    "AgentManifest",
    "AgentRegistry",
    "Example",
    "ManifestSecurityError",
    "ManifestValidationError",
    "SkillsSpec",
    "SystemSpec",
    "ToolsSpec",
    "TrustClass",
]
