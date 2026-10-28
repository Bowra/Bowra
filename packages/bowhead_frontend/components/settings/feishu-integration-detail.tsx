"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  ArrowLeft,
  Check,
  ExternalLink,
  RefreshCw,
  Spinner,
} from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { SaveBar } from "@/components/ui/save-bar";
import { Switch } from "@/components/ui/switch";
import {
  createFeishuSetupSession,
  fetchFeishuAdminConfig,
  fetchFeishuDiagnostics,
  fetchFeishuHealth,
  fetchFeishuSetupSession,
  updateFeishuAdminConfig,
  type FeishuAdminConfig,
  type FeishuAdminConfigUpdate,
  type FeishuDiagnostics,
  type FeishuPublicHealth,
  type FeishuSetupChecklistItem,
  type FeishuSetupSession,
} from "@/lib/feishu-integration-api";
import {
  feishuAutomaticChecklist,
  feishuAutomaticChecksPassed,
  feishuCredentialGaps,
  feishuRuntimeProblemKeys,
  feishuRuntimeState,
  type FeishuRuntimeProblemKey,
} from "@/lib/feishu-integration-state";
import { useAppRouter } from "@/lib/use-app-router";

import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./integration-pages.module.css";

// Append global ui-integ-* hooks to every class (styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-integ");

type StageId = "01" | "02" | "03" | "04";
type PendingNavigation =
  | { kind: "route"; href: string }
  | { kind: "stage"; stage: StageId }
  | { kind: "browser"; href: string };
type DetailTone = "good" | "warn" | "bad" | "muted";
type DetailStatusItem = {
  label: string;
  value: string;
  tone: DetailTone;
};
type SideTask = {
  stage: StageId;
  title: string;
  description: string;
};

type ConfigDraft = {
  enabled: boolean;
  eventIngress: "webhook" | "long_connection";
  appId: string;
  appSecret: string;
  verificationToken: string;
  encryptKey: string;
  botOpenId: string;
  groupPublicAnswersEnabled: boolean;
};

const STAGES: StageId[] = ["01", "02", "03", "04"];
const FEISHU_DEVELOPER_CONSOLE_URL = "https://open.feishu.cn/app";
const LARK_DEVELOPER_CONSOLE_URL = "https://open.larksuite.com/app";

