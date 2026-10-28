/**
 * AgentBadge — the single-glyph agent mark used in block headers, provenance
 * chains and parallel/fold rows. A fill square coloured by `--ag` with a
 * language-neutral line icon. Custom / non-system agents fall back to the
 * name's first letter on a stable hashed hue. Decorative (`aria-hidden`); identity is conveyed in
 * text by the surrounding header / provenance label.
 */
import type { CSSProperties } from "react";
import { PlanIcon } from "../plan-card/icons";
import {
  agentKeyFor,
  agentMetaFor,
  customAgentHue,
  customAgentInitial,
  isCustomAgent,
} from "./agent-meta";

export interface AgentBadgeProps {
  /** backend agent_name — `compute`, `financial-analyst`, or a custom name. */
  agent: string;
  /** badge edge length in px (default 22; provenance/fold rows use 18). */
  size?: number;
}

export function AgentBadge({ agent, size = 22 }: AgentBadgeProps) {
  const key = agentKeyFor(agent);
  const meta = agentMetaFor(agent);
  const custom = key === "generic" && isCustomAgent(agent);

  const style: CSSProperties = { width: size, height: size };
  if (custom) {
    // give a custom agent its own hue instead of the generic accent fallback
    (style as Record<string, string | number>)["--ag"] =
      `oklch(0.52 0.09 ${customAgentHue(agent)})`;
  }

  return (
    <span className={`ag-badge ${meta.cls}`} style={style} aria-hidden="true">
      {custom ? (
        <span className="ag-badge-init" style={{ fontSize: Math.round(size * 0.5) }}>
          {customAgentInitial(agent)}
        </span>
      ) : (
        <PlanIcon name={meta.icon} size={Math.round(size * 0.6)} />
      )}
    </span>
  );
}
