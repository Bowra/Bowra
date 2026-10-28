"use client";
// wf-shared.tsx — shared primitives for Workflow Solidification HITL surfaces. Ported verbatim from
// Claude Design solidify-shared.jsx (React.createElement). Clones P3 v10 card vocabulary:
// stepper, soft borders, mono labels, terracotta accent, --c-pos-soft success. Uses WfIcon.
import { useTranslations } from "next-intl";
import React from "react";

import { WfIcon } from "./wf-icons";
import { CharIcon } from "@/components/char-icon";

type CSS = React.CSSProperties;
type Tone = "warn" | "info" | "pos" | "err";

export const spin: CSS = {
  width: 13, height: 13, borderRadius: "50%", border: "1.6px solid currentColor",
  borderRightColor: "transparent", display: "inline-block", animation: "wf-spin .8s linear infinite",
};

// ───────────────────── STEPPER ─────────────────────
export function Stepper({ active, compact }: { active: number; compact?: boolean }) {
  const t = useTranslations("workflow_solidify.shared");
  const steps: [string, string][] = [["①", t("step_preview")], ["②", t("step_triggers")], ["③", t("step_form")], ["✓", t("step_done")]];
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: compact ? 5 : 7,
    padding: compact ? "9px 13px" : "11px 16px", borderTop: "1px solid var(--c-line-2)",
    borderBottom: "1px solid var(--c-line-2)", background: "var(--c-bg)" } },
    steps.map((s, i) => {
      const done = i < active, cur = i === active;
      const ring = done || cur ? "var(--c-accent)" : "var(--c-line)";
      const fill = done ? "var(--c-accent)" : cur ? "var(--c-accent-tint)" : "transparent";
      const fg = done ? "#fff" : cur ? "var(--c-accent)" : "var(--c-ink-4)";
      const sz = compact ? 19 : 20;
      return React.createElement(React.Fragment, { key: i },
        React.createElement("div", { style: { display: "flex", alignItems: "center", gap: compact ? 5 : 7 } },
          React.createElement("span", { style: { width: sz, height: sz, borderRadius: "50%",
            border: "1.5px solid " + ring, background: fill, color: fg, display: "grid", placeItems: "center",
            fontSize: "0.6875rem", fontWeight: 700, flex: "0 0 " + sz + "px" } },
            done ? React.createElement(WfIcon, { name: "check", s: 12, stroke: 3 }) : React.createElement(CharIcon, { ch: s[0] })),
          (!compact || cur) && React.createElement("span", { style: { fontSize: compact ? "0.71875rem" : "0.75rem",
            fontWeight: cur ? 600 : 500, whiteSpace: "nowrap",
            color: cur ? "var(--c-ink)" : done ? "var(--c-ink-3)" : "var(--c-ink-4)" } }, s[1])),
        i < 3 && React.createElement("span", { style: { flex: 1, height: 1, minWidth: compact ? 8 : 14,
          background: i < active ? "var(--c-accent)" : "var(--c-line)", opacity: i < active ? .5 : 1 } }));
    }));
}

// ───────────────────── CALLOUT ─────────────────────
export function Callout({ tone, icon, children, style }: { tone: Tone; icon: string; children?: React.ReactNode; style?: CSS }) {
  const map = {
    warn: { bg: "var(--c-warn-soft)", bd: "var(--c-warn-line)", fg: "var(--c-warn-ink)", ic: "var(--c-warn)" },
    info: { bg: "var(--c-surface-2)", bd: "var(--c-line)", fg: "var(--c-ink-3)", ic: "var(--c-ink-4)" },
    pos: { bg: "var(--c-pos-soft)", bd: "var(--c-pos-line)", fg: "var(--c-pos-ink)", ic: "var(--c-pos)" },
    err: { bg: "var(--c-err-soft)", bd: "var(--c-err-line)", fg: "var(--c-err-ink)", ic: "var(--c-err)" },
  }[tone];
  return React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "flex-start",
    padding: "10px 12px", background: map.bg, border: "1px solid " + map.bd, borderRadius: "var(--r-2)", ...style } },
    React.createElement("span", { style: { flex: "0 0 auto", color: map.ic, marginTop: 1 } },
      React.createElement(WfIcon, { name: icon, s: 16, stroke: 2 })),
    React.createElement("div", { style: { flex: 1, fontSize: "0.75rem", lineHeight: 1.55, color: map.fg } }, children));
}

