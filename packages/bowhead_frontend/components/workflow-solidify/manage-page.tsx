"use client";
// manage-page.tsx — surface C: recipe management (personal + team). Ported from Claude Design manage-page.jsx.
// Scenarios: list · expanded (one row View open) · empty. Scope: personal | team.
import { useTranslations } from "next-intl";
import React from "react";

import { WfIcon } from "./wf-icons";
import {
  CheckpointRow, FormBadge, ScriptBlock, StepLine, VerdictBadge, VisBadge, WriteTag, linkBtn, useSampleRecipe,
} from "./wf-shared";

type Rec = {
  dn: string; name: string; form: string; verdict: string; when: string; desc: string; trig: string;
  size: string; updated: string; vis?: string; write?: boolean; disabled?: boolean; by?: string;
};
// Sample rows: language-invariant fields live here; names / times / descriptions / trigger phrases / authors are under messages
// workflow_solidify.sample.{personal,team} (aligned by index).
type RecText = { dn: string; when: string; desc: string; trig: string; updated: string; by?: string };
const PERSONAL_FIXED: Omit<Rec, keyof RecText>[] = [
  { name: "channel-margin-reconcile", form: "skill", verdict: "pass", size: "6.2 KB" },
  { name: "weekly-attainment-snapshot", form: "skill", verdict: "partial", size: "4.1 KB" },
  { name: "churn-early-warning", form: "skill", verdict: "fail", disabled: true, size: "3.7 KB" },
];
const TEAM_FIXED: Omit<Rec, keyof RecText>[] = [
  { name: "channel-margin-reconcile", form: "team-skill", vis: "team_wide", verdict: "pass", size: "6.2 KB" },
  { name: "sales-analysis", form: "subagent", vis: "team_wide", verdict: "pass", write: true, size: "12.8 KB" },
  { name: "expense-reconcile", form: "team-skill", vis: "creator_only", verdict: "skipped", size: "5.4 KB" },
];
function useSampleRows(): { personal: Rec[]; team: Rec[] } {
  const t = useTranslations("workflow_solidify.sample");
  const merge = (fixed: Omit<Rec, keyof RecText>[], text: RecText[]): Rec[] => fixed.map((f, i) => ({ ...text[i], ...f }));
  return {
    personal: merge(PERSONAL_FIXED, t.raw("personal") as RecText[]),
    team: merge(TEAM_FIXED, t.raw("team") as RecText[]),
  };
}

