#!/usr/bin/env tsx
/**
 * V5 PRD-E1 audit script — dictionary coverage check.
 *
 * Usage: npx tsx scripts/audit-tool-labels.ts
 *
 * Cross-check the backend tool_registry's 29 static tool names against lib/tool-labels.ts
 * EMOJI_MAP / LABEL_NAMES_ZH in both directions, reporting coverage.
 *
 * Ship gate: ≥ 90% (current target 100% because all 29 are known).
 *
 * MCP tools (dynamic names mcp.<server>.<tool>) are excluded, using the ⚙️ fallback.
 */

import { auditToolLabelCoverage } from "../lib/tool-labels";

const result = auditToolLabelCoverage();

console.log(
  `Tool label coverage: ${result.coveragePct.toFixed(1)}% (${result.covered}/${result.total})`,
);
if (result.missing.length > 0) {
  console.log("Missing entries (need EMOJI_MAP + LABEL_NAMES_ZH/EN):");
  for (const t of result.missing) {
    console.log(`  - ${t}`);
  }
}

const SHIP_GATE = 90;
if (result.coveragePct < SHIP_GATE) {
  console.error(`\nFAIL: coverage ${result.coveragePct.toFixed(1)}% < ${SHIP_GATE}% ship gate.`);
  process.exit(1);
}
console.log(`\nPASS: coverage ≥ ${SHIP_GATE}% ship gate.`);
process.exit(0);