// ───────────────────── BUTTONS / FIELDS ─────────────────────
export function btnPrimary(disabled?: boolean): CSS {
  return { display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, fontSize: "0.84375rem",
    fontWeight: 600, padding: "10px 18px", borderRadius: "var(--r-1)", border: "none", cursor: disabled ? "default" : "pointer",
    background: disabled ? "var(--c-surface-3)" : "var(--c-accent)", color: disabled ? "var(--c-ink-4)" : "#fff" };
}
export function btnPos(): CSS {
  return { display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, fontSize: "0.84375rem",
    fontWeight: 600, padding: "10px 18px", borderRadius: "var(--r-1)", border: "none", cursor: "pointer",
    background: "var(--c-pos)", color: "#fff" };
}
export function btnGhost(): CSS {
  return { display: "inline-flex", alignItems: "center", gap: 6, fontSize: "0.84375rem", fontWeight: 600,
    padding: "10px 16px", borderRadius: "var(--r-1)", border: "1px solid var(--c-line)", cursor: "pointer",
    background: "var(--c-surface)", color: "var(--c-ink-2)" };
}
export function linkBtn(): CSS {
  return { fontSize: "0.78125rem", fontWeight: 500, color: "var(--c-ink-3)", background: "transparent", border: "none",
    cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 5, padding: "8px 2px",
    textDecorationLine: "underline", textDecorationColor: "var(--c-line)", textUnderlineOffset: 3 };
}
export function fieldInput(): CSS {
  return { width: "100%", border: "1.5px solid var(--c-line)", borderRadius: "var(--r-1)", padding: "10px 12px",
    fontSize: "0.84375rem", color: "var(--c-ink)", background: "var(--c-surface)", outline: "none", fontFamily: "var(--f-sans)" };
}
export function FieldLabel({ children, hint }: { children?: React.ReactNode; hint?: string }) {
  return React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 } },
    React.createElement("label", { style: { fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)" } }, children),
    hint && React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", whiteSpace: "nowrap" } }, hint));
}
export function SectionLabel({ icon, children, hint }: { icon?: string; children?: React.ReactNode; hint?: string }) {
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, marginBottom: 2 } },
    icon && React.createElement("span", { style: { color: "var(--c-ink-4)" } }, React.createElement(WfIcon, { name: icon, s: 14, stroke: 2 })),
    React.createElement("span", { style: { fontSize: "0.71875rem", fontWeight: 700, color: "var(--c-ink-2)", letterSpacing: ".02em", whiteSpace: "nowrap" } }, children),
    hint && React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", whiteSpace: "nowrap" } }, hint));
}

