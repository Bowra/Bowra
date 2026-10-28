"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle, Bot, ChevronRight, Clock, History, X } from "@/components/icons";
import { DialogShell } from "@/components/ui/dialog";
import type {
  ResourceUsageResponse,
  ResourceUsageSummary,
} from "@/lib/resource-usage";

const GROUPS = ["current", "running", "draft", "history"] as const;

export function UsageSummaryInline({
  summary,
  onOpen,
}: {
  summary?: ResourceUsageSummary | null;
  onOpen?: () => void;
}) {
  const t = useTranslations("resource_usage");
  if (!summary) return null;
  const parts = [
    summary.current_agent_count > 0
      ? t("summary_current", { count: summary.current_agent_count })
      : null,
    summary.running_task_count > 0
      ? t("summary_running", { count: summary.running_task_count })
      : null,
    summary.draft_agent_count > 0
      ? t("summary_draft", { count: summary.draft_agent_count })
      : null,
  ].filter((part): part is string => Boolean(part));
  const text =
    parts.length > 0
      ? parts.join(" · ")
      : summary.completeness === "partial"
        ? t("summary_partial")
        : t("summary_unused");
  const content = (
    <>
      <span>{text}</span>
      {summary.completeness === "partial" ? (
        <AlertTriangle size={12} aria-hidden="true" />
      ) : null}
      {onOpen ? <ChevronRight size={13} aria-hidden="true" /> : null}
    </>
  );
  const className = `legacy-usage-summary${summary.completeness === "partial" ? " is-partial" : ""}`;
  if (!onOpen) return <span className={className}>{content}</span>;
  return (
    <button
      type="button"
      className={className}
      aria-label={`${t("label")}：${text}`}
      onClick={(event) => {
        event.stopPropagation();
        onOpen();
      }}
    >
      {content}
    </button>
  );
}

export function ResourceUsageDetails({
  data,
  loading,
  error,
  onRetry,
  onNavigate,
}: {
  data: ResourceUsageResponse | null;
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
  onNavigate?: (href: string) => void;
}) {
  const t = useTranslations("resource_usage");
  if (loading) {
    return (
      <div className="legacy-usage-loading">
        {t("loading")}
      </div>
    );
  }
  if (error || !data) {
    return (
      <div
        role="alert" className="legacy-usage-error"
      >
        <div>{t("load_error")}</div>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry} className="legacy-usage-retry"
          >
            {t("retry")}
          </button>
        ) : null}
      </div>
    );
  }

  const visibleCount = GROUPS.reduce((count, group) => count + data.groups[group].length, 0);
  return (
    <div className="legacy-usage-details">
      {data.completeness === "partial" ? (
        <div className="legacy-usage-partial"
        >
          <AlertTriangle size={14} className="legacy-usage-partial-icon" />
          <span>{t("partial_notice")}</span>
        </div>
      ) : null}

      {visibleCount === 0 ? (
        <div className="legacy-usage-empty">
          {t("empty_all")}
        </div>
      ) : null}

      {visibleCount > 0
        ? GROUPS.map((group) => {
        const items = data.groups[group];
        const Icon = group === "running" ? Clock : group === "history" ? History : Bot;
        return (
          <section key={group} aria-labelledby={`usage-${group}`}>
            <div
              id={`usage-${group}`} className="legacy-usage-group-head"
            >
              <Icon size={13} aria-hidden="true" />
              <span>{t(`groups.${group}`)}</span>
              <span className="legacy-usage-group-count">{items.length}</span>
            </div>
            {items.length > 0 ? (
              <div className="legacy-usage-group-list">
                {items.map((item) => {
                  const row = (
                    <>
                      <span className="legacy-usage-row-main">
                        <span className="legacy-usage-row-name"
                        >
                          {item.display_name}
                        </span>
                        <span className="legacy-usage-row-kind"
                        >
                          {item.kind === "task" ? t("task") : t("agent")}
                          {item.evidence_level === "stable_only"
                            ? ` · ${t("stable_only")}`
                            : ""}
                        </span>
                      </span>
                      <span className={`legacy-usage-readiness is-${item.readiness}`}>
                        <span aria-hidden="true" className="legacy-usage-readiness-dot" />
                        {t(`readiness.${item.readiness}`)}
                      </span>
                      {item.href && onNavigate ? <ChevronRight size={14} /> : null}
                    </>
                  );
                  return item.href && onNavigate ? (
                    <button
                      type="button"
                      key={`${item.kind}:${item.id}:${item.subject_ref}`} className="legacy-usage-row"
                      onClick={() => onNavigate(item.href!)}
                    >
                      {row}
                    </button>
                  ) : (
                    <div key={`${item.kind}:${item.id}:${item.subject_ref}`} className="legacy-usage-row">
                      {row}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="legacy-usage-note">{t("empty_group")}</div>
            )}
          </section>
        );
          })
        : null}

      {data.summary.restricted_count > 0 ? (
        <div className="legacy-usage-note">
          {t("restricted", { count: data.summary.restricted_count })}
        </div>
      ) : null}
      <div className="legacy-usage-note-actual">
        {t("actual_unavailable")}
      </div>
    </div>
  );
}

export function ResourceUsageDrawer({
  title,
  load,
  onClose,
  onNavigate,
}: {
  title: string;
  load: () => Promise<ResourceUsageResponse>;
  onClose: () => void;
  onNavigate: (href: string) => void;
}) {
  const t = useTranslations("resource_usage");
  const [data, setData] = useState<ResourceUsageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await load());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [load]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <DialogShell
      onClose={onClose}
      label={t("title", { name: title })}
      mount="inline"
      backdropClassName="legacy-usage-drawer-backdrop"
      panelClassName="legacy-usage-drawer"
    >
      <header className="legacy-usage-drawer-head"
      >
        <div className="legacy-usage-drawer-headtext">
          <div className="legacy-usage-drawer-eyebrow">
            {t("label")}
          </div>
          <h2 className="legacy-usage-drawer-title">
            {title}
          </h2>
        </div>
        <button
          type="button"
          aria-label={t("close")}
          onClick={onClose} className="legacy-usage-drawer-close"
        >
          <X size={15} />
        </button>
      </header>
      <div className="legacy-usage-drawer-body">
        <ResourceUsageDetails
          data={data}
          loading={loading}
          error={error}
          onRetry={() => void refresh()}
          onNavigate={(href) => {
            onClose();
            onNavigate(href);
          }}
        />
      </div>
    </DialogShell>
  );
}