export function FeishuIntegrationDetail() {
  const t = useTranslations("settings.integrations");
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const queryStage = validStage(searchParams.get("stage"));
  const setupId =
    searchParams.get("setup") ?? searchParams.get("feishuSetup") ?? "";
  const [activeStage, setActiveStage] = useState<StageId | null>(queryStage);
  const [config, setConfig] = useState<FeishuAdminConfig | null>(null);
  const [diagnostics, setDiagnostics] = useState<FeishuDiagnostics | null>(
    null,
  );
  const [health, setHealth] = useState<FeishuPublicHealth | null>(null);
  const [draft, setDraft] = useState<ConfigDraft | null>(null);
  const [baseline, setBaseline] = useState<ConfigDraft | null>(null);
  const [setup, setSetup] = useState<FeishuSetupSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkStatus, setCheckStatus] = useState<string | null>(null);
  const [creatingSetup, setCreatingSetup] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [copiedLabel, setCopiedLabel] = useState<string | null>(null);
  const [confirmGroupPublic, setConfirmGroupPublic] = useState(false);
  const [pendingNavigation, setPendingNavigation] =
    useState<PendingNavigation | null>(null);
  const currentUrlRef = useRef("");
  const copyResetRef = useRef<number | null>(null);
  const bypassPopRef = useRef(false);
  const allowUnloadRef = useRef(false);
  const deepLinkedStageRef = useRef<StageId | null>(null);
  const stageRefs = useRef<Record<StageId, HTMLElement | null>>({
    "01": null,
    "02": null,
    "03": null,
    "04": null,
  });

  const dirty = useMemo(
    () =>
      draft !== null &&
      baseline !== null &&
      JSON.stringify(draft) !== JSON.stringify(baseline),
    [baseline, draft],
  );

  const focusStage = useCallback(
    (stage: StageId, behavior?: ScrollBehavior) => {
      requestAnimationFrame(() => {
        const target = stageRefs.current[stage];
        if (!target) return;
        const reduceMotion =
          typeof window.matchMedia === "function" &&
          window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (typeof target.scrollIntoView === "function") {
          target.scrollIntoView({
            behavior: behavior ?? (reduceMotion ? "auto" : "smooth"),
            block: "start",
          });
        }
        target.focus({ preventScroll: true });
      });
    },
    [],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [nextConfig, nextDiagnostics, nextHealth] = await Promise.all([
        fetchFeishuAdminConfig(),
        fetchFeishuDiagnostics(),
        fetchFeishuHealth(),
      ]);
      const nextDraft = draftFromConfig(nextConfig);
      setConfig(nextConfig);
      setDiagnostics(nextDiagnostics);
      setHealth(nextHealth);
      setDraft(nextDraft);
      setBaseline(nextDraft);
      if (!queryStage) {
        const hasMissingCredentials =
          feishuCredentialGaps(nextDiagnostics).length > 0;
        setActiveStage(
          !nextDraft.enabled || hasMissingCredentials
            ? "01"
            : nextDiagnostics.degraded
              ? "03"
              : null,
        );
      }
      if (setupId) {
        setSetup(await fetchFeishuSetupSession(setupId));
      }
    } catch (error) {
      setLoadError(errorText(error, t("detail_load_failed")));
    } finally {
      setLoading(false);
    }
  }, [queryStage, setupId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setActiveStage(queryStage);
    if (!queryStage) {
      deepLinkedStageRef.current = null;
    }
  }, [queryStage]);

  useEffect(() => {
    if (
      loading ||
      !queryStage ||
      deepLinkedStageRef.current === queryStage
    ) {
      return;
    }
    deepLinkedStageRef.current = queryStage;
    focusStage(queryStage, "auto");
  }, [focusStage, loading, queryStage]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      currentUrlRef.current = window.location.href;
    }
  }, [activeStage]);

  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (allowUnloadRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const onPopState = () => {
      if (bypassPopRef.current) {
        bypassPopRef.current = false;
        return;
      }
      const targetHref = window.location.href;
      window.history.pushState(null, "", currentUrlRef.current);
      setPendingNavigation({ kind: "browser", href: targetHref });
    };
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener("popstate", onPopState);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("popstate", onPopState);
    };
  }, [dirty]);

  useEffect(
    () => () => {
      if (copyResetRef.current !== null) {
        window.clearTimeout(copyResetRef.current);
      }
    },
    [],
  );

  const setDraftValue = <K extends keyof ConfigDraft>(
    key: K,
    value: ConfigDraft[K],
  ) => {
    setSaved(false);
    setSaveError(null);
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };

  const refreshDiagnostics = async () => {
    setChecking(true);
    setLoadError(null);
    setCopyStatus(null);
    setCheckStatus(t("checking_now"));
    try {
      const [nextDiagnostics, nextHealth] = await Promise.all([
        fetchFeishuDiagnostics(),
        fetchFeishuHealth(),
      ]);
      setDiagnostics(nextDiagnostics);
      setHealth(nextHealth);
      setCheckStatus(t("recheck_complete"));
    } catch (error) {
      setLoadError(errorText(error, t("diagnostics_failed")));
      setCheckStatus(t("recheck_failed"));
    } finally {
      setChecking(false);
    }
  };

  const saveConfig = async (): Promise<boolean> => {
    if (!draft) return false;
    setSaving(true);
    setSaved(false);
    setSaveError(null);
    const payload: FeishuAdminConfigUpdate = {
      enabled: draft.enabled,
      event_ingress: draft.eventIngress,
      app_id: draft.appId.trim(),
      bot_open_id: draft.botOpenId.trim(),
      group_public_answers_enabled: draft.groupPublicAnswersEnabled,
    };
    if (draft.appSecret.trim()) payload.app_secret = draft.appSecret.trim();
    if (draft.verificationToken.trim()) {
      payload.verification_token = draft.verificationToken.trim();
    }
    if (draft.encryptKey.trim()) payload.encrypt_key = draft.encryptKey.trim();
    try {
      const nextConfig = await updateFeishuAdminConfig(payload);
      const nextDraft = draftFromConfig(nextConfig);
      setConfig(nextConfig);
      setDraft(nextDraft);
      setBaseline(nextDraft);
      setSaved(true);
      try {
        const [nextDiagnostics, nextHealth] = await Promise.all([
          fetchFeishuDiagnostics(),
          fetchFeishuHealth(),
        ]);
        setDiagnostics(nextDiagnostics);
        setHealth(nextHealth);
        setLoadError(null);
      } catch {
        setLoadError(t("saved_refresh_failed"));
      }
      return true;
    } catch (error) {
      setSaveError(errorText(error, t("config_save_failed")));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const applyNavigation = (navigation: PendingNavigation) => {
    setPendingNavigation(null);
    if (navigation.kind === "stage") {
      const href = `/team/settings/integrations/feishu/?stage=${navigation.stage}`;
      setActiveStage(navigation.stage);
      deepLinkedStageRef.current = navigation.stage;
      if (typeof window !== "undefined") {
        currentUrlRef.current = new URL(href, window.location.href).toString();
      }
      router.replace(href);
      focusStage(navigation.stage);
      return;
    }
    if (navigation.kind === "browser") {
      bypassPopRef.current = true;
      window.location.assign(navigation.href);
      return;
    }
    router.push(navigation.href);
  };

  const requestNavigation = (navigation: PendingNavigation) => {
    if (dirty) {
      setPendingNavigation(navigation);
      return;
    }
    applyNavigation(navigation);
  };

  const discardAndNavigate = () => {
    if (!pendingNavigation || !baseline) return;
    setDraft(baseline);
    allowNextUnload(allowUnloadRef);
    applyNavigation(pendingNavigation);
  };

  const saveAndNavigate = async () => {
    if (!pendingNavigation) return;
    const navigation = pendingNavigation;
    if (await saveConfig()) {
      allowNextUnload(allowUnloadRef);
      applyNavigation(navigation);
    }
  };

  const copy = async (value: string, label: string) => {
    setCheckStatus(null);
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus(t("copy_success", { label }));
      setCopiedLabel(label);
      if (copyResetRef.current !== null) {
        window.clearTimeout(copyResetRef.current);
      }
      copyResetRef.current = window.setTimeout(() => {
        setCopiedLabel(null);
        setCopyStatus(null);
        copyResetRef.current = null;
      }, 1600);
    } catch {
      selectCopySource(value);
      setCopyStatus(t("copy_failed_manual", { label }));
      setCopiedLabel(null);
    }
  };

  const createSetup = async () => {
    setCreatingSetup(true);
    setLoadError(null);
    try {
      setSetup(await createFeishuSetupSession());
    } catch (error) {
      setLoadError(errorText(error, t("setup_create_failed")));
    } finally {
      setCreatingSetup(false);
    }
  };

  if (loading) {
    // The loading skeleton appears only after 300ms.
    return (
      <LoadingSkeleton variant="rows" rows={4} label={t("detail_loading")} />
    );
  }

  if (!draft || !config || !diagnostics || !health) {
    return (
      <div className={styles.page}>
        <button
          className={styles.backButton}
          type="button"
          onClick={() =>
            router.push("/team/settings/integrations/")
          }
        >
          <ArrowLeft size={14} aria-hidden="true" />
          {t("detail_back")}
        </button>
        <div className={`${styles.statusBanner} ${styles.statusBannerWarn}`}>
          <div>
            <strong>{t("detail_load_failed")}</strong>
            <p>{loadError}</p>
            <Button disabled={checking} onClick={() => void refreshDiagnostics()}>{t("recheck")}</Button>
          </div>
        </div>
        <button
          className={styles.secondaryButton}
          type="button"
          onClick={() => void load()}
        >
          {t("retry")}
        </button>
      </div>
    );
  }

  const missingCredentials = feishuCredentialGaps(diagnostics);
  const runtimeProblems = collectRuntimeProblems(diagnostics, health, t);
  const autoPassed = feishuAutomaticChecksPassed(diagnostics, health);
  const runtimeStatus = detailRuntimeStatus(
    feishuRuntimeState(diagnostics, health),
    t,
  );
  const stageMeta: Record<
    StageId,
    { title: string; subtitle: string; status: string; tone: string }
  > = {
    "01": {
      title: t("stage01_title"),
      subtitle: t("stage01_subtitle"),
      status: !draft.enabled
        ? t("status_stopped")
        : missingCredentials.length === 0
          ? t("stage01_completed")
          : t("status_needs_config", {
              count: missingCredentials.length,
            }),
      tone: !draft.enabled
        ? styles.stageStatusMuted
        : missingCredentials.length === 0
          ? styles.stageStatusGood
          : styles.stageStatusWarn,
    },
    "02": {
      title: t("stage02_title"),
      subtitle: t("stage02_subtitle"),
      status: t("stage02_manual_review"),
      tone: styles.stageStatusWarn,
    },
    "03": {
      title: t("stage03_title"),
      subtitle: t("stage03_subtitle"),
      status: !draft.enabled
        ? t("status_not_running")
        : autoPassed
        ? t("stage03_auto_passed")
        : t("status_needs_attention"),
      tone: !draft.enabled
        ? styles.stageStatusMuted
        : autoPassed
          ? styles.stageStatusGood
          : styles.stageStatusBad,
    },
    "04": {
      title: t("stage04_title"),
      subtitle: t("stage04_subtitle"),
      status: t("stage04_manual_acceptance"),
      tone: styles.stageStatusWarn,
    },
  };
  const sideTasks = buildSideTasks({
    enabled: draft.enabled,
    missingCredentials,
    runtimeProblems,
    autoPassed,
    t,
  });
  const detailStatuses: DetailStatusItem[] = [
    {
      label: t("overview_switch"),
      value: draft.enabled ? t("status_enabled") : t("status_stopped"),
      tone: draft.enabled ? "good" : "muted",
    },
    {
      label: t("overview_readiness"),
      value: missingCredentials.length
        ? t("status_needs_config", { count: missingCredentials.length })
        : t("status_auto_passed"),
      tone: missingCredentials.length ? "warn" : "good",
    },
    {
      label: t("overview_runtime"),
      value: runtimeStatus.value,
      tone: runtimeStatus.tone,
    },
  ];
  const collapseStage = (stage: StageId) => {
    setActiveStage(null);
    const href = "/team/settings/integrations/feishu/";
    if (typeof window !== "undefined") {
      currentUrlRef.current = new URL(href, window.location.href).toString();
    }
    router.replace(href);
    stageRefs.current[stage]?.focus({ preventScroll: true });
  };
  const exitToOverview = () =>
    requestNavigation({
      kind: "route",
      href: "/team/settings/integrations/",
    });
  return (
    <div className={styles.page}>
      <header className={styles.detailHeader}>
        <div className={styles.detailIdentity}>
          <div className={styles.providerIcon} aria-hidden="true">
            <FeishuGlyph />
          </div>
          <div>
            <p className={styles.eyebrow}>{t("detail_eyebrow")}</p>
            <h1 className="ui-page-title">{t("detail_title")}</h1>
            <p className="ui-page-desc">{t("detail_subtitle")}</p>
          </div>
        </div>
        <button
          className={styles.backButton}
          type="button"
          onClick={exitToOverview}
        >
          <ArrowLeft size={14} aria-hidden="true" />
          {autoPassed ? t("detail_back") : t("detail_skip")}
        </button>
      </header>

      <StatusStrip
        statuses={detailStatuses}
        label={t("detail_status_summary")}
      />

      {loadError ? (
        <div
          className={`${styles.statusBanner} ${styles.statusBannerWarn}`}
          role="alert"
        >
          <div>
            <strong>{t("status_needs_attention")}</strong>
            <p>{loadError}</p>
            <Button disabled={checking} onClick={() => void refreshDiagnostics()}>{t("recheck")}</Button>
          </div>
        </div>
      ) : null}

      <div className={styles.detailLayout}>
        <main className={styles.mainColumn}>
          {STAGES.map((stage) => {
            const stageHasProblem =
              (stage === "01" &&
                (!draft.enabled || missingCredentials.length > 0)) ||
              (stage === "03" && draft.enabled && !autoPassed);
            const stageOpen = activeStage === stage || stageHasProblem;
            return (
              <details
                className={styles.stage}
                key={stage}
                open={stageOpen}
              >
                <summary
                  ref={(node) => {
                    stageRefs.current[stage] = node;
                  }}
                  className={styles.stageSummary}
                  onClick={(event) => {
                    event.preventDefault();
                    if (
                      stageOpen &&
                      activeStage === stage &&
                      !stageHasProblem
                    ) {
                      collapseStage(stage);
                      return;
                    }
                    requestNavigation({ kind: "stage", stage });
                  }}
                >
                  <span className={styles.stageSummaryNumber}>{stage}</span>
                  <span className={styles.stageSummaryCopy}>
                    <strong>{stageMeta[stage].title}</strong>
                    <small>{stageMeta[stage].subtitle}</small>
                  </span>
                  <span
                    className={`${styles.stageStatus} ${stageMeta[stage].tone}`}
                  >
                    {stageMeta[stage].status}
                  </span>
                </summary>
                {stageOpen ? (
                  <div className={styles.stageBody}>
                    {stage === "01" ? (
                      <CredentialsStage
                        draft={draft}
                        config={config}
                        dirty={dirty}
                        saving={saving}
                        saved={saved}
                        saveError={saveError}
                        onChange={setDraftValue}
                        onEnableGroupPublic={() => setConfirmGroupPublic(true)}
                        onNext={() =>
                          requestNavigation({ kind: "stage", stage: "02" })
                        }
                      />
                    ) : null}
                    {stage === "02" ? (
                      <ConsoleStage
                        diagnostics={diagnostics}
                        setup={setup}
                        creating={creatingSetup}
                        copyStatus={copyStatus}
                        copiedLabel={copiedLabel}
                        onCopy={(value, label) => void copy(value, label)}
                        onCreate={() => void createSetup()}
                        onExit={exitToOverview}
                        onNext={() =>
                          requestNavigation({ kind: "stage", stage: "03" })
                        }
                      />
                    ) : null}
                    {stage === "03" ? (
                      <DiagnosticsStage
                        diagnostics={diagnostics}
                        problems={runtimeProblems}
                        checking={checking}
                        checkStatus={checkStatus}
                        copyStatus={copyStatus}
                        copiedLabel={copiedLabel}
                        onCheck={() => void refreshDiagnostics()}
                        onCopy={(value, label) => void copy(value, label)}
                        onExit={exitToOverview}
                        onNext={() =>
                          requestNavigation({ kind: "stage", stage: "04" })
                        }
                      />
                    ) : null}
                    {stage === "04" ? (
                      <AcceptanceStage
                        diagnostics={diagnostics}
                        copyStatus={copyStatus}
                        copiedLabel={copiedLabel}
                        onCopy={(value, label) => void copy(value, label)}
                        onExit={exitToOverview}
                      />
                    ) : null}
                  </div>
                ) : null}
              </details>
            );
          })}
        </main>

        <aside className={styles.sideColumn}>
          <section
            className={`${styles.sideCard} ${styles.nextStepsCard}`}
            aria-labelledby="feishu-next-steps"
          >
            <h2 id="feishu-next-steps">
              {autoPassed
                ? t("manual_tasks_title")
                : t("next_steps_title")}
            </h2>
            <ol className={styles.nextStepList}>
              {sideTasks.map((task) => (
                <li key={`${task.stage}-${task.title}`}>
                  <button
                    type="button"
                    onClick={() =>
                      requestNavigation({ kind: "stage", stage: task.stage })
                    }
                  >
                    <span>{task.stage}</span>
                    <span>
                      <strong>{task.title}</strong>
                      <small>{task.description}</small>
                    </span>
                  </button>
                </li>
              ))}
            </ol>
          </section>
          <nav className={styles.stageNav} aria-label={t("stage_navigation")}>
            <h2>{t("stage_navigation")}</h2>
            <ol>
              {STAGES.map((stage) => (
                <li key={stage}>
                  <button
                    type="button"
                    aria-current={activeStage === stage ? "step" : undefined}
                    onClick={() =>
                      requestNavigation({ kind: "stage", stage })
                    }
                  >
                    <span className={styles.stageNumber}>{stage}</span>
                    <span className={styles.stageNavLabel}>
                      {stageMeta[stage].title}
                      <small>{stageMeta[stage].status}</small>
                    </span>
                  </button>
                </li>
              ))}
            </ol>
          </nav>
          <section className={styles.sideCard}>
            <h2>{t("boundary_title")}</h2>
            <ul>
              <li>{t("boundary_no_auto_app")}</li>
              <li>{t("boundary_no_auto_manual")}</li>
              <li>{t("boundary_no_admin_binding")}</li>
              <li>{t("boundary_no_metrics")}</li>
            </ul>
          </section>
        </aside>
      </div>

      {confirmGroupPublic ? (
        <Dialog
          title={t("group_public_confirm_title")}
          onClose={() => setConfirmGroupPublic(false)}
          footer={
            <>
              <Button onClick={() => setConfirmGroupPublic(false)}>
                {t("cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  setDraftValue("groupPublicAnswersEnabled", true);
                  setConfirmGroupPublic(false);
                }}
              >
                {t("group_public_confirm")}
              </Button>
            </>
          }
        >
          <p className={styles.dialogCopy}>
            {t("group_public_confirm_description")}
          </p>
        </Dialog>
      ) : null}

      {pendingNavigation ? (
        <Dialog
          title={t("unsaved_title")}
          onClose={() => setPendingNavigation(null)}
          busy={saving}
          footer={
            <>
              <Button
                onClick={() => setPendingNavigation(null)}
                disabled={saving}
              >
                {t("unsaved_stay")}
              </Button>
              <Button
                variant="danger"
                onClick={discardAndNavigate}
                disabled={saving}
              >
                {t("unsaved_discard")}
              </Button>
              <Button
                variant="primary"
                onClick={() => void saveAndNavigate()}
                disabled={saving}
              >
                {saving ? t("saving_config") : t("unsaved_save_leave")}
              </Button>
            </>
          }
        >
          <p className={styles.dialogCopy}>
            {pendingNavigation.kind === "stage"
              ? t("unsaved_description_stage", {
                  stage: pendingNavigation.stage,
                })
              : pendingNavigation.kind === "browser"
                ? t("unsaved_description_browser")
                : t("unsaved_description_back")}
          </p>
          {saveError ? <p className={styles.errorText}>{saveError}</p> : null}
        </Dialog>
      ) : null}
      {/* Dirty changes show the Save bar (tradeoff 17); leave confirmation retains this page's own guard. */}
      <SaveBar
        dirty={dirty}
        saving={saving}
        guard={false}
        onSave={() => void saveConfig()}
        onDiscard={() => {
          if (!baseline) return;
          setSaved(false);
          setSaveError(null);
          setDraft(baseline);
        }}
      />
    </div>
  );
}