// ───────────────────── BADGES ─────────────────────
export function FormBadge({ form, big }: { form: string; big?: boolean }) {
  const t = useTranslations("workflow_solidify.shared");
  const map: Record<string, { ic: string; lbl: string; fg: string; bg: string; bd: string }> = {
    skill: { ic: "user", lbl: t("form_skill"), fg: "var(--c-accent)", bg: "var(--c-accent-tint)", bd: "var(--c-accent-line)" },
    "team-skill": { ic: "users", lbl: t("form_team_skill"), fg: "var(--c-pos-ink)", bg: "var(--c-pos-soft)", bd: "var(--c-pos-line)" },
    subagent: { ic: "bot", lbl: t("form_subagent"), fg: "var(--c-info-ink)", bg: "var(--c-info-soft)", bd: "var(--c-info-line)" },
  };
  const m = map[form] || map.skill;
  return React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 5,
    fontSize: big ? "0.75rem" : "0.6875rem", fontWeight: 600, color: m.fg, background: m.bg, border: "1px solid " + m.bd,
    borderRadius: 999, padding: big ? "4px 11px" : "2px 9px", whiteSpace: "nowrap", flex: "0 0 auto" } },
    React.createElement(WfIcon, { name: m.ic, s: big ? 13 : 12, stroke: 2 }), m.lbl);
}
export function SuggestTag() {
  const t = useTranslations("workflow_solidify.shared");
  return React.createElement("span", { className: "bh-mono", style: { fontSize: "0.59375rem", fontWeight: 600,
    letterSpacing: ".04em", color: "var(--c-ink-4)", background: "var(--c-surface-2)", border: "1px solid var(--c-line)",
    borderRadius: 5, padding: "1px 6px", whiteSpace: "nowrap" } }, t("suggest_tag"));
}
export function VisBadge({ vis }: { vis: string }) {
  const t = useTranslations("workflow_solidify.shared");
  const team = vis === "team_wide";
  return React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 4,
    fontSize: "0.6875rem", fontWeight: 600, color: team ? "var(--c-info-ink)" : "var(--c-ink-3)",
    background: team ? "var(--c-info-soft)" : "var(--c-surface-2)", border: "1px solid " + (team ? "var(--c-info-line)" : "var(--c-line)"),
    borderRadius: 999, padding: "2px 9px", whiteSpace: "nowrap", flex: "0 0 auto" } },
    React.createElement(WfIcon, { name: team ? "globe" : "eyeOff", s: 12, stroke: 2 }),
    team ? t("vis_team_wide") : t("vis_creator_only"));
}
export function WriteTag() {
  const t = useTranslations("workflow_solidify.shared");
  return React.createElement("span", { style: { fontSize: "0.65625rem", fontWeight: 600, color: "var(--c-warn)",
    background: "var(--c-warn-soft)", border: "1px solid var(--c-warn-line)", borderRadius: 5, padding: "1px 7px",
    display: "inline-flex", alignItems: "center", gap: 3 } },
    React.createElement(WfIcon, { name: "alertTriangle", s: 10.5, stroke: 2.2 }), t("write_tag"));
}
export function VerifiedTag() {
  const t = useTranslations("workflow_solidify.shared");
  return React.createElement("span", { style: { fontSize: "0.59375rem", fontWeight: 600, color: "var(--c-pos)",
    background: "var(--c-pos-soft)", border: "1px solid var(--c-pos-line)", borderRadius: 5, padding: "1px 6px",
    display: "inline-flex", alignItems: "center", gap: 3 } },
    React.createElement(WfIcon, { name: "check", s: 10, stroke: 3 }), t("verified_tag"));
}
// lbl = copy key under workflow_solidify.shared.
export const VERDICT: Record<string, { lbl: string; fg: string; bg: string; bd: string; ic: string }> = {
  pass: { lbl: "verdict_pass", fg: "var(--c-pos-ink)", bg: "var(--c-pos-soft)", bd: "var(--c-pos-line)", ic: "check" },
  fail: { lbl: "verdict_fail", fg: "var(--c-err-ink)", bg: "var(--c-err-soft)", bd: "var(--c-err-line)", ic: "x" },
  partial: { lbl: "verdict_partial", fg: "var(--c-warn-ink)", bg: "var(--c-warn-soft)", bd: "var(--c-warn-line)", ic: "minusCircle" },
  skipped: { lbl: "verdict_skipped", fg: "var(--c-ink-3)", bg: "var(--c-surface-2)", bd: "var(--c-line)", ic: "slash" },
  error: { lbl: "verdict_error", fg: "var(--c-err-ink)", bg: "var(--c-err-soft)", bd: "var(--c-err-line)", ic: "alertTriangle" },
};
export function VerdictBadge({ verdict, big }: { verdict: string; big?: boolean }) {
  const t = useTranslations("workflow_solidify.shared");
  const v = VERDICT[verdict] || VERDICT.skipped;
  return React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 6,
    fontSize: big ? "0.8125rem" : "0.71875rem", fontWeight: 700, color: v.fg, background: v.bg, border: "1px solid " + v.bd,
    borderRadius: 999, padding: big ? "5px 13px" : "3px 10px", whiteSpace: "nowrap", flex: "0 0 auto" } },
    React.createElement(WfIcon, { name: v.ic, s: big ? 14 : 12, stroke: 2.4 }), t(v.lbl));
}

