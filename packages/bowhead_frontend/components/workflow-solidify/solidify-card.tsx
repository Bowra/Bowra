"use client";
// solidify-card.tsx — face A: solidification confirmation card (workflow_creator HITL). Ported verbatim from Claude Design
// solidify-card.jsx. One component, scenario-switched; same in desktop + mobile (compact prop).
// Scenarios: loading · step1 · step2 · step3-skill · step3-subagent · oral · conflict · readonly
//   · success · dryrun-running · dryrun-(pass|fail|partial|skipped|error)
import { useTranslations } from "next-intl";
import React from "react";

import { WfIcon } from "./wf-icons";
import {
  Callout, CheckpointRow, FieldLabel, FormBadge, ScriptBlock, SectionLabel,
  SlotRow, Stepper, StepLine, SuggestTag, TriggerChip, VerdictBadge, VisBadge, WriteTag,
  btnGhost, btnPos, btnPrimary, fieldInput, linkBtn, spin, useSampleDryRun, useSampleRecipe,
  type SampleRecipe,
} from "./wf-shared";

type CSS = React.CSSProperties;
type Translate = ReturnType<typeof useTranslations>;
// Render functions are not components (cannot call hooks); SolidifyCard resolves copy and sample recipes, then passes them in.
type Ctx = { compact?: boolean; t: Translate; R: SampleRecipe };
const bold = (chunks: React.ReactNode) => React.createElement("b", { style: { fontWeight: 600 } }, chunks);
const wrap: CSS = { border: "1px solid var(--c-line)", borderRadius: "var(--r-4)", background: "var(--c-surface)",
  overflow: "hidden", boxShadow: "var(--shadow-float)" };
const pad = (compact?: boolean): CSS => ({ padding: compact ? "15px 15px 16px" : "16px 18px 18px", display: "flex", flexDirection: "column", gap: 15 });
const footRow: CSS = { display: "flex", alignItems: "center", gap: 12, marginTop: 2, paddingTop: 14, borderTop: "1px solid var(--c-line-2)" };

function Header({ compact }: { compact?: boolean }) {
  const t = useTranslations("workflow_solidify.card");
  return React.createElement("div", { style: { display: "flex", alignItems: "flex-start", gap: 12, padding: compact ? "14px 15px 12px" : "16px 18px 14px" } },
    React.createElement("span", { style: { width: 36, height: 36, flex: "0 0 36px", borderRadius: 11,
      background: "var(--c-accent-soft)", color: "var(--c-accent)", display: "grid", placeItems: "center" } },
      React.createElement(WfIcon, { name: "crystal", s: 19, stroke: 1.8 })),
    React.createElement("div", { style: { flex: 1, minWidth: 0 } },
      React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 9, flexWrap: "wrap" } },
        React.createElement("h3", { style: { margin: 0, fontSize: compact ? "0.96875rem" : "1.03125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em" } }, t("title")),
        React.createElement("span", { className: "bh-mono", style: { fontSize: "0.625rem", color: "var(--c-ink-4)",
          background: "var(--c-surface-2)", border: "1px solid var(--c-line)", borderRadius: 999, padding: "2px 8px" } }, t("badge"))),
      React.createElement("p", { style: { margin: "4px 0 0", fontSize: "0.78125rem", color: "var(--c-ink-3)" } }, t("subtitle"))),
    React.createElement("button", { "aria-label": t("close_aria"), style: { width: 28, height: 28, flex: "0 0 28px", border: "none",
      background: "transparent", color: "var(--c-ink-4)", cursor: "pointer", borderRadius: 8, display: "grid", placeItems: "center" } },
      React.createElement(WfIcon, { name: "x", s: 17, stroke: 2 })));
}