function CredentialsStage({
  draft,
  config,
  dirty,
  saving,
  saved,
  saveError,
  onChange,
  onEnableGroupPublic,
  onNext,
}: {
  draft: ConfigDraft;
  config: FeishuAdminConfig;
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  saveError: string | null;
  onChange: <K extends keyof ConfigDraft>(
    key: K,
    value: ConfigDraft[K],
  ) => void;
  onEnableGroupPublic: () => void;
  /** The page's bottom Save bar saves; the phase action bar keeps only feedback and Next Phase. */
  onNext: () => void;
}) {
  const t = useTranslations("settings.integrations");
  // Main-toggle/ingress rules: table and narrow list rows share the same data.
  const linkageRows = [
    { key: "enabled-webhook", status: t("status_enabled"), ingress: t("ingress_webhook"), result: t("linkage_enabled_webhook"), tone: "good" },
    { key: "enabled-long", status: t("status_enabled"), ingress: t("ingress_long_connection"), result: t("linkage_enabled_long"), tone: "good" },
    { key: "stopped-webhook", status: t("status_stopped"), ingress: t("ingress_webhook"), result: t("linkage_stopped_webhook"), tone: "muted" },
    { key: "stopped-long", status: t("status_stopped"), ingress: t("ingress_long_connection"), result: t("linkage_stopped_long"), tone: "muted" },
    { key: "excluded", status: t("linkage_excluded"), ingress: t("status_disabled_v3"), result: t("linkage_disabled_excluded"), tone: "warn" },
  ] as const;
  return (
    <>
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("master_switch_title")}</h3>
            <p>{t("master_switch_description")}</p>
          </div>
        </div>
        <SwitchRow
          label={t("master_switch_label")}
          description={t("master_switch_hint")}
          checked={draft.enabled}
          onChange={(checked) => onChange("enabled", checked)}
        />
        {!draft.enabled ? (
          <div className={styles.inheritedNote}>
            {t("disabled_preserves_config")}
          </div>
        ) : null}
      </section>

      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("credentials_title")}</h3>
            <p>
              {t("config_source", { source: config.source })}
              {" · "}
              {t("credentials_secret_hint")}
            </p>
          </div>
        </div>
        <div className={styles.fieldGrid}>
          <Field label="APP_ID" htmlFor="feishu-app-id">
            <input
              className={styles.input}
              id="feishu-app-id"
              name="feishu-app-id"
              autoComplete="off"
              spellCheck={false}
              value={draft.appId}
              onChange={(event) => onChange("appId", event.target.value)}
            />
          </Field>
          <Field
            label="APP_SECRET"
            htmlFor="feishu-app-secret"
            hint={
              config.app_secret_configured
                ? t("secret_leave_blank")
                : t("secret_required")
            }
          >
            <input
              className={styles.input}
              id="feishu-app-secret"
              name="feishu-app-secret"
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              value={draft.appSecret}
              placeholder={
                config.app_secret_configured ? "••••••••" : undefined
              }
              onChange={(event) => onChange("appSecret", event.target.value)}
            />
          </Field>
          <Field label="BOT_OPEN_ID" htmlFor="feishu-bot-open-id">
            <input
              className={styles.input}
              id="feishu-bot-open-id"
              name="feishu-bot-open-id"
              autoComplete="off"
              spellCheck={false}
              value={draft.botOpenId}
              onChange={(event) => onChange("botOpenId", event.target.value)}
            />
          </Field>
          {draft.eventIngress === "webhook" ? (
            <>
              <Field
                label="VERIFICATION_TOKEN"
                htmlFor="feishu-verification-token"
                hint={
                  config.verification_token_configured
                    ? t("secret_leave_blank")
                    : t("secret_optional_ingress")
                }
              >
                <input
                  className={styles.input}
                  id="feishu-verification-token"
                  name="feishu-verification-token"
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  value={draft.verificationToken}
                  placeholder={
                    config.verification_token_configured
                      ? "••••••••"
                      : undefined
                  }
                  onChange={(event) =>
                    onChange("verificationToken", event.target.value)
                  }
                />
              </Field>
              <Field
                label="ENCRYPT_KEY"
                htmlFor="feishu-encrypt-key"
                hint={
                  config.encrypt_key_configured
                    ? t("secret_leave_blank")
                    : t("encrypt_key_requirement")
                }
              >
                <input
                  className={styles.input}
                  id="feishu-encrypt-key"
                  name="feishu-encrypt-key"
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  value={draft.encryptKey}
                  placeholder={
                    config.encrypt_key_configured ? "••••••••" : undefined
                  }
                  onChange={(event) =>
                    onChange("encryptKey", event.target.value)
                  }
                />
              </Field>
            </>
          ) : null}
        </div>
      </section>

      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("ingress_title")}</h3>
            <p>{t("ingress_description")}</p>
          </div>
        </div>
        <RadioGroup
          className={styles.choiceGrid}
          orientation="both"
          aria-label={t("ingress_title")}
          value={draft.eventIngress}
          onValueChange={(value) => onChange("eventIngress", value as ConfigDraft["eventIngress"])}
        >
          <Radio
            value="webhook"
            className={styles.choiceCard}
            aria-label={t("ingress_webhook")}
          >
            <strong>{t("ingress_webhook")}</strong>
            <span>{t("ingress_webhook_hint")}</span>
          </Radio>
          <Radio
            value="long_connection"
            className={styles.choiceCard}
            aria-label={t("ingress_long_connection")}
          >
            <strong>{t("ingress_long_connection")}</strong>
            <span>{t("ingress_long_connection_hint")}</span>
          </Radio>
        </RadioGroup>
        <div className={styles.linkageTableFrame}>
          <table className={styles.linkageTable}>
            <caption>{t("linkage_rules_caption")}</caption>
            <thead>
              <tr>
                <th scope="col">{t("overview_switch")}</th>
                <th scope="col">{t("ingress_title")}</th>
                <th scope="col">{t("linkage_result")}</th>
              </tr>
            </thead>
            <tbody>
              {linkageRows.map((row) => (
                <tr key={row.key}>
                  <td>{row.status}</td>
                  <td>{row.ingress}</td>
                  <td>{row.result}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* Narrow screens (< 640): list rows over the same data as the table (decision 12). */}
          <p className="ui-integ-linkage-rows-caption" id="feishu-linkage-rows-caption">{t("linkage_rules_caption")}</p>
          <ul className="ui-integ-linkage-rows" aria-labelledby="feishu-linkage-rows-caption">
            {linkageRows.map((row) => (
              <li key={row.key} className="ui-integ-linkage-row">
                <span className="ui-integ-linkage-row__main">
                  <span className="ui-integ-linkage-row__title">{row.result}</span>
                  <span className="ui-integ-linkage-row__meta">
                    {t("ingress_title")} · {row.ingress}
                  </span>
                </span>
                <span className={`ui-integ-linkage-row__trail is-${row.tone}`}>{row.status}</span>
              </li>
            ))}
          </ul>
          <p>{t("linkage_legacy_note")}</p>
        </div>
      </section>

      <section className={styles.subsection}>
        <SwitchRow
          label={t("group_public_title")}
          description={t("group_public_description")}
          checked={draft.groupPublicAnswersEnabled}
          onChange={(checked) => {
            if (checked) onEnableGroupPublic();
            else onChange("groupPublicAnswersEnabled", false);
          }}
        />
      </section>

      <div
        className={`${styles.actionBar} ${
          saved ? styles.actionBarSuccess : ""
        }`}
      >
        <div className={styles.actionFeedback} aria-live="polite">
          {saving ? (
            t("saving_config_live")
          ) : saveError ? (
            <span className={styles.errorText}>
              {t("config_save_failed_not_written", { error: saveError })}
            </span>
          ) : saved ? (
            <strong>{t("config_saved_v3")}</strong>
          ) : dirty ? (
            t("config_unsaved_warning")
          ) : (
            t("config_save_hint")
          )}
        </div>
        <div className={styles.actionButtons}>
          <button
            className={styles.stageAction}
            type="button"
            onClick={onNext}
          >
            {t("next_stage")}
          </button>
        </div>
      </div>
    </>
  );
}

