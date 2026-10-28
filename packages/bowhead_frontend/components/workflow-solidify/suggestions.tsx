"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";
// suggestions.tsx — surface D (P2 stretch): first-screen pinned_recipes suggestion chips. Ported from Claude Design
// suggestions.jsx. Partitions team-solidified workflows (pinned, crystal, no TTL) from AI question suggestions (LLM-gen, 7d).
import { useTranslations } from "next-intl";
import React from "react";

import { WfIcon } from "./wf-icons";

// Sample chips: label/value and AI suggestions live in messages workflow_solidify.sample.{pinned,llm} (aligned by index).
const PINNED_FORMS = ["team-skill", "skill", "subagent", "team-skill"];

function Mark({ s = 20 }: { s?: number }) {
  return React.createElement("span", { style: { width: s, height: s, flex: "0 0 " + s + "px", borderRadius: 6,
    background: "var(--c-ink)", color: "var(--c-bg)", display: "grid", placeItems: "center",
    fontFamily: "var(--f-serif)", fontSize: s * 0.5, fontWeight: 700 } }, "B");
}
function PinnedChip({ c, compact }: { c: { label: string; value: string }; compact?: boolean }) {
  const t = useTranslations("workflow_solidify.suggestions");
  return React.createElement("button", { style: { display: "inline-flex", alignItems: "center", gap: 9,
    padding: compact ? "9px 13px" : "10px 15px", borderRadius: "var(--r-2)", cursor: "pointer", textAlign: "left",
    background: "var(--c-surface)", border: "1px solid var(--c-line)", boxShadow: "var(--shadow-1)", maxWidth: compact ? "100%" : 340 } },
    React.createElement("span", { style: { width: 30, height: 30, flex: "0 0 30px", borderRadius: 9, background: "var(--c-accent-tint)",
      border: "1px solid #ecd2c1", color: "var(--c-accent)", display: "grid", placeItems: "center" } },
      React.createElement(WfIcon, { name: "crystal", s: 15, stroke: 1.8 })),
    React.createElement("span", { style: { minWidth: 0, lineHeight: 1.35 } },
      React.createElement("span", { style: { display: "block", fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, c.label),
      React.createElement("span", { style: { display: "block", fontSize: "0.6875rem", color: "var(--c-ink-4)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, t("quoted", { text: c.value }))),
    React.createElement("span", { style: { color: "var(--c-ink-4)", flex: "0 0 auto" } }, React.createElement(WfIcon, { name: "arrowRight", s: 14, stroke: 2 })));
}
function LlmChip({ t }: { t: string }) {
  return React.createElement("button", { style: { display: "inline-flex", alignItems: "center", gap: 7,
    padding: "9px 14px", borderRadius: 999, cursor: "pointer", background: "transparent", border: "1px solid var(--c-line)",
    fontSize: "0.78125rem", color: "var(--c-ink-2)", whiteSpace: "nowrap" } },
    React.createElement(WfIcon, { name: "sparkle", s: 13, stroke: 2, style: { color: "var(--c-ink-4)" } }), t);
}
function PartLabel({ icon, accent, children, hint }: { icon: string; accent?: boolean; children?: React.ReactNode; hint?: string }) {
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, marginBottom: 12 } },
    React.createElement("span", { style: { color: accent ? "var(--c-accent)" : "var(--c-ink-4)", display: "inline-flex" } },
      React.createElement(WfIcon, { name: icon, s: 14, stroke: 2 })),
    React.createElement("span", { style: { fontSize: "0.71875rem", fontWeight: 700, letterSpacing: ".04em", color: "var(--c-ink-2)" } }, children),
    hint && React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", whiteSpace: "nowrap" } }, hint),
    React.createElement("span", { style: { flex: 1, height: 1, background: "var(--c-line-2)", marginLeft: 4 } }));
}
function SuggestBody({ compact }: { compact?: boolean }) {
  const t = useTranslations("workflow_solidify.suggestions");
  const sample = useTranslations("workflow_solidify.sample");
  const PINNED = (sample.raw("pinned") as { label: string; value: string }[]).map((c, i) => ({ ...c, form: PINNED_FORMS[i] }));
  const LLM = sample.raw("llm") as string[];
  return React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: compact ? 24 : 28 } },
    React.createElement("div", null,
      React.createElement("h1", { style: { margin: 0, fontSize: compact ? "1.375rem" : "1.75rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.015em", fontFamily: "var(--f-serif)", textAlign: "center" } }, t("heading")),
      React.createElement("div", { style: { maxWidth: 620, margin: compact ? "18px auto 0" : "22px auto 0", display: "flex", alignItems: "center", gap: 10,
        padding: "13px 16px", border: "1px solid var(--c-line)", borderRadius: "var(--r-3)", background: "var(--c-surface)", boxShadow: "var(--shadow-2)" } },
        React.createElement("span", { style: { fontSize: "0.875rem", color: "var(--c-ink-4)", flex: 1, fontStyle: "italic" } }, t("placeholder")),
        React.createElement("span", { style: { width: 32, height: 32, borderRadius: "var(--r-1)", background: "var(--c-ink)", color: "#fff", display: "grid", placeItems: "center", flex: "0 0 auto" } },
          React.createElement(WfIcon, { name: "arrowUpRight", s: 16, stroke: 2 })))),
    React.createElement("div", { style: { maxWidth: 720, margin: "0 auto", width: "100%" } },
      React.createElement(PartLabel, { icon: "crystal", accent: true, hint: t("pinned_hint") }, t("pinned_label")),
      React.createElement("div", { style: { display: "flex", flexWrap: "wrap", gap: 10 } },
        PINNED.map((c, i) => React.createElement(PinnedChip, { key: i, c, compact })))),
    React.createElement("div", { style: { maxWidth: 720, margin: "0 auto", width: "100%" } },
      React.createElement(PartLabel, { icon: "sparkle", hint: t("llm_hint") }, t("llm_label")),
      React.createElement("div", { style: { display: "flex", flexWrap: "wrap", gap: 9 } },
        LLM.map((t, i) => React.createElement(LlmChip, { key: i, t })))));
}

