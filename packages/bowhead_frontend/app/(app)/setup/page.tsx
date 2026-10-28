"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { RefreshCw } from "lucide-react";

import { Check, ChevronRight, ExternalLink, Info } from "@/components/icons";
import { ErrorNotice } from "@/components/error-notice";
import { PageActionLink, PageFrame, PageHeader, PageMobileAction } from "@/components/shell/page-frame";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { SectionHeading } from "@/components/ui/section-heading";
import { appPath } from "@/lib/base-path";
import { safeInternalPath } from "@/lib/safe-next-path";
import { MODEL_ADMIN_ROLES } from "@/lib/settings-navigation";
import {
  fetchTeamOnboardingStatus,
  TeamOnboardingApiError,
  type ReadinessItem,
  type ReadinessState,
  type TeamOnboardingStatus,
} from "@/lib/team-onboarding-api";
import { useAppRouter } from "@/lib/use-app-router";

import styles from "./setup.module.css";

const CORE_IDS = ["license_active", "public_entry_ready", "llm_primary_configured", "space_ready", "first_turn_succeeded"] as const;
const SUPPORT_HREF = process.env.NEXT_PUBLIC_BOWHEAD_LICENSE_CONTACT_URL || "/team/settings/license/";

const STATE_TONES: Record<ReadinessState, BadgeTone> = {
  ready: "positive",
  not_applicable: "neutral",
  warning: "warning",
  blocked: "danger",
  unknown: "warning",
  missing: "neutral",
};

