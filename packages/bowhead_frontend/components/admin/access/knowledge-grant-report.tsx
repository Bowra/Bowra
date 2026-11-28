"use client";

/**
 * Access control › Knowledge base grants (organization phase 1, slice 6; rendered only while `/me` reports
 * `capabilities.knowledge_bases`). Read-only: every `kb_scope` and `kb_file` grant of the Team with who it
 * applies to, its target by name, what it restricts today (and whether enforcement is on) and the knowledge
 * base members it would map to. Grants keep narrowing and never grant access; the mapping applies nothing.
 */
import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { ApiError } from "@/lib/api-client";
import { formatDateYMD } from "@/lib/format";
import { knowledgeBaseName } from "@/lib/knowledge-base-display";
import {
  type GrantReport,
  type GrantReportItem,
  type GrantReportKnowledgeBase,
  type GrantReportPrincipal,
  type GrantReportTarget,
  fetchKnowledgeGrantReport,
} from "@/lib/knowledge-grant-report-api";

type Translator = (key: string, values?: Record<string, string | number>) => string;

/** Stored grant permissions with a label under `admin.access.grant.permission`. */
const PERMISSION_LABELS = new Set(["view", "read", "use", "invoke", "edit", "write", "manage", "share", "admin"]);

/** Localized load failure: 403 role, 404 switched off meanwhile, 422 stale page position, otherwise retry. */
export function grantReportErrorKey(error: unknown): string {
  const status = error instanceof ApiError ? error.status : 0;
  if (status === 403) return "load_forbidden";
  if (status === 404) return "load_unavailable";
  if (status === 422) return "cursor_invalid";
  return "load_failed";
}

function principalLabel(p: GrantReportPrincipal, t: Translator): string {
  switch (p.type) {
    case "user":
      if (!p.label) return t("principal_user_unknown");
      return p.state === "not_in_team" ? t("principal_user_left", { name: p.label }) : p.label;
    case "group":
      if (!p.label) return t("principal_group_unknown");
      if (p.state === "group_removed") return t("principal_group_removed", { name: p.label });
      return p.space_name ? t("principal_group_in_space", { name: p.label, space: p.space_name }) : t("principal_group", { name: p.label });
    case "workspace":
      if (!p.label) return t("principal_space_unknown");
      return t(p.state === "space_archived" ? "principal_space_archived" : "principal_space", { name: p.label });
    case "team":
      return t(p.state === "active" ? "principal_team" : "principal_team_other");
    default:
      return t("principal_unrecognized");
  }
}

function targetLabel(target: GrantReportTarget, t: Translator): string {
  if (target.kind === "kb_scope") {
    if (target.state === "unrecognized") return t("target_scope_unrecognized");
    if (target.state === "not_found") return t("target_scope_not_found");
    if (target.home === "team") return t("target_scope_team");
    return t(target.state === "space_archived" ? "target_scope_space_archived" : "target_scope_space", { space: target.space_name ?? "" });
  }
  switch (target.state) {
    case "active":
      return t("target_file", { file: target.file_name ?? "" });
    case "deleted":
      return t("target_file_deleted");
    case "unavailable":
      return t("target_file_unavailable");
    case "hidden":
      if (target.home === "personal") return t("target_file_hidden_personal");
      if (target.home === "team") return t("target_file_hidden_team");
      return t("target_file_hidden_space", { space: target.space_name ?? "" });
    default:
      return t("target_file_not_found");
  }
}

function useKnowledgeBaseList(): (kbs: GrantReportKnowledgeBase[], more: number) => string {
  const locale = useLocale();
  const tCommon = useTranslations("knowledge_bases.common");
  const t = useTranslations("admin.access.kb_report");
  return useCallback(
    (kbs: GrantReportKnowledgeBase[], more: number) => {
      const names = kbs.map((kb) => t("kb_name", { name: knowledgeBaseName(kb, tCommon, () => kb.home_name) }));
      if (more > 0) names.push(t("map_more", { count: more }));
      try {
        return new Intl.ListFormat(locale, { type: "conjunction" }).format(names);
      } catch {
        return names.join(", ");
      }
    },
    [locale, t, tCommon],
  );
}