// ───────────────────── ROWS / CHIPS ─────────────────────
export function TriggerChip({ text, editable }: { text: string; editable?: boolean }) {
  return React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 7,
    fontSize: "0.78125rem", color: "var(--c-ink-2)", background: "var(--c-surface)", border: "1px solid var(--c-line)",
    borderRadius: 999, padding: "6px 12px", boxShadow: "var(--shadow-1)" } },
    React.createElement("span", { className: "bh-mono", style: { fontSize: "0.625rem", color: "var(--c-accent)" } }, "「"),
    text,
    React.createElement("span", { className: "bh-mono", style: { fontSize: "0.625rem", color: "var(--c-accent)" } }, "」"),
    editable && React.createElement("span", { style: { color: "var(--c-ink-4)", display: "inline-flex", marginLeft: 1 } },
      React.createElement(WfIcon, { name: "pencil", s: 11, stroke: 2 })));
}
export function SlotRow({ token, example, editable }: { token: string; example: string; editable?: boolean }) {
  const t = useTranslations("workflow_solidify.shared");
  return React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10,
    padding: "9px 12px", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)" } },
    React.createElement("code", { className: "bh-mono", style: { fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-accent)",
      background: "var(--c-accent-tint)", border: "1px solid var(--c-accent-line)", borderRadius: 6, padding: "3px 8px", flex: "0 0 auto", whiteSpace: "nowrap" } }, token),
    React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", flex: "0 0 auto" } }, t("slot_example")),
    React.createElement("div", { style: { flex: 1, display: "flex", alignItems: "center", gap: 7,
      border: "1.5px solid " + (editable ? "var(--c-line)" : "transparent"), borderRadius: "var(--r-1)",
      background: editable ? "var(--c-bg)" : "transparent", padding: editable ? "6px 10px" : "6px 0" } },
      React.createElement("span", { className: "bh-mono", style: { fontSize: "0.78125rem", color: "var(--c-ink)", whiteSpace: "nowrap" } }, example),
      editable && React.createElement("span", { style: { color: "var(--c-ink-4)", display: "inline-flex", marginLeft: "auto" } },
        React.createElement(WfIcon, { name: "pencil", s: 11, stroke: 2 }))));
}
export function ScriptBlock({ name, lines, open, truncated }: { name: string; lines: string[]; open?: boolean; truncated?: boolean }) {
  const t = useTranslations("workflow_solidify.shared");
  return React.createElement("div", { style: { border: "1px solid var(--c-line)", borderRadius: "var(--r-2)",
    background: "var(--c-surface)", overflow: "hidden" } },
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, padding: "9px 12px",
      borderBottom: open ? "1px solid var(--c-line-2)" : "none" } },
      React.createElement("span", { style: { color: "var(--c-ink-4)", transition: "transform .15s",
        transform: open ? "rotate(90deg)" : "none", display: "inline-flex" } },
        React.createElement(WfIcon, { name: "chevronRight", s: 14, stroke: 2.2 })),
      React.createElement(WfIcon, { name: "code", s: 14, stroke: 2, style: { color: "var(--c-cool)" } }),
      React.createElement("code", { className: "bh-mono", style: { fontSize: "0.75rem", fontWeight: 600, color: "var(--c-ink)" } }, name),
      React.createElement("div", { style: { flex: 1 } }),
      truncated ? React.createElement("span", { style: { fontSize: "0.59375rem", fontWeight: 600, color: "var(--c-warn)",
        background: "var(--c-warn-soft)", border: "1px solid var(--c-warn-line)", borderRadius: 5, padding: "1px 6px" } }, t("script_partial"))
        : React.createElement(VerifiedTag)),
    open && React.createElement("pre", { className: "bh-mono bh-scroll", style: { margin: 0, padding: "11px 13px",
      fontSize: "0.71875rem", lineHeight: 1.6, color: "var(--c-ink-2)", background: "var(--c-bg)", overflowX: "auto",
      whiteSpace: "pre" } }, lines.join("\n")));
}
export function CheckpointRow({ text, pass, reason, mode }: { text: string; pass?: boolean | null; reason?: string; mode?: string }) {
  if (mode === "judge") {
    const ok = pass === true, skip = pass == null;
    const c = ok ? { fg: "var(--c-pos-ink)", bg: "var(--c-pos-soft)", bd: "var(--c-pos-line)", ic: "check" }
      : skip ? { fg: "var(--c-ink-4)", bg: "var(--c-surface-2)", bd: "var(--c-line)", ic: "slash" }
        : { fg: "var(--c-err-ink)", bg: "var(--c-err-soft)", bd: "var(--c-err-line)", ic: "x" };
    return React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "flex-start",
      padding: "10px 12px", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)" } },
      React.createElement("span", { style: { flex: "0 0 18px", width: 18, height: 18, borderRadius: "50%",
        background: c.bg, border: "1px solid " + c.bd, color: c.fg, display: "grid", placeItems: "center", marginTop: 1 } },
        React.createElement(WfIcon, { name: c.ic, s: 11, stroke: 2.6 })),
      React.createElement("div", { style: { flex: 1, minWidth: 0 } },
        React.createElement("div", { style: { fontSize: "0.78125rem", color: "var(--c-ink)", lineHeight: 1.45 } }, text),
        reason && React.createElement("div", { style: { fontSize: "0.71875rem", color: "var(--c-ink-3)", lineHeight: 1.45,
          marginTop: 3, fontStyle: "italic" } }, "— " + reason)));
  }
  return React.createElement("div", { style: { display: "flex", gap: 10, alignItems: "flex-start", padding: "4px 0" } },
    React.createElement("span", { style: { flex: "0 0 16px", width: 16, height: 16, borderRadius: 5,
      border: "1.5px solid var(--c-line)", color: "var(--c-ink-4)", display: "grid", placeItems: "center", marginTop: 1 } },
      React.createElement(WfIcon, { name: "target", s: 10, stroke: 2 })),
    React.createElement("div", { style: { flex: 1, fontSize: "0.78125rem", color: "var(--c-ink-2)", lineHeight: 1.5 } }, text));
}
export function StepLine({ n, children, truncated }: { n: number; children?: React.ReactNode; truncated?: boolean }) {
  const t = useTranslations("workflow_solidify.shared");
  return React.createElement("div", { style: { display: "flex", gap: 11, alignItems: "flex-start" } },
    React.createElement("span", { className: "bh-mono", style: { flex: "0 0 22px", width: 22, height: 22,
      borderRadius: 7, background: truncated ? "var(--c-warn-soft)" : "var(--c-surface-2)",
      border: "1px solid " + (truncated ? "var(--c-warn-line)" : "var(--c-line)"),
      color: truncated ? "var(--c-warn)" : "var(--c-ink-3)", display: "grid", placeItems: "center",
      fontSize: "0.6875rem", fontWeight: 600, marginTop: 1 } }, n),
    React.createElement("div", { style: { flex: 1, fontSize: "0.8125rem", color: "var(--c-ink-2)", lineHeight: 1.6 } },
      children,
      truncated && React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 4,
        marginLeft: 8, fontSize: "0.65625rem", fontWeight: 600, color: "var(--c-warn)", verticalAlign: "1px" } },
        React.createElement(WfIcon, { name: "scissors", s: 11, stroke: 2 }), t("step_truncated"))));
}

