"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";
// turn-chip.tsx—surface B: turn-card “⊕ 固化此流程” chip + preview TurnCard. Ported from Claude Design
// turn-chip.jsx. The chip is the reusable export (lives in a completed turn's action row); TurnCard
// is preview scaffolding for the 4 variants (done shows chip; chat/running/error don't).
import { useTranslations } from "next-intl";
import React from "react";

import { WfIcon } from "./wf-icons";

// ── the reusable solidify chip (quiet secondary action) ──
// `label` **required and must use i18n** (Chinese default previously let both callers omit it →
// English interface showed Chinese, measured on 116 on 2026-08-15). `icon` is overridable: S2's Solidify Entire Conversation
// uses the same shape with different copy/icon (same action family, side by side visually, without another button variant).
export function SolidifyChip({
  onClick,
  label,
  icon = "crystal",
  testId,
}: {
  onClick?: () => void;
  /**
   * ⚠ **Required and must use i18n**. Previously defaulted to Chinese `"固化此流程"`,
   * so both callers omitted it → English interface rendered Chinese (user-reported on 116, 2026-08-15).
   * Removing the default turns missing i18n into a **compile error**, rather than a defect visible only after switching to English.
   */
  label: string;
  icon?: string;
  testId?: string;
}) {
  return React.createElement("button", { onClick, "data-testid": testId, style: { display: "inline-flex", alignItems: "center", gap: 6,
    fontSize: "0.78125rem", fontWeight: 500, color: "var(--c-ink-2)", background: "var(--c-surface)",
    border: "1px solid var(--c-line)", borderRadius: 999, padding: "6px 13px", cursor: "pointer" } },
    React.createElement("span", { style: { color: "var(--c-accent)", display: "inline-flex" } },
      React.createElement(WfIcon, { name: icon, s: 14, stroke: 1.9 })),
    label);
}

function QuietAction({ icon, label }: { icon: string; label: string }) {
  return React.createElement("button", { style: { display: "inline-flex", alignItems: "center", gap: 5,
    fontSize: "0.78125rem", fontWeight: 500, color: "var(--c-ink-4)", background: "transparent", border: "none",
    cursor: "pointer", padding: "6px 4px" } },
    React.createElement(WfIcon, { name: icon, s: 14, stroke: 2 }), label);
}
function ActionRow({ chip }: { chip?: boolean }) {
  const t = useTranslations("workflow_solidify.turn_card");
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, marginTop: 14,
    paddingTop: 12, borderTop: "1px solid var(--c-line-2)" } },
    chip && React.createElement(SolidifyChip, { label: t("solidify") }),
    React.createElement("div", { style: { flex: 1 } }),
    React.createElement(QuietAction, { icon: "rotateCw", label: t("rerun") }),
    React.createElement(QuietAction, { icon: "copy", label: t("copy") }),
    React.createElement(QuietAction, { icon: "arrowUpRight", label: t("feedback") }));
}
function LeadHead({ state, dur }: { state: string; dur?: string }) {
  const t = useTranslations("workflow_solidify.turn_card");
  const map: Record<string, { fg: string; dot: string; lbl: string }> = {
    done: { fg: "var(--c-pos)", dot: "var(--c-pos)", lbl: "done" },
    error: { fg: "var(--c-err)", dot: "var(--c-err)", lbl: "error" },
    running: { fg: "var(--c-accent)", dot: "var(--c-accent)", lbl: t("running") },
  };
  const m = map[state] || map.done;
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, marginBottom: 9 } },
    React.createElement("span", { style: { width: 22, height: 22, borderRadius: 7, background: "var(--c-ink)",
      color: "var(--c-bg)", display: "grid", placeItems: "center", fontFamily: "var(--f-serif)", fontSize: "0.6875rem", fontWeight: 600 } }, "B"),
    React.createElement("span", { style: { fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)" } }, PRODUCT_NAME),
    React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 5, fontSize: "0.6875rem", color: m.fg, fontWeight: 500 } },
      React.createElement("span", { style: { width: 6, height: 6, borderRadius: "50%", background: m.dot } }), m.lbl),
    React.createElement("div", { style: { flex: 1 } }),
    dur && React.createElement("span", { className: "bh-mono", style: { fontSize: "0.65625rem", color: "var(--c-ink-4)" } }, dur));
}
function Finding({ k, v, tone }: { k: string; v: string; tone?: string }) {
  const c = tone === "down" ? "var(--c-err)" : tone === "up" ? "var(--c-pos)" : "var(--c-ink-2)";
  return React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 10, padding: "7px 0", borderTop: "1px solid var(--c-line-2)" } },
    React.createElement("span", { style: { fontSize: "0.8125rem", color: "var(--c-ink-2)", flex: 1 } }, k),
    React.createElement("span", { className: "bh-mono bh-num", style: { fontSize: "0.8125rem", fontWeight: 600, color: c } }, v));
}