export function SuggestScreen({ compact }: { compact?: boolean }) {
  if (compact) {
    return React.createElement("div", { style: { width: 412, display: "flex", flexDirection: "column", background: "var(--c-bg)", fontFamily: "var(--f-sans)", overflow: "hidden", border: "1px solid var(--c-line)" }, "data-screen-label": "suggestions · mobile" },
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, padding: "0 16px", height: 50, flex: "0 0 50px" } },
        React.createElement(Mark, { s: 20 }),
        React.createElement("span", { style: { fontFamily: "var(--f-serif)", fontSize: "0.9375rem", fontWeight: 600, color: "var(--c-ink)" } }, PRODUCT_NAME),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("span", { style: { color: "var(--c-ink-4)" } }, React.createElement(WfIcon, { name: "menu", s: 18, stroke: 2 }))),
      React.createElement("div", { className: "bh-scroll", style: { overflow: "auto", padding: "20px 15px 24px" } },
        React.createElement(SuggestBody, { compact: true })));
  }
  return React.createElement("div", { style: { display: "flex", background: "var(--c-bg)", fontFamily: "var(--f-sans)", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", overflow: "hidden" }, "data-screen-label": "suggestions · desktop" },
    React.createElement("div", { style: { width: 248, flex: "0 0 248px", borderRight: "1px solid var(--c-line)", background: "var(--c-surface-2)" } },
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, padding: "16px 16px" } },
        React.createElement(Mark, { s: 22 }),
        React.createElement("span", { style: { fontFamily: "var(--f-serif)", fontSize: "0.9375rem", fontWeight: 600, color: "var(--c-ink)" } }, PRODUCT_NAME))),
    React.createElement("div", { className: "bh-scroll", style: { flex: 1, overflow: "auto", padding: "64px 0" } },
      React.createElement("div", { style: { maxWidth: 800, margin: "0 auto", padding: "0 40px" } },
        React.createElement(SuggestBody, { compact: false }))));
}