function renderLoading({ compact, t }: Ctx) {
  return React.createElement("div", { style: { padding: compact ? "26px 18px 30px" : "34px 22px 38px", display: "flex",
    flexDirection: "column", alignItems: "center", textAlign: "center" } },
    React.createElement("div", { style: { position: "relative", width: 52, height: 52, marginBottom: 18 } },
      React.createElement("span", { style: { position: "absolute", inset: 0, borderRadius: "50%", border: "2px solid var(--c-accent-soft)" } }),
      React.createElement("span", { style: { position: "absolute", inset: 0, borderRadius: "50%", border: "2px solid transparent", borderTopColor: "var(--c-accent)", animation: "wf-spin 1s linear infinite" } }),
      React.createElement("span", { style: { position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "var(--c-accent)", animation: "wf-pulse 1.6s ease-in-out infinite" } },
        React.createElement(WfIcon, { name: "crystal", s: 22, stroke: 1.8 }))),
    React.createElement("h3", { style: { margin: 0, fontSize: "0.96875rem", fontWeight: 600, color: "var(--c-ink)" } }, t("loading_title")),
    React.createElement("p", { style: { margin: "7px 0 0", fontSize: "0.78125rem", color: "var(--c-ink-3)", lineHeight: 1.6, maxWidth: 360 } },
      t.rich("loading_body", { b: (chunks) => React.createElement("b", { style: { color: "var(--c-ink-2)", fontWeight: 600 } }, chunks) })),
    React.createElement("div", { style: { width: "100%", maxWidth: 420, marginTop: 22, display: "flex", flexDirection: "column", gap: 10, textAlign: "left" } },
      React.createElement("div", { className: "wf-skel", style: { height: 22, width: "52%" } }),
      React.createElement("div", { className: "wf-skel", style: { height: 12, width: "88%" } }),
      React.createElement("div", { className: "wf-skel", style: { height: 12, width: "74%" } }),
      React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 4 } },
        React.createElement("div", { className: "wf-skel", style: { height: 30, width: 30, borderRadius: 8 } }),
        React.createElement("div", { className: "wf-skel", style: { height: 30, flex: 1, borderRadius: 8 } })),
      React.createElement("div", { style: { display: "flex", gap: 8 } },
        React.createElement("div", { className: "wf-skel", style: { height: 30, width: 30, borderRadius: 8 } }),
        React.createElement("div", { className: "wf-skel", style: { height: 30, flex: 1, borderRadius: 8 } }))),
    React.createElement("div", { style: { marginTop: 20, display: "flex", alignItems: "center", gap: 8, fontSize: "0.71875rem", color: "var(--c-ink-4)" } },
      React.createElement("span", { className: "bh-mono" }, "0:14"), "/", React.createElement("span", { className: "bh-mono" }, "~1:00"),
      React.createElement("span", { style: { width: 120, height: 4, borderRadius: 4, background: "var(--c-surface-3)", overflow: "hidden" } },
        React.createElement("span", { style: { display: "block", width: "24%", height: "100%", background: "var(--c-accent)" } }))));
}

function renderStep1({ compact, t, R }: Ctx) {
  return React.createElement("div", { className: "bh-scroll", style: pad(compact) },
    React.createElement("div", null,
      React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" } },
        React.createElement("h2", { style: { margin: 0, fontSize: compact ? "1.1875rem" : "1.3125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em", fontFamily: "var(--f-serif)" } }, R.display_name),
        React.createElement(FormBadge, { form: R.suggested_form }), React.createElement(SuggestTag)),
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 7, marginTop: 6 } },
        React.createElement("code", { className: "bh-mono", style: { fontSize: "0.75rem", color: "var(--c-ink-3)" } }, R.name),
        React.createElement("span", { style: { color: "var(--c-ink-4)", display: "inline-flex" } }, React.createElement(WfIcon, { name: "pencil", s: 11, stroke: 2 })),
        React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", whiteSpace: "nowrap" } }, t("slug_hint"))),
      React.createElement("p", { style: { margin: "10px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-2)", lineHeight: 1.6 } },
        R.description, " ",
        React.createElement("span", { style: { color: "var(--c-ink-4)" } }, t("trigger_line", { suffix: R.triggerSuffix })))),
    React.createElement(Callout, { tone: "info", icon: "scissors" },
      React.createElement("span", null, t.rich("truncated_callout", { b: bold }))),
    React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "listChecks", hint: t("steps_hint", { n: R.steps.length }) }, t("steps_label")),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 10, marginTop: 10 } },
        R.steps.map((s, i) => React.createElement(StepLine, { key: i, n: i + 1, truncated: R.truncated_steps.includes(i) }, s)))),
    React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "target", hint: t("checkpoints_hint") }, t("checkpoints_label")),
      React.createElement("div", { style: { marginTop: 8, padding: "8px 12px", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)" } },
        R.checkpoints.map((c, i) => React.createElement(CheckpointRow, { key: i, text: c, mode: "assert" })))),
    React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "code", hint: t("scripts_hint") }, "scripts"),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8, marginTop: 8 } },
        React.createElement(ScriptBlock, { name: R.scripts[0].name, lines: R.scripts[0].lines, open: true }),
        React.createElement(ScriptBlock, { name: R.scripts[1].name, lines: R.scripts[1].lines, open: false, truncated: true }))),
    React.createElement("div", { style: footRow },
      React.createElement("span", { className: "bh-mono", style: { fontSize: "0.65625rem", color: "var(--c-ink-4)" } }, t("distilled_from")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPrimary(false) }, t("next_triggers"), React.createElement(WfIcon, { name: "chevronRight", s: 16, stroke: 2.2 }))));
}