export function TurnCard({ variant = "done", compact }: { variant?: string; compact?: boolean }) {
  const t = useTranslations("workflow_solidify.turn_card");
  const wrap: React.CSSProperties = { border: "1px solid var(--c-line)", borderRadius: "var(--r-3)",
    background: "var(--c-surface)", padding: compact ? "14px 15px" : "16px 18px" };
  if (variant === "chat") {
    return React.createElement("div", { style: wrap, "data-screen-label": "turn · chat" },
      React.createElement(LeadHead, { state: "done", dur: t("chat_dur") }),
      React.createElement("p", { style: { margin: 0, fontSize: "0.875rem", lineHeight: 1.65, color: "var(--c-ink-2)" } },
        t("chat_body")),
      React.createElement(ActionRow, { chip: false }),
      React.createElement("div", { style: { marginTop: 8, fontSize: "0.65625rem", color: "var(--c-ink-4)", display: "flex", alignItems: "center", gap: 6 } },
        React.createElement(WfIcon, { name: "crystal", s: 12, stroke: 1.9, style: { opacity: .5 } }), t("chat_note")));
  }
  if (variant === "running") {
    return React.createElement("div", { style: wrap, "data-screen-label": "turn · running" },
      React.createElement(LeadHead, { state: "running", dur: t("running_dur") }),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 9 } },
        React.createElement("div", { className: "wf-skel", style: { height: 13, width: "86%" } }),
        React.createElement("div", { className: "wf-skel", style: { height: 13, width: "72%" } }),
        React.createElement("div", { className: "wf-skel", style: { height: 13, width: "64%" } })),
      React.createElement("div", { style: { marginTop: 14, fontSize: "0.65625rem", color: "var(--c-ink-4)", display: "flex", alignItems: "center", gap: 6 } },
        React.createElement(WfIcon, { name: "crystal", s: 12, stroke: 1.9, style: { opacity: .5 } }), t("running_note")));
  }
  if (variant === "error") {
    return React.createElement("div", { style: wrap, "data-screen-label": "turn · error" },
      React.createElement(LeadHead, { state: "error", dur: t("error_dur") }),
      React.createElement("div", { style: { display: "flex", gap: 11, alignItems: "flex-start", padding: "11px 13px",
        background: "var(--c-err-soft)", border: "1px solid #ecc6cc", borderRadius: "var(--r-2)" } },
        React.createElement("span", { style: { color: "var(--c-err)", flex: "0 0 auto", marginTop: 1 } },
          React.createElement(WfIcon, { name: "alertTriangle", s: 16, stroke: 2 })),
        React.createElement("div", { style: { flex: 1, fontSize: "0.8125rem", color: "#8c2233", lineHeight: 1.55 } },
          t("error_body"),
          React.createElement("button", { style: { marginLeft: 8, fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-err)",
            background: "transparent", border: "none", cursor: "pointer", textDecoration: "underline", padding: 0 } }, t("retry")))),
      React.createElement("div", { style: { marginTop: 12, fontSize: "0.65625rem", color: "var(--c-ink-4)", display: "flex", alignItems: "center", gap: 6 } },
        React.createElement(WfIcon, { name: "crystal", s: 12, stroke: 1.9, style: { opacity: .5 } }), t("error_note")));
  }
  return React.createElement("div", { style: wrap, "data-screen-label": "turn · done" },
    React.createElement(LeadHead, { state: "done", dur: t("done_dur") }),
    React.createElement("p", { style: { margin: "0 0 12px", fontSize: "0.875rem", lineHeight: 1.65, color: "var(--c-ink-2)" } },
      t.rich("done_body", { b: (chunks) => React.createElement("b", { style: { color: "var(--c-ink)", fontWeight: 600 } }, chunks) })),
    React.createElement("div", { style: { border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", padding: "4px 13px 8px", background: "var(--c-bg)" } },
      React.createElement("div", { style: { fontSize: "0.65625rem", fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--c-ink-4)", padding: "9px 0 3px" } }, t("table_title")),
      React.createElement(Finding, { k: t("row_east_ka"), v: "18.2%  −2.1pct", tone: "down" }),
      React.createElement(Finding, { k: t("row_online"), v: "21.4%  +0.3pct", tone: "up" }),
      React.createElement(Finding, { k: t("row_distribution"), v: t("flat"), tone: "flat" })),
    React.createElement(ActionRow, { chip: true }));
}
