"use client";
/**
 * workflow-solidify S2 — CreateWorkflowCard (solidification HITL, live).
 *
 * The functional sibling of the verified presentational SolidifyCard (components/workflow-solidify/
 * solidify-card.tsx, in the spike): same Claude Design visual vocabulary (wf-shared primitives,
 * --c-* tokens, stepper) but a real state machine wired to /api/v1/workflow-solidify/{distill,
 * dry-run} + /api/v1/team-skills + /skills/generate. Opened by the cca.workflow_solidify_open event
 * (page.tsx seeds it from runner.solidifyOpen). Flow: distill → preview → triggers/slots → form →
 * save → success(+ dry-run). Falls back to oral mode when distill returns {ok:false}.
 */
import { useTranslations } from "next-intl";
import React, { useCallback, useEffect, useRef, useState } from "react";

import { ErrorNotice } from "@/components/error-notice";
import { WfIcon } from "@/components/workflow-solidify/wf-icons";
import {
  Callout, CheckpointRow, FieldLabel, FormBadge, ScriptBlock, SectionLabel, SlotRow, Stepper,
  StepLine, VerdictBadge, VisBadge, btnGhost, btnPos, btnPrimary, linkBtn, spin,
} from "@/components/workflow-solidify/wf-shared";
import {
  createTeamSkill, createPersonalSkill, distillSession, distillWorkflow, dryRunRecipe,
  type DistillResult, type DryRunResult,
} from "@/lib/workflow-solidify-api";
import type { ErrorPresentation } from "@/lib/error-presentation";
import {
  semanticSkillWorkflowErrorPresentation,
  skillWorkflowErrorPresentation,
} from "@/lib/skill-workflow-error";
import { getActiveSpaceId } from "@/lib/space-storage";
import { appPath } from "@/lib/base-path";

export interface CreateWorkflowCardProps {
  /** distill-target turn id; null on the conversational entry (resolved by the caller). */
  turnId: string | null;
  /** Whole-conversation solidification (S2). Mutually exclusive with turnId: when supplied, distill whole session and ignore turnId. */
  sessionId?: string | null;
  hint?: string | null;
  onClose: () => void;
  onSaved?: (name: string) => void;
}

type Step = "loading" | "preview" | "triggers" | "form" | "saving" | "success" | "oral" | "error";
// P1 (review): remove subagent — save() has no subagent branch (silently saves as team-skill, making UI misleading).
// Actual subagent creation uses separate /team-subagents path, to be connected later.
type FormChoice = "skill" | "team-skill";

const wrap: React.CSSProperties = { border: "1px solid var(--c-line)", borderRadius: "var(--r-4)",
  background: "var(--c-surface)", overflow: "hidden", boxShadow: "var(--shadow-float)" };
const STEP_IDX: Record<Step, number> = { loading: 0, preview: 0, triggers: 1, form: 2, saving: 2, oral: 2, success: 3, error: 0 };

/** body_md numbered steps → array (for StepLine); fall back to the whole body if none parse. */
function parseSteps(bodyMd: string): string[] {
  const lines = (bodyMd || "").split("\n").map((l) => l.trim());
  const steps = lines.filter((l) => /^\d+[.、)]/.test(l)).map((l) => l.replace(/^\d+[.、)]\s*/, ""));
  return steps.length ? steps : [];
}

