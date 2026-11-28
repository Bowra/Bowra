"use client";

import { useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";
import { useKnowledgeBasesEnabled } from "@/components/knowledge/bases/use-knowledge-bases";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { getAgentKnowledgeBaseSources, getAgentKnowledgeSources } from "@/lib/agents-api";
import {
  MAX_SELECTED_KNOWLEDGE_BASES,
  readKnowledgeAccess,
  selectedKnowledgeBaseIds,
  type KnowledgeAccess,
  type KnowledgeBaseSourceCatalog,
  type KnowledgeBaseSourceGroup,
  type KnowledgeBaseSourceItem,
  type KnowledgeSourceCatalog,
} from "@/lib/agent-knowledge-sources";
import { audienceLabelKey, knowledgeBaseName } from "@/lib/knowledge-base-display";

type Mode = KnowledgeAccess["mode"];

/**
 * The selected-knowledge-base catalog (FEATURE_KNOWLEDGE_BASES), read once per panel: groups do not depend on the
 * selection, so names of selected knowledge bases come from the groups.
 */
function useKnowledgeBaseSources(agentId: string, enabled: boolean) {
  const [catalog, setCatalog] = useState<KnowledgeBaseSourceCatalog | null>(null);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    setError(false);
    void getAgentKnowledgeBaseSources(agentId, controller.signal).then((result) => {
      if (!controller.signal.aborted) setCatalog(result);
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [agentId, enabled, reload]);
  return { catalog: enabled ? catalog : null, error: enabled && error, retry: () => setReload((n) => n + 1) };
}

/** Group headings: My knowledge bases, each Space by name, the Team. */
export function knowledgeBaseGroupLabel(group: Pick<KnowledgeBaseSourceGroup, "home" | "name">, t: (key: string, values?: Record<string, string>) => string): string {
  if (group.home === "personal") return t("knowledge_sources_bases_group_personal");
  if (group.home === "team") return t("knowledge_sources_bases_group_team");
  return group.name ? t("knowledge_sources_bases_group_space", { name: group.name }) : t("knowledge_sources_bases_group_space_unnamed");
}

export function AgentKnowledgeSourcesPanel({ agentId, value, disabled, onChange }: {
  agentId: string; value: unknown; disabled: boolean; onChange: (value: KnowledgeAccess) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const tBases = useTranslations("knowledge_bases.common");
  const basesSwitch = useKnowledgeBasesEnabled();
  const reasonId = useId();
  const policy = readKnowledgeAccess(value);
  const selected = policy && policy !== "unsupported" ? policy.file_ids ?? [] : [];
  const selectedBases = selectedKnowledgeBaseIds(policy);
  const selectedKey = JSON.stringify(selected);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState<KnowledgeSourceCatalog | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [picking, setPicking] = useState(false);
  // The third mode is offered with the switch on, and shown for a saved selection even when it is off.
  const offerBases = basesSwitch || (policy !== null && policy !== "unsupported" && policy.mode === "selected_knowledge_bases");
  const bases = useKnowledgeBaseSources(agentId, offerBases);
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
  const defaults = current.defaults ? { defaults: current.defaults } : {};
  const locked = disabled || !page?.enabled || !page.available || loading || error;
  const names = new Map(page?.selected_items.map((item) => [item.id, item.name]));
  const changeSelection = (ids: string[]) => onChange({ schema_version: 1, mode: "selected_files", file_ids: [...ids].sort(), ...defaults });
  const changeBases = (ids: string[]) => onChange({ schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [...new Set(ids)].sort(), ...defaults });
  const changeMode = (mode: Mode) => {
    if (mode === current.mode) return;
    if (mode === "selected_files") changeSelection([]);
    else if (mode === "selected_knowledge_bases") changeBases([]);
    else onChange({ schema_version: 1, mode: "context_readable", ...defaults });
  };

  const catalog = bases.catalog;
  const basesReady = Boolean(catalog?.enabled && catalog.state === "ready");
  const basesReason = !offerBases || basesReady || (!catalog && !bases.error) ? null
    : bases.error || catalog?.state === "unavailable" ? t("knowledge_sources_bases_unavailable")
      : catalog?.state === "preparing" ? t("knowledge_sources_bases_preparing")
        : t("knowledge_sources_bases_disabled");
  const maxBases = catalog?.max_selected ?? MAX_SELECTED_KNOWLEDGE_BASES;
  const items = new Map<string, { item: KnowledgeBaseSourceItem; group: KnowledgeBaseSourceGroup }>();
  for (const group of catalog?.groups ?? []) for (const item of group.items) items.set(item.id, { item, group });
  const spaceName = (spaceId: string) => catalog?.groups.find((group) => group.home === "space" && group.home_id === spaceId)?.name ?? null;
  const baseName = (item: KnowledgeBaseSourceItem) => knowledgeBaseName(item, tBases, spaceName);
  const basesLocked = locked || !basesReady;
  const atLimit = selectedBases.length >= maxBases;

  return <section className="am-release-section am-knowledge-sources">
    <div className="am-release-subsection-heading"><h3>{t("knowledge_sources_title")}</h3><p>{t("knowledge_sources_body")}</p></div>
    {error || page?.available === false ? <p role="alert">{t("knowledge_sources_load_failed")} <button type="button" onClick={() => setReload((n) => n + 1)}>{t("retry")}</button></p> : null}
    {page && !page.enabled ? <p role="status">{t("knowledge_sources_closed")}</p> : null}
    {offerBases ? <>
      <RadioGroup<Mode> className="am-knowledge-modes" orientation="both" aria-label={t("knowledge_sources_mode")} value={current.mode} onValueChange={changeMode} data-testid="knowledge-source-modes">
        <Radio value="context_readable" disabled={locked}><strong>{t("knowledge_sources_context")}</strong><small>{t("knowledge_sources_context_hint")}</small></Radio>
        <Radio value="selected_knowledge_bases" disabled={basesLocked} aria-describedby={basesReason ? reasonId : undefined}><strong>{t("knowledge_sources_bases")}</strong><small>{t("knowledge_sources_bases_hint")}</small></Radio>
        <Radio value="selected_files" disabled={locked}><strong>{t("knowledge_sources_selected")}</strong><small>{t("knowledge_sources_files_hint", { count: page?.max_selected ?? 64 })}</small></Radio>
      </RadioGroup>
      {basesReason ? <p id={reasonId} role="status" className="am-knowledge-bases-reason">{basesReason} {bases.error || catalog?.state !== "disabled" ? <button type="button" onClick={bases.retry}>{t("knowledge_sources_bases_check_again")}</button> : null}</p> : null}
    </> : <label><span>{t("knowledge_sources_mode")}</span><select aria-label={t("knowledge_sources_mode")} disabled={locked} value={current.mode} onChange={(event) => changeMode(event.target.value as Mode)}>
      <option value="context_readable">{t("knowledge_sources_context")}</option>
      <option value="selected_files">{t("knowledge_sources_selected")}</option>
    </select></label>}
    <p>{t("knowledge_sources_broadening")}</p>
    {current.mode === "selected_knowledge_bases" ? <>
      <p role="status">{t("knowledge_sources_bases_count", { count: selectedBases.length, max: maxBases })}</p>
      {selectedBases.length === 0 ? <p>{t("knowledge_sources_bases_empty")}</p> : null}
      <ul className="am-knowledge-bases-selected">
        {selectedBases.map((id, index) => {
          const known = items.get(id);
          const name = known ? baseName(known.item) : t("knowledge_sources_bases_unavailable_item", { number: index + 1 });
          return <li className="am-optional-skill-selected" key={id} data-testid={`knowledge-base-selected-${id}`}>
            <span><strong>{name}</strong>{known ? <small>{knowledgeBaseGroupLabel(known.group, t)}</small> : null}</span>
            {known ? <Badge tone="outline" size="sm">{tBases(audienceLabelKey(known.item))}</Badge> : null}
            <button type="button" disabled={locked} aria-label={t("knowledge_sources_remove_named", { name })} onClick={() => changeBases(selectedBases.filter((item) => item !== id))}>{t("knowledge_sources_remove")}</button>
          </li>;
        })}
      </ul>
      <Button size="sm" variant="ghost" disabled={basesLocked} aria-expanded={picking} onClick={() => setPicking((open) => !open)}>{t("knowledge_sources_bases_add")}</Button>
      {picking && catalog ? <div className="am-knowledge-bases-picker" data-testid="knowledge-base-picker">
        {atLimit ? <p role="status">{t("knowledge_sources_bases_limit", { max: maxBases })}</p> : null}
        {catalog.groups.length === 0 ? <p role="status">{t("knowledge_sources_bases_none")}</p> : null}
        {catalog.groups.map((group) => <fieldset key={`${group.home}:${group.home_id}`} className="am-knowledge-bases-group">
          <legend>{knowledgeBaseGroupLabel(group, t)}</legend>
          {group.items.map((item) => {
            const checked = selectedBases.includes(item.id);
            return <label key={item.id} className="am-optional-skill-selected">
              <input type="checkbox" checked={checked} disabled={basesLocked || (!checked && atLimit)} onChange={(event) => changeBases(event.target.checked ? [...selectedBases, item.id] : selectedBases.filter((id) => id !== item.id))} />
              <span><strong>{baseName(item)}</strong>{item.is_default ? <Badge tone="outline" size="sm">{tBases("default_badge")}</Badge> : null}<small>{tBases(audienceLabelKey(item))}</small></span>
            </label>;
          })}
        </fieldset>)}
      </div> : null}
      <p className="am-knowledge-bases-footnote">{t("knowledge_sources_bases_footnote")}</p>
    </> : null}
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