function renderStep2({ compact, t, R }: Ctx) {
  return React.createElement("div", { className: "bh-scroll", style: pad(compact) },
    Callout({ tone: "info", icon: "route", children: t("step2_intro") }),
    React.createElement("div", null,
      React.createElement(FieldLabel, { hint: t("examples_hint") }, t("examples_label")),
      React.createElement("div", { style: { display: "flex", flexWrap: "wrap", gap: 8 } },
        R.triggers.map((t, i) => React.createElement(TriggerChip, { key: i, text: t, editable: true })),
        React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 5, fontSize: "0.78125rem",
          color: "var(--c-ink-3)", background: "transparent", border: "1px dashed var(--c-line)", borderRadius: 999, padding: "6px 12px", cursor: "pointer" } },
          React.createElement(WfIcon, { name: "plus", s: 13, stroke: 2 }), t("add_example")))),
    React.createElement("div", null,
      React.createElement(FieldLabel, { hint: t("trigger_words_hint") }, t("trigger_words_label")),
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, ...fieldInput(), padding: "8px 10px" } },
        React.createElement(WfIcon, { name: "target", s: 14, stroke: 2, style: { color: "var(--c-accent)", flex: "0 0 auto" } }),
        React.createElement("span", { className: "bh-mono", style: { fontSize: "0.78125rem", color: "var(--c-ink)" } }, R.triggerSuffix))),
    React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "sliders", hint: t("slots_hint") }, t("slots_label")),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8, marginTop: 9 } },
        R.slots.map((s, i) => React.createElement(SlotRow, { key: i, token: s.token, example: s.example, editable: true }))),
      React.createElement("div", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)", marginTop: 8, display: "flex", alignItems: "center", gap: 6 } },
        React.createElement(WfIcon, { name: "sparkle", s: 12, stroke: 2, style: { color: "var(--c-ink-4)" } }),
        t("slots_note"))),
    React.createElement("div", { style: footRow },
      React.createElement("button", { style: btnGhost() }, React.createElement(WfIcon, { name: "chevronLeft", s: 15, stroke: 2 }), t("back")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPrimary(false) }, t("next_form"), React.createElement(WfIcon, { name: "chevronRight", s: 16, stroke: 2.2 }))));
}