export function CreateWorkflowCard({ turnId, sessionId, hint, onClose, onSaved }: CreateWorkflowCardProps) {
  const t = useTranslations("create_workflow");
  const te = useTranslations("errors");
  const presentError = useCallback(
    (error: unknown) =>
      skillWorkflowErrorPresentation(error, {
        surface: "task",
        copy: {
          generic: t("error_generic"),
          transport: {
            requestTimedOut: te("transport.requestTimedOut"),
            serviceUnreachable: te("transport.serviceUnreachable"),
            retry: te("transport.retry"),
          },
        },
      }),
    [t, te],
  );
  const [step, setStep] = useState<Step>("loading");
  const [data, setData] = useState<DistillResult | null>(null);
  const [err, setErr] = useState<ErrorPresentation | null>(null);
  const [form, setForm] = useState<FormChoice>("skill");
  const [visibility, setVisibility] = useState<"team_wide" | "creator_only">("team_wide");
  const [savedName, setSavedName] = useState<string>("");
  // scheduled-recipe-runs S1: retain **structured reference** after saving (personal=name, team=backend-returned ID).
  // Name alone is insufficient: team_skill binding key is ID; name guesses can bind a different same-name copy.
  const [savedRef, setSavedRef] = useState<{ form: string; id_or_name: string } | null>(null);
  const [dryrun, setDryrun] = useState<DryRunResult | null>(null);
  const [dryrunError, setDryrunError] = useState<ErrorPresentation | null>(null);
  const [dryrunBusy, setDryrunBusy] = useState(false);

  // P2 (review): mountedRef guards every distill path (opening card + error/oral retries), avoiding setState warnings after unmount.
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  // Distillation (shared routine for opening + retry, with unified alive guard + friendly error mapping).
  // S2: sessionId present = whole-conversation solidification via session leg; otherwise retain single-turn path.
  const runDistill = useCallback(async () => {
    if (!sessionId && !turnId) { setStep("error"); setErr({ message: t("no_turn") }); return; }
    setStep("loading");
    setErr(null);
    try {
      const r = sessionId ? await distillSession(sessionId) : await distillWorkflow(turnId as string);
      if (!mountedRef.current) return;
      setData(r);
      if (r.ok === false) { setStep("oral"); return; }
      setForm("skill");   // P1: personal skill by default; user can switch to team sharing in form step.
      setStep("preview");
    } catch (e) {
      if (!mountedRef.current) return;
      setErr(presentError(e));
      setStep("error");
    }
  }, [presentError, sessionId, turnId, t]);

  useEffect(() => { void runDistill(); }, [runDistill]);

  const save = useCallback(async () => {
    if (!data) return;
    setStep("saving");
    try {
      let name = data.name;
      let ref: { form: string; id_or_name: string } | null = null;
      if (form === "skill") {
        const out = await createPersonalSkill({ bodyMd: data.body_md, scripts: data.scripts, sourceTurnId: turnId });
        name = out.name || data.name;
        ref = { form: "personal_skill", id_or_name: name };
      } else {
        const out = await createTeamSkill({ bodyMd: data.body_md, scripts: data.scripts,
          displayName: data.display_name, description: data.description, visibility });
        name = out.name || data.name;
        if (out.id) ref = { form: "team_skill", id_or_name: out.id };
      }
      setSavedName(name);
      setSavedRef(ref);
      setStep("success");
      onSaved?.(name);
    } catch (e) {
      setErr(presentError(e));
      setStep("error");
    }
  }, [data, form, visibility, turnId, onSaved, presentError]);

  const runDryRun = useCallback(async () => {
    if (!data) return;
    setDryrunBusy(true);
    setDryrunError(null);
    try {
      // Batch 1/c10 (impl review P1): backend no longer passes team ID as space (c4 fail-closed 400),
      // dry-run must explicitly include current space ID — read canonical key (same as kb-api getWorkspaceHeader).
      const wsId = getActiveSpaceId() || null;  // Batch 3/c2 consolidation
      const r = await dryRunRecipe({ name: savedName || data.name, triggerQuery: hint || data.description,
        form, checkpoints: data.checkpoints, workspaceId: wsId || undefined });  // P2: send actual form (skill|team-skill) → verdict reaches correct store.
      setDryrun(r);
      if (r.dispatch_error_code) {
        setDryrunError(
          semanticSkillWorkflowErrorPresentation(
            {
              code: r.dispatch_error_code,
              message: r.dispatch_error,
              diagnostics: r.dispatch_error_diagnostics,
            },
            t("error_generic"),
          ),
        );
      }
    } catch (e) {
      setDryrunError(presentError(e));
    } finally { setDryrunBusy(false); }
  }, [data, savedName, hint, form, presentError, t]);

  const Header = () => React.createElement("div", { style: { display: "flex", alignItems: "flex-start", gap: 12, padding: "16px 18px 14px" } },
    React.createElement("span", { style: { width: 36, height: 36, flex: "0 0 36px", borderRadius: 11, background: "var(--c-accent-soft)", color: "var(--c-accent)", display: "grid", placeItems: "center" } },
      React.createElement(WfIcon, { name: "crystal", s: 19, stroke: 1.8 })),
    React.createElement("div", { style: { flex: 1, minWidth: 0 } },
      React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 9, flexWrap: "wrap" } },
        React.createElement("h3", { style: { margin: 0, fontSize: "1.03125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em" } }, t("title")),
        React.createElement("span", { className: "bh-mono", style: { fontSize: "0.625rem", color: "var(--c-ink-4)", background: "var(--c-surface-2)", border: "1px solid var(--c-line)", borderRadius: 999, padding: "2px 8px" } }, t("confirm_badge"))),
      React.createElement("p", { style: { margin: "4px 0 0", fontSize: "0.78125rem", color: "var(--c-ink-3)" } }, t("subtitle"))),
    React.createElement("button", { onClick: onClose, "aria-label": t("close_aria"), style: { width: 28, height: 28, flex: "0 0 28px", border: "none", background: "transparent", color: "var(--c-ink-4)", cursor: "pointer", borderRadius: 8, display: "grid", placeItems: "center" } },
      React.createElement(WfIcon, { name: "x", s: 17, stroke: 2 })));

  const pad: React.CSSProperties = { padding: "16px 18px 18px", display: "flex", flexDirection: "column", gap: 15 };
  const footRow: React.CSSProperties = { display: "flex", alignItems: "center", gap: 12, marginTop: 2, paddingTop: 14, borderTop: "1px solid var(--c-line-2)" };

  let body: React.ReactNode = null;
  const d = data;

  if (step === "loading" || step === "saving") {
    body = React.createElement("div", { style: { padding: "34px 22px 38px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center" } },
      React.createElement("div", { style: { position: "relative", width: 52, height: 52, marginBottom: 18 } },
        React.createElement("span", { style: { position: "absolute", inset: 0, borderRadius: "50%", border: "2px solid var(--c-accent-soft)" } }),
        React.createElement("span", { style: { position: "absolute", inset: 0, borderRadius: "50%", border: "2px solid transparent", borderTopColor: "var(--c-accent)", animation: "wf-spin 1s linear infinite" } }),
        React.createElement("span", { style: { position: "absolute", inset: 0, display: "grid", placeItems: "center", color: "var(--c-accent)" } }, React.createElement(WfIcon, { name: "crystal", s: 22, stroke: 1.8 }))),
      React.createElement("h3", { style: { margin: 0, fontSize: "0.96875rem", fontWeight: 600, color: "var(--c-ink)" } }, step === "saving" ? t("saving_title") : t("distilling_title")),
      React.createElement("p", { style: { margin: "7px 0 0", fontSize: "0.78125rem", color: "var(--c-ink-3)", lineHeight: 1.6, maxWidth: 360 } },
        step === "saving" ? t("saving_body") : [t("distilling_body_pre"), React.createElement("b", { key: "b", style: { color: "var(--c-ink-2)", fontWeight: 600 } }, t("distilling_body_emphasis")), t("distilling_body_post")]));
  } else if (step === "error") {
    body = React.createElement("div", { style: pad },
      React.createElement(ErrorNotice, {
        message: err?.message || t("error_generic"),
        diagnostics: err?.diagnostics,
        surface: "task",
        diagnosticsEnabled: true,
      }),
      React.createElement("div", { style: footRow },
        (turnId || sessionId) ? React.createElement("button", { onClick: () => void runDistill(), style: btnGhost() },
          React.createElement(WfIcon, { name: "rotateCw", s: 15, stroke: 2 }), t("retry")) : null,
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { onClick: onClose, style: btnGhost() }, t("close"))));
  } else if (step === "oral" && d) {
    const oralError = semanticSkillWorkflowErrorPresentation(
      {
        code: d.reason_code,
        message: d.reason,
        diagnostics: d.diagnostics,
      },
      t("distill_failed"),
    );
    body = React.createElement("div", { className: "bh-scroll", style: pad },
      React.createElement(ErrorNotice, {
        message: `${t("oral_pre")}${oralError.message}${t("oral_post")}`,
        diagnostics: oralError.diagnostics,
        surface: "task",
        diagnosticsEnabled: true,
        liveRole: "status",
      }),
      d.scripts.length > 0 && React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "code", hint: t("scripts_verified_hint") }, "scripts"),
        React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8, marginTop: 8 } },
          d.scripts.map((s, i) => React.createElement(ScriptBlock, { key: i, name: s.name, lines: s.content.split("\n"), open: i === 0 })))),
      React.createElement("div", { style: footRow },
        React.createElement("button", { onClick: () => void runDistill(), style: btnGhost() },
          React.createElement(WfIcon, { name: "rotateCw", s: 15, stroke: 2 }), t("retry_distill")),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { onClick: onClose, style: linkBtn() }, t("close"))));
  } else if (step === "preview" && d) {
    const steps = parseSteps(d.body_md);
    body = React.createElement("div", { className: "bh-scroll", style: pad },
      React.createElement("div", null,
        React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" } },
          React.createElement("h2", { style: { margin: 0, fontSize: "1.3125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em", fontFamily: "var(--f-serif)" } }, d.display_name || d.name)),
        React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 7, marginTop: 6 } },
          React.createElement("code", { className: "bh-mono", style: { fontSize: "0.75rem", color: "var(--c-ink-3)" } }, d.name)),
        React.createElement("p", { style: { margin: "10px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-2)", lineHeight: 1.6 } }, d.description)),
      // S2: whole-conversation solidification states merged turn count — user chose "固化整段对话" and must see multiple turns were included,
      // and superseded interpretations do not enter steps (backend filters for final consensus path).
      d.source === "session" && Callout({ tone: "info", icon: "layers", children: React.createElement("span", null,
        t("session_scope_pre"), React.createElement("b", { style: { fontWeight: 600 } }, d.turn_count ?? 0), t("session_scope_post")) }),
      d.truncated_notice_code && Callout({ tone: "warn", icon: "scissors", children: t("session_truncated") }),
      d.truncated_steps.length > 0 && Callout({ tone: "info", icon: "scissors", children: React.createElement("span", null,
        t("truncated_pre"), React.createElement("b", { style: { fontWeight: 600 } }, d.truncated_steps.length), t("truncated_post")) }),
      steps.length > 0 && React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "listChecks", hint: t("steps_hint", { count: steps.length }) }, t("steps_label")),
        React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 10, marginTop: 10 } },
          steps.map((s, i) => React.createElement(StepLine, { key: i, n: i + 1 }, s)))),
      d.checkpoints.length > 0 && React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "target", hint: t("checkpoints_hint") }, t("checkpoints_label")),
        React.createElement("div", { style: { marginTop: 8, padding: "8px 12px", border: "1px solid var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)" } },
          d.checkpoints.map((c, i) => React.createElement(CheckpointRow, { key: i, text: c, mode: "assert" })))),
      d.scripts.length > 0 && React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "code", hint: t("scripts_readonly_hint") }, "scripts"),
        React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8, marginTop: 8 } },
          d.scripts.map((s, i) => React.createElement(ScriptBlock, { key: i, name: s.name, lines: s.content.split("\n"), open: i === 0 })))),
      React.createElement("div", { style: footRow },
        React.createElement("span", { className: "bh-mono", style: { fontSize: "0.65625rem", color: "var(--c-ink-4)" } }, t("readonly_preview")),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { onClick: () => setStep(d.slots.length ? "triggers" : "form"), style: btnPrimary(false) },
          d.slots.length ? t("next_params") : t("next_form"), React.createElement(WfIcon, { name: "chevronRight", s: 16, stroke: 2.2 }))));
  } else if (step === "triggers" && d) {
    body = React.createElement("div", { className: "bh-scroll", style: pad },
      Callout({ tone: "info", icon: "route", children: t("triggers_intro") }),
      React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "sliders", hint: t("slots_hint") }, t("slots_label")),
        React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8, marginTop: 9 } },
          d.slots.map((s, i) => React.createElement(SlotRow, { key: i, token: s.token, example: s.example || "" })))),
      React.createElement("div", { style: footRow },
        React.createElement("button", { onClick: () => setStep("preview"), style: btnGhost() }, React.createElement(WfIcon, { name: "chevronLeft", s: 15, stroke: 2 }), t("back")),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { onClick: () => setStep("form"), style: btnPrimary(false) }, t("next_form"), React.createElement(WfIcon, { name: "chevronRight", s: 16, stroke: 2.2 }))));
  } else if (step === "form" && d) {
    const opt = (key: FormChoice, icon: string, title: string, sub: string) =>
      React.createElement("button", { key, onClick: () => setForm(key), style: { display: "flex", gap: 12, alignItems: "flex-start", padding: "12px 13px", borderRadius: "var(--r-2)", cursor: "pointer", textAlign: "left", width: "100%",
        background: form === key ? "var(--c-accent-tint)" : "var(--c-surface)", border: "1.5px solid " + (form === key ? "var(--c-accent)" : "var(--c-line)"), boxShadow: form === key ? "0 0 0 3px var(--c-accent-soft)" : "none" } },
        React.createElement("span", { style: { width: 19, height: 19, flex: "0 0 19px", borderRadius: "50%", marginTop: 1, border: "1.5px solid " + (form === key ? "var(--c-accent)" : "var(--c-line)"), background: form === key ? "var(--c-accent)" : "var(--c-surface)", display: "grid", placeItems: "center" } },
          form === key && React.createElement("span", { style: { width: 7, height: 7, borderRadius: "50%", background: "var(--c-accent-ink)" } })),
        React.createElement("span", { style: { color: form === key ? "var(--c-accent)" : "var(--c-ink-4)", flex: "0 0 auto", marginTop: 1 } }, React.createElement(WfIcon, { name: icon, s: 18, stroke: 1.9 })),
        React.createElement("span", { style: { flex: 1 } },
          React.createElement("span", { style: { display: "block", fontSize: "0.84375rem", fontWeight: 600, color: "var(--c-ink)" } }, title),
          React.createElement("span", { style: { display: "block", fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 3, lineHeight: 1.45 } }, sub)));
    body = React.createElement("div", { className: "bh-scroll", style: pad },
      React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "layers" }, t("form_label")),
        React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 9, marginTop: 9 } },
          opt("skill", "user", t("form_personal_title"), t("form_personal_sub")),
          opt("team-skill", "users", t("form_team_title"), t("form_team_sub")))),
      form === "team-skill" && React.createElement("div", null,
        React.createElement(SectionLabel, { icon: "eye" }, t("visibility_label")),
        React.createElement("div", { style: { display: "flex", gap: 4, padding: 4, background: "var(--c-surface-2)", borderRadius: "var(--r-2)", border: "1px solid var(--c-line)", marginTop: 9 } },
          ([["team_wide", "globe", t("vis_team_wide")], ["creator_only", "eyeOff", t("vis_creator_only")]] as [string, string, string][]).map(([k, ic, lbl]) => {
            const sel = visibility === k;
            return React.createElement("button", { key: k, onClick: () => setVisibility(k as "team_wide" | "creator_only"), style: { flex: 1, display: "flex", alignItems: "center", gap: 9, padding: "9px 12px", borderRadius: 9, cursor: "pointer", border: "1px solid " + (sel ? "var(--c-line)" : "transparent"), background: sel ? "var(--c-surface)" : "transparent", boxShadow: sel ? "var(--shadow-1)" : "none" } },
              React.createElement("span", { style: { color: sel ? "var(--c-accent)" : "var(--c-ink-4)" } }, React.createElement(WfIcon, { name: ic, s: 16, stroke: 1.9 })),
              React.createElement("span", { style: { fontSize: "0.78125rem", fontWeight: 600, color: sel ? "var(--c-ink)" : "var(--c-ink-3)" } }, lbl));
          }))),
      React.createElement("div", { style: footRow },
        React.createElement("button", { onClick: () => setStep(d.slots.length ? "triggers" : "preview"), style: btnGhost() }, React.createElement(WfIcon, { name: "chevronLeft", s: 15, stroke: 2 }), t("back")),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { onClick: save, style: btnPrimary(false) }, React.createElement(WfIcon, { name: "crystal", s: 15, stroke: 1.8 }), t("save_solidify"))));
  } else if (step === "success" && d) {
    body = React.createElement("div", { className: "bh-scroll", style: { padding: "26px 22px 20px", display: "flex", flexDirection: "column", gap: 16 } },
      React.createElement("div", { style: { display: "flex", alignItems: "flex-start", gap: 13 } },
        React.createElement("span", { style: { width: 44, height: 44, flex: "0 0 44px", borderRadius: "50%", background: "var(--c-pos-soft)", border: "1.5px solid var(--c-pos-line)", color: "var(--c-pos)", display: "grid", placeItems: "center" } },
          React.createElement(WfIcon, { name: "check", s: 24, stroke: 2.6 })),
        React.createElement("div", { style: { flex: 1, minWidth: 0 } },
          React.createElement("h3", { style: { margin: 0, fontSize: "1.09375rem", fontWeight: 600, color: "var(--c-ink)", display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" } },
            t("success_pre"), React.createElement("span", { style: { fontFamily: "var(--f-serif)", color: "var(--c-accent)" } }, d.display_name || savedName), t("success_post")),
          React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8, marginTop: 8, flexWrap: "wrap" } },
            React.createElement(FormBadge, { form }), (form !== "skill") && React.createElement(VisBadge, { vis: visibility }),
            React.createElement("code", { className: "bh-mono", style: { fontSize: "0.6875rem", color: "var(--c-ink-4)" } }, savedName || d.name)))),
      dryrun
        ? React.createElement("div", { style: { border: "1px solid var(--c-line)", borderRadius: "var(--r-3)", background: "var(--c-surface)", overflow: "hidden" } },
            React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--c-line-2)", background: "var(--c-bg)" } },
              React.createElement(WfIcon, { name: "flask", s: 16, stroke: 2, style: { color: "var(--c-info)" } }),
              React.createElement("span", { style: { fontSize: "0.78125rem", fontWeight: 700, color: "var(--c-ink-2)" } }, t("dryrun_result")),
              React.createElement("div", { style: { flex: 1 } }), React.createElement(VerdictBadge, { verdict: dryrun.verdict, big: true })),
            React.createElement("div", { style: { padding: "13px 14px", display: "flex", flexDirection: "column", gap: 8 } },
              dryrun.checkpoints.map((c, i) => React.createElement(CheckpointRow, { key: i, mode: "judge", text: c.text, pass: c.pass, reason: c.reason })),
              dryrun.final_answer_preview && React.createElement("div", { style: { border: "1px dashed var(--c-line)", borderRadius: "var(--r-2)", padding: "11px 13px", background: "var(--c-bg)" } },
                React.createElement("div", { style: { fontSize: "0.65625rem", fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--c-ink-4)", marginBottom: 6 } }, t("final_answer_preview")),
                React.createElement("p", { style: { margin: 0, fontSize: "0.78125rem", color: "var(--c-ink-2)", lineHeight: 1.6 } }, dryrun.final_answer_preview))))
        : React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, padding: "13px 14px", border: "1px solid var(--c-accent)", borderRadius: "var(--r-2)", background: "var(--c-accent-tint)" } },
            React.createElement(WfIcon, { name: "flask", s: 18, stroke: 2, style: { color: "var(--c-accent)", flex: "0 0 auto" } }),
            React.createElement("div", { style: { flex: 1, lineHeight: 1.45 } },
              React.createElement("div", { style: { fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)" } }, t("dryrun_cta_title")),
              React.createElement("div", { style: { fontSize: "0.71875rem", color: "var(--c-ink-3)" } }, t("dryrun_cta_sub"))),
            React.createElement("button", { onClick: runDryRun, disabled: dryrunBusy, style: btnPrimary(dryrunBusy) },
              dryrunBusy ? React.createElement("span", { style: spin }) : React.createElement(WfIcon, { name: "play", s: 13, stroke: 2 }), dryrunBusy ? t("dryrun_running") : t("dryrun_run"))),
      dryrunError && React.createElement(ErrorNotice, {
        message: dryrunError.message,
        action: dryrunError.action,
        diagnostics: dryrunError.diagnostics,
        surface: "task",
        onAction: () => void runDryRun(),
        diagnosticsEnabled: true,
      }),
      React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, paddingTop: 6, borderTop: "1px solid var(--c-line-2)" } },
        // "设为定时任务" — cross-component drawer interaction would require host-page state; instead carry **structured reference**
        // to routines settings (?bind=<form>:<id>), which reads params to open creation drawer and preselect.
        savedRef && React.createElement("a", {
          "data-testid": "solidify-schedule",
          href: appPath(`/team/settings/scheduled/?bind=${encodeURIComponent(`${savedRef.form}:${savedRef.id_or_name}`)}`),
          style: { ...btnGhost(), textDecoration: "none" },
        }, React.createElement(WfIcon, { name: "clock", s: 15, stroke: 2 }), t("schedule_it")),
        React.createElement("div", { style: { flex: 1 } }),
        React.createElement("button", { onClick: onClose, style: btnPos() }, React.createElement(WfIcon, { name: "check", s: 15, stroke: 2.4 }), t("done"))));
  }

  const showStepper = step !== "loading" && step !== "oral" && step !== "error" && step !== "saving";
  return React.createElement("div", { style: wrap, "data-screen-label": "solidify · live · " + step },
    React.createElement(Header, null),
    showStepper && React.createElement(Stepper, { active: STEP_IDX[step] }),
    body);
}
