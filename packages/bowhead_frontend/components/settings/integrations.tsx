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
  const planned = visibleProviders.filter(
    ({ item }) => !item.enabled || !item.healthy,
  );

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <p className={styles.eyebrow}>{t("overview_eyebrow")}</p>
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
      <ProviderSection
        title={t("overview_active")}
        empty={t("overview_active_empty")}
        providers={connected}
        action={t("overview_manage_action")}
        onOpen={(href) => router.push(href)}
      />
      <ProviderSection
        title={t("overview_planned")}
        empty={
          loading ? t("overview_planned_loading") : t("overview_planned_empty")
        }
        providers={planned}
        action={t("overview_start_action")}
        onOpen={(href) => router.push(href)}
      />
      <div className={styles.sectionLabel}>
        {t("overview_coming_soon")}
      </div>
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
  action,
  onOpen,
}: {
  title: string;
  empty: string;
  providers: Array<ProviderDefinition & { item: IntegrationOverviewItemDTO }>;
  action: string;
  onOpen: (href: string) => void;
}) {
  return (
    <>
      <div className={styles.sectionLabel}>{title}</div>
      {providers.length ? (
        <div className={styles.providerGrid}>
          {providers.map((provider) => (
            <ProviderCard
              key={provider.id}
              provider={provider}
              action={action}
              onOpen={onOpen}
            />
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
  action,
  onOpen,
}: {
  provider: ProviderDefinition & { item: IntegrationOverviewItemDTO };
  action: string;
  onOpen: (href: string) => void;
}) {
  const t = useTranslations("settings.integrations");
  const detail = provider.item.configured
    ? t("provider_configured_detail")
    : t("provider_unconfigured_detail");
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
            <strong className={styles.good}>
              <i className={styles.statusDot} aria-hidden="true" />
              {t("provider_status_healthy")}
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
  const t = useTranslations("settings.integrations");
  return (
    <div className={styles.plannedCard}>
      <div className={styles.plannedMark} aria-hidden="true">
        <Plug size={15} />
      </div>
      <div>
        <strong>{name}</strong>
        <span>
          {detail} · {t("provider_planned")}
        </span>
      </div>
    </div>
  );
}