function ConsoleStage({
  diagnostics,
  setup,
  creating,
  copyStatus,
  copiedLabel,
  onCopy,
  onCreate,
  onExit,
  onNext,
}: {
  diagnostics: FeishuDiagnostics;
  setup: FeishuSetupSession | null;
  creating: boolean;
  copyStatus: string | null;
  copiedLabel: string | null;
  onCopy: (value: string, label: string) => void;
  onCreate: () => void;
  onExit: () => void;
  onNext: () => void;
}) {
  const t = useTranslations("settings.integrations");
  return (
    <>
      <StageQuickExit stage="02" onExit={onExit} />
      <div className={styles.manualNote}>
        <strong>{t("manual_console_notice_title")}</strong>
        <br />
        {t("manual_console_notice_description")}
      </div>
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("developer_console_title")}</h3>
            <p>{t("developer_console_hint")}</p>
          </div>
        </div>
        <div className={styles.consoleLinks}>
          <a
            className={styles.secondaryButton}
            href={FEISHU_DEVELOPER_CONSOLE_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t("open_feishu_console")}
            <ExternalLink size={13} aria-hidden="true" />
          </a>
          <a
            className={styles.secondaryButton}
            href={LARK_DEVELOPER_CONSOLE_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t("open_lark_console")}
            <ExternalLink size={13} aria-hidden="true" />
          </a>
        </div>
      </section>
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("callback_urls")}</h3>
            <p>{t("callback_urls_hint")}</p>
          </div>
        </div>
        <div className={styles.urlList}>
          <UrlRow
            label={t("event_url")}
            value={diagnostics.event_subscription_url}
            copied={copiedLabel === t("event_url")}
            onCopy={onCopy}
          />
          <UrlRow
            label={t("card_url")}
            value={diagnostics.card_callback_url}
            copied={copiedLabel === t("card_url")}
            onCopy={onCopy}
          />
        </div>
      </section>
      <CollapsibleCodeSection
        title={t("required_events_title")}
        hint={t("required_events_hint")}
        values={diagnostics.required_event_subscriptions}
      />
      <CollapsibleCodeSection
        title={t("required_permissions_title")}
        hint={t("required_permissions_hint")}
        values={diagnostics.required_permissions}
      />
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("setup_session_title_v3")}</h3>
            <p>{t("setup_session_hint_v3")}</p>
          </div>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={creating}
            onClick={onCreate}
          >
            {creating ? (
              <Spinner className={styles.spinner} aria-hidden="true" />
            ) : null}
            {creating ? t("creating_setup") : t("create_setup")}
          </button>
        </div>
        {setup ? (
          <div className={styles.setupResult}>
            <img
              className={styles.setupQr}
              src={setup.qr_svg_url}
              alt={t("setup_qr")}
            />
            <div className={styles.setupDetails}>
              <strong>{t("setup_link_ready")}</strong>
              <code data-copy-source="true">{setup.setup_url}</code>
              <p>
                {t("setup_expires")}: {formatDateTime(setup.expires_at)}
              </p>
              <div className={styles.actionButtons}>
                <button
                  className={styles.copyButton}
                  type="button"
                  onClick={() => onCopy(setup.setup_url, t("setup_url"))}
                >
                  {copiedLabel === t("setup_url")
                    ? t("copied")
                    : t("copy_setup_url")}
                </button>
                <a
                  className={styles.secondaryButton}
                  href={setup.setup_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t("open_setup_link")}
                  <ExternalLink size={13} aria-hidden="true" />
                </a>
              </div>
            </div>
          </div>
        ) : null}
      </section>
      <div className={styles.actionBar}>
        <div className={styles.actionFeedback} aria-live="polite">
          {copyStatus ?? t("manual_status_persists")}
        </div>
        <div className={styles.actionButtons}>
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={onExit}
          >
            {t("detail_skip")}
          </button>
          <button
            className={styles.stageAction}
            type="button"
            onClick={onNext}
          >
            {t("next_stage")}
          </button>
        </div>
      </div>
    </>
  );
}