function GrantReportRow({ item, enforced }: { item: GrantReportItem; enforced: boolean }) {
  const t = useTranslations("admin.access.kb_report") as Translator;
  const tAccess = useTranslations("admin.access") as Translator;
  const tCommon = useTranslations("knowledge_bases.common") as Translator;
  const listKnowledgeBases = useKnowledgeBaseList();
  const principal = principalLabel(item.applies_to, t);
  const permission = PERMISSION_LABELS.has(item.permission) ? tAccess(`grant.permission.${item.permission}`) : item.permission;
  const actions = item.restriction.actions.includes("write") ? t("actions_read_write") : t("actions_read");
  const { restriction, mapping, target } = item;

  let restricts: string;
  if (restriction.kind === "none") {
    restricts = restriction.reason === "expired"
      ? t("restricts_expired", { date: formatDateYMD(item.expires_at) })
      : t("restricts_not_checked", { permission });
  } else if (restriction.kind === "deny") {
    restricts = t("restricts_deny", { principal, actions });
  } else {
    restricts = restriction.actions.length
      ? t("restricts_allow_list", { principal, actions })
      : t("restricts_allow_list_nothing", { principal, permission });
  }

  let maps: string;
  if (mapping.action === "none") {
    maps = t(`map_none_${mapping.reason ?? "target_unavailable"}`);
  } else {
    const kbs = mapping.knowledge_bases.length ? listKnowledgeBases(mapping.knowledge_bases, mapping.more) : "";
    const level = tCommon(`level_${mapping.level ?? "use"}`);
    if (mapping.action === "keep_out") {
      maps = kbs ? t("map_keep_out", { principal, kbs }) : t("map_keep_out_hidden", { principal });
    } else {
      maps = kbs ? t("map_needs_level", { principal, level, kbs }) : t("map_needs_level_hidden", { principal, level });
    }
  }

  const kb = target.knowledge_base;
  const effectDeny = item.effect === "deny";
  return (
    <li className="kb-grant-report__item" data-testid="kb-grant-report-item">
      <div className="kb-grant-report__item-head">
        <strong className="kb-grant-report__target">{targetLabel(target, t)}</strong>
        <span className="kb-grant-report__pills">
          <span className={effectDeny ? "kb-grant-report__pill is-deny" : "kb-grant-report__pill is-allow"}>
            {tAccess(effectDeny ? "effect_deny" : "effect_allow")}
          </span>
          <span className="kb-grant-report__pill">{permission}</span>
          {item.expired ? <span className="kb-grant-report__pill is-muted">{t("expired_badge")}</span> : null}
        </span>
      </div>
      {kb ? (
        <p className="kb-grant-report__sub">
          {t("target_in_knowledge_base", { kb: t("kb_name", { name: knowledgeBaseName(kb, tCommon, () => kb.home_name) }) })}
        </p>
      ) : null}
      <dl className="kb-grant-report__facts">
        <div>
          <dt>{t("field_applies_to")}</dt>
          <dd>
            {principal}
            {item.applies_to.type === "group" && item.applies_to.member_count != null && item.applies_to.state === "active"
              ? <span className="kb-grant-report__muted"> · {t("group_members", { count: item.applies_to.member_count })}</span>
              : null}
          </dd>
        </div>
        <div>
          <dt>{t("field_restricts")}</dt>
          <dd>
            {restricts}
            {restriction.kind !== "none" && !enforced ? <span className="kb-grant-report__muted"> {t("restricts_log_only")}</span> : null}
            {target.kind === "kb_file" && target.home === "personal" ? <span className="kb-grant-report__note">{t("personal_note")}</span> : null}
            {target.kind === "kb_scope" && target.home === "space" && restriction.kind !== "none"
              ? <span className="kb-grant-report__note">{t("space_search_note")}</span>
              : null}
          </dd>
        </div>
        <div>
          <dt>{t("field_mapping")}</dt>
          <dd>
            {maps}
            {mapping.action !== "none" && mapping.whole_home ? <span className="kb-grant-report__note">{t("map_whole_home")}</span> : null}
            {mapping.action !== "none" && mapping.open_audience ? <span className="kb-grant-report__note">{t("map_open_audience")}</span> : null}
          </dd>
        </div>
        <div>
          <dt>{t("field_expires")}</dt>
          <dd>{item.expires_at ? formatDateYMD(item.expires_at) : tAccess("grant.no_expiry")}</dd>
        </div>
      </dl>
      <style jsx>{`
        .kb-grant-report__item {
          border: 1px solid var(--c-line);
          border-radius: 8px;
          padding: 10px 12px;
          min-width: 0;
          font-size: 0.8125rem;
        }
        .kb-grant-report__item-head {
          display: flex;
          flex-wrap: wrap;
          justify-content: space-between;
          align-items: baseline;
          gap: 8px;
        }
        .kb-grant-report__target {
          overflow-wrap: anywhere;
        }
        .kb-grant-report__pills {
          display: inline-flex;
          flex-wrap: wrap;
          gap: 6px;
        }
        .kb-grant-report__pill {
          display: inline-flex;
          align-items: center;
          height: 22px;
          padding: 0 8px;
          border-radius: 999px;
          border: 1px solid var(--c-line);
          font-size: 0.75rem;
          white-space: nowrap;
        }
        .kb-grant-report__pill.is-allow {
          color: var(--c-pos-ink);
          background: var(--c-pos-soft);
        }
        .kb-grant-report__pill.is-deny {
          color: var(--c-err-ink);
          background: var(--c-err-soft);
        }
        .kb-grant-report__pill.is-muted {
          color: var(--c-ink-3);
          background: var(--c-surface-2);
        }
        .kb-grant-report__sub {
          margin: 2px 0 0;
          color: var(--c-ink-3);
          font-size: 0.75rem;
          overflow-wrap: anywhere;
        }
        .kb-grant-report__facts {
          display: grid;
          gap: 6px;
          margin: 8px 0 0;
        }
        .kb-grant-report__facts > div {
          display: grid;
          grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
          gap: 10px;
        }
        .kb-grant-report__facts dt {
          color: var(--c-ink-3);
        }
        .kb-grant-report__facts dd {
          margin: 0;
          min-width: 0;
          overflow-wrap: anywhere;
          line-height: 1.5;
        }
        .kb-grant-report__muted {
          color: var(--c-ink-3);
        }
        .kb-grant-report__note {
          display: block;
          margin-top: 2px;
          color: var(--c-ink-3);
          font-size: 0.75rem;
        }
        @media (max-width: 640px) {
          .kb-grant-report__facts > div {
            grid-template-columns: minmax(0, 1fr);
            gap: 2px;
          }
        }
      `}</style>
    </li>
  );
}

