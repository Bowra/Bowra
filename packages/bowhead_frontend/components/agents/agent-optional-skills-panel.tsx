"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { getAgentOptionalSkillCatalog } from "@/lib/agents-api";
import {
  emptyRuntimeExtensions, optionalSkillKey, optionalSkillsNeedPrivateAudience, readOptionalSkillCatalog, readRuntimeExtensions,
  type AgentAudienceMode, type AgentRuntimeExtensions, type OptionalSkillItem, type OptionalSkillSelector,
} from "@/lib/agent-runtime-extensions";

interface Props {
  agentId: string;
  value: unknown;
  audience: AgentAudienceMode;
  disabled: boolean;
  onChange: (value: AgentRuntimeExtensions | null) => void;
}
interface Page {
  key: string;
  items: OptionalSkillItem[];
  next: number | null;
  status: "loading" | "ready" | "error";
  labels: Record<string, OptionalSkillItem>;
}

export function AgentOptionalSkillsPanel({ agentId, value, audience, disabled, onChange }: Props) {
  const t = useTranslations("agents_marketplace");
  const skillReasonId = useId();
  const savedPolicy = readRuntimeExtensions(value);
  const [configuring, setConfiguring] = useState(false);
  const policy = savedPolicy === null && configuring ? emptyRuntimeExtensions() : savedPolicy;
  const active = policy !== null && policy !== "unsupported" && !disabled;
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState<Page | null>(null);
  const generation = useRef(0);
  const key = JSON.stringify([agentId, query]);
  const current = page?.key === key ? page : null;
  const labels = current?.labels ?? {};

  useEffect(() => {
    const serial = ++generation.current;
    if (!active) return;
    const controller = new AbortController();
    setPage((previous) => ({ key, items: offset && previous?.key === key ? previous.items : [], next: null, status: "loading", labels: previous?.labels ?? {} }));
    const timer = window.setTimeout(async () => {
      try {
        const result = readOptionalSkillCatalog(await getAgentOptionalSkillCatalog(agentId, query, offset, controller.signal), agentId, offset);
        if (controller.signal.aborted || generation.current !== serial) return;
        setPage((previous) => {
          const items = offset && previous?.key === key ? [...previous.items] : [];
          for (const item of result.items) {
            const duplicate = items.find((old) => optionalSkillKey(old.resource) === optionalSkillKey(item.resource));
            if (duplicate && duplicate.resource.version !== item.resource.version) return { key, items: [], next: null, status: "error", labels: {} };
            if (!duplicate) items.push(item);
          }
          return { key, items, next: result.next_offset, status: "ready", labels: { ...previous?.labels, ...Object.fromEntries(result.items.map((item) => [optionalSkillKey(item.resource), item])) } };
        });
      } catch {
        if (controller.signal.aborted || generation.current !== serial) return;
        setPage({ key, items: [], next: null, status: "error", labels: {} });
      }
    }, 180);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [active, agentId, key, offset, query, reload]);

  const commit = (rule: AgentRuntimeExtensions["skill_read"]) => {
    if (!disabled && policy !== null && policy !== "unsupported") onChange({ ...policy, skill_read: rule });
  };
  const sourceLabel = (source: OptionalSkillSelector["source_kind"]) => t(source === "builtin" ? "release_bowhead_skill" : source === "team" ? "release_team_skill" : "release_personal_skill");
  const remove = (selector: OptionalSkillSelector) => {
    if (policy === null || policy === "unsupported") return;
    const selected = policy.skill_read.resource_allowlist.filter((item) => optionalSkillKey(item) !== optionalSkillKey(selector));
    commit({ ...policy.skill_read, resource_allowlist: selected, enabled: policy.skill_read.enabled && selected.length > 0 });
  };

  return <div className="am-optional-skills">
    <div className="am-release-subsection-heading"><h3>{t("optional_skills_title")}</h3><p>{t("optional_skills_body")}</p></div>
    {policy === null ? <button type="button" disabled={disabled} onClick={() => { if (!disabled) setConfiguring(true); }}>{t("optional_skills_configure")}</button>
      : policy === "unsupported" ? <>
        <p role="status">{t("optional_skills_unsupported")}</p>
        <button type="button" disabled={disabled} onClick={() => { if (!disabled) onChange(null); }}>{t("optional_skills_clear")}</button>
      </> : <>
        <div className="am-release-form-grid am-release-compact-fields">
          <label className="am-optional-skills-toggle"><input type="checkbox" name="agent-optional-skills-enabled" checked={policy.skill_read.enabled} disabled={disabled || policy.skill_read.resource_allowlist.length === 0}
            onChange={(event) => commit({ ...policy.skill_read, enabled: event.target.checked })} /><span>{t("optional_skills_enabled")}</span></label>
          <label><span>{t("optional_skills_max")}</span><select name="agent-optional-skills-max" disabled={disabled} value={policy.skill_read.max_selected}
            onChange={(event) => commit({ ...policy.skill_read, max_selected: Number(event.target.value) })}>
            {[1, 2, 3].map((count) => <option key={count} value={count}>{count}</option>)}
          </select></label>
        </div>
        {optionalSkillsNeedPrivateAudience(value, audience) ? <p className="am-release-inline-error" role="alert">{t("optional_skills_private")}</p> : null}
        <div className="am-optional-skill-list" aria-label={t("optional_skills_selected")}>
          {policy.skill_read.resource_allowlist.map((selector, index) => {
            const item = labels[optionalSkillKey(selector)];
            const label = item?.label ?? t("optional_skills_selected_method", { number: index + 1 });
            return <div className="am-optional-skill-selected" key={optionalSkillKey(selector)}>
              <span><strong>{label}</strong><small>{sourceLabel(selector.source_kind)} · {selector.version ? t("optional_skills_pinned", { version: selector.version.slice(7, 15) }) : t("optional_skills_unpinned")}</small></span>
              {item && selector.version !== item.resource.version ? <button type="button" disabled={disabled} onClick={() => commit({ ...policy.skill_read,
                resource_allowlist: policy.skill_read.resource_allowlist.map((old) => optionalSkillKey(old) === optionalSkillKey(selector) ? { ...old, version: item.resource.version } : old),
              })}>{t("optional_skills_use_current")}</button> : null}
              <button type="button" disabled={disabled} aria-label={t("optional_skills_remove_named", { name: label })} onClick={() => remove(selector)}>{t("optional_skills_remove")}</button>
            </div>;
          })}
          {policy.skill_read.resource_allowlist.length === 0 ? <p>{t("optional_skills_choose_first")}</p> : null}
        </div>
        <label className="am-release-field-wide"><span>{t("optional_skills_search")}</span><input type="search" name="agent-optional-skills-search" maxLength={120} disabled={disabled} value={query}
          onChange={(event) => { setQuery(event.target.value); setOffset(0); }} /></label>
        {current?.status === "error" ? <p role="alert">{t("optional_skills_load_failed")} <button type="button" disabled={disabled} onClick={() => { setOffset(0); setReload((v) => v + 1); }}>{t("retry")}</button></p> : null}
        {active && (!current || current.status === "loading") ? <p role="status">{t("optional_skills_loading")}</p> : null}
        <div className="am-release-picker-grid" aria-label={t("optional_skills_results")}>
          {current?.items.map((item) => {
            const selected = policy.skill_read.resource_allowlist.some((selector) => optionalSkillKey(selector) === optionalSkillKey(item.resource));
            const audienceAllowed = item.allowed_audiences.includes(audience);
            const audienceReasonId = `${skillReasonId}-${encodeURIComponent(optionalSkillKey(item.resource))}`;
            return <label key={optionalSkillKey(item.resource)} data-selected={selected}>
              <input type="checkbox" name="agent-optional-skill" checked={selected} aria-describedby={!audienceAllowed ? audienceReasonId : undefined} disabled={disabled || current.status !== "ready" || !selected && (!audienceAllowed || policy.skill_read.resource_allowlist.length >= 32)}
                onChange={() => {
                  if (selected) return remove(item.resource);
                  if (!audienceAllowed || policy.skill_read.resource_allowlist.length >= 32) return;
                  const selector: OptionalSkillSelector = { source_kind: item.resource.source_kind, id: item.resource.id, version: item.resource.version };
                  commit({ ...policy.skill_read, allowed_sources: [...new Set([...policy.skill_read.allowed_sources, selector.source_kind])],
                    resource_allowlist: [...policy.skill_read.resource_allowlist, selector] });
                }} />
              <span><strong>{item.label}</strong><small>{item.description}</small><em>{sourceLabel(item.resource.source_kind)}{!audienceAllowed ? ` · ${t("release_private_only")}` : ""}</em>
                {!audienceAllowed ? <small id={audienceReasonId}>{t("release_skill_audience_reason", { audience: t(`release_audience_${audience}`) })}</small> : null}
              </span>
            </label>;
          })}
        </div>
        {current?.status === "ready" && current.items.length === 0 ? <p>{t("optional_skills_empty")}</p> : null}
        {current?.next !== null && current?.next !== undefined ? <button type="button" disabled={disabled || current.status !== "ready"} onClick={() => setOffset(current.next!)}>{t("optional_skills_more")}</button> : null}
        <p><small>{t("optional_skills_limit", { count: policy.skill_read.resource_allowlist.length })}</small></p>
        <button type="button" disabled={disabled} onClick={() => {
          if (disabled) return;
          setConfiguring(false);
          setQuery("");
          setOffset(0);
          setPage(null);
          if (savedPolicy !== null) onChange(null);
        }}>{t("optional_skills_clear")}</button>
      </>}
  </div>;
}
