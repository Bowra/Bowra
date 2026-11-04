"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ApiError } from "@/lib/api-client";
import { randomUuid } from "@/lib/random-id";
import type { KbFileDTO } from "@/lib/kb-api";
import { fetchSourceNavigation, findSourceCategories, generateSourceDescription, saveSourceCategory, saveSourceNavigation, type SourceCategory, type SourceNavigation } from "@/lib/kb-source-navigation-api";

/** File management owns editing; displayed Matrix text is never copied into a manual draft. */
export function KbSourceNavigation({ file, onDirtyChange, onBusyChange, readOnly = false }: { file: KbFileDTO; readOnly?: boolean; onDirtyChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void }) {
  const t = useTranslations("sourceNavigation");
  const [data, setData] = useState<SourceNavigation | null>(null);
  const [text, setText] = useState("");
  const [labels, setLabels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState<SourceCategory | "new" | null>(null);
  const [categoryQuery, setCategoryQuery] = useState("");
  const [categoryName, setCategoryName] = useState("");
  const [aliases, setAliases] = useState("");
  const [parent, setParent] = useState("");
  const baseline = useRef<SourceNavigation | null>(null);
  const epoch = useRef(0);
  const actionId = useRef<string | null>(null);
  const manual = baseline.current?.description_kind === "manual" ? baseline.current.description : "";
  const storedLabels = (baseline.current?.categories ?? []).map((item) => item.id).sort().join(",");
  const dirty = text !== manual || [...labels].sort().join(",") !== storedLabels;
  useEffect(() => { onDirtyChange?.(dirty || category !== null); return () => onDirtyChange?.(false); }, [dirty, category, onDirtyChange]);
  useEffect(() => { onBusyChange?.(busy); return () => onBusyChange?.(false); }, [busy, onBusyChange]);
  useEffect(() => {
    if (!dirty && category === null) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty, category]);
  const canEdit = Boolean(data?.can_edit && !readOnly);
  const generating = Boolean(data?.description_job && ["pending", "claimed", "dispatching"].includes(data.description_job.state));

  const adopt = (value: SourceNavigation) => {
    if (actionId.current && value.description_job?.action_id === actionId.current && ["ready", "failed", "superseded"].includes(value.description_job.state)) actionId.current = null;
    if (baseline.current && (baseline.current.source_version !== value.source_version || baseline.current.generation_id !== value.generation_id || baseline.current.navigation_revision !== value.navigation_revision || baseline.current.generation_model !== value.generation_model)) actionId.current = null;
    baseline.current = value;
    setData(value);
    setText(value.description_kind === "manual" ? value.description : "");
    setLabels(value.categories.map((item) => item.id));
  };
  const advice = (code: string) => code === "knowledge_description_credit_unavailable" ? "job_credit" : code === "output_invalid" ? "job_output_invalid" : ["outcome_unknown", "prior_outcome_unknown"].includes(code) ? "outcome_unknown" : code === "knowledge_generation_unverified" || code === "knowledge_description_source_unavailable" || code === "source_unavailable" ? "job_source" : code === "knowledge_description_runtime_unavailable" || code === "knowledge_description_assignment_changed" ? "job_model" : "job_action";
  const failure = (reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 409) actionId.current = null;
    setError(reason instanceof ApiError && reason.code === "knowledge_generation_unverified" ? t("source_unverified") : reason instanceof ApiError && reason.status === 422 ? t("check_fields") : reason instanceof ApiError && reason.status === 409 ? t("conflict") : t("operation_failed"));
  };

  useEffect(() => {
    const current = ++epoch.current;
    const controller = new AbortController();
    setData(null);
    setError(null);
    setCategory(null);
    actionId.current = null;
    void fetchSourceNavigation(file.file_id, controller.signal).then((value) => {
      if (epoch.current === current) adopt(value);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted && epoch.current === current) failure(reason);
    });
    return () => { epoch.current++; controller.abort(); };
  }, [file.file_id, file.version]);

  useEffect(() => {
    if (!generating || busy) return;
    let inFlight = false;
    const current = epoch.current;
    const controller = new AbortController();
    const timer = setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void fetchSourceNavigation(file.file_id, controller.signal).then((value) => {
        if (!controller.signal.aborted && epoch.current === current) setData(value);
      }).catch(() => { /* Keep the current form and allow explicit refresh after a transient poll failure. */ }).finally(() => { inFlight = false; });
    }, 2000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [file.file_id, generating, busy]);

  const refresh = async (discardCategory = false) => {
    const current = epoch.current;
    const value = await fetchSourceNavigation(file.file_id);
    if (current === epoch.current) { adopt(value); if (discardCategory) setCategory(null); }
  };
  const save = async () => {
    if (!data || !canEdit) return;
    const current = epoch.current;
    setBusy(true); setError(null);
    try {
      await saveSourceNavigation(baseline.current || data, {
        ...(text !== manual ? { manual_description: text.trim() || null } : {}),
        ...([...labels].sort().join(",") !== storedLabels ? { category_ids: labels } : {}),
      });
      if (current === epoch.current) await refresh();
    } catch (reason) { if (current === epoch.current) failure(reason); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const generate = async () => {
    if (!data || !canEdit) return;
    const current = epoch.current;
    setBusy(true); setError(null);
    try {
      actionId.current ??= randomUuid();
      await generateSourceDescription(data, actionId.current);
      if (current === epoch.current) { actionId.current = null; await refresh(); }
    } catch (reason) { if (current === epoch.current) failure(reason); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const editCategory = (value: SourceCategory | "new") => {
    setCategory(value);
    setCategoryName(value === "new" ? "" : value.name);
    setAliases(value === "new" ? "" : value.aliases.join(", "));
    setParent(value === "new" ? "" : value.parent_id || "");
  };
  const saveCategory = async () => {
    if (!category || !data) return;
    const current = epoch.current;
    setBusy(true); setError(null);
    try {
      await saveSourceCategory({ scope: category === "new" ? file.scope : category.scope, name: categoryName.trim(),
        aliases: aliases.split(",").map((item) => item.trim()).filter(Boolean), parent_id: parent || null,
        ...(category !== "new" ? { expected_revision: category.revision } : {}),
      }, category === "new" ? undefined : category.id);
      if (current === epoch.current) {
        const value = await fetchSourceNavigation(file.file_id);
        if (current === epoch.current) { setData(value); setCategory(null); }
      }
    } catch (reason) { if (current === epoch.current) failure(reason); }
    finally { if (current === epoch.current) setBusy(false); }
  };

  return <section className="source-navigation" aria-label={t("title")}>
    <div className="source-heading"><h3>{t("title")}</h3>{data && <span>{t(`origin_${data.description_kind}`)}</span>}</div>
    <p className="source-note">{t("purpose")}</p>
    {error && <div role="alert" className="source-error">{error} <button className="kb-btn kb-btn-ghost kb-btn-sm" type="button" disabled={busy} onClick={() => { void refresh(true).catch(failure); }}>{t(dirty || category !== null ? "discard_refresh" : "refresh")}</button>{(dirty || category !== null) && <span>{t("keep_draft")}</span>}</div>}
    {!data && !error && <p role="status">{t("loading")}</p>}
    {data && <>
      {data.description && data.description_kind !== "manual" && <p className="source-preview">{data.description}</p>}
      {canEdit ? <>
        {data.manual_needs_review && <p className="source-note">{t("needs_review")}</p>}
        <label htmlFor="kb-source-description">{t("manual")}</label>
        <textarea id="kb-source-description" value={text} maxLength={1000} onChange={(event) => setText(Array.from(event.target.value).slice(0, 500).join(""))} placeholder={t("placeholder")} disabled={busy} rows={3} />
        <div className="source-note source-count">{Array.from(text).length}/500</div>
        {manual && <button className="kb-btn kb-btn-ghost kb-btn-sm" type="button" disabled={busy} onClick={() => setText("")}>{t("clear_manual")}</button>}
        <div className="source-heading"><h4>{t("categories")}</h4><button className="kb-btn kb-btn-ghost kb-btn-sm" type="button" disabled={busy} onClick={() => editCategory("new")}>{t("add_category")}</button></div>
        <div className="source-actions"><input aria-label={t("category_search")} value={categoryQuery} maxLength={80} placeholder={t("category_search")} onChange={(event) => setCategoryQuery(event.target.value)} /><button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" disabled={busy} onClick={() => {
          const current = epoch.current;
          setBusy(true); setError(null);
          void findSourceCategories(categoryQuery).then((result) => {
            if (current === epoch.current) setData((previous) => previous ? { ...previous, category_options: [...new Map([...previous.category_options.filter((item) => labels.includes(item.id)), ...result.categories].map((item) => [item.id, item])).values()] } : previous);
          }).catch((reason) => { if (current === epoch.current) failure(reason); }).finally(() => { if (current === epoch.current) setBusy(false); });
        }}>{t("search")}</button></div>
        <div className="source-labels">{data.category_options.map((item) => <div key={item.id} className="source-label">
          <label><input type="checkbox" checked={labels.includes(item.id)} disabled={busy || (!labels.includes(item.id) && labels.length >= 8)} onChange={(event) => setLabels((values) => event.target.checked ? [...values, item.id] : values.filter((id) => id !== item.id))} />{item.parent_id ? "↳ " : ""}{item.name}</label>
          {item.scope === file.scope && <button className="kb-btn kb-btn-ghost kb-btn-sm" type="button" disabled={busy} onClick={() => editCategory(item)} aria-label={t("edit_category", { name: item.name })}>{t("edit")}</button>}
        </div>)}</div>
        {!labels.length && <p className="source-note">{t("unclassified")}</p>}
        {category && <div className="source-category-editor">
          <label>{t("category_name")}<input value={categoryName} maxLength={80} onChange={(event) => setCategoryName(event.target.value)} disabled={busy} /></label>
          <label>{t("aliases")}<input value={aliases} maxLength={640} onChange={(event) => setAliases(event.target.value)} disabled={busy} /></label>
          <label>{t("parent")}<select value={parent} onChange={(event) => setParent(event.target.value)} disabled={busy}><option value="">{t("root")}</option>{data.category_options.filter((item) => item.scope === (category === "new" ? file.scope : category.scope) && !item.parent_id && (category === "new" || item.id !== category.id)).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <div className="source-actions"><button className="kb-btn kb-btn-secondary kb-btn-sm" type="button" disabled={busy || !categoryName.trim() || aliases.split(",").filter((item) => item.trim()).length > 8 || aliases.split(",").some((item) => Array.from(item.trim()).length > 80)} onClick={() => void saveCategory()}>{t("save_category")}</button><button className="kb-btn kb-btn-ghost kb-btn-sm" type="button" disabled={busy} onClick={() => setCategory(null)}>{t("cancel")}</button></div>
        </div>}
        <div className="source-actions"><button className="kb-btn kb-btn-secondary kb-btn-sm" type="button" disabled={busy || !dirty || category !== null} onClick={() => void save()}>{t("save")}</button></div>
        <div className="source-generation">
          <p className="source-note">{data.generation_enabled ? t("cost", { model: data.generation_model || "" }) : t(data.generation_status === "unavailable" ? "model_unavailable" : "disabled")}</p>
          <button className="kb-btn kb-btn-secondary kb-btn-sm" type="button" disabled={busy || dirty || generating || !data.generation_enabled || !data.generation_id || ["manual", "standalone"].includes(data.description_kind)} onClick={() => void generate()}>{generating ? t("generating") : t("generate")}</button>
          {data.description_job && <p role="status" className="source-note">{t(`job_${data.description_job.state}`)}{data.description_job.error_code && <> {t(advice(data.description_job.error_code))} <code>{data.description_job.id}</code></>}</p>}
        </div>
      </> : <>{data.description_kind === "manual" && <p className="source-preview">{data.description}</p>}<p className="source-note">{data.categories.map((item) => item.name).join(" · ") || t("unclassified")}</p></>}
    </>}
    <style jsx>{`
      .source-navigation{padding:16px 0;border-top:1px solid var(--border);margin-top:16px;font-size:.8125rem}
      .source-heading,.source-actions,.source-label{display:flex;align-items:center;justify-content:space-between;gap:8px}
      .source-heading h3,.source-heading h4{font-size:inherit;font-weight:600;margin:0}
      .source-heading span,.source-note{color:var(--muted);font-size:.75rem;line-height:1.5}
      .source-note{margin:6px 0 10px}.source-preview{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6}
      label{display:block;font-weight:500;margin:8px 0 5px}textarea,input:not([type=checkbox]),select{width:100%;border:1px solid var(--border);border-radius:var(--radius-sm,6px);padding:8px;background:var(--bg);color:var(--txt);font:inherit}
      textarea{resize:vertical;min-height:72px}textarea:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
      .source-count{text-align:right;margin:2px 0}.source-label label{display:flex;gap:6px;align-items:center;font-weight:400;margin:4px 0}
      .source-labels{max-height:180px;overflow:auto}.source-category-editor{padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm,6px);margin:10px 0}
      .source-actions{justify-content:flex-start;margin-top:10px;flex-wrap:wrap}.source-generation{border-top:1px solid var(--border);margin-top:14px;padding-top:10px}
      .source-error{color:var(--error);line-height:1.5;overflow-wrap:anywhere}code{overflow-wrap:anywhere;font-size:.6875rem}
    `}</style>
  </section>;
}
