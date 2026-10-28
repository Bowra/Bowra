"""bowhead_core.agents.prompts — Lead orchestrator prompt builders.

Sprint θ Phase 8b-1: introduces Python module prompt builders alongside the
existing `system/` directory (which holds markdown system prompts for legacy
specialists like compute / financial-analyst / canvas-writer).

Modules:
- `lead`: XML-tagged Lead system prompt builder (≤1200 tok worst-case)
- `active_facts`: δ active facts block builder (G3 wire #1)

Note: `system/` subdir continues to hold markdown files for non-Lead specialists
loaded via manifest.system.file. Lead orchestrator does NOT use system.file —
it builds prompt in-memory per-turn (per F2 fix) to bypass concurrent-turn
file race.
"""
