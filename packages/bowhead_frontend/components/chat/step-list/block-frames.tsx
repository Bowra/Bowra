"use client";

/**
 * block-frames.tsx — multi-agent composition wrappers (§7/§9 of the
 * block-model-spec):
 *
 *   MultiAgentGroup → neutral participant group with no topology claim
 *   Prose         → live peer-prose lane (streaming italic quote)
 *
 * Class/token-driven; labels come from the production message catalog.
 */
import { type ReactNode, useState } from "react";
import { useTranslations } from "next-intl";

/* ── neutral multi-agent group ────────────────────────────────────────── */
export interface MultiAgentGroupProps {
  /** Number of visible peer participants. This does not imply concurrency. */
  n: number;
  children?: ReactNode;
}

export function MultiAgentGroup({ n, children }: MultiAgentGroupProps) {
  const t = useTranslations("step_list");
  return (
    <div className="cframe" data-group-kind="multi-agent" data-agent-count={n}>
      <div className="cframe-head">
        <span className="cframe-glyph">{t("n_agents_participated", { n })}</span>
      </div>
      {children}
    </div>
  );
}

/* ── live peer-prose lane ─────────────────────────────────────────────── */

/** chat-ux-2026-08 batch A — collapse by default above this length.
 *
 *  This lane renders collaborating Agents' self-description, but models often put **internal reasoning** into it.
 *  Exact excerpt observed on 116: “让我换个方式——去掉 `delivery_surface` 字段, 走标准
 *  report payload…根据铁律 8, 我需要改变 query shape…`soft_refuse` 似乎是因为
 *  同样的 `subagent_return` 被调用了太多次” — an entire internal-protocol monologue directly on screen,
 *  **previously without truncation or folding**, filling the whole left column with one passage.
 *
 *  Avoid keyword filtering (whack-a-mole); change **presentation weight**: long self-descriptions start collapsed, expanding on request. */
const PROSE_COLLAPSE_CHARS = 140;

export function Prose({ children }: { children: ReactNode }) {
  const t = useTranslations("step_list");
  const [showAll, setShowAll] = useState(false);
  // Only plain text has a measurable length for folding; render non-string children unchanged (no guessing).
  const text = typeof children === "string" ? children : null;
  const collapsible = text !== null && text.length > PROSE_COLLAPSE_CHARS;
  const clipped = collapsible && !showAll;

  return (
    <div className={`prosewrap${clipped ? " is-clipped" : ""}`}>
      <div className="prose">“{children}”</div>
      {collapsible && (
        <button
          type="button"
          className="prose-more"
          onClick={() => setShowAll((v) => !v)}
          aria-expanded={showAll}
        >
          {showAll ? t("collapse") : t("code_show_all")}
        </button>
      )}
    </div>
  );
}