function FormOption({ icon, title, sub, on, badge }: { icon: string; title: string; sub: string; on?: boolean; badge?: React.ReactNode }) {
  return React.createElement("label", { style: { display: "flex", gap: 12, alignItems: "flex-start", padding: "12px 13px",
    borderRadius: "var(--r-2)", cursor: "pointer", background: on ? "var(--c-accent-tint)" : "var(--c-surface)",
    border: "1.5px solid " + (on ? "var(--c-accent)" : "var(--c-line)"),
    boxShadow: on ? "0 0 0 3px var(--c-accent-soft)" : "none", transition: "all .14s" } },
    React.createElement("span", { style: { width: 19, height: 19, flex: "0 0 19px", borderRadius: "50%", marginTop: 1,
      border: "1.5px solid " + (on ? "var(--c-accent)" : "var(--c-line)"), background: on ? "var(--c-accent)" : "var(--c-surface)",
      display: "grid", placeItems: "center" } }, on && React.createElement("span", { style: { width: 7, height: 7, borderRadius: "50%", background: "#fff" } })),
    React.createElement("span", { style: { color: on ? "var(--c-accent)" : "var(--c-ink-4)", flex: "0 0 auto", marginTop: 1 } },
      React.createElement(WfIcon, { name: icon, s: 18, stroke: 1.9 })),
    React.createElement("span", { style: { flex: 1, minWidth: 0 } },
      React.createElement("span", { style: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" } },
        React.createElement("span", { style: { fontSize: "0.84375rem", fontWeight: 600, color: "var(--c-ink)", whiteSpace: "nowrap", flex: "0 0 auto" } }, title), badge),
      React.createElement("span", { style: { display: "block", fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 3, lineHeight: 1.45 } }, sub)));
}
function renderStep3({ compact, t, R }: Ctx, form: string) {
  const isSub = form === "subagent";
  const teamForm = form === "team-skill" || isSub;
  return React.createElement("div", { className: "bh-scroll", style: pad(compact) },
    React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "layers" }, t("form_label")),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 9, marginTop: 9 } },
        React.createElement(FormOption, { icon: "user", title: t("form_skill_title"), sub: t("form_skill_sub"), on: form === "skill",
          badge: R.suggested_form === "skill" ? React.createElement(SuggestTag) : null }),
        React.createElement(FormOption, { icon: "users", title: t("form_team_title"), sub: t("form_team_sub"), on: form === "team-skill" }),
        React.createElement(FormOption, { icon: "bot", title: t("form_subagent_title"), sub: t("form_subagent_sub"), on: isSub, badge: React.createElement(WriteTag) }))),
    isSub && React.createElement("div", null,
      React.createElement(FieldLabel, { hint: t("orchestrator_hint") }, t("orchestrator_label")),
      React.createElement("textarea", { className: "bh-scroll", rows: 3, readOnly: true,
        defaultValue: R.orchestrator_prompt,
        style: { width: "100%", resize: "vertical", border: "1.5px solid var(--c-line)", borderRadius: "var(--r-1)",
          padding: "10px 12px", fontSize: "0.78125rem", lineHeight: 1.6, color: "var(--c-ink-2)", background: "var(--c-bg)", outline: "none", fontFamily: "var(--f-sans)" } })),
    teamForm && React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "eye" }, t("visibility_label")),
      React.createElement("div", { style: { display: "flex", gap: 4, padding: 4, background: "var(--c-surface-2)", borderRadius: "var(--r-2)", border: "1px solid var(--c-line)", marginTop: 9 } },
        ([["team_wide", "globe", t("vis_team_wide"), t("vis_team_wide_sub")], ["creator_only", "eyeOff", t("vis_creator_only"), t("vis_creator_only_sub")]] as [string, string, string, string][]).map(([k, ic, lbl, sub], i) => {
          const sel = i === 0;
          return React.createElement("div", { key: k, style: { flex: 1, display: "flex", alignItems: "center", gap: 9, padding: "9px 12px",
            borderRadius: 9, border: "1px solid " + (sel ? "var(--c-line)" : "transparent"), background: sel ? "var(--c-surface)" : "transparent",
            boxShadow: sel ? "var(--shadow-1)" : "none" } },
            React.createElement("span", { style: { color: sel ? "var(--c-accent)" : "var(--c-ink-4)" } }, React.createElement(WfIcon, { name: ic, s: 16, stroke: 1.9 })),
            React.createElement("span", { style: { lineHeight: 1.3 } },
              React.createElement("span", { style: { display: "block", fontSize: "0.78125rem", fontWeight: 600, color: sel ? "var(--c-ink)" : "var(--c-ink-3)" } }, lbl),
              React.createElement("span", { style: { display: "block", fontSize: "0.65625rem", color: "var(--c-ink-4)" } }, sub)));
        }))),
    teamForm && Callout({ tone: "warn", icon: "alertTriangle",
      children: React.createElement("span", null, t.rich("write_warning", { b: bold })) }),
    React.createElement("div", { style: footRow },
      React.createElement("button", { style: btnGhost() }, React.createElement(WfIcon, { name: "chevronLeft", s: 15, stroke: 2 }), t("back")),
      React.createElement("button", { style: linkBtn() }, React.createElement(WfIcon, { name: "flask", s: 13, stroke: 2 }), t("dry_run_first")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPrimary(false) }, React.createElement(WfIcon, { name: "crystal", s: 15, stroke: 1.8 }), t("save"))));
}