function Mark({ s = 20 }: { s?: number }) {
  return React.createElement("span", { style: { width: s, height: s, flex: "0 0 " + s + "px", borderRadius: 6,
    background: "var(--c-ink)", color: "var(--c-bg)", display: "grid", placeItems: "center",
    fontFamily: "var(--f-serif)", fontSize: s * 0.5, fontWeight: 700 } }, "B");
}
function ActionBtn({ icon, label, danger }: { icon: string; label: string; danger?: boolean }) {
  return React.createElement("button", { style: { display: "inline-flex", alignItems: "center", gap: 5,
    fontSize: "0.75rem", fontWeight: 500, color: danger ? "var(--c-err)" : "var(--c-ink-3)", background: "transparent",
    border: "none", cursor: "pointer", padding: "5px 2px" } },
    React.createElement(WfIcon, { name: icon, s: 13, stroke: 2 }), label);
}
function VerdictLine({ verdict, when }: { verdict: string; when: string }) {
  const t = useTranslations("workflow_solidify.manage");
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" } },
    React.createElement(VerdictBadge, { verdict }),
    React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", whiteSpace: "nowrap" } }, t("last_dryrun", { when })),
    React.createElement("button", { style: { ...linkBtn(), padding: "2px 2px", fontSize: "0.71875rem" } },
      React.createElement(WfIcon, { name: "arrowRight", s: 12, stroke: 2 }), t("view_last_dryrun")),
    React.createElement("button", { style: { ...linkBtn(), padding: "2px 2px", fontSize: "0.71875rem", textDecorationLine: "none" } },
      React.createElement(WfIcon, { name: "rotateCw", s: 12, stroke: 2 }), t("rerun")));
}
function ExpandPanel({ compact }: { compact?: boolean }) {
  const t = useTranslations("workflow_solidify.manage");
  const RECIPE = useSampleRecipe();
  const lbl = (t: string) => React.createElement("div", { style: { fontSize: "0.6875rem", fontWeight: 700, letterSpacing: ".04em", color: "var(--c-ink-3)", marginBottom: 9 } }, t);
  return React.createElement("div", { style: { marginTop: 13, paddingTop: 14, borderTop: "1px solid var(--c-line-2)", display: "flex", flexDirection: "column", gap: 14 } },
    React.createElement("div", null, lbl(t("steps_label")),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 9 } },
        RECIPE.steps.map((s, i) => React.createElement(StepLine, { key: i, n: i + 1, truncated: RECIPE.truncated_steps.includes(i) }, s)))),
    React.createElement("div", null, lbl(t("checkpoints_label")),
      React.createElement("div", { style: { padding: "6px 12px", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-bg)" } },
        RECIPE.checkpoints.map((c, i) => React.createElement(CheckpointRow, { key: i, text: c, mode: "assert" })))),
    React.createElement("div", null, lbl(t("scripts_label")),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 } },
        React.createElement(ScriptBlock, { name: RECIPE.scripts[0].name, lines: RECIPE.scripts[0].lines, open: !compact }),
        React.createElement(ScriptBlock, { name: RECIPE.scripts[1].name, lines: RECIPE.scripts[1].lines, open: false, truncated: true }))));
}
function RecipeRow({ r, scope, expanded, compact }: { r: Rec; scope: string; expanded?: boolean; compact?: boolean }) {
  const t = useTranslations("workflow_solidify.manage");
  return React.createElement("div", { style: { border: "1px solid var(--c-line)", borderRadius: "var(--r-3)",
    background: "var(--c-surface)", padding: compact ? "14px 14px" : "16px 18px", opacity: r.disabled ? .62 : 1,
    boxShadow: expanded ? "var(--shadow-2)" : "none" }, "data-screen-label": "recipe · " + r.name },
    React.createElement("div", { style: { display: "flex", alignItems: "flex-start", gap: 12, flexWrap: compact ? "wrap" : "nowrap" } },
      React.createElement("div", { style: { flex: 1, minWidth: 0 } },
        React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" } },
          React.createElement(FormBadge, { form: r.form }),
          r.vis && React.createElement(VisBadge, { vis: r.vis }),
          r.write && React.createElement(WriteTag),
          r.disabled && React.createElement("span", { style: { fontSize: "0.65625rem", fontWeight: 600, color: "var(--c-ink-4)",
            background: "var(--c-surface-2)", border: "1px solid var(--c-line)", borderRadius: 5, padding: "1px 7px" } }, t("disabled_tag"))),
        React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 9, marginTop: 9, flexWrap: "wrap" } },
          React.createElement("h3", { style: { margin: 0, fontSize: compact ? "0.9375rem" : "1rem", fontWeight: 600, color: "var(--c-ink)", fontFamily: "var(--f-serif)" } }, r.dn),
          React.createElement("code", { className: "bh-mono", style: { fontSize: "0.6875rem", color: "var(--c-ink-4)" } }, r.name)),
        React.createElement("p", { style: { margin: "7px 0 0", fontSize: "0.78125rem", color: "var(--c-ink-3)", lineHeight: 1.55 } }, r.desc),
        React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 7, marginTop: 7, fontSize: "0.71875rem", color: "var(--c-ink-4)" } },
          React.createElement(WfIcon, { name: "route", s: 12, stroke: 2, style: { color: "var(--c-cool)" } }), t("trigger_line", { trigger: r.trig }))),
      React.createElement("div", { style: { flex: compact ? "1 1 100%" : "0 0 auto", display: "flex", flexDirection: "column",
        alignItems: compact ? "flex-start" : "flex-end", gap: 7, marginTop: compact ? 4 : 0 } },
        React.createElement(VerdictLine, { verdict: r.verdict, when: r.when }))),
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14, marginTop: 13, paddingTop: 12, borderTop: "1px solid var(--c-line-2)", flexWrap: "wrap" } },
      React.createElement("span", { className: "bh-mono", style: { fontSize: "0.65625rem", color: "var(--c-ink-4)" } },
        (scope === "team" && r.by ? r.by + " · " : "") + t("meta_updated", { size: r.size, updated: r.updated })),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement(ActionBtn, { icon: expanded ? "chevronDown" : "chevronRight", label: t("view") }),
      React.createElement(ActionBtn, { icon: "settings", label: t("edit_meta") }),
      React.createElement(ActionBtn, { icon: "powerOff", label: r.disabled ? t("enable") : t("disable") }),
      React.createElement(ActionBtn, { icon: "trash", label: t("delete"), danger: true })),
    expanded && React.createElement(ExpandPanel, { compact }));
}
function ScopeTabs({ scope, counts }: { scope: string; counts: { p: number; o: number } }) {
  const t = useTranslations("workflow_solidify.manage");
  return React.createElement("div", { style: { display: "flex", gap: 4, padding: 4, background: "var(--c-surface-2)",
    borderRadius: "var(--r-2)", border: "1px solid var(--c-line)", width: "fit-content" } },
    ([["personal", "user", t("tab_personal"), counts.p], ["team", "users", t("tab_team"), counts.o]] as [string, string, string, number][]).map(([k, ic, lbl, n]) => {
      const on = scope === k;
      return React.createElement("button", { key: k, style: { display: "inline-flex", alignItems: "center", gap: 7,
        padding: "8px 16px", borderRadius: 8, border: "1px solid " + (on ? "var(--c-line)" : "transparent"), cursor: "pointer",
        background: on ? "var(--c-surface)" : "transparent", boxShadow: on ? "var(--shadow-1)" : "none",
        fontSize: "0.8125rem", fontWeight: 600, color: on ? "var(--c-ink)" : "var(--c-ink-3)" } },
        React.createElement(WfIcon, { name: ic, s: 15, stroke: 2, style: { color: on ? "var(--c-accent)" : "var(--c-ink-4)" } }),
        lbl, React.createElement("span", { className: "bh-mono", style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", fontWeight: 500 } }, n));
    }));
}
function EmptyState({ compact }: { compact?: boolean }) {
  const t = useTranslations("workflow_solidify.manage");
  return React.createElement("div", { style: { border: "1px dashed var(--c-line)", borderRadius: "var(--r-3)",
    background: "var(--c-surface)", padding: compact ? "40px 22px" : "60px 30px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center" } },
    React.createElement("span", { style: { width: 56, height: 56, borderRadius: "50%", background: "var(--c-accent-tint)",
      border: "1px solid #ecd2c1", color: "var(--c-accent)", display: "grid", placeItems: "center", marginBottom: 18 } },
      React.createElement(WfIcon, { name: "crystal", s: 26, stroke: 1.7 })),
    React.createElement("h3", { style: { margin: 0, fontSize: "1rem", fontWeight: 600, color: "var(--c-ink)" } }, t("empty_title")),
    React.createElement("p", { style: { margin: "8px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-3)", lineHeight: 1.65, maxWidth: 380 } },
      t("empty_body")),
    React.createElement("div", { style: { display: "inline-flex", alignItems: "center", gap: 7, marginTop: 18, padding: "8px 14px",
      background: "var(--c-surface)", border: "1px solid var(--c-line)", borderRadius: 999, fontSize: "0.78125rem", color: "var(--c-ink-2)" } },
      React.createElement("span", { style: { color: "var(--c-accent)", display: "inline-flex" } }, React.createElement(WfIcon, { name: "crystal", s: 14, stroke: 1.9 })), t("empty_chip")));
}
function SettingsNav() {
  const t = useTranslations("workflow_solidify.manage");
  const items: [string, string, boolean?][] = [["user", t("nav_profile")], ["lock", t("nav_security")], ["layers", t("nav_workflows"), true], ["building", t("nav_team")], ["coins", t("nav_billing")]];
  return React.createElement("div", { style: { width: 240, flex: "0 0 240px", borderRight: "1px solid var(--c-line)",
    background: "var(--c-surface-2)", padding: "20px 14px", display: "flex", flexDirection: "column", gap: 3 } },
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, padding: "2px 8px 16px" } },
      React.createElement(Mark, { s: 20 }),
      React.createElement("span", { style: { fontFamily: "var(--f-serif)", fontSize: "0.875rem", fontWeight: 600, color: "var(--c-ink)" } }, t("nav_title"))),
    items.map(([ic, lbl, on], i) => React.createElement("div", { key: i, style: { display: "flex", alignItems: "center", gap: 10,
      padding: "9px 12px", borderRadius: "var(--r-1)", fontSize: "0.8125rem", fontWeight: on ? 600 : 400,
      background: on ? "var(--c-surface)" : "transparent", border: "1px solid " + (on ? "var(--c-line)" : "transparent"),
      color: on ? "var(--c-ink)" : "var(--c-ink-3)" } },
      React.createElement(WfIcon, { name: ic, s: 16, stroke: 1.9, style: { color: on ? "var(--c-accent)" : "var(--c-ink-4)" } }), lbl)));
}
function PageHeader({ compact }: { compact?: boolean }) {
  const t = useTranslations("workflow_solidify.manage");
  return React.createElement("div", { style: { marginBottom: 18 } },
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10 } },
      React.createElement("h1", { style: { margin: 0, fontSize: compact ? "1.25rem" : "1.5rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em", fontFamily: "var(--f-serif)", whiteSpace: "nowrap" } }, t("title")),
      React.createElement("div", { style: { flex: 1 } })),
    React.createElement("p", { style: { margin: "8px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-3)", lineHeight: 1.6 } },
      t("description")));
}

