/**
 * Agent → identity mapping for the plan card. RECONCILED (P4b) onto the same
 * 10-agent model the chat trace uses (`step-list/agent-meta`), so one agent is
 * the SAME colour + icon in the plan card and in its trace block (design-review
 * #5 — previously the card collapsed 10 agents into 5 `--role-*` roles, so e.g.
 * compute and business showed the same colour). Colours come from the pal-a
 * `--ag-<key>` tokens (auto-adapt to dark); the chip tint is a color-mix so all
 * ten agents get a consistent soft background without per-key tint vars.
 *
 * `roleForAgent` keeps its shape so PlanCard.tsx is unchanged. `RoleKey` is kept
 * as a legacy exported alias (index.ts re-exports it; no other consumer).
 */

import { agentDisplayName, agentKeyFor, agentMetaFor, type AgentKey } from "../step-list/agent-meta";
import type { PlanIconName } from "./icons";

/** @deprecated legacy 5-role union — superseded by the 10-agent AgentKey. */
export type RoleKey = "legal" | "finance" | "data" | "kb" | "writer" | "generic";

export interface RoleDef {
  /** the resolved 10-agent key (was a 5-role RoleKey). */
  key: string;
  icon: PlanIconName;
  /** CSS value for the icon colour (an `--ag-*` token). */
  colorVar: string;
  /** CSS value for the chip tint background. */
  tintVar: string;
  /** @deprecated Use `planAgentLabel()`. This `plan_card.role_*` dictionary disagrees with chat's
   *  `agentDisplayName`, duplicates compute/business names, and covers only 5/10 agents.
   *  Retain the field only to preserve RoleDef's shape (graph icons/colors still use roleForAgent). */
  labelKey: string | null;
}

/** Per-agent identity colour token (pal-a long-name aliases; all dark-aware). */
const AG_COLOR: Record<AgentKey, string> = {
  lead: "var(--ag-lead)",
  compute: "var(--ag-compute)",
  kb: "var(--ag-kb)",
  writer: "var(--ag-writer)",
  financial: "var(--ag-financial)",
  business: "var(--ag-business)",
  compliance: "var(--ag-compliance)",
  consultant: "var(--ag-consultant)",
  marketing: "var(--ag-marketing)",
  configuration: "var(--c-ink-3)",
  coach: "var(--c-ink-3)",
  generic: "var(--c-ink-3)",
};

/** Preserve the existing plan_card i18n role labels where an agent maps to one;
 *  the rest fall back to the raw agent name (unchanged from the 5-role version). */
const LABEL_KEY: Partial<Record<AgentKey, string>> = {
  compliance: "role_legal",
  financial: "role_finance",
  compute: "role_data",
  business: "role_data",
  kb: "role_kb",
  writer: "role_writer",
};

/**
 * Display name for an Agent in the plan card — **the same function as chat execution disclosure**.
 *
 * chat-ux-2026-08 plan integration batch. Previously cards used `LABEL_KEY` → `plan_card.role_*`,
 * an **independent dictionary** disagreeing with external disclosure's `agentDisplayName`. In one turn,
 * roughly 200px apart, the same Agent had two names (real-browser evidence):
 *
 * | agent            | Inside (old) | Outside      |
 * | financial-analyst| 财务审计     | 财务         |
 * | compute          | 经营数据分析 | 主分析       |
 * | kb               | 知识库检索   | KB 研究      |
 * | canvas-writer    | 报告撰写     | 报告编辑     |
 *
 * The old dictionary also had two more serious problems:
 *  ① **Name collisions** — `LABEL_KEY` mapped both `compute` and `business` to `role_data`,
 *    so compute and business-analyst **had the same name** in plan cards;
 *  ② Only 5 buckets covered; `lead`/`consultant`/`marketing`/`generic` lacked labelKey,
 *    exposing raw English slugs ("consultant") inside cards;
 *  ③ Ignored **team-configured** subagent names (added by the built-in subagent admin sprint):
 *    renaming financial-analyst to “王会计” was recognized outside the card but not inside.
 *
 * `agentDisplayName` solves all three: configured names first → coverage of all 10 Agents → shared disclosure source.
 * Therefore **remove the redundant dictionary** rather than replace it with another.
 */
export function planAgentLabel(agent: string, configuredDisplayName?: string | null): string {
  return agentDisplayName(agent, configuredDisplayName);
}

export function roleForAgent(agent: string): RoleDef {
  const key = agentKeyFor(agent);
  const colorVar = AG_COLOR[key];
  return {
    key,
    icon: agentMetaFor(agent).icon,
    colorVar,
    tintVar:
      key === "generic"
        ? "var(--c-surface-2)"
        : `color-mix(in oklab, ${colorVar} 14%, var(--c-surface))`,
    labelKey: LABEL_KEY[key] ?? null,
  };
}
