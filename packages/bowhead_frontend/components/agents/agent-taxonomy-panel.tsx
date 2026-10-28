"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import {
  getAgentTaxonomy,
  saveAgentTaxonomy,
  suggestAgentTaxonomy,
  type AgentTaxonomyManager,
  type AgentTaxonomySuggestion,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { CharIcon } from "@/components/char-icon";

function toggle(values: string[], value: string, limit: number): string[] {
  if (values.includes(value)) return values.filter((item) => item !== value);
  return values.length < limit ? [...values, value].sort() : values;
}

export function AgentTaxonomyPanel({
  agentId,
  archived,
  parentDirty,
  refreshToken,
  onTaxonomyChanged,
}: {
  agentId: string;
  archived: boolean;
  parentDirty: boolean;
  refreshToken: string;
  onTaxonomyChanged: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const [manager, setManager] = useState<AgentTaxonomyManager | null>(null);
  const [available, setAvailable] = useState(true);
  const [domains, setDomains] = useState<string[]>([]);
  const [jobs, setJobs] = useState<string[]>([]);
  const [teamTagIds, setTeamTagIds] = useState<string[]>([]);
  const [newTags, setNewTags] = useState<string[]>([]);
  const [newTag, setNewTag] = useState("");
  const [appliedSuggestionId, setAppliedSuggestionId] = useState<string | null>(null);
  const [busy, setBusy] = useState<"suggest" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const setFromManager = useCallback((value: AgentTaxonomyManager) => {
    setDomains(value.selection.domains);
    setJobs(value.selection.jobs);
    setTeamTagIds(value.selection.team_tag_ids);
    setNewTags([]);
    setNewTag("");
    setAppliedSuggestionId(null);
  }, []);

  const load = useCallback(async () => {
    try {
      const value = await getAgentTaxonomy(agentId);
      setManager(value);
      setFromManager(value);
      setAvailable(true);
      setError(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        setAvailable(false);
        return;
      }
      setError(cause instanceof Error ? cause.message : t("taxonomy_load_failed"));
    }
  }, [agentId, setFromManager, t]);

  useEffect(() => { void load(); }, [load, refreshToken]);

  const dirty = useMemo(() => manager !== null && (
    JSON.stringify(domains) !== JSON.stringify(manager.selection.domains)
    || JSON.stringify(jobs) !== JSON.stringify(manager.selection.jobs)
    || JSON.stringify(teamTagIds) !== JSON.stringify(manager.selection.team_tag_ids)
    || newTags.length > 0
    || appliedSuggestionId !== null
  ), [appliedSuggestionId, domains, jobs, manager, newTags.length, teamTagIds]);
  const formBlocked = archived || parentDirty || busy !== null;

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const applySuggestion = (suggestion: AgentTaxonomySuggestion) => {
    if (suggestion.state !== "pending") return;
    setDomains(suggestion.suggestion.domains);
    setJobs(suggestion.suggestion.jobs);
    setTeamTagIds(suggestion.suggestion.team_tag_ids);
    setNewTags(suggestion.suggestion.new_team_tags);
    setAppliedSuggestionId(suggestion.id);
    setNotice(t("taxonomy_suggestion_applied"));
  };

  const suggest = async () => {
    if (!manager) return;
    setBusy("suggest");
    setError(null);
    setNotice(null);
    try {
      const suggestion = await suggestAgentTaxonomy(agentId, manager.source_hash);
      const next = { ...manager, suggestion };
      setManager(next);
      applySuggestion(suggestion);
      setNotice(t("taxonomy_ai_ready"));
    } catch (cause) {
      if (cause instanceof ApiError && [
        "agent_taxonomy_source_changed",
        "agent_working_copy_version_conflict",
      ].includes(cause.code ?? "")) {
        await load();
        setError(t("taxonomy_refresh_required"));
      } else {
        setError(cause instanceof Error ? cause.message : t("taxonomy_suggest_failed"));
      }
    } finally {
      setBusy(null);
    }
  };

  const addNewTag = () => {
    const value = newTag.trim();
    if (!value || newTags.some((item) => item.toLocaleLowerCase() === value.toLocaleLowerCase())) return;
    if (teamTagIds.length + newTags.length >= 12) return;
    setNewTags((current) => [...current, value]);
    setNewTag("");
  };

  const save = async () => {
    if (!manager || !dirty) return;
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      const value = await saveAgentTaxonomy(agentId, {
        domains,
        jobs,
        team_tag_ids: teamTagIds,
        new_team_tags: newTags,
        expected_working_copy_row_version: manager.working_copy_row_version,
        suggestion_id: appliedSuggestionId,
      });
      setManager(value);
      setFromManager(value);
      setNotice(t("taxonomy_saved"));
      onTaxonomyChanged();
    } catch (cause) {
      if (cause instanceof ApiError && [
        "agent_taxonomy_source_changed",
        "agent_working_copy_version_conflict",
        "agent_taxonomy_suggestion_stale",
      ].includes(cause.code ?? "")) {
        await load();
        setError(t("taxonomy_refresh_required"));
      } else {
        setError(cause instanceof Error ? cause.message : t("taxonomy_save_failed"));
      }
    } finally {
      setBusy(null);
    }
  };

  if (!available) return null;

  return (
    <section className="am-taxonomy-section" aria-labelledby="agent-taxonomy-title">
      <header className="am-taxonomy-header">
        <div>
          <span>{t("taxonomy_eyebrow")}</span>
          <h2 id="agent-taxonomy-title">{t("taxonomy_title")}</h2>
          <p>{t("taxonomy_body")}</p>
        </div>
        <button type="button" disabled={formBlocked || !manager || dirty} onClick={() => void suggest()}>
          {busy === "suggest" ? t("taxonomy_suggesting") : t("taxonomy_suggest")}
        </button>
      </header>

      {!manager ? <div className="am-taxonomy-loading" aria-busy="true">{t("loading")}</div> : (
        <>
          {manager.suggestion?.state === "pending" && appliedSuggestionId !== manager.suggestion.id ? (
            <div className="am-taxonomy-suggestion">
              <div><strong>{t("taxonomy_suggestion_ready")}</strong><p>{manager.suggestion.suggestion.reasons.join(" · ") || t("taxonomy_suggestion_no_reason")}</p></div>
              <button type="button" disabled={formBlocked || dirty} onClick={() => applySuggestion(manager.suggestion as AgentTaxonomySuggestion)}>{t("taxonomy_use_suggestion")}</button>
            </div>
          ) : null}

          <div className="am-taxonomy-grid">
            <fieldset>
              <legend>{t("taxonomy_domains")}</legend>
              <p>{t("taxonomy_domains_body")}</p>
              <div className="am-taxonomy-options">
                {manager.platform.domains.map((term) => (
                  <button key={term.id} type="button" aria-pressed={domains.includes(term.id)} disabled={formBlocked || (!domains.includes(term.id) && domains.length >= 3)} onClick={() => setDomains((current) => toggle(current, term.id, 3))}>{term.label}</button>
                ))}
              </div>
            </fieldset>
            <fieldset>
              <legend>{t("taxonomy_jobs")}</legend>
              <p>{t("taxonomy_jobs_body")}</p>
              <div className="am-taxonomy-options">
                {manager.platform.jobs.map((term) => (
                  <button key={term.id} type="button" aria-pressed={jobs.includes(term.id)} disabled={formBlocked || (!jobs.includes(term.id) && jobs.length >= 5)} onClick={() => setJobs((current) => toggle(current, term.id, 5))}>{term.label}</button>
                ))}
              </div>
            </fieldset>
          </div>

          <fieldset className="am-taxonomy-tags">
            <legend>{t("taxonomy_team_tags")}</legend>
            <p>{t("taxonomy_team_tags_body")}</p>
            {manager.team_tags.length ? <div className="am-taxonomy-options">
              {manager.team_tags.map((tagValue) => (
                <button key={tagValue.id} type="button" aria-pressed={teamTagIds.includes(tagValue.id)} disabled={formBlocked || (!teamTagIds.includes(tagValue.id) && teamTagIds.length + newTags.length >= 12)} onClick={() => setTeamTagIds((current) => toggle(current, tagValue.id, 12 - newTags.length))}>{tagValue.display_name}</button>
              ))}
            </div> : <small>{t("taxonomy_no_team_tags")}</small>}
            {newTags.length ? <div className="am-taxonomy-new-tags">{newTags.map((value) => <span key={value}>{value}<button type="button" disabled={formBlocked} onClick={() => setNewTags((current) => current.filter((item) => item !== value))} aria-label={t("taxonomy_remove_new_tag", { name: value })}><CharIcon ch="×" aria-hidden /></button></span>)}</div> : null}
            <div className="am-taxonomy-add-tag">
              <input aria-label={t("taxonomy_new_tag_placeholder")} name="agent-team-tag" autoComplete="off" value={newTag} maxLength={40} disabled={formBlocked || teamTagIds.length + newTags.length >= 12} onChange={(event) => setNewTag(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addNewTag(); } }} placeholder={t("taxonomy_new_tag_placeholder")} />
              <button type="button" disabled={formBlocked || !newTag.trim() || teamTagIds.length + newTags.length >= 12} onClick={addNewTag}>{t("taxonomy_add_tag")}</button>
            </div>
          </fieldset>

          <div className="am-taxonomy-truth">
            <strong>{t("taxonomy_truth_title")}</strong>
            <p>{t("taxonomy_truth_body")}</p>
          </div>
          {parentDirty ? <p className="am-localization-guidance">{t("taxonomy_save_parent_first")}</p> : null}
          {error ? <p className="am-release-error" role="alert">{error}</p> : null}
          {notice ? <p className="am-release-notice" role="status">{notice}</p> : null}
          <div className="am-taxonomy-actions">
            <small>{t("taxonomy_release_note")}</small>
            <button type="button" className="am-action-primary" disabled={formBlocked || !dirty} onClick={() => void save()}>{busy === "save" ? t("taxonomy_saving") : t("taxonomy_save")}</button>
          </div>
        </>
      )}
    </section>
  );
}