export function ManagePage({ scope = "personal", scenario, compact }: { scope?: string; scenario?: string; compact?: boolean }) {
  const t = useTranslations("workflow_solidify.manage");
  const rows = useSampleRows();
  const list = scope === "team" ? rows.team : rows.personal;
  const counts = { p: rows.personal.length, o: rows.team.length };
  const expandedName = scenario === "expanded" ? list[0].name : null;
  const content = React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 16 } },
    React.createElement(PageHeader, { compact }),
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" } },
      React.createElement(ScopeTabs, { scope, counts }),
      React.createElement("div", { style: { flex: 1 } }),
      scenario !== "empty" && React.createElement("span", { className: "bh-mono", style: { fontSize: "0.71875rem", color: "var(--c-ink-4)" } }, t("count", { n: list.length }))),
    scenario === "empty"
      ? React.createElement(EmptyState, { compact })
      : React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 12 } },
          list.map((r) => React.createElement(RecipeRow, { key: r.name, r, scope, compact, expanded: r.name === expandedName }))));

  if (compact) {
    return React.createElement("div", { style: { width: 412, display: "flex", flexDirection: "column", background: "var(--c-bg)", fontFamily: "var(--f-sans)", overflow: "hidden", border: "1px solid var(--c-line)" }, "data-screen-label": "manage · " + scope + " · " + scenario },
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, padding: "0 16px", height: 50, flex: "0 0 50px", borderBottom: "1px solid var(--c-line)", background: "var(--c-surface)" } },
        React.createElement("span", { style: { color: "var(--c-ink-3)" } }, React.createElement(WfIcon, { name: "chevronLeft", s: 20, stroke: 2 })),
        React.createElement("span", { style: { fontSize: "0.875rem", fontWeight: 600, color: "var(--c-ink)" } }, t("title"))),
      React.createElement("div", { className: "bh-scroll", style: { overflow: "auto", padding: "16px 13px 20px" } }, content));
  }
  return React.createElement("div", { style: { display: "flex", background: "var(--c-bg)", fontFamily: "var(--f-sans)", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", overflow: "hidden" }, "data-screen-label": "manage · " + scope + " · " + scenario },
    React.createElement(SettingsNav),
    React.createElement("div", { className: "bh-scroll", style: { flex: 1, overflow: "auto", padding: "34px 0" } },
      React.createElement("div", { style: { maxWidth: 880, margin: "0 auto", padding: "0 40px" } }, content)));
}