function renderConflict({ compact, t, R }: Ctx) {
  return React.createElement("div", { className: "bh-scroll", style: pad(compact) },
    Callout({ tone: "err", icon: "alertTriangle", children: React.createElement("span", null,
      t.rich("conflict", { name: R.name, code: (chunks) => React.createElement("code", { className: "bh-mono", style: { fontWeight: 600, color: "var(--c-err)" } }, chunks) })) }),
    React.createElement("div", null,
      React.createElement(FieldLabel, { hint: t("slug_rules") }, t("slug_label")),
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, border: "1.5px solid var(--c-err)",
        borderRadius: "var(--r-1)", background: "var(--c-surface)", padding: "0 12px", boxShadow: "0 0 0 3px var(--c-err-soft)" } },
        React.createElement(WfIcon, { name: "hash", s: 14, stroke: 2, style: { color: "var(--c-ink-4)", flex: "0 0 auto" } }),
        React.createElement("span", { className: "bh-mono", style: { flex: 1, padding: "10px 0", fontSize: "0.8125rem", color: "var(--c-ink)" } }, R.name),
        React.createElement("span", { style: { color: "var(--c-err)" } }, React.createElement(WfIcon, { name: "alertTriangle", s: 14, stroke: 2 }))),
      React.createElement("div", { style: { fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 7, display: "flex", alignItems: "center", gap: 6 } },
        React.createElement(WfIcon, { name: "sparkle", s: 12, stroke: 2 }), t("suggestion_prefix"), React.createElement("code", { className: "bh-mono", style: { color: "var(--c-accent)" } }, R.name + "-v2"))),
    React.createElement("div", { style: footRow },
      React.createElement("button", { style: btnGhost() }, React.createElement(WfIcon, { name: "chevronLeft", s: 15, stroke: 2 }), t("back")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPrimary(true), disabled: true }, React.createElement(WfIcon, { name: "crystal", s: 15, stroke: 1.8 }), t("save"))));
}

function renderReadonly({ compact, t }: Ctx) {
  return React.createElement("div", { className: "bh-scroll", style: pad(compact) },
    Callout({ tone: "warn", icon: "lock", children: React.createElement("span", null, t.rich("readonly_callout", { b: bold })) }),
    React.createElement("div", null,
      React.createElement(SectionLabel, { icon: "layers" }, t("form_label")),
      React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 9, marginTop: 9 } },
        React.createElement(FormOption, { icon: "user", title: t("form_skill_title"), sub: t("form_skill_sub_available"), on: true }),
        React.createElement("div", { style: { opacity: .5, pointerEvents: "none" } },
          React.createElement(FormOption, { icon: "users", title: t("form_team_title"), sub: t("needs_admin"), on: false })),
        React.createElement("div", { style: { opacity: .5, pointerEvents: "none" } },
          React.createElement(FormOption, { icon: "bot", title: t("form_subagent_title"), sub: t("needs_admin_writable"), on: false })))),
    React.createElement("div", { style: footRow },
      React.createElement("button", { style: btnGhost() }, React.createElement(WfIcon, { name: "chevronLeft", s: 15, stroke: 2 }), t("back")),
      React.createElement("button", { style: linkBtn() }, React.createElement(WfIcon, { name: "users", s: 13, stroke: 2 }), t("request_access")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPrimary(false) }, React.createElement(WfIcon, { name: "user", s: 15, stroke: 1.9 }), t("save_personal"))));
}

