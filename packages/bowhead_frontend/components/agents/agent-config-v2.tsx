"use client";

/**
 * Pieces of the redesigned Configure tab (design §4, slices-2-5-tech-design §3.1; FEATURE_AGENT_RELEASE_BAR): what the
 * Agent has, as rows with 移除 / 修改, instead of the catalogs inline. The state stays in AgentReleaseManager; these
 * render it.
 */

import { useEffect, useId, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getAgentKnowledgeBaseSources, getAgentKnowledgeSources } from "@/lib/agents-api";
import { readKnowledgeAccess, selectedKnowledgeBaseIds, type KnowledgeBaseSourceGroup } from "@/lib/agent-knowledge-sources";
import { knowledgeBaseName } from "@/lib/knowledge-base-display";
import type { DraftFieldChip } from "@/lib/agent-draft-changes";

/** 你改过 / Coach 改过 / AI 优化过 beside a field label. */
export function FieldChip({ chip }: { chip: DraftFieldChip | null }) {
  const t = useTranslations("agent_config_v2");
  if (!chip) return null;
  return <Badge size="sm" tone={chip === "manual" ? "neutral" : "accent"} className="am-cfg2-chip">{t(`chip_${chip}` as never)}</Badge>;
}

/** One selected Plugin, connection, Tool or Skill (or a knowledge / methods row) with its action. */
export function ConfigRow({ kind, title, meta, action, tone, children }: {
  kind: string;
  title: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  tone?: "warning";
  children?: ReactNode;
}) {
  return (
    <li className="am-cfg2-row" data-tone={tone}>
      <span className="am-cfg2-row-kind">{kind}</span>
      <span className="am-cfg2-row-body">
        <strong>{title}</strong>
        {meta ? <small>{meta}</small> : null}
        {children}
      </span>
      {action ? <span className="am-cfg2-row-action">{action}</span> : null}
    </li>
  );
}

export function RemoveButton({ name, disabled, reason, onRemove }: { name: string; disabled?: boolean; reason?: string; onRemove: () => void }) {
  const t = useTranslations("agent_config_v2");
  const reasonId = useId();
  return <>
    <Button size="sm" variant="ghost" disabled={disabled} aria-label={t("remove_named", { name })} aria-describedby={reason ? reasonId : undefined} onClick={onRemove}>{t("remove")}</Button>
    {reason ? <small id={reasonId} className="am-cfg2-row-reason">{reason}</small> : null}
  </>;
}

export interface KnowledgeSummary {
  /** The knowledge source setting exists for this Team (or the Agent already has a policy). */
  available: boolean;
  /** "当前上下文中能读到的知识 · 每次搜索结果数用团队默认值" and similar. */
  line: string;
  /** Names the AI 优化 request lists; file names when the Agent reads selected files. */
  names: string[];
}

/**
 * The 知识来源 row: the mode, the file count and the search setting in words. It reads the same catalog the
 * knowledge panel reads (file names for selected files), once per selection.
 */