/** Read-only report; `refreshKey` changes when the page's Refresh is pressed or a grant changes. */
export function KnowledgeGrantReport({ refreshKey = 0 }: { refreshKey?: number }) {
  const t = useTranslations("admin.access.kb_report") as Translator;
  const [report, setReport] = useState<GrantReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ShownError | null>(null);
  const [moreLoading, setMoreLoading] = useState(false);
  const [moreError, setMoreError] = useState<ShownError | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setMoreError(null);
    fetchKnowledgeGrantReport()
      .then((page) => {
        if (!cancelled) setReport(page);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setReport(null);
          setError(shownError(t(grantReportErrorKey(err)), err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, attempt, t]);

  const loadMore = async () => {
    if (!report?.next_cursor || moreLoading) return;
    setMoreLoading(true);
    setMoreError(null);
    try {
      const page = await fetchKnowledgeGrantReport({ cursor: report.next_cursor });
      setReport((current) => current && {
        ...page,
        items: [...current.items, ...page.items.filter((item) => !current.items.some((seen) => seen.grant_id === item.grant_id))],
      });
    } catch (err) {
      setMoreError(shownError(t(grantReportErrorKey(err) === "load_failed" ? "more_failed" : grantReportErrorKey(err)), err));
    } finally {
      setMoreLoading(false);
    }
  };

  return (
    <section className="kb-grant-report" aria-labelledby="kb-grant-report-title" data-testid="kb-grant-report">
      <div className="kb-grant-report__head">
        <h2 id="kb-grant-report-title">{t("title")}</h2>
        {report ? <span>{t("count", { count: report.total })}</span> : null}
      </div>
      <p className="kb-grant-report__intro">{t("intro")}</p>
      {report ? (
        <p className="kb-grant-report__enforcement" data-testid="kb-grant-report-enforcement">
          <span className={report.enforced ? "kb-grant-report__pill is-deny" : "kb-grant-report__pill is-muted"}>
            {t(report.enforced ? "enforced_badge" : "log_only_badge")}
          </span>
          <span>{t(report.enforced ? "enforced_body" : "log_only_body")}</span>
        </p>
      ) : null}
      {loading && !report ? <LoadingSkeleton rows={2} label={t("loading")} /> : null}
      {error ? (
        <div className="kb-grant-report__error">
          <p role="alert">{error.message}</p>
          <ErrorDiagnosticsInline error={error.error} surface="settings" />
          <button type="button" className="ui-btn" onClick={() => setAttempt((n) => n + 1)}>{t("retry")}</button>
        </div>
      ) : null}
      {report && report.items.length === 0 ? <p className="kb-grant-report__empty">{t("empty")}</p> : null}
      {report && report.items.length > 0 ? (
        <ul className="kb-grant-report__list" aria-label={t("title")}>
          {report.items.map((item) => <GrantReportRow key={item.grant_id} item={item} enforced={report.enforced} />)}
        </ul>
      ) : null}
      {moreError ? (
        <div className="kb-grant-report__error">
          <p role="alert">{moreError.message}</p>
          <ErrorDiagnosticsInline error={moreError.error} surface="settings" />
        </div>
      ) : null}
      {report?.next_cursor ? (
        <button type="button" className="ui-btn kb-grant-report__more" onClick={() => void loadMore()} disabled={moreLoading}>
          {moreLoading ? t("loading_more") : t("load_more")}
        </button>
      ) : null}
      <style jsx>{`
        .kb-grant-report {
          border: 1px solid var(--c-line);
          border-radius: 8px;
          background: var(--c-surface);
          padding: 14px;
          margin-bottom: 14px;
          min-width: 0;
        }
        .kb-grant-report__head {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 12px;
          margin-bottom: 8px;
        }
        .kb-grant-report__head h2 {
          margin: 0;
          font-size: 0.9375rem;
          font-weight: 620;
          letter-spacing: 0;
        }
        .kb-grant-report__head span,
        .kb-grant-report__intro,
        .kb-grant-report__empty {
          color: var(--c-ink-3);
          font-size: 0.75rem;
        }
        .kb-grant-report__intro {
          margin: 0 0 10px;
          line-height: 1.5;
        }
        .kb-grant-report__enforcement {
          display: flex;
          flex-wrap: wrap;
          align-items: baseline;
          gap: 8px;
          margin: 0 0 12px;
          font-size: 0.8125rem;
        }
        .kb-grant-report__list {
          display: grid;
          gap: 8px;
          margin: 0;
          padding: 0;
          list-style: none;
        }
        .kb-grant-report__error {
          display: grid;
          gap: 8px;
          justify-items: start;
          margin-bottom: 12px;
          color: var(--c-err-ink);
          font-size: 0.8125rem;
        }
        .kb-grant-report__error p {
          margin: 0;
        }
        .kb-grant-report__more {
          margin-top: 10px;
        }
        .kb-grant-report__pill {
          display: inline-flex;
          align-items: center;
          height: 22px;
          padding: 0 8px;
          border-radius: 999px;
          border: 1px solid var(--c-line);
          font-size: 0.75rem;
          white-space: nowrap;
        }
        .kb-grant-report__pill.is-deny {
          color: var(--c-err-ink);
          background: var(--c-err-soft);
        }
        .kb-grant-report__pill.is-muted {
          color: var(--c-ink-3);
          background: var(--c-surface-2);
        }
      `}</style>
    </section>
  );
}