function OralQ({ n, q, hint, prefilled, value, mono, area }: { n: number; q: string; hint?: string; prefilled?: boolean; value?: string; mono?: boolean; area?: string }) {
  const t = useTranslations("workflow_solidify.card");
  return React.createElement("div", null,
    React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 8, marginBottom: 7 } },
      React.createElement("span", { className: "bh-mono", style: { fontSize: "0.6875rem", fontWeight: 700, color: "var(--c-accent)" } }, "Q" + n),
      React.createElement("label", { style: { fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)" } }, q),
      prefilled && React.createElement("span", { style: { fontSize: "0.59375rem", fontWeight: 600, color: "var(--c-pos)", background: "var(--c-pos-soft)",
        border: "1px solid var(--c-pos-line)", borderRadius: 5, padding: "1px 6px" } }, t("prefilled")),
      hint && React.createElement("span", { style: { fontSize: "0.6875rem", color: "var(--c-ink-4)" } }, hint)),
    area
      ? React.createElement("textarea", { rows: 3, defaultValue: value, className: area, style: { ...fieldInput(), resize: "vertical", lineHeight: 1.6,
          fontFamily: mono ? "var(--f-mono)" : "var(--f-sans)", fontSize: mono ? "0.78125rem" : "0.84375rem" } })
      : React.createElement("input", { defaultValue: value, style: { ...fieldInput(), fontFamily: mono ? "var(--f-mono)" : "var(--f-sans)" } }));
}
function renderOral({ compact, t, R }: Ctx) {
  return React.createElement("div", { className: "bh-scroll", style: pad(compact) },
    Callout({ tone: "warn", icon: "helpCircle", children: React.createElement("span", null, t.rich("oral_callout", { b: bold })) }),
    React.createElement(OralQ, { n: 1, q: t("oral_q1"), value: t("oral_name_value", { display: R.display_name, name: R.name }) }),
    React.createElement(OralQ, { n: 2, q: t("oral_q2"), hint: t("oral_q2_hint"), value: R.triggers[0] }),
    React.createElement(OralQ, { n: 3, q: t("oral_q3"), prefilled: true, area: "bh-scroll", mono: false, value: R.steps.map((s, i) => (i + 1) + ". " + s).join("\n") }),
    React.createElement(OralQ, { n: 4, q: t("oral_q4"), hint: "checkpoint", area: "bh-scroll", value: R.checkpoints.map((c) => "· " + c).join("\n") }),
    Callout({ tone: "info", icon: "code", children: React.createElement("span", null, t.rich("scripts_kept", { n: R.scripts.length, b: bold })) }),
    React.createElement("div", { style: footRow },
      React.createElement("button", { style: btnGhost() }, React.createElement(WfIcon, { name: "rotateCw", s: 15, stroke: 2 }), t("retry_distill")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPrimary(false) }, React.createElement(WfIcon, { name: "crystal", s: 15, stroke: 1.8 }), t("save_oral"))));
}