function DiagnosticsStage({
  diagnostics,
  problems,
  checking,
  checkStatus,
  copyStatus,
  copiedLabel,
  onCheck,
  onCopy,
  onExit,
  onNext,
}: {
  diagnostics: FeishuDiagnostics;
  problems: RuntimeProblem[];
  checking: boolean;
  checkStatus: string | null;
  copyStatus: string | null;
  copiedLabel: string | null;
  onCheck: () => void;
  onCopy: (value: string, label: string) => void;
  onExit: () => void;
  onNext: () => void;
}) {
  const t = useTranslations("settings.integrations");
  const automaticItems = feishuAutomaticChecklist(diagnostics);
  const readyItems = automaticItems.filter((item) => item.status === "ok");
  const attentionItems = automaticItems.filter((item) => item.status !== "ok");
  const stopped =
    !diagnostics.enabled || diagnostics.event_ingress === "disabled";
  const passed =
    !stopped &&
    diagnostics.ok &&
    problems.length === 0 &&
    feishuCredentialGaps(diagnostics).length === 0;
  return (
    <>
      <StageQuickExit stage="03" onExit={onExit} />
      <div
        className={`${styles.statusBanner} ${
          passed ? "" : styles.statusBannerWarn
        }`}
      >
        <div>
          <strong>
            {stopped
              ? t("auto_stopped_title")
              : passed
                ? t("auto_pass_title")
                : t("auto_attention_title")}
          </strong>
          <p>
            {stopped
              ? t("auto_stopped_description")
              : passed
                ? t("auto_pass_description")
                : t("auto_attention_description")}
          </p>
        </div>
      </div>
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("automatic_checks_title")}</h3>
            <p>{t("automatic_checks_hint")}</p>
          </div>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={checking}
            onClick={onCheck}
          >
            {checking ? (
              <Spinner className={styles.spinner} aria-hidden="true" />
            ) : (
              <RefreshCw size={13} aria-hidden="true" />
            )}
            {checking ? t("checking_now") : t("recheck")}
          </button>
        </div>
        {attentionItems.length ? (
          <div className={styles.checkList}>
            {attentionItems.map((item) => (
              <ChecklistRow key={item.key} item={item} />
            ))}
          </div>
        ) : null}
        {readyItems.length ? (
          <CollapsibleChecklist
            title={t("ready_checks_title", { count: readyItems.length })}
            items={readyItems}
          />
        ) : null}
      </section>
      {problems.length ? (
        <section className={styles.subsection}>
          <div className={styles.subsectionHeading}>
            <div>
              <h3>{t("runtime_problems_title")}</h3>
              <p>{t("runtime_problems_hint")}</p>
            </div>
          </div>
          <div className={styles.problemList}>
            {problems.map((problem) => (
              <div className={styles.problemRow} key={problem.key}>
                <span
                  className={`${styles.checkIcon} ${styles.checkIconBad}`}
                  aria-hidden="true"
                >
                  !
                </span>
                <div>
                  <strong>{problem.title}</strong>
                  <p>{problem.description}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}
      <section className={styles.subsection}>
        <div className={styles.manualNote}>
          <strong>{t("auto_not_approval_title")}</strong>
          <br />
          {t("auto_not_approval_description")}
        </div>
      </section>
      <div className={styles.actionBar}>
        <div className={styles.actionFeedback} aria-live="polite">
          {checkStatus ?? copyStatus}
        </div>
        <div className={styles.actionButtons}>
          {diagnostics.launch_record_template ? (
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() =>
                onCopy(
                  diagnostics.launch_record_template ?? "",
                  t("launch_record"),
                )
              }
            >
              {copiedLabel === t("launch_record")
                ? t("copied")
                : t("copy_launch_record")}
            </button>
          ) : null}
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={onExit}
          >
            {t("detail_skip")}
          </button>
          <button
            className={styles.stageAction}
            type="button"
            onClick={onNext}
          >
            {t("next_stage")}
          </button>
        </div>
      </div>
    </>
  );
}

function StageQuickExit({
  stage,
  onExit,
}: {
  stage: "02" | "03";
  onExit: () => void;
}) {
  const t = useTranslations("settings.integrations");
  return (
    <div className={styles.stageQuickExit}>
      <button
        className={styles.secondaryButton}
        data-testid={`stage-${stage}-quick-exit`}
        type="button"
        onClick={onExit}
      >
        {t("detail_skip")}
      </button>
    </div>
  );
}

function AcceptanceStage({
  diagnostics,
  copyStatus,
  copiedLabel,
  onCopy,
  onExit,
}: {
  diagnostics: FeishuDiagnostics;
  copyStatus: string | null;
  copiedLabel: string | null;
  onCopy: (value: string, label: string) => void;
  onExit: () => void;
}) {
  const t = useTranslations("settings.integrations");
  const smokeItems = [
    "launch_gate_smoke_direct",
    "launch_gate_smoke_card",
    "launch_gate_smoke_workspace",
    "launch_gate_smoke_deny",
    "launch_gate_smoke_group_public",
    "launch_gate_smoke_group_disabled",
  ] as const;
  const bindingSteps = [
    "binding_step_message",
    "binding_step_session",
    "binding_step_card",
    "binding_step_login",
  ] as const;
  return (
    <>
      <div className={styles.manualNote}>
        <strong>{t("acceptance_manual_title")}</strong>
        <br />
        {t("acceptance_manual_description")}
      </div>
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("acceptance_checklist_title")}</h3>
            <p>{t("acceptance_checklist_hint")}</p>
          </div>
        </div>
        <div className={styles.checkList}>
          {smokeItems.map((key) => (
            <div className={styles.checkRow} key={key}>
              <span
                className={`${styles.checkIcon} ${styles.checkIconWarn}`}
                aria-hidden="true"
              >
                ·
              </span>
              <div className={styles.checkCopy}>
                <strong>{t(key)}</strong>
                <p>{t("manual_result_not_stored")}</p>
              </div>
            </div>
          ))}
        </div>
      </section>
      <section className={styles.subsection}>
        <div className={styles.subsectionHeading}>
          <div>
            <h3>{t("binding_explanation_title")}</h3>
            <p>{t("binding_explanation_hint")}</p>
          </div>
        </div>
        <ol className={styles.bindingSteps}>
          {bindingSteps.map((key, index) => (
            <li key={key}>
              <span>{index + 1}</span>
              <p>{t(key)}</p>
            </li>
          ))}
        </ol>
        <div className={styles.bindingNote}>{t("binding_admin_boundary")}</div>
      </section>
      {diagnostics.launch_record_template ? (
        <section className={styles.subsection}>
          <div className={styles.subsectionHeading}>
            <div>
              <h3>{t("launch_gate_title")}</h3>
              <p>{t("launch_gate_description")}</p>
            </div>
          </div>
          <pre className={styles.launchRecord} data-copy-source="true">
            {diagnostics.launch_record_template}
          </pre>
        </section>
      ) : null}
      <div className={styles.actionBar}>
        <div className={styles.actionFeedback} aria-live="polite">
          {copyStatus ?? t("acceptance_remains_manual")}
        </div>
        <div className={styles.actionButtons}>
          {diagnostics.launch_record_template ? (
            <button
              className={styles.primaryButton}
              type="button"
              onClick={() =>
                onCopy(
                  diagnostics.launch_record_template ?? "",
                  t("launch_record"),
                )
              }
            >
              {copiedLabel === t("launch_record")
                ? t("copied")
                : t("copy_launch_record")}
            </button>
          ) : null}
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={onExit}
          >
            {t("finish_later")}
          </button>
        </div>
      </div>
    </>
  );
}

function SwitchRow({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className={styles.switchRow}>
      <div>
        <strong>{label}</strong>
        <p>{description}</p>
      </div>
      <Switch className={styles.switch} aria-label={label} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className={styles.field}>
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint ? <small>{hint}</small> : null}
    </div>
  );
}

