"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Chevron } from "./primitives";
import type { PlanItem } from "./types";
import { CharIcon } from "@/components/char-icon";

export interface PlanSectionProps {
  items: PlanItem[];
  /** When true the section starts collapsed. */
  defaultCollapsed?: boolean;
  compact?: boolean;
}

/**
 * Plan section (top of the step list card). Vertical layout is the only
 * variant we ship per tech-design §3.6 + Claude Design v8 recommendation
 * — chips/progress dropped to keep state machine 1-shape.
 *
 * Phase 1 emits this only when backend actually has plan items (task_plan
 * HITL turn). The component is harmless when items=[] (returns null).
 *
 * Phase 1.7-D visual-diff audit against `design/v8/plan-section.jsx` (217 LOC,
 * VerticalList @ lines 74-111): styling matches 1:1 — softbg background,
 * 3px accent border-left, 8px radius, numbered prefix in mono mute2, status
 * icon column @ 16px width, status colors ok/accent/mute2, line-through on
 * skip status, fontSize 13.5px text. chips/progress layouts deferred to
 * Phase 3+ (tech-design §6.2).
 */
export function PlanSection({ items, defaultCollapsed = false, compact = false }: PlanSectionProps) {
  const tStep = useTranslations("step_list");
  const [open, setOpen] = useState(!defaultCollapsed);

  if (!items.length) return null;

  const total = items.length;
  const doneCount = items.filter((i) => i.status === "done").length;
  const doingIdx = items.findIndex((i) => i.status === "doing");

  return (
    <div
      style={{
        margin: "6px 0 18px",
        padding: "10px 14px 12px",
        background: "var(--c-surface-2)",
        border: `1px solid var(--c-line)`,
        borderLeft: `3px solid var(--c-accent)`,
        borderRadius: 8,
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          width: "100%",
          background: "transparent",
          border: 0,
          padding: 0,
          cursor: "pointer",
          font: "inherit",
          color: "inherit",
          textAlign: "left",
        }}
      >
        <span
          style={{
            fontFamily: "var(--f-mono)",
            fontSize: "0.65625rem",
            color: "var(--c-accent)",
            letterSpacing: "0.04em",
            textTransform: "uppercase",
            fontWeight: 600,
          }}
        >
          {tStep("plan_eyebrow")}
        </span>
        <span style={{ fontFamily: "var(--f-sans)", fontSize: "0.75rem", color: "var(--c-ink-4)" }}>
          {tStep("plan_progress", { done: doneCount, total })}
          {doingIdx >= 0 ? ` · ${tStep("plan_doing", { index: doingIdx + 1 })}` : ""}
        </span>
        <span style={{ flex: 1 }} />
        <Chevron open={open} />
      </button>

      {open ? (
        <div style={{ marginTop: 10 }}>
          <div style={{ display: "grid", gap: 3 }}>
            {items.map((item, i) => (
              <PlanRow key={item.id} item={item} index={i} compact={compact} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

interface PlanRowProps {
  item: PlanItem;
  index: number;
  compact?: boolean;
}

function PlanRow({ item, index }: PlanRowProps) {
  const ic = statusIcon(item.status);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "baseline",
        gap: 10,
        padding: "4px 2px",
      }}
    >
      <span
        style={{
          fontFamily: "var(--f-mono)",
          fontSize: "0.6875rem",
          color: "var(--c-ink-4)",
          width: 18,
          textAlign: "right",
          flex: "0 0 18px",
        }}
      >
        {index + 1}.
      </span>
      <span
        style={{
          color: ic.col,
          fontFamily: "var(--f-mono)",
          fontSize: "0.8125rem",
          width: 16,
          flex: "0 0 16px",
          textAlign: "center",
        }}
      >
        <CharIcon ch={ic.ch} />
      </span>
      <span
        style={{
          fontFamily: "var(--f-sans)",
          fontSize: "0.84375rem",
          color: item.status === "done" ? "var(--c-ink-4)" : "var(--c-ink)",
          textDecoration: item.status === "skip" ? "line-through" : "none",
          lineHeight: 1.45,
          flex: 1,
        }}
      >
        {item.text}
      </span>
    </div>
  );
}

function statusIcon(status: PlanItem["status"]): { ch: string; col: string } {
  if (status === "done") return { ch: "✓", col: "var(--c-ok)" };
  if (status === "doing") return { ch: "◐", col: "var(--c-accent)" };
  if (status === "skip") return { ch: "⊘", col: "var(--c-ink-4)" };
  return { ch: "○", col: "var(--c-ink-4)" };
}