function DryRunPanel({ verdict, running }: { verdict?: string; running?: boolean }) {
  const t = useTranslations("workflow_solidify.card");
  const R = useSampleRecipe();
  const dryrun = useSampleDryRun();
  const rows = (verdict && dryrun[verdict]) || [];
  return React.createElement("div", { style: { border: "1px solid var(--c-line)", borderRadius: "var(--r-3)", background: "var(--c-surface)", overflow: "hidden", marginTop: 4 } },
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--c-line-2)", background: "var(--c-bg)" } },
      React.createElement(WfIcon, { name: "flask", s: 16, stroke: 2, style: { color: "var(--c-cool)" } }),
      React.createElement("span", { style: { fontSize: "0.78125rem", fontWeight: 700, color: "var(--c-ink-2)" } }, t("dryrun_result")),
      React.createElement("div", { style: { flex: 1 } }),
      running ? React.createElement("span", { style: { display: "inline-flex", alignItems: "center", gap: 7, fontSize: "0.71875rem", color: "var(--c-cool)" } },
        React.createElement("span", { style: { ...spin, color: "var(--c-cool)" } }), t("dryrun_running"))
        : React.createElement(VerdictBadge, { verdict: verdict || "skipped", big: true })),
    React.createElement("div", { style: { padding: "13px 14px", display: "flex", flexDirection: "column", gap: 11 } },
      running
        ? [React.createElement("p", { key: "p", style: { margin: 0, fontSize: "0.75rem", color: "var(--c-ink-3)", lineHeight: 1.6 } },
            t.rich("dryrun_running_body", { b: (chunks) => React.createElement("b", { style: { color: "var(--c-ink-2)", fontWeight: 600 } }, chunks) })),
          React.createElement("div", { key: "s", style: { display: "flex", flexDirection: "column", gap: 8 } },
            R.checkpoints.map((c, i) => React.createElement("div", { key: i, style: { display: "flex", gap: 10, alignItems: "center",
              padding: "10px 12px", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", opacity: i === 0 ? 1 : .5 } },
              React.createElement("span", { style: { ...spin, color: "var(--c-ink-4)", flex: "0 0 13px" } }),
              React.createElement("span", { style: { fontSize: "0.78125rem", color: "var(--c-ink-3)" } }, c))))]
        : verdict === "error"
          ? Callout({ tone: "err", icon: "alertTriangle", children: React.createElement("span", null,
              t.rich("dryrun_error", { code: (chunks) => React.createElement("code", { className: "bh-mono", style: { color: "var(--c-err)", fontWeight: 600 } }, chunks) })) })
          : [verdict === "skipped" ? React.createElement("div", { key: "sk" }, Callout({ tone: "info", icon: "slash",
              children: t("dryrun_skipped") })) : null,
            React.createElement("div", { key: "cp", style: { display: "flex", flexDirection: "column", gap: 8 } },
              rows.map((r, i) => React.createElement(CheckpointRow, { key: i, mode: "judge", text: r.text, pass: r.pass, reason: r.reason }))),
            React.createElement("div", { key: "fa", style: { border: "1px dashed var(--c-line)", borderRadius: "var(--r-2)", padding: "11px 13px", background: "var(--c-bg)" } },
              React.createElement("div", { style: { fontSize: "0.65625rem", fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--c-ink-4)", marginBottom: 6 } }, t("final_answer_preview")),
              React.createElement("p", { style: { margin: 0, fontSize: "0.78125rem", color: "var(--c-ink-2)", lineHeight: 1.6 } }, R.final_answer_preview))],
      !running && React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14, paddingTop: 4 } },
        React.createElement("button", { style: linkBtn() }, React.createElement(WfIcon, { name: "arrowRight", s: 13, stroke: 2 }), t("view_full_dryrun")),
        React.createElement("span", { className: "bh-mono", style: { fontSize: "0.65625rem", color: "var(--c-ink-4)" } }, R.scratch_session_id),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { style: { ...btnGhost(), padding: "7px 13px", fontSize: "0.78125rem" } }, React.createElement(WfIcon, { name: "rotateCw", s: 13, stroke: 2 }), t("rerun")))));
}