function UrlRow({
  label,
  value,
  copied,
  onCopy,
}: {
  label: string;
  value: string;
  copied: boolean;
  onCopy: (value: string, label: string) => void;
}) {
  const t = useTranslations("settings.integrations");
  return (
    <div className={styles.urlRow}>
      <span>{label}</span>
      <code data-copy-source="true">{value}</code>
      <button
        className={styles.copyButton}
        type="button"
        aria-label={
          copied
            ? t("copy_success", { label })
            : t("copy_value", { label })
        }
        onClick={() => onCopy(value, label)}
      >
        {copied ? t("copied") : t("copy")}
      </button>
    </div>
  );
}

function SimpleCodeList({ values }: { values: string[] }) {
  return (
    <div className={styles.checkList}>
      {values.map((value) => (
        <div className={styles.checkRow} key={value}>
          <span className={styles.checkIcon} aria-hidden="true">
            <Check />
          </span>
          <code>{value}</code>
        </div>
      ))}
    </div>
  );
}

function CollapsibleCodeSection({
  title,
  hint,
  values,
}: {
  title: string;
  hint: string;
  values: string[];
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className={styles.subsection}>
      <button
        className={styles.collapsibleHeading}
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>
          <strong>{title}</strong>
          <small>{hint}</small>
        </span>
        <span aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      {open ? <SimpleCodeList values={values} /> : null}
    </section>
  );
}