export function useKnowledgeSummary(agentId: string, value: unknown, enabled = true): KnowledgeSummary {
  const t = useTranslations("agent_config_v2");
  const tBases = useTranslations("knowledge_bases.common");
  const policy = readKnowledgeAccess(value);
  const selected = policy && policy !== "unsupported" ? policy.file_ids ?? [] : [];
  const selectedBases = selectedKnowledgeBaseIds(policy);
  const key = JSON.stringify(selected);
  const [catalog, setCatalog] = useState<{ key: string; enabled: boolean; names: Map<string, string> } | null>(null);
  const [bases, setBases] = useState<KnowledgeBaseSourceGroup[] | null>(null);
  const readBases = enabled && selectedBases.length > 0;
  useEffect(() => {
    if (!enabled) return undefined;
    let active = true;
    void getAgentKnowledgeSources(agentId, "", JSON.parse(key) as string[]).then((page) => {
      if (active) setCatalog({ key, enabled: page.enabled, names: new Map(page.selected_items.map((item) => [item.id, item.name])) });
    }).catch(() => {
      if (active) setCatalog({ key, enabled: false, names: new Map() });
    });
    return () => { active = false; };
  }, [agentId, enabled, key]);
  useEffect(() => {
    // Selected knowledge bases only: the names of those the editor can use.
    if (!readBases) return undefined;
    let active = true;
    void getAgentKnowledgeBaseSources(agentId).then((result) => {
      if (active) setBases(result.groups);
    }).catch(() => { if (active) setBases([]); });
    return () => { active = false; };
  }, [agentId, readBases]);
  if (policy === "unsupported") return { available: true, line: t("knowledge_unsupported"), names: [] };
  if (policy === null && !(catalog?.enabled ?? false)) return { available: false, line: "", names: [] };
  const mode = policy?.mode ?? "context_readable";
  const topK = policy?.defaults?.top_k;
  const search = topK ? t("knowledge_top_k", { count: topK }) : t("knowledge_top_k_default");
  if (mode === "context_readable") {
    return { available: true, line: `${t("knowledge_context")}${t("separator")}${search}`, names: [t("knowledge_context")] };
  }
  if (mode === "selected_knowledge_bases") {
    const spaceName = (spaceId: string) => bases?.find((group) => group.home === "space" && group.home_id === spaceId)?.name ?? null;
    const known = new Map((bases ?? []).flatMap((group) => group.items).map((item) => [item.id, knowledgeBaseName(item, tBases, spaceName)]));
    const baseNames = selectedBases.map((id, index) => {
      const name = known.get(id);
      return name ? t("knowledge_base_name", { name }) : t("knowledge_base_unavailable", { number: index + 1 });
    });
    return {
      available: true,
      line: `${t("knowledge_bases", { count: selectedBases.length })}${t("separator")}${search}`,
      names: baseNames.length ? baseNames : [t("knowledge_bases_none")],
    };
  }
  const names = selected.map((id, index) => catalog?.key === key ? catalog.names.get(id) ?? t("knowledge_file_unavailable", { number: index + 1 }) : t("knowledge_file_unavailable", { number: index + 1 }));
  return {
    available: true,
    line: `${t("knowledge_files", { count: selected.length })}${t("separator")}${search}`,
    names: names.length ? names.map((name) => t("knowledge_file_name", { name })) : [t("knowledge_files_none")],
  };
}

/** 共享后会怎样: at most three bullets, shown as they apply (design §4). Newly added members get no
 * notification (Q9), so the notice bullet offers the Agent link to send them when the page can copy it. */
export function ShareConsequences({ mode, copyLink }: {
  mode: "private" | "custom" | "teamwide";
  copyLink?: { label: string; onCopy: () => void };
}) {
  const t = useTranslations("agent_config_v2");
  const titleId = useId();
  if (mode === "private") return <p className="am-cfg2-share-note" role="note">{t("who_private_note")}</p>;
  return (
    <section className="am-cfg2-share" aria-labelledby={titleId}>
      <h4 id={titleId}>{t("who_after_title")}</h4>
      <ul>
        <li>{t("who_after_review")}</li>
        <li>
          {t("who_after_notice")}
          {copyLink ? <Button size="sm" variant="ghost" onClick={copyLink.onCopy}>{copyLink.label}</Button> : null}
        </li>
        <li>{t("who_after_permissions")}</li>
      </ul>
    </section>
  );
}

/** One row of 更多设置: a title, its current value and a disclosure that opens the existing panel below it. */
export function MoreRow({ id, title, value, open, onToggle, children }: {
  id: string;
  title: string;
  value?: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  const t = useTranslations("agent_config_v2");
  const bodyId = `${id}-body`;
  return (
    <li className="am-cfg2-more" data-open={open || undefined}>
      <div className="am-cfg2-more-head">
        <span className="am-cfg2-more-title">{title}</span>
        {value ? <span className="am-cfg2-more-value">{value}</span> : null}
        <Button size="sm" variant="ghost" aria-expanded={open} aria-controls={bodyId} aria-label={t(open ? "more_hide_named" : "more_open_named", { name: title })} onClick={onToggle}>{t(open ? "more_hide" : "more_open")}</Button>
      </div>
      <div id={bodyId} className="am-cfg2-more-body" hidden={!open}>{children}</div>
    </li>
  );
}
