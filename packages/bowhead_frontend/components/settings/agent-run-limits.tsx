"use client";

/**
 * Team agent run limits (turn time, step limit per task, tool calls per turn).
 * Design: docs/design/agent-run-limits.md §4.1.1 and the "Agent Run Limits Spec" mockup.
 * Owners and Team admins only (the module and the API are admin-only).
 */


import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ApiError } from "@/lib/api-client";
import {
  resetAgentRunLimits,
  saveAgentRunLimits,
  type AgentRunLimits,
} from "@/lib/agent-run-limits-api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { fieldDescribedBy } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { SaveBar } from "@/components/ui/save-bar";
import { FieldRow } from "@/components/ui/section-heading";

type StepMode = "unlimited" | "at_most";
type CallMode = "automatic" | "at_most";
type ErrorKind = "save_failed" | "conflict" | "forbidden";

interface Draft {
  minutes: string;
  stepMode: StepMode;
  steps: string;
  toolCalls: string;
  modelCallMode: CallMode;
  modelCallSeconds: string;
}

function draftOf(limits: AgentRunLimits, previousSteps?: string): Draft {
  return {
    minutes: String(Math.round(limits.turnTimeLimitS / 60)),
    stepMode: limits.stepLimit === null ? "unlimited" : "at_most",
    steps: limits.stepLimit === null ? (previousSteps ?? "100") : String(limits.stepLimit),
    toolCalls: String(limits.toolCallLimit),
    modelCallMode: limits.modelCallTimeLimitS == null ? "automatic" : "at_most",
    modelCallSeconds: String(limits.modelCallTimeLimitS ?? 300),
  };
}

function parseInt10(value: string): number {
  return /^[0-9]+$/.test(value.trim()) ? Number(value.trim()) : NaN;
}

function hoursText(minutes: number): string {
  const hours = minutes / 60;
  return Number.isInteger(hours) ? String(hours) : hours.toFixed(1);
}

/** Owners and Team admins only: the Agents settings module is admin-only and so is this API. */
export function AgentRunLimitsSettings({ initial }: { initial: AgentRunLimits }) {
  return <RunLimitsEditor initial={initial} />;
}

