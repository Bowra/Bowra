"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { Plug, RefreshCw, Spinner } from "@/components/icons";
import {
  fetchIntegrationOverview,
  type IntegrationOverviewItemDTO,
  type IntegrationProviderId,
} from "@/lib/api";
import { useAppRouter } from "@/lib/use-app-router";

import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./integration-pages.module.css";
import { CharIcon } from "@/components/char-icon";

// Append a global ui-integ-* hook to each class (new appearance in styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-integ");

// Copy keys all live under settings.integrations; resolve names and descriptions in the UI language, never hardcode them here.
type ProviderDefinition = {
  id: IntegrationProviderId;
  titleKey: "provider_feishu_name" | "provider_slack" | "provider_teams_name";
  descriptionKey:
    | "provider_feishu_card"
    | "provider_slack_card"
    | "provider_teams_card";
  href: string;
};

const PROVIDERS: ProviderDefinition[] = [
  {
    id: "feishu",
    titleKey: "provider_feishu_name",
    descriptionKey: "provider_feishu_card",
    href: "/team/settings/integrations/feishu/",
  },
  {
    id: "slack",
    titleKey: "provider_slack",
    descriptionKey: "provider_slack_card",
    href: "/team/settings/integrations/slack/",
  },
  {
    id: "teams",
    titleKey: "provider_teams_name",
    descriptionKey: "provider_teams_card",
    href: "/team/settings/integrations/teams/",
  },
];

export function SettingsIntegrations() {
  const t = useTranslations("settings.integrations");
  const router = useAppRouter();
  const [items, setItems] = useState<IntegrationOverviewItemDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      setItems((await fetchIntegrationOverview()).items);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const visibleProviders = useMemo(
    () =>
      PROVIDERS.flatMap((provider) => {
        const item = items.find(({ provider: id }) => id === provider.id);
        return item?.available ? [{ ...provider, item }] : [];
      }),
    [items],
  );
  const connected = visibleProviders.filter(
    ({ item }) => item.enabled && item.healthy,
  );
  const available = visibleProviders.filter(
    ({ item }) => !item.enabled || !item.healthy,
  );

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <h1 className="ui-page-title">{t("title")}</h1>
          <p className="ui-page-desc">{t("overview_description")}</p>
        </div>
        <button
          className={styles.refreshButton}
          type="button"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? (
            <Spinner className={styles.spinner} aria-hidden="true" />
          ) : (
            <RefreshCw size={14} aria-hidden="true" />
          )}
          {loading ? t("refreshing") : t("refresh")}
        </button>
      </header>
      {error ? (
        <div className={`${styles.statusBanner} ${styles.statusBannerWarn}`}>
          <div>
            <strong>{t("overview_load_failed")}</strong>
            <p>{t("overview_load_failed_description")}</p>
          </div>
        </div>
      ) : null}
      {(!loading && !error) || visibleProviders.length > 0 ? (
        <>
          <ProviderSection
            title={t("overview_active")}
            empty={t("overview_active_empty")}
            providers={connected}
            onOpen={(href) => router.push(href)}
          />
          <ProviderSection
            title={t("overview_available")}
            empty={t("overview_available_empty")}
            providers={available}
            onOpen={(href) => router.push(href)}
          />
        </>
      ) : null}
      <h2 className={styles.sectionLabel}>
        {t("overview_coming_soon")}
      </h2>
      <div className={styles.plannedGrid}>
        <PlannedCard
          name={t("provider_wecom")}
          detail={t("provider_wecom_detail")}
        />
        <PlannedCard
          name={t("provider_dingtalk")}
          detail={t("provider_dingtalk_detail")}
        />
      </div>
    </div>
  );
}

function ProviderSection({
  title,
  empty,
  providers,
  onOpen,
}: {
  title: string;
  empty: string;
  providers: Array<ProviderDefinition & { item: IntegrationOverviewItemDTO }>;
  onOpen: (href: string) => void;
}) {
  return (
    <>
      <h2 className={styles.sectionLabel}>{title}</h2>
      {providers.length ? (
        <div className={styles.providerGrid}>
          {providers.map((provider) => (
              <ProviderCard key={provider.id} provider={provider} onOpen={onOpen} />
          ))}
        </div>
      ) : (
        <section className={styles.callbackCard}>
          <p>{empty}</p>
        </section>
      )}
    </>
  );
}

function ProviderCard({
  provider,
  onOpen,
}: {
  provider: ProviderDefinition & { item: IntegrationOverviewItemDTO };
  onOpen: (href: string) => void;
}) {
  const t = useTranslations("settings.integrations");
  const detail = provider.item.configured
    ? t("provider_configured_detail")
    : t("provider_unconfigured_detail");
  const { enabled, healthy, configured } = provider.item;
  const action = enabled
    ? t(healthy ? "overview_manage_action" : "overview_review_action")
    : t(configured ? "overview_resume_action" : "overview_start_action");
  return (
    <article className={styles.providerCard}>
      <span className={styles.providerIcon} aria-hidden="true">
        <Plug />
      </span>
      <span>
        <span className={styles.providerTitle}>
          {t(provider.titleKey)}
        </span>
        <span className={styles.providerDescription}>
          {t(provider.descriptionKey)}
        </span>
      </span>
      {provider.item.enabled ? (
        <span className={styles.statusGrid}>
          <span className={styles.statusCell}>
            <span>{t("provider_status_label")}</span>
            <strong className={healthy ? styles.good : styles.warn}>
              <i className={styles.statusDot} aria-hidden="true" />
              {t(healthy ? "provider_status_healthy" : "provider_status_unhealthy")}
            </strong>
          </span>
        </span>
      ) : (
        <span className={styles.providerDescription}>{detail}</span>
      )}
      <button
        className={`${styles.primaryButton} ${styles.providerAction}`}
        type="button"
        onClick={() => onOpen(provider.href)}
      >
        {action}
        <span aria-hidden="true"><CharIcon ch="→" aria-hidden /></span>
      </button>
    </article>
  );
}

function PlannedCard({ name, detail }: { name: string; detail: string }) {
  return (
    <div className={styles.plannedCard}>
      <div className={styles.plannedMark} aria-hidden="true">
        <Plug size={15} />
      </div>
      <div>
        <strong>{name}</strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}
