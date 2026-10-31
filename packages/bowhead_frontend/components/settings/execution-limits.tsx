"use client";


import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { SaveBar, SaveBarSlotContext } from "@/components/ui/save-bar";
import { FieldRow } from "@/components/ui/section-heading";
import { ApiError } from "@/lib/api-client";
import { getExecutionLimits, saveExecutionLimits, type ExecutionLimits, type ExecutionLimitsValues } from "@/lib/execution-limits-api";

const MIB = 1024 * 1024;
type Draft = Record<keyof ExecutionLimitsValues, string>;
type ErrorKind = "load" | "save" | "conflict" | "forbidden" | "range";

function draftOf(value: ExecutionLimitsValues): Draft {
  return {
    sandbox_default_timeout_s: String(value.sandbox_default_timeout_s),
    sandbox_max_timeout_s: String(value.sandbox_max_timeout_s),
    html_max_bytes: String(value.html_max_bytes / MIB),
  };
}

export function ExecutionLimitsSettings() {
  const t = useTranslations("settings.execution_limits");
  const common = useTranslations("common");
  const [limits, setLimits] = useState<ExecutionLimits | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [inheritDefaults, setInheritDefaults] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorKind | null>(null);
  const [saved, setSaved] = useState(false);
  const request = useRef<AbortController | null>(null);

  const accept = useCallback((next: ExecutionLimits) => {
    setLimits(next);
    setDraft(draftOf(next));
    setInheritDefaults(next.is_default);
  }, []);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(null);
    try {
      const next = await getExecutionLimits(controller.signal);
      if (!controller.signal.aborted) accept(next);
    } catch {
      if (!controller.signal.aborted) setError("load");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [accept]);

  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);

  if (!limits || !draft) {
    return error ? <Notice tone="error">{t("error_load")} <Button variant="secondary" onClick={() => void load()}>{common("retry")}</Button></Notice>
      : <LoadingSkeleton label={t("loading")} />;
  }

  const values: ExecutionLimitsValues = {
    sandbox_default_timeout_s: Number(draft.sandbox_default_timeout_s),
    sandbox_max_timeout_s: Number(draft.sandbox_max_timeout_s),
    html_max_bytes: Math.round(Number(draft.html_max_bytes) * MIB),
  };
  const fields = Object.keys(values) as Array<keyof ExecutionLimitsValues>;
  const valid = (key: keyof ExecutionLimitsValues) => {
    const r = limits.ranges[key];
    return /^\d+(?:\.\d+)?$/.test(draft[key]) && Number.isSafeInteger(values[key])
      && (key === "html_max_bytes" || /^\d+$/.test(draft[key]))
      && values[key] >= r.min && values[key] <= r.max
      && (key !== "sandbox_default_timeout_s" || values[key] <= values.sandbox_max_timeout_s);
  };
  const baseline = draftOf(limits);
  const dirty = inheritDefaults !== limits.is_default || fields.some((key) => draft[key] !== baseline[key]);
  const validAll = fields.every(valid);

  const save = async () => {
    if (!dirty || !validAll || busy) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const next = await saveExecutionLimits(inheritDefaults ? null : values, limits.version, controller.signal);
      if (!controller.signal.aborted) { accept(next); setSaved(true); }
    } catch (caught) {
      if (!controller.signal.aborted) setError(caught instanceof ApiError
        ? caught.status === 409 ? "conflict" : caught.status === 403 ? "forbidden" : caught.status === 422 ? "range" : "save"
        : "save");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const control = (key: keyof ExecutionLimitsValues) => {
    const id = `execution-${key}`;
    const html = key === "html_max_bytes";
    const range = limits.ranges[key];
    return <span className="agent-run-limits__control">
      <span className="agent-run-limits__num">
        <input id={id} className="settings-input ui-input agent-run-limits__input" inputMode={html ? "decimal" : "numeric"}
          autoComplete="off" aria-labelledby={`${id}-title`} aria-describedby={`${id}-hint${valid(key) ? "" : ` ${id}-error`}`}
          aria-invalid={!valid(key)} disabled={busy} value={draft[key]}
          onChange={(event) => { setDraft({ ...draft, [key]: event.target.value }); setInheritDefaults(false); setSaved(false); }} />
        <span>{html ? "MiB" : t("unit_seconds")}</span>
      </span>
      <span id={`${id}-hint`} className="settings-field__hint">
        {t(html ? "html_hint" : "sandbox_hint", { default: html ? limits.defaults[key] / MIB : limits.defaults[key], max: html ? range.max / MIB : range.max })}
      </span>
      {!valid(key) ? <span id={`${id}-error`} role="alert" className="settings-field__error">{t(key === "sandbox_default_timeout_s" ? "error_default_range" : "error_validation", { min: html ? range.min / MIB : range.min, max: html ? range.max / MIB : range.max })}</span> : null}
    </span>;
  };

  return <form className="execution-limits" aria-label={t("form_label")} onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <div className="execution-limits__status">
      {limits.is_default && !dirty ? <Badge tone="neutral" size="sm">{t("default_badge")}</Badge> : null}
      <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => { setDraft(draftOf(limits.defaults)); setInheritDefaults(true); setSaved(false); }}>{t("reset")}</Button>
    </div>
    {error ? <Notice tone="error">{t(`error_${error}`)} {error === "conflict" || error === "range" ? <Button type="button" variant="secondary" disabled={busy} onClick={() => void load()}>{t("reload")}</Button> : null}</Notice> : null}
    {saved ? <Notice tone="success">{t("saved")}</Notice> : null}
    <section aria-labelledby="sandbox-limits-title" className="execution-limits__section">
      <h2 id="sandbox-limits-title" className="agent-run-limits__title">{t("sandbox_title")}</h2>
      <p className="ui-page-desc">{t("sandbox_intro")}</p>
      <div className="ui-group-card agent-run-limits__rows">
        {(["sandbox_default_timeout_s", "sandbox_max_timeout_s"] as const).map((key) => <FieldRow key={key} className="agent-run-limits__row" titleId={`execution-${key}-title`} title={t(`${key}_title`)} help={<ConfigurationFieldHelp contract={limits.field_contract} field={`team_execution_limits.${key}`} facts={{ minimum: limits.ranges[key].min, maximum: key === "sandbox_default_timeout_s" && Number.isSafeInteger(values.sandbox_max_timeout_s) && values.sandbox_max_timeout_s >= limits.ranges[key].min ? Math.min(limits.ranges[key].max, values.sandbox_max_timeout_s) : limits.ranges[key].max }} label={t(`${key}_title`)} />} description={t(`${key}_desc`)} control={control(key)} />)}
      </div>
      <p className="settings-field__hint">{t("sandbox_boundary", { max: limits.ranges.sandbox_max_timeout_s.max })}</p>
    </section>
    <section aria-labelledby="html-limits-title" className="execution-limits__section">
      <h2 id="html-limits-title" className="agent-run-limits__title">{t("html_title")}</h2>
      <p className="ui-page-desc">{t("html_intro")}</p>
      <div className="ui-group-card agent-run-limits__rows"><FieldRow className="agent-run-limits__row" titleId="execution-html_max_bytes-title" title={t("html_max_bytes_title")} help={<ConfigurationFieldHelp contract={limits.field_contract} field="team_execution_limits.html_max_bytes" label={t("html_max_bytes_title")} />} description={t("html_max_bytes_desc")} control={control("html_max_bytes")} /></div>
    </section>
    <p className="settings-field__hint">{t("effective")}</p>
    <SaveBarSlotContext.Provider value={null}><SaveBar dirty={dirty} saving={busy && dirty} saveDisabled={!validAll || busy} saveLabel={t("save")} onSave={() => void save()} onDiscard={() => { accept(limits); setError(null); }} /></SaveBarSlotContext.Provider>
  </form>;
}