function CollapsibleChecklist({
  title,
  items,
}: {
  title: string;
  items: FeishuSetupChecklistItem[];
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={styles.collapsibleGroup}>
      <button
        className={styles.collapsibleHeading}
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>
          <strong>{title}</strong>
        </span>
        <span aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      {open ? (
        <div className={styles.checkList}>
          {items.map((item) => (
            <ChecklistRow key={item.key} item={item} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ChecklistRow({ item }: { item: FeishuSetupChecklistItem }) {
  const t = useTranslations("settings.integrations");
  const tone =
    item.status === "ok"
      ? ""
      : item.status === "manual"
        ? styles.checkIconWarn
        : styles.checkIconBad;
  return (
    <div className={styles.checkRow}>
      <span className={`${styles.checkIcon} ${tone}`} aria-hidden="true">
        {item.status === "ok" ? <Check /> : item.status === "manual" ? "·" : "!"}
      </span>
      <div className={styles.checkCopy}>
        <strong>{item.label}</strong>
        <p>
          {item.detail} · {t(`check_${item.status}`)}
        </p>
      </div>
    </div>
  );
}

type RuntimeProblem = {
  key: FeishuRuntimeProblemKey;
  title: string;
  description: string;
};

function collectRuntimeProblems(
  diagnostics: FeishuDiagnostics,
  health: FeishuPublicHealth,
  t: ReturnType<typeof useTranslations<"settings.integrations">>,
): RuntimeProblem[] {
  return feishuRuntimeProblemKeys(diagnostics, health).map((key) => ({
    key,
    title: t(`problem_${key}_title`),
    description: t(`problem_${key}_description`),
  }));
}

function draftFromConfig(config: FeishuAdminConfig): ConfigDraft {
  return {
    enabled: config.event_ingress === "disabled" ? false : config.enabled,
    eventIngress:
      config.event_ingress === "long_connection"
        ? "long_connection"
        : "webhook",
    appId: config.app_id,
    appSecret: "",
    verificationToken: "",
    encryptKey: "",
    botOpenId: config.bot_open_id,
    groupPublicAnswersEnabled: config.group_public_answers_enabled,
  };
}

function validStage(value: string | null): StageId | null {
  return STAGES.includes(value as StageId) ? (value as StageId) : null;
}

function errorText(_error: unknown, fallback: string): string {
  return fallback;
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function allowNextUnload(ref: { current: boolean }): void {
  ref.current = true;
  window.setTimeout(() => {
    ref.current = false;
  }, 1000);
}

function StatusStrip({
  statuses,
  label,
}: {
  statuses: DetailStatusItem[];
  label: string;
}) {
  return (
    <div
      className={`${styles.statusGrid} ${styles.detailStatusGrid}`}
      role="list"
      aria-label={label}
    >
      {statuses.map((status) => (
        <div className={styles.statusCell} role="listitem" key={status.label}>
          <span>{status.label}</span>
          <strong className={styles[status.tone]}>
            <i className={styles.statusDot} aria-hidden="true" />
            {status.value}
          </strong>
        </div>
      ))}
    </div>
  );
}

function detailRuntimeStatus(
  state: ReturnType<typeof feishuRuntimeState>,
  t: ReturnType<typeof useTranslations<"settings.integrations">>,
): { value: string; tone: DetailTone } {
  if (state === "stopped") {
    return { value: t("status_not_running"), tone: "muted" };
  }
  if (state === "normal") {
    return { value: t("status_runtime_normal"), tone: "good" };
  }
  if (state === "restart_required") {
    return { value: t("status_restart_required"), tone: "warn" };
  }
  const reasonKey = {
    long_failed: "runtime_reason_long_failed",
    long_not_running: "runtime_reason_long_not_running",
    event_store: "runtime_reason_event_store",
    provider_secret: "runtime_reason_provider_secret",
    dev_auth: "runtime_reason_dev_auth",
    unknown_runtime: "runtime_reason_unknown",
  }[state];
  return {
    value: t("status_runtime_error_reason", { reason: t(reasonKey) }),
    tone: "bad",
  };
}

function buildSideTasks({
  enabled,
  missingCredentials,
  runtimeProblems,
  autoPassed,
  t,
}: {
  enabled: boolean;
  missingCredentials: FeishuRuntimeProblemKey[];
  runtimeProblems: RuntimeProblem[];
  autoPassed: boolean;
  t: ReturnType<typeof useTranslations<"settings.integrations">>;
}): SideTask[] {
  if (autoPassed) {
    return [
      {
        stage: "02",
        title: t("manual_task_console_title"),
        description: t("manual_task_console_description"),
      },
      {
        stage: "04",
        title: t("manual_task_acceptance_title"),
        description: t("manual_task_acceptance_description"),
      },
    ];
  }

  const tasks: SideTask[] = [];
  if (!enabled) {
    tasks.push({
      stage: "01",
      title: t("next_enable_title"),
      description: t("next_enable_description"),
    });
  }
  if (missingCredentials.length > 0) {
    tasks.push({
      stage: "01",
      title: t("next_configure_title", {
        count: missingCredentials.length,
      }),
      description: t("next_configure_description"),
    });
  }
  if (enabled) {
    const credentialSet = new Set(missingCredentials);
    for (const problem of runtimeProblems) {
      if (credentialSet.has(problem.key)) continue;
      tasks.push({
        stage: "03",
        title: problem.title,
        description: problem.description,
      });
      if (tasks.length >= 3) break;
    }
  }
  if (tasks.length === 0) {
    tasks.push({
      stage: "03",
      title: t("next_recheck_title"),
      description: t("next_recheck_description"),
    });
  }
  return tasks;
}

function selectCopySource(value: string): void {
  const nodes = Array.from(
    document.querySelectorAll<HTMLElement>("[data-copy-source='true']"),
  );
  const source = nodes.find((node) => node.textContent === value);
  if (source) {
    const range = document.createRange();
    range.selectNodeContents(source);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    source.focus({ preventScroll: true });
    return;
  }

  document.getElementById("integration-manual-copy")?.remove();
  const textarea = document.createElement("textarea");
  textarea.id = "integration-manual-copy";
  textarea.value = value;
  textarea.setAttribute("aria-label", "Manual copy");
  Object.assign(textarea.style, {
    position: "fixed",
    left: "16px",
    bottom: "16px",
    zIndex: "9999",
    width: "min(560px, calc(100vw - 32px))",
    height: "120px",
  });
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  window.setTimeout(() => {
    if (document.activeElement !== textarea) textarea.remove();
  }, 30_000);
}

function FeishuGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none">
      <path
        d="M6.5 4.5h7.3a4.2 4.2 0 0 1 0 8.4H10l-3.5 3.4v-3.9a4 4 0 0 1 0-7.9Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <path
        d="M9 9h7.2a3.3 3.3 0 0 1 0 6.6H15l-3 2.6v-2.6"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}
