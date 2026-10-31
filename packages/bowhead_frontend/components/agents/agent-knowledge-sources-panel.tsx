"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { getAgentKnowledgeSources } from "@/lib/agents-api";
import { readKnowledgeAccess, type KnowledgeAccess, type KnowledgeSourceCatalog } from "@/lib/agent-knowledge-sources";

export function AgentKnowledgeSourcesPanel({ agentId, value, disabled, onChange }: {
  agentId: string; value: unknown; disabled: boolean; onChange: (value: KnowledgeAccess) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const policy = readKnowledgeAccess(value);
  const selected = policy && policy !== "unsupported" ? policy.file_ids ?? [] : [];
  const selectedKey = JSON.stringify(selected);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState<KnowledgeSourceCatalog | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setPage(null);
    setError(false);
    const timer = window.setTimeout(() => {
      void getAgentKnowledgeSources(agentId, query, JSON.parse(selectedKey), offset, controller.signal).then((result) => {
        if (!controller.signal.aborted) { setPage(result); setLoading(false); }
      }).catch(() => { if (!controller.signal.aborted) { setError(true); setLoading(false); } });
    }, 150);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [agentId, query, selectedKey, offset, reload]);
  if (policy === null && (loading || (page && !page.enabled))) return null;
  if (policy === "unsupported") return <p role="alert">{t("knowledge_sources_unsupported")}</p>;
  const current: KnowledgeAccess = policy ?? { schema_version: 1, mode: "context_readable" };
  const locked = disabled || !page?.enabled || !page.available || loading || error;
  const names = new Map(page?.selected_items.map((item) => [item.id, item.name]));
  const changeSelection = (ids: string[]) => onChange({ ...current, mode: "selected_files", file_ids: [...ids].sort() });
  return <section className="am-release-section am-knowledge-sources">
    <div className="am-release-subsection-heading"><h3>{t("knowledge_sources_title")}</h3><p>{t("knowledge_sources_body")}</p></div>
    {error || page?.available === false ? <p role="alert">{t("knowledge_sources_load_failed")} <button type="button" onClick={() => setReload((n) => n + 1)}>{t("retry")}</button></p> : null}
    {page && !page.enabled ? <p role="status">{t("knowledge_sources_closed")}</p> : null}
    <label><span>{t("knowledge_sources_mode")}</span><select aria-label={t("knowledge_sources_mode")} disabled={locked} value={current.mode} onChange={(event) => {
      if (event.target.value === "selected_files") changeSelection([]);
      else onChange({ schema_version: 1, mode: "context_readable", ...(current.defaults ? { defaults: current.defaults } : {}) });
    }}>
      <option value="context_readable">{t("knowledge_sources_context")}</option>
      <option value="selected_files">{t("knowledge_sources_selected")}</option>
    </select></label>
    <p>{t("knowledge_sources_broadening")}</p>
    {current.mode === "selected_files" ? <>
      <p role="status">{t("knowledge_sources_count", { count: selected.length })}</p>
      {selected.length === 0 ? <p>{t("knowledge_sources_empty")}</p> : null}
      {selected.map((id, index) => <div className="am-optional-skill-selected" key={id}>
        <span>{names.get(id) ?? t("knowledge_sources_unavailable", { number: index + 1 })}</span>
        <button type="button" disabled={locked} aria-label={t("knowledge_sources_remove_named", { name: names.get(id) ?? String(index + 1) })} onClick={() => changeSelection(selected.filter((item) => item !== id))}>{t("knowledge_sources_remove")}</button>
      </div>)}
      <label><span>{t("knowledge_sources_search")}</span><input type="search" value={query} disabled={disabled} onChange={(event) => { setQuery(event.target.value); setOffset(0); }} /></label>
      {page?.items.map((item) => <label key={item.id} className="am-optional-skill-selected"><input type="checkbox" checked={selected.includes(item.id)} disabled={locked || (!selected.includes(item.id) && selected.length >= page.max_selected)} onChange={(event) => changeSelection(event.target.checked ? [...selected, item.id] : selected.filter((id) => id !== item.id))} /><span>{item.name}</span></label>)}
      <button type="button" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>{t("knowledge_sources_previous")}</button>
      <button type="button" disabled={loading || !page || offset + 20 >= page.total} onClick={() => setOffset(offset + 20)}>{t("knowledge_sources_next")}</button>
    </> : null}
    <label><span>{t("knowledge_sources_top_k")}</span><input type="number" aria-label={t("knowledge_sources_top_k")} disabled={locked} min={page?.top_k_min} max={page?.top_k_max} step={1} placeholder={t("knowledge_sources_team_default")} value={current.defaults?.top_k ?? ""} onChange={(event) => {
      const number = Number(event.target.value);
      if (event.target.value === "") { const { defaults: _defaults, ...rest } = current; onChange(rest); }
      else if (Number.isInteger(number) && page && number >= page.top_k_min && number <= page.top_k_max) onChange({ ...current, defaults: { top_k: number } });
    }} /></label>
  </section>;
}