function isAbortError(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

function formatObservedAt(value: string, locale?: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export default function SetupPage() {
  const t = useTranslations("team_onboarding");
  const locale = useLocale();
  const router = useAppRouter();
  const snapshotRef = useRef<TeamOnboardingStatus | null>(null);
  const manualRefreshRef = useRef(false);
  const [snapshot, setSnapshot] = useState<TeamOnboardingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [stale, setStale] = useState(false);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  // credential-readiness R8: branch by can_act (B6, same criterion as chat surface A0a).
  // For nonadmins, lazily fetch the minimal set of people who can configure (capability-contacts, display names only).
  const [completedOpen, setCompletedOpen] = useState(false);
  const [laterOpen, setLaterOpen] = useState(false);
  const [environmentOpen, setEnvironmentOpen] = useState(false);
  const [capabilityContacts, setCapabilityContacts] = useState<string[]>([]);
  const canConfigureModels = snapshot != null && (MODEL_ADMIN_ROLES as readonly string[]).includes(snapshot.context.current_role);
  useEffect(() => {
    if (snapshot == null || canConfigureModels || !laterOpen) return;
    const states = Object.values(snapshot.context.optional_capabilities);
    if (!states.some((s) => s !== "enabled")) return;
    let cancelled = false;
    void (async () => {
      try {
        const { fetchWithRefresh } = await import("@/lib/auth-api");
        const resp = await fetchWithRefresh("/api/v1/team/capability-contacts");
        if (!resp.ok) return;
        const data = (await resp.json()) as { contacts?: { model_config?: string[] } };
        if (!cancelled) setCapabilityContacts((data.contacts?.model_config ?? []).filter(Boolean));
      } catch {
        // Contact-list failure does not affect primary copy (fallback asks a team admin to enable it, without naming people).
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [snapshot, canConfigureModels, laterOpen]);

  const load = useCallback(
    async (signal?: AbortSignal, dedupeManualRefresh = false) => {
      if (dedupeManualRefresh && manualRefreshRef.current) return;
      if (dedupeManualRefresh) manualRefreshRef.current = true;
      if (snapshotRef.current) setRefreshing(true);
      else setLoading(true);
      setErrorStatus(null);
      try {
        const next = await fetchTeamOnboardingStatus(signal);
        snapshotRef.current = next;
        setSnapshot(next);
        setStale(false);
      } catch (cause) {
        if (isAbortError(cause)) return;
        const status = cause instanceof TeamOnboardingApiError ? cause.status : 0;
        if (status === 401) {
          router.replace("/login?next=%2Fsetup");
          return;
        }
        if (snapshotRef.current) setStale(true);
        else setErrorStatus(status);
      } finally {
        if (dedupeManualRefresh) manualRefreshRef.current = false;
        setLoading(false);
        setRefreshing(false);
      }
    },
    [router],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const openAction = (item: ReadinessItem) => {
    const href = safeInternalPath(item.action?.href);
    if (!href || !item.can_act) return;
    router.push(href);
  };

  if (loading && !snapshot) {
    return (
      <PageFrame title={t("title")} width="form">
        <PageHeader title={t("title")} />
        <LoadingSkeleton rows={5} label={t("loading")} />
      </PageFrame>
    );
  }

  if (!snapshot) {
    const denied = errorStatus === 403;
    const unavailable = errorStatus === 404;
    return (
      <PageFrame title={t("title")} width="form">
        <PageHeader title={denied ? t("errors.denied_title") : t("errors.load_title")} />
        <ErrorNotice
          surface="settings"
          message={denied ? t("errors.denied_body") : unavailable ? t("errors.unavailable_body") : t("errors.load_body")}
          action={!denied && !unavailable ? { kind: "retry", label: t("actions.retry") } : undefined}
          onAction={() => void load(undefined, true)}
        />
        <Button className={styles.errorExit} onClick={() => router.push("/")}>
          {t("actions.enter")}
        </Button>
      </PageFrame>
    );
  }

  const itemsById = new Map(snapshot.items.map((item) => [item.id, item]));
  const coreReady = snapshot.milestone === "core_ready";
  const coreItems = CORE_IDS.map((id) => itemsById.get(id)).filter((item): item is ReadinessItem => Boolean(item));
  const pendingCore = coreItems.filter((item) => !item.satisfied);
  const focusItem = pendingCore[0] ?? null;
  const safeAction = (item: ReadinessItem | null): string | null => (item?.can_act ? safeInternalPath(item.action?.href) : null);
  const focusAction = safeAction(focusItem);
  const laterCoreAction = pendingCore.slice(1).find((item) => Boolean(safeAction(item))) ?? null;
  const allPendingCoreUnknown = pendingCore.length > 0 && pendingCore.every((item) => item.state === "unknown");
  const focusRoles = focusItem?.required_roles.map((role) => t(`roles.${role}`)).join(" / ");
  const supportHref = appPath(SUPPORT_HREF);
  const supportIsExternal = !SUPPORT_HREF.startsWith("/");
  const formatTime = (value: string) => formatObservedAt(value, locale);

  const completedCore = coreItems.filter((item) => item.satisfied && item.state !== "warning");
  const attentionCore = coreItems.filter((item) => !item.satisfied || item.state === "warning");
  const runtime = snapshot.context.runtime;
  const pageTitle = coreReady ? t("completion.title") : t("title");
  const refreshButton = (
    <Button loading={refreshing} disabled={refreshing} onClick={() => void load(undefined, true)}>
      {t(refreshing ? "actions.refreshing" : "actions.refresh")}
    </Button>
  );

  const renderItem = (item: ReadinessItem) => {
    const roles = item.required_roles.map((role) => t(`roles.${role}`)).join(" / ");
    return (
      <li className={styles.item} key={item.id} data-state={item.state}>
        <div className={styles.itemHeader}>
          <h3>{t(`items.${item.id}.title`)}</h3>
          <Badge tone={STATE_TONES[item.state]}>{t(`states.${item.state}`)}</Badge>
        </div>
        <p>{t(`reasons.${item.reason_code}`)}</p>
        <div className={styles.itemFooter}>
          <details className={styles.details}>
            <summary>{t("actions.show_details")}</summary>
            <dl className={styles.facts}>
              <div>
                <dt>{t("item_meta.impact")}</dt>
                <dd>{t(`items.${item.id}.impact`)}</dd>
              </div>
              <div>
                <dt>{t("item_meta.owner")}</dt>
                <dd>{roles || "—"}</dd>
              </div>
              <div>
                <dt>{t("item_meta.observed_label")}</dt>
                <dd>
                  <time dateTime={item.observed_at}>{formatTime(item.observed_at)}</time>
                </dd>
              </div>
            </dl>
            {item.checks.length ? (
              <details className={styles.details}>
                <summary>{t("checks_label", { count: item.checks.length })}</summary>
                <ul className={styles.checks}>
                  {item.checks.map((check) => (
                    <li key={check.id}>
                      <Badge tone={STATE_TONES[check.state]}>{t(`states.${check.state}`)}</Badge>
                      <span>
                        {t(`checks.${check.id}`)}
                        {check.required ? <span className={styles.required}> · {t("required")}</span> : null}
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </details>
          {item.action && item.state !== "not_applicable" ? (
            <Button variant="ghost" disabled={!item.can_act || !safeInternalPath(item.action.href)} onClick={() => openAction(item)}>
              {item.can_act ? t(`items.${item.id}.action`) : t("actions.requires_role", { roles: roles || t("actions.no_permission") })}
              <ChevronRight size={14} aria-hidden="true" />
            </Button>
          ) : null}
        </div>
      </li>
    );
  };

  return (
    <PageFrame
      title={pageTitle}
      width="form"
      mobileActions={
        <PageMobileAction
          label={t(refreshing ? "actions.refreshing" : "actions.refresh")}
          icon={RefreshCw}
          onClick={() => void load(undefined, true)}
        />
      }
      className={styles.page}
      testId="setup-frame"
    >
      <PageHeader
        title={pageTitle}
        description={t(coreReady ? "completion.description" : "subtitle", { team: snapshot.context.team_name })}
        secondary={refreshButton}
      />
      <div className={styles.content} aria-busy={refreshing}>
        <p className={styles.srOnly} aria-live="polite">
          {refreshing ? t("actions.refreshing") : t("refresh_complete", { time: formatTime(snapshot.generated_at) })}
        </p>
        {stale ? <Notice tone="muted">{t("stale")}</Notice> : null}
        {snapshot.partial ? <Notice>{t("partial")}</Notice> : null}

        <section className={styles.nextAction} aria-labelledby="team-setup-next-action">
          <div className={styles.progress} aria-label={t("progress.label")}>
            <Badge tone={coreReady ? "positive" : "neutral"}>{t(coreReady ? "progress.core_ready" : "progress.core_configuring")}</Badge>
            <span>
              {snapshot.core.satisfied} / {snapshot.core.total}
            </span>
            {!coreReady ? (
              <progress
                max={snapshot.core.total}
                value={snapshot.core.satisfied}
                aria-label={t("progress.count_label", { count: snapshot.core.satisfied, total: snapshot.core.total })}
              />
            ) : null}
          </div>
          <div className={styles.nextActionCopy}>
            {coreReady ? <Check size={24} className={styles.successIcon} aria-hidden="true" /> : null}
            <h2 id="team-setup-next-action">
              {coreReady ? t("next_action.ready_title") : focusItem ? t(`items.${focusItem.id}.title`) : t("next_action.unknown_title")}
            </h2>
            <p>{coreReady ? t("next_action.ready_body") : focusItem ? t(`reasons.${focusItem.reason_code}`) : t("next_action.unknown_body")}</p>
          </div>
          {!coreReady && focusItem?.action && !focusItem.can_act ? (
            <Notice tone="muted">{t("next_action.role_required", { roles: focusRoles || t("actions.no_permission") })}</Notice>
          ) : null}
          <div className={styles.actions}>
            {coreReady ? (
              <Button variant="primary" data-testid="primary-setup-action" onClick={() => router.push("/")}>
                {t("actions.enter")}
                <ChevronRight size={14} aria-hidden="true" />
              </Button>
            ) : allPendingCoreUnknown ? (
              <Button
                variant="primary"
                data-testid="primary-setup-action"
                loading={refreshing}
                disabled={refreshing}
                onClick={() => void load(undefined, true)}
              >
                {t("actions.refresh")}
              </Button>
            ) : focusItem && focusAction ? (
              <Button variant="primary" data-testid="primary-setup-action" onClick={() => openAction(focusItem)}>
                {t(`items.${focusItem.id}.action`)}
                <ChevronRight size={14} aria-hidden="true" />
              </Button>
            ) : focusItem && !focusItem.action ? (
              <a
                className="settings-button settings-button--primary settings-button--md"
                data-testid="primary-setup-action"
                href={supportHref}
                target={supportIsExternal ? "_blank" : undefined}
                rel={supportIsExternal ? "noreferrer" : undefined}
              >
                {t("actions.get_help")}
                {supportIsExternal ? <ExternalLink size={14} aria-hidden="true" /> : null}
              </a>
            ) : (
              <Button variant="primary" data-testid="primary-setup-action" onClick={() => router.push("/")}>
                {t("actions.enter")}
              </Button>
            )}
            {!coreReady && !focusAction && laterCoreAction ? (
              <Button onClick={() => openAction(laterCoreAction)}>
                {t("next_action.continue_with", { item: t(`items.${laterCoreAction.id}.title`) })}
              </Button>
            ) : null}
            {!coreReady && (focusAction || allPendingCoreUnknown || !focusItem?.action) ? (
              <Button variant="plain" onClick={() => router.push("/")}>
                {t("actions.continue_later")}
              </Button>
            ) : null}
          </div>
        </section>

        {runtime && runtime.llm_refusals_total > 0 ? (
          <section data-testid="runtime-signal" aria-label={t("runtime.label")}>
            <Notice tone="error">
              <div data-testid="runtime-signal-degraded">
                <strong>
                  {t("runtime.degraded", {
                    count: runtime.llm_refusals_total,
                    since: runtime.observed_since ? formatTime(runtime.observed_since) : "—",
                  })}
                </strong>
                <p>{t("runtime.degraded_note")}</p>
                <ul>
                  {Object.entries(runtime.llm_refusals_by_reason).map(([reason, count]) => (
                    <li key={reason}>{t("runtime.reason_line", { reason, count })}</li>
                  ))}
                </ul>
              </div>
            </Notice>
          </section>
        ) : null}

        {attentionCore.length ? (
          <section className={styles.section} aria-labelledby="setup-pending-title">
            <SectionHeading title={t("sections.pending")} count={attentionCore.length} id="setup-pending-title" level={2} />
            <ul className={styles.items}>{attentionCore.map(renderItem)}</ul>
          </section>
        ) : null}

        {completedCore.length ? (
          <section className={styles.section}>
            <SectionHeading
              title={t("sections.completed")}
              count={completedCore.length}
              level={2}
              collapsible
              open={completedOpen}
              onToggle={() => setCompletedOpen((open) => !open)}
              controls="setup-completed"
            />
            <div id="setup-completed" hidden={!completedOpen}>
              <ul className={styles.items}>{completedCore.map(renderItem)}</ul>
            </div>
          </section>
        ) : null}

        <section className={styles.section}>
          <SectionHeading
            title={t("sections.later")}
            level={2}
            collapsible
            open={laterOpen}
            onToggle={() => setLaterOpen((open) => !open)}
            controls="setup-later"
          />
          <div id="setup-later" hidden={!laterOpen} className={styles.expanded}>
            <p className={styles.sectionDescription}>{t("sections.later_description")}</p>
            {(["conditional", "recommended"] as const).map((group) => {
              const items = snapshot.items.filter((item) => item.group === group);
              return items.length ? (
                <section key={group} className={styles.section} aria-label={t(`groups.${group}.title`)}>
                  <SectionHeading title={t(`groups.${group}.title`)} level={3} />
                  <p className={styles.sectionDescription}>{t(`groups.${group}.description`)}</p>
                  <ul className={styles.items}>{items.map(renderItem)}</ul>
                </section>
              ) : null;
            })}
            <section className={styles.section} aria-label={t("summary.capabilities")}>
              <SectionHeading title={t("summary.capabilities")} level={3} />
              <ul className={styles.capabilities}>
                {Object.entries(snapshot.context.optional_capabilities).map(([capability, state]) => (
                  <li key={capability} data-testid={`capability-${capability}`}>
                    <div className={styles.itemHeader}>
                      <span>{t(`capabilities.${capability}`)}</span>
                      <Badge tone={state === "enabled" ? "positive" : state === "invalid" ? "danger" : "neutral"}>
                        {t(`capability_states.${state}`)}
                      </Badge>
                    </div>
                    {state !== "enabled" ? (
                      <div className={styles.capabilityConsequence}>
                        <p>{t(`capability_consequences.${capability}`)}</p>
                        {canConfigureModels ? (
                          <PageActionLink href="/team/settings/models/" variant="secondary" data-testid={`capability-cta-${capability}`}>
                            {capability.startsWith("primary_") ? t("actions.switch_primary_model") : t("actions.configure_capability")}
                          </PageActionLink>
                        ) : (
                          <span>
                            {capabilityContacts.length
                              ? t("capability_needs_admin_named", { names: capabilityContacts.join("、") })
                              : t("capability_needs_admin")}
                          </span>
                        )}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            </section>
          </div>
        </section>

        <section className={styles.section}>
          <SectionHeading
            title={t("summary.environment")}
            level={2}
            collapsible
            open={environmentOpen}
            onToggle={() => setEnvironmentOpen((open) => !open)}
            controls="setup-environment"
          />
          <div id="setup-environment" hidden={!environmentOpen}>
            <dl className={styles.facts}>
              <div>
                <dt>{t("summary.role")}</dt>
                <dd>{t(`roles.${snapshot.context.current_role}`)}</dd>
              </div>
              <div>
                <dt>{t("summary.license")}</dt>
                <dd>{snapshot.context.license_health ? t(`license_states.${snapshot.context.license_health}`) : "—"}</dd>
              </div>
              {snapshot.context.license_expires_at ? (
                <div>
                  <dt>{t("summary.license_expires")}</dt>
                  <dd>
                    <time dateTime={snapshot.context.license_expires_at}>{formatTime(snapshot.context.license_expires_at)}</time>
                  </dd>
                </div>
              ) : null}
              {snapshot.context.grace_until ? (
                <div>
                  <dt>{t("summary.grace_until")}</dt>
                  <dd>
                    <time dateTime={snapshot.context.grace_until}>{formatTime(snapshot.context.grace_until)}</time>
                  </dd>
                </div>
              ) : null}
              <div>
                <dt>{t("summary.spaces")}</dt>
                <dd>{snapshot.context.active_spaces ?? "—"}</dd>
              </div>
              <div>
                <dt>{t("summary.seats")}</dt>
                <dd>
                  {snapshot.context.active_seats ?? "—"}
                  {snapshot.context.max_seats !== null ? ` / ${snapshot.context.max_seats}` : ""}
                </dd>
              </div>
              <div>
                <dt>{t("summary.public_entry")}</dt>
                <dd>{snapshot.context.current_public_entry ?? "—"}</dd>
              </div>
            </dl>
            {runtime && runtime.llm_refusals_total === 0 ? (
              <div data-testid="runtime-signal">
                <p data-testid="runtime-signal-ok">{t("runtime.ok", { since: runtime.observed_since ? formatTime(runtime.observed_since) : "—" })}</p>
              </div>
            ) : null}
          </div>
        </section>
        <footer className={styles.footer}>
          <p>
            <Info size={14} aria-hidden="true" />
            <span>
              {t("summary.verification_title")} · {t("summary.verification_body")}
            </span>
          </p>
          <a
            className="settings-button settings-button--plain settings-button--md"
            href={supportHref}
            target={supportIsExternal ? "_blank" : undefined}
            rel={supportIsExternal ? "noreferrer" : undefined}
          >
            {t("actions.get_help")}
            {supportIsExternal ? <ExternalLink size={14} aria-hidden="true" /> : null}
          </a>
          <span>
            {t("summary.observed")} <time dateTime={snapshot.generated_at}>{formatTime(snapshot.generated_at)}</time>
          </span>
        </footer>
      </div>
    </PageFrame>
  );
}