// ───────────────────── SAMPLE DATA (preview only) ─────────────────────
// Sample recipes and dry-run results for preview (/spike/workflow-solidify). Copy lives under messages
// workflow_solidify.sample, read as whole strings by UI language (t.raw: contains {{slot}} braces, bypassing ICU formatting).
export type SampleRecipe = {
  display_name: string; name: string; description: string; triggerSuffix: string; suggested_form: string;
  triggers: string[]; steps: string[]; truncated_steps: number[]; checkpoints: string[];
  scripts: { name: string; lines: string[]; truncated?: boolean }[];
  slots: { token: string; example: string }[];
  final_answer_preview: string; orchestrator_prompt: string; scratch_session_id: string;
};
export function useSampleRecipe(): SampleRecipe {
  const t = useTranslations("workflow_solidify.sample");
  return {
    display_name: t.raw("display_name") as string,
    name: "channel-margin-reconcile",
    description: t.raw("description") as string,
    triggerSuffix: t.raw("trigger_suffix") as string,
    suggested_form: "skill",
    triggers: t.raw("triggers") as string[],
    steps: t.raw("steps") as string[],
    truncated_steps: [1],
    checkpoints: t.raw("checkpoints") as string[],
    scripts: [
      { name: "pull_channel_margin.sql", lines: t.raw("script_sql") as string[] },
      { name: "margin_reconcile.py", truncated: true, lines: t.raw("script_py") as string[] },
    ],
    slots: t.raw("slots") as { token: string; example: string }[],
    final_answer_preview: t.raw("final_answer_preview") as string,
    orchestrator_prompt: t.raw("orchestrator_prompt") as string,
    scratch_session_id: "s_dryrun_4f9a2c",
  };
}
const DRYRUN_PASS: Record<string, (boolean | null)[]> = {
  pass: [true, true, true], fail: [true, false, false], partial: [true, true, false], skipped: [null, null, null],
};
export function useSampleDryRun(): Record<string, { text: string; pass: boolean | null; reason: string }[]> {
  const t = useTranslations("workflow_solidify.sample");
  const checkpoints = t.raw("checkpoints") as string[];
  const reasons = t.raw("dryrun") as Record<string, string[]>;
  const out: Record<string, { text: string; pass: boolean | null; reason: string }[]> = { error: [] };
  for (const [verdict, passes] of Object.entries(DRYRUN_PASS)) {
    out[verdict] = passes.map((pass, i) => ({ text: checkpoints[i], pass, reason: reasons[verdict][i] }));
  }
  return out;
}