function renderSuccess({ compact, t, R }: Ctx, dryrun: { verdict?: string; running?: boolean } | null) {
  return React.createElement("div", { className: "bh-scroll", style: { padding: compact ? "22px 16px 18px" : "26px 22px 20px", display: "flex", flexDirection: "column", gap: 16 } },
    React.createElement("div", { style: { display: "flex", alignItems: "flex-start", gap: 13 } },
      React.createElement("span", { style: { width: 44, height: 44, flex: "0 0 44px", borderRadius: "50%", background: "var(--c-pos-soft)",
        border: "1.5px solid var(--c-pos-line)", color: "var(--c-pos)", display: "grid", placeItems: "center" } },
        React.createElement(WfIcon, { name: "check", s: 24, stroke: 2.6 })),
      React.createElement("div", { style: { flex: 1, minWidth: 0 } },
        React.createElement("h3", { style: { margin: 0, fontSize: compact ? "1rem" : "1.09375rem", fontWeight: 600, color: "var(--c-ink)", display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" } },
          t.rich("success_title", { name: R.display_name, accent: (chunks) => React.createElement("span", { style: { fontFamily: "var(--f-serif)", color: "var(--c-accent)" } }, chunks) })),
        React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, marginTop: 8, flexWrap: "wrap" } },
          React.createElement(FormBadge, { form: "team-skill" }), React.createElement(VisBadge, { vis: "team_wide" }),
          React.createElement("code", { className: "bh-mono", style: { fontSize: "0.6875rem", color: "var(--c-ink-4)" } }, R.name)))),
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, padding: "9px 13px", background: "var(--c-pos-soft)", border: "1px solid var(--c-pos-line)", borderRadius: "var(--r-2)" } },
      React.createElement(WfIcon, { name: "route", s: 14, stroke: 2, style: { color: "var(--c-pos)" } }),
      React.createElement("span", { style: { fontSize: "0.75rem", color: "var(--c-pos-ink)", fontWeight: 500, whiteSpace: "nowrap" } }, t("routed"))),
    dryrun
      ? React.createElement(DryRunPanel, { verdict: dryrun.verdict, running: dryrun.running })
      : React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, padding: "13px 14px", border: "1px solid var(--c-accent)", borderRadius: "var(--r-2)", background: "var(--c-accent-tint)" } },
          React.createElement(WfIcon, { name: "flask", s: 18, stroke: 2, style: { color: "var(--c-accent)", flex: "0 0 auto" } }),
          React.createElement("div", { style: { flex: 1, lineHeight: 1.45 } },
            React.createElement("div", { style: { fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)" } }, t("try_run_title")),
            React.createElement("div", { style: { fontSize: "0.71875rem", color: "var(--c-ink-3)" } }, t("try_run_sub"))),
          React.createElement("button", { style: btnPrimary(false) }, React.createElement(WfIcon, { name: "play", s: 13, stroke: 2 }), t("try_run"))),
    React.createElement("div", null,
      React.createElement("div", { style: { fontSize: "0.71875rem", color: "var(--c-ink-3)", marginBottom: 7 } }, t("reuse_hint")),
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 9, padding: "10px 13px", background: "var(--c-bg)", border: "1px dashed var(--c-line)", borderRadius: "var(--r-2)" } },
        React.createElement(WfIcon, { name: "route", s: 14, stroke: 2, style: { color: "var(--c-cool)", flex: "0 0 auto" } }),
        React.createElement("span", { style: { flex: 1, fontSize: "0.78125rem", color: "var(--c-ink-2)" } }, t("quoted", { text: R.triggers[0] })),
        React.createElement("button", { style: { ...linkBtn(), padding: "4px 6px" } }, React.createElement(WfIcon, { name: "copy", s: 13, stroke: 2 }), t("copy")),
        React.createElement("button", { style: { ...btnPrimary(false), padding: "7px 13px", fontSize: "0.78125rem" } }, React.createElement(WfIcon, { name: "play", s: 12, stroke: 2 }), t("launch")))),
    React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, paddingTop: 6, borderTop: "1px solid var(--c-line-2)" } },
      React.createElement("button", { style: linkBtn() }, React.createElement(WfIcon, { name: "layers", s: 13, stroke: 2 }), t("go_manage")),
      React.createElement("div", { style: { flex: 1 } }),
      React.createElement("button", { style: btnPos() }, React.createElement(WfIcon, { name: "check", s: 15, stroke: 2.4 }), t("done"))));
}

const STEP_OF: Record<string, number> = { loading: 0, step1: 0, step2: 1, "step3-skill": 2, "step3-subagent": 2, conflict: 2, readonly: 2, oral: 2 };

export function SolidifyCard({ scenario = "step1", compact }: { scenario?: string; compact?: boolean }) {
  const isSuccess = scenario === "success" || scenario.startsWith("dryrun");
  const step = isSuccess ? 3 : (STEP_OF[scenario] ?? 0);
  const showStepper = scenario !== "loading" && scenario !== "oral";
  const t = useTranslations("workflow_solidify.card");
  const R = useSampleRecipe();
  const ctx: Ctx = { compact, t, R };

  let body: React.ReactNode;
  if (scenario === "loading") body = renderLoading(ctx);
  else if (scenario === "step1") body = renderStep1(ctx);
  else if (scenario === "step2") body = renderStep2(ctx);
  else if (scenario === "step3-skill") body = renderStep3(ctx, "skill");
  else if (scenario === "step3-subagent") body = renderStep3(ctx, "subagent");
  else if (scenario === "conflict") body = renderConflict(ctx);
  else if (scenario === "readonly") body = renderReadonly(ctx);
  else if (scenario === "oral") body = renderOral(ctx);
  else if (scenario === "success") body = renderSuccess(ctx, null);
  else if (scenario === "dryrun-running") body = renderSuccess(ctx, { running: true });
  else body = renderSuccess(ctx, { verdict: scenario.replace("dryrun-", "") });

  return React.createElement("div", { style: wrap, "data-screen-label": "solidify · " + scenario },
    React.createElement(Header, { compact }),
    showStepper && React.createElement(Stepper, { active: step, compact }),
    body);
}
