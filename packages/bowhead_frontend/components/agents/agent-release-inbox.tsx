"use client";

import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { listAgentReleaseInbox, type AgentReleaseInboxItem } from "@/lib/agents-api";
import { appPath } from "@/lib/base-path";
import { agentArtworkStyle } from "./agent-artwork";
import { agentMark } from "./agent-mark";

function formatDate(value: string, locale: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

export function AgentReleaseInbox({ totalCount = 0 }: { totalCount?: number }) {
  const t = useTranslations("agents_marketplace");
  const locale = useLocale();
  const [items, setItems] = useState<AgentReleaseInboxItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void listAgentReleaseInbox().then((result) => {
      if (cancelled) return;
      setItems(result.items);
      setError(false);
    }).catch(() => {
      if (!cancelled) setError(true);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [reloadToken]);

  return (
    <section className="am-release-inbox" aria-labelledby="am-release-inbox-title" aria-busy={loading}>
      <header>
        <div><span>{t("release_inbox_eyebrow")}</span><h2 id="am-release-inbox-title">{t("release_inbox_title")}</h2><p>{t("release_inbox_body")}</p></div>
        <strong>{t("release_inbox_count", { count: Math.max(totalCount, items.length) })}</strong>
      </header>
      {error ? <div className="am-release-inbox-empty"><p>{t("release_inbox_failed")}</p><button type="button" onClick={() => setReloadToken((value) => value + 1)}>{t("retry")}</button></div> : loading ? <div className="am-release-inbox-loading"><i /><i /></div> : items.length ? (
        <div className="am-release-inbox-list">
          {items.map((item) => (
            <a key={item.candidate_id} href={appPath(`${item.detail_url}&agent_id=${encodeURIComponent(item.agent_id)}`)}>
              <span className="am-management-mark" style={agentArtworkStyle(`${item.agent_id}:${item.display_name}`)}>{agentMark(item.display_name)}</span>
              <div><h3>{item.display_name}</h3><p>{t("release_inbox_request", { audience: t(`release_audience_${item.audience.mode}` as never), risk: t(`risk_${item.risk.level ?? "unknown"}` as never) })}</p><small>{formatDate(item.submitted_at, locale)}</small></div>
              <span>{t("release_review_action")}</span>
            </a>
          ))}
        </div>
      ) : <div className="am-release-inbox-empty"><strong>{t("release_inbox_empty_title")}</strong><p>{t("release_inbox_empty_body")}</p></div>}
    </section>
  );
}