function RunLimitsEditor({ initial }: { initial: AgentRunLimits }) {
  const t = useTranslations("settings.agent_run_limits");
  const [limits, setLimits] = useState(initial);
  const [draft, setDraft] = useState<Draft>(() => draftOf(initial));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorKind | null>(null);
  const [saved, setSaved] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  const r = limits.ranges;
  const minMinutes = Math.ceil(r.turnTimeLimitS.min / 60);
  const maxMinutes = Math.floor(r.turnTimeLimitS.max / 60);
  const minutes = parseInt10(draft.minutes);
  const steps = parseInt10(draft.steps);
  const toolCalls = parseInt10(draft.toolCalls);
  const callRange = r.modelCallTimeLimitS;
  const modelCallSeconds = parseInt10(draft.modelCallSeconds);
  const modelCallOk = !callRange || draft.modelCallMode === "automatic"
    || (Number.isInteger(modelCallSeconds) && modelCallSeconds >= callRange.min && modelCallSeconds <= callRange.max);
  const minutesOk = Number.isInteger(minutes) && minutes >= minMinutes && minutes <= maxMinutes;
  const stepsOk = draft.stepMode === "unlimited" || (Number.isInteger(steps) && steps >= r.stepLimit.min && steps <= r.stepLimit.max);
  const toolCallsOk = Number.isInteger(toolCalls) && toolCalls >= r.toolCallLimit.min && toolCalls <= r.toolCallLimit.max;
  const invalidCount = [minutesOk, stepsOk, toolCallsOk, modelCallOk].filter((ok) => !ok).length;

  const baseline = draftOf(limits, draft.steps);
  const dirty =
    draft.minutes !== baseline.minutes ||
    draft.stepMode !== baseline.stepMode ||
    (draft.stepMode === "at_most" && draft.steps !== baseline.steps) ||
    draft.toolCalls !== baseline.toolCalls ||
    draft.modelCallMode !== baseline.modelCallMode ||
    (draft.modelCallMode === "at_most" && draft.modelCallSeconds !== baseline.modelCallSeconds);
  const canSave = dirty && invalidCount === 0 && !busy;

  const run = async (action: (signal: AbortSignal) => Promise<AgentRunLimits>) => {
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setBusy(true);
    setSaved(false);
    setError(null);
    try {
      const updated = await action(controller.signal);
      if (controller.signal.aborted) return;
      setLimits(updated);
      setDraft(draftOf(updated, draft.steps));
      setSaved(true);
    } catch (caught: unknown) {
      if (controller.signal.aborted) return;
      setError(
        caught instanceof ApiError && caught.status === 409
          ? "conflict"
          : caught instanceof ApiError && caught.status === 403
            ? "forbidden"
            : "save_failed",
      );
    } finally {
      if (!controller.signal.aborted) {
        setBusy(false);
        setConfirmingReset(false);
      }
    }
  };

  const save = () => {
    if (!canSave) return;
    void run((signal) =>
      saveAgentRunLimits(
        {
          turnTimeLimitS: minutes * 60,
          stepLimit: draft.stepMode === "unlimited" ? null : steps,
          toolCallLimit: toolCalls,
          ...(callRange ? { modelCallTimeLimitS: draft.modelCallMode === "automatic" ? null : modelCallSeconds } : {}),
        },
        limits.version,
        signal,
      ),
    );
  };

  const edit = (patch: Partial<Draft>) => {
    setDraft((current) => ({ ...current, ...patch }));
    setSaved(false);
  };

  const rangeError = (label: string, min: number, max: number) => t("error_range", { field: label, min, max });

  return (
    <section className="agent-run-limits" aria-labelledby="agent-run-limits-title">
      <div className="agent-run-limits__head">
        <h2 id="agent-run-limits-title" className="agent-run-limits__title">{t("title")}</h2>
        {limits.isDefault ? <Badge tone="neutral" size="sm">{t("default_badge")}</Badge> : (
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirmingReset(true)}>{t("reset")}</Button>
        )}
      </div>
      <p className="ui-page-desc">{t("intro")}</p>
      {limits.runtimeControls ? (
        <Notice tone="info">
          {t("effective_policy", {
            effective: limits.runtimeControls.effectiveStepLimit ?? t("steps_unlimited"),
            system: limits.runtimeControls.systemStepLimit ?? t("steps_unlimited"),
            seconds: limits.runtimeControls.stallTimeoutS,
          })}
        </Notice>
      ) : null}
      {error ? <Notice tone="error">{t(`error_${error}`)}</Notice> : null}
      {saved ? <Notice tone="success">{t("saved")}</Notice> : null}
      <form className="ui-group-card agent-run-limits__rows" onSubmit={(event) => { event.preventDefault(); save(); }}>
        <FieldRow
          className="agent-run-limits__row"
          titleId="arl-turn-time-title"
          title={t("turn_time_title")}
          help={<ConfigurationFieldHelp contract={limits.field_contract} field="team_run_limits.turn_time_limit_s" label={t("turn_time_title")} />}
          description={t("turn_time_desc")}
          control={
            <span className="agent-run-limits__control">
              <span className="agent-run-limits__num">
                <input
                  id="arl-turn-time"
                  className="settings-input ui-input agent-run-limits__input"
                  inputMode="numeric"
                  autoComplete="off"
                  aria-labelledby="arl-turn-time-title"
                  aria-invalid={!minutesOk}
                  aria-describedby={fieldDescribedBy("arl-turn-time", { hint: true, error: !minutesOk })}
                  value={draft.minutes}
                  disabled={busy}
                  onChange={(event) => edit({ minutes: event.target.value })}
                />
                <span>{t("unit_minutes")}</span>
              </span>
              <span id="arl-turn-time-hint" className="settings-field__hint">
                {minutesOk
                  ? t("turn_time_hint_value", { hours: hoursText(minutes), min: minMinutes, max: hoursText(maxMinutes) })
                  : t("turn_time_hint", { min: minMinutes, max: hoursText(maxMinutes) })}
              </span>
              {!minutesOk ? (
                <span id="arl-turn-time-error" className="settings-field__error" role="alert">
                  {rangeError(t("turn_time_title"), minMinutes, maxMinutes)}
                </span>
              ) : null}
            </span>
          }
        />
        {callRange ? <FieldRow
          className="agent-run-limits__row"
          titleId="arl-model-call-title"
          title={t("model_call_title")}
          help={<ConfigurationFieldHelp contract={limits.field_contract} field="team_run_limits.model_call_time_limit_s" label={t("model_call_title")} />}
          description={t("model_call_desc")}
          control={
            <span className="agent-run-limits__control">
              <RadioGroup<CallMode>
                appearance="segmented"
                busy={busy}
                orientation="both"
                className="agent-run-limits__radios"
                aria-labelledby="arl-model-call-title"
                value={draft.modelCallMode}
                onValueChange={(modelCallMode) => edit({ modelCallMode })}
              >
                <Radio value="automatic" className="agent-run-limits__radio">{t("model_call_automatic")}</Radio>
                <Radio value="at_most" className="agent-run-limits__radio">{t("steps_at_most")}</Radio>
              </RadioGroup>
              <span className="agent-run-limits__num">
                <input
                  id="arl-model-call"
                  className="settings-input ui-input agent-run-limits__input"
                  inputMode="numeric"
                  autoComplete="off"
                  aria-label={t("model_call_input_label")}
                  aria-invalid={!modelCallOk}
                  aria-describedby={fieldDescribedBy("arl-model-call", { hint: true, error: !modelCallOk })}
                  value={draft.modelCallSeconds}
                  disabled={busy || draft.modelCallMode === "automatic"}
                  onChange={(event) => edit({ modelCallSeconds: event.target.value })}
                />
                <span className="agent-run-limits__unit">{t("seconds")}</span>
              </span>
              <span id="arl-model-call-hint" className="settings-field__hint">{t("model_call_hint", { min: callRange.min, max: callRange.max })}</span>
              {!modelCallOk ? <span id="arl-model-call-error" className="settings-field__error" role="alert">
                {rangeError(t("model_call_title"), callRange.min, callRange.max)}
              </span> : null}
            </span>
          }
        /> : null}
        <FieldRow
          className="agent-run-limits__row"
          titleId="arl-steps-title"
          title={t("steps_title")}
          help={<ConfigurationFieldHelp contract={limits.field_contract} field="team_run_limits.step_limit" label={t("steps_title")} />}
          description={t("steps_desc")}
          control={
            <span className="agent-run-limits__control">
              <RadioGroup<StepMode>
                appearance="segmented"
                busy={busy}
                orientation="both"
                className="agent-run-limits__radios"
                aria-labelledby="arl-steps-title"
                value={draft.stepMode}
                onValueChange={(stepMode) => edit({ stepMode })}
              >
                <Radio value="unlimited" className="agent-run-limits__radio">{t("steps_unlimited")}</Radio>
                <Radio value="at_most" className="agent-run-limits__radio">{t("steps_at_most")}</Radio>
              </RadioGroup>
              <span className="agent-run-limits__num">
                <input
                  id="arl-steps"
                  className="settings-input ui-input agent-run-limits__input"
                  inputMode="numeric"
                  autoComplete="off"
                  aria-label={t("steps_input_label")}
                  aria-invalid={!stepsOk}
                  aria-describedby={fieldDescribedBy("arl-steps", { error: !stepsOk })}
                  value={draft.steps}
                  disabled={busy || draft.stepMode === "unlimited"}
                  onChange={(event) => edit({ steps: event.target.value })}
                />
                <span>{t("unit_steps")}</span>
              </span>
              {!stepsOk ? (
                <span id="arl-steps-error" className="settings-field__error" role="alert">
                  {rangeError(t("steps_title"), r.stepLimit.min, r.stepLimit.max)}
                </span>
              ) : null}
            </span>
          }
        />
        <FieldRow
          className="agent-run-limits__row"
          titleId="arl-tool-calls-title"
          title={t("tool_calls_title")}
          help={<ConfigurationFieldHelp contract={limits.field_contract} field="team_run_limits.tool_call_limit" label={t("tool_calls_title")} />}
          description={t("tool_calls_desc")}
          control={
            <span className="agent-run-limits__control">
              <span className="agent-run-limits__num">
                <input
                  id="arl-tool-calls"
                  className="settings-input ui-input agent-run-limits__input"
                  inputMode="numeric"
                  autoComplete="off"
                  aria-labelledby="arl-tool-calls-title"
                  aria-invalid={!toolCallsOk}
                  aria-describedby={fieldDescribedBy("arl-tool-calls", { hint: true, error: !toolCallsOk })}
                  value={draft.toolCalls}
                  disabled={busy}
                  onChange={(event) => edit({ toolCalls: event.target.value })}
                />
                <span>{t("unit_calls")}</span>
              </span>
              <span id="arl-tool-calls-hint" className="settings-field__hint">
                {t("tool_calls_hint", { min: r.toolCallLimit.min, max: r.toolCallLimit.max })}
              </span>
              {!toolCallsOk ? (
                <span id="arl-tool-calls-error" className="settings-field__error" role="alert">
                  {rangeError(t("tool_calls_title"), r.toolCallLimit.min, r.toolCallLimit.max)}
                </span>
              ) : null}
            </span>
          }
        />
      </form>
      <Notice tone="muted">{t("footer")}</Notice>
      <SaveBar
        dirty={dirty}
        saving={busy}
        saveDisabled={!canSave}
        message={invalidCount > 0 ? t("invalid_count", { count: invalidCount }) : t("unsaved")}
        onSave={save}
        onDiscard={() => { setDraft(draftOf(limits, draft.steps)); setError(null); }}
      />
      {confirmingReset ? (
        <ConfirmDialog
          title={t("reset_title")}
          description={t("reset_confirm", {
            minutes: Math.round(limits.defaults.turnTimeLimitS / 60),
            calls: limits.defaults.toolCallLimit,
          }) + (callRange ? ` ${t("model_call_reset")}` : "")}
          confirmLabel={t("reset")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => { if (!busy) setConfirmingReset(false); }}
          onConfirm={() => void run((signal) => resetAgentRunLimits(signal))}
        />
      ) : null}
    </section>
  );
}
