"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import {
  changeAgentTeamTagStatus,
  createAgentTeamTag,
  getAgentTeamTags,
  type AgentTeamTagManager,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";

export function AgentTeamTagGovernance() {
  const t = useTranslations("agents_marketplace");
  const [manager, setManager] = useState<AgentTeamTagManager | null>(null);
  const [available, setAvailable] = useState(true);
  const [newTag, setNewTag] = useState("");
  const [mergeTargets, setMergeTargets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const { confirm, dialog: confirmDialog } = useConfirmDialog();

  const load = useCallback(async () => {
    try {
      setManager(await getAgentTeamTags());
      setAvailable(true);
      setError(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        setAvailable(false);
        return;
      }
      setError(cause instanceof Error ? cause.message : t("team_tags_load_failed"));
    }
  }, [t]);

  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    const value = newTag.trim();
    if (!value || busy) return;
    setBusy("create");
    setError(null);
    setNotice(null);
    try {
      await createAgentTeamTag(value);
      setNewTag("");
      setNotice(t("team_tags_created"));
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("team_tags_create_failed"));
    } finally {
      setBusy(null);
    }
  };

  const change = async (
    tagId: string,
    rowVersion: number,
    action: "deactivate" | "reactivate" | "merge",
  ) => {
    if (busy) return;
    const target = mergeTargets[tagId];
    if (action === "merge" && !target) return;
    if (action === "merge") {
      const names = {
        source: namesById.get(tagId) ?? tagId,
        target: namesById.get(target) ?? target,
      };
      const ok = await confirm({
        title: t("team_tags_merge_title", names),
        description: t("team_tags_merge_confirm", names),
        confirmLabel: t("team_tags_merge"),
      });
      if (!ok) return;
    }
    setBusy(tagId);
    setError(null);
    setNotice(null);
    try {
      await changeAgentTeamTagStatus(tagId, {
        action,
        row_version: rowVersion,
        ...(target ? { target_tag_id: target } : {}),
      });
      setNotice(t(action === "merge" ? "team_tags_merged" : action === "reactivate" ? "team_tags_reactivated" : "team_tags_deactivated"));
      await load();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) await load();
      setError(cause instanceof Error ? cause.message : t("team_tags_change_failed"));
    } finally {
      setBusy(null);
    }
  };

  const activeTags = manager?.items.filter((item) => item.status === "active") ?? [];
  const namesById = new Map(manager?.items.map((item) => [item.id, item.display_name]) ?? []);
  const aliasTargetIds = new Set(
    manager?.items.flatMap((item) => item.status === "merged" && item.merged_into_id ? [item.merged_into_id] : []) ?? [],
  );

  if (!available) return null;

  return (
    <section className="am-tag-governance" aria-labelledby="team-tag-governance-title">
      <header>
        <div><span>{t("team_tags_eyebrow")}</span><h2 id="team-tag-governance-title">{t("team_tags_title")}</h2><p>{t("team_tags_body")}</p></div>
        <div className="am-tag-create"><input aria-label={t("team_tags_create_placeholder")} name="team-agent-tag" autoComplete="off" value={newTag} maxLength={40} disabled={busy !== null} onChange={(event) => setNewTag(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void create(); } }} placeholder={t("team_tags_create_placeholder")} /><button type="button" disabled={busy !== null || !newTag.trim()} onClick={() => void create()}>{busy === "create" ? t("team_tags_creating") : t("team_tags_create")}</button></div>
      </header>
      {error ? <p className="am-release-error" role="alert">{error}</p> : null}
      {notice ? <p className="am-release-notice" role="status">{notice}</p> : null}
      {!manager ? <div className="am-taxonomy-loading" aria-busy="true">{t("loading")}</div> : manager.items.length ? (
        <div className="am-tag-governance-list">
          {manager.items.map((item) => (
            <article key={item.id}>
              <div><strong>{item.display_name}</strong><span data-state={item.status}>{t(`team_tags_state_${item.status}` as never)}</span>{item.status === "merged" && item.merged_into_id ? <small>{t("team_tags_alias_of", { name: namesById.get(item.merged_into_id) ?? item.merged_into_id })}</small> : null}{aliasTargetIds.has(item.id) ? <small>{t("team_tags_alias_target_locked")}</small> : null}</div>
              {item.status !== "merged" ? <div className="am-tag-governance-actions">
                <select aria-label={t("team_tags_merge_target", { name: item.display_name })} value={mergeTargets[item.id] ?? ""} disabled={busy !== null || aliasTargetIds.has(item.id) || activeTags.filter((candidate) => candidate.id !== item.id).length === 0} onChange={(event) => setMergeTargets((current) => ({ ...current, [item.id]: event.target.value }))}>
                  <option value="">{t("team_tags_choose_target")}</option>
                  {activeTags.filter((candidate) => candidate.id !== item.id).map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.display_name}</option>)}
                </select>
                <button type="button" disabled={busy !== null || !mergeTargets[item.id] || aliasTargetIds.has(item.id)} onClick={() => void change(item.id, item.row_version, "merge")}>{t("team_tags_merge")}</button>
                <button type="button" disabled={busy !== null || aliasTargetIds.has(item.id)} onClick={() => void change(item.id, item.row_version, item.status === "active" ? "deactivate" : "reactivate")}>{t(item.status === "active" ? "team_tags_deactivate" : "team_tags_reactivate")}</button>
              </div> : null}
            </article>
          ))}
        </div>
      ) : <p className="am-tag-governance-empty">{t("team_tags_empty")}</p>}
      {confirmDialog}
    </section>
  );
}
