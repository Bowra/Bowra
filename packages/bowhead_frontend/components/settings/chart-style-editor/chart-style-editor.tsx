"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ApiError } from "@/lib/api-client";
import {
  chartThemeBaseForMode,
  resolveChartThemeForV2Snapshot,
  type ChartTheme,
} from "@/lib/chart-theme";
import {
  createTeamChartStyleDraft,
  deleteDraftOnlyTeamChartStyle,
  discardTeamChartStyleDraft,
  getTeamChartStyle,
  getTeamChartStylePolicyV2,
  listTeamChartStyleRevisions,
  previewTeamChartStyle,
  publishTeamChartStyle,
  restoreTeamChartStyleRevision,
  saveTeamChartStyleDraft,
  setTeamChartStylePolicyV2,
  type ChartStyleIssue,
  type ChartStylePreviewResult,
  type ChartStyleRevision,
  type TeamChartStyleDetail,
  type TeamChartStyleOverrides,
  type TeamChartStyleSource,
} from "@/lib/team-chart-styles-api";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";
import { useAppRouter } from "@/lib/use-app-router";

import {
  AlertCircle,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Check,
  Clock,
  Edit,
  Info,
  Plus,
  RefreshCw,
  Trash,
} from "@/components/icons";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { ChartStylePreview } from "./chart-style-preview";
import { describeIssue } from "./issue-description";

type SaveStatus = "idle" | "saving" | "saved" | "publishing" | "conflict" | "error";
type PaletteMode = "light" | "dark";

/** What the preview pane is actually showing — never "some other style, unlabelled". */
export type PreviewView =
  | { kind: "candidate"; preview: ChartStylePreviewResult }
  | { kind: "lastValid"; preview: ChartStylePreviewResult }
  | { kind: "loading" }
  | { kind: "blocked" };

/** Publish gate bounds (chart_style_materializer `palette.count`), named once. */
const MIN_PALETTE_COLORS = 6;
const MAX_PALETTE_COLORS = 8;

const BUILT_IN_LABELS: Record<string, string> = {
  "bowhead-editorial": "presets.bowheadEditorial",
  "clean-blue": "presets.cleanBlue",
  "analytical-neutral": "presets.analyticalNeutral",
};
const HEX = /^#[0-9A-F]{6}$/;
const DEFAULT_NEW_COLORS = ["#2F6F84", "#7A527C", "#4B6F44", "#B04A2A", "#8A6D3B", "#4F5B5C"];

export function ChartStyleEditor({ styleId }: { styleId: string }) {
  const t = useTranslations("settings.appearance.chartStyleV2");
  const baseT = useTranslations("settings.appearance.chartStyle");
  const locale = useLocale();
  const router = useAppRouter();
  const [detail, setDetail] = useState<TeamChartStyleDetail | null>(null);
  const [revisions, setRevisions] = useState<ChartStyleRevision[]>([]);
  const [serverSource, setServerSource] = useState<TeamChartStyleSource | null>(null);
  const [candidate, setCandidate] = useState<TeamChartStyleSource | null>(null);
  const [preview, setPreview] = useState<ChartStylePreviewResult | null>(null);
  // The server drops candidateSnapshot AND surfaceEvidence together whenever the
  // candidate has a blocker. Keeping the last publishable result lets the preview
  // hold the last valid frame (PRD §13.2) instead of silently rendering something
  // that belongs to neither this draft nor this team's published style.
  const [lastValidPreview, setLastValidPreview] = useState<ChartStylePreviewResult | null>(null);
  const [previewFingerprint, setPreviewFingerprint] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [refreshNeeded, setRefreshNeeded] = useState(false);
  const [refreshBusy, setRefreshBusy] = useState(false);
  const operationEpoch = useRef(0);
  const [unavailable, setUnavailable] = useState<UnavailableReason | null>(null);
  const [paletteMode, setPaletteMode] = useState<PaletteMode>("light");
  const [acceptedWarningFingerprint, setAcceptedWarningFingerprint] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [restoreTarget, setRestoreTarget] = useState<ChartStyleRevision | null>(null);
  const [restoreValidation, setRestoreValidation] = useState<ChartStylePreviewResult | null>(null);
  const [publishNextOpen, setPublishNextOpen] = useState(false);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const mounted = useRef(true);

  const adopt = useCallback((
    next: TeamChartStyleDetail,
    options: { preserveWarningAcceptance?: boolean } = {},
  ) => {
    const source = next.draft?.source ?? (next.activeRevision
      ? {
          schemaVersion: 2 as const,
          name: next.activeRevision.name,
          description: next.activeRevision.description,
          overrides: next.activeRevision.overrides,
        }
      : null);
    setDetail(next);
    setServerSource(source);
    setCandidate(source ? structuredClone(source) : null);
    if (!options.preserveWarningAcceptance) setAcceptedWarningFingerprint(null);
    setStatus("idle");
  }, []);

  const load = useCallback(async () => {
    operationEpoch.current += 1;
    setRefreshNeeded(false);
    setRefreshBusy(false);
    setError(null);
    setUnavailable(null);
    // `?styleId=` is the only thing that names a style here. Without it the two
    // requests below become `/chart-styles/` (the *list*) and `/chart-styles//revisions`
    // (404), which surfaced as a permanent "temporarily unavailable" + a retry that
    // can never succeed. Nothing to load ⇒ say so and point back to the library.
    if (!styleId) {
      setUnavailable("missingStyle");
      return;
    }
    try {
      const [nextDetail, history] = await Promise.all([
        getTeamChartStyle(styleId),
        listTeamChartStyleRevisions(styleId),
      ]);
      if (!mounted.current) return;
      adopt(nextDetail);
      setRevisions(history.items);
    } catch (caught) {
      if (!mounted.current) return;
      const reason = unavailableReason(caught);
      if (reason) setUnavailable(reason);
      else setError(messageOf(caught, t("errors.load"), t));
    }
  }, [adopt, styleId, t]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const serializedCandidate = candidate ? JSON.stringify(candidate) : "";
  const serializedServer = serverSource ? JSON.stringify(serverSource) : "";
  const dirty = serializedCandidate !== serializedServer;
  const invalidPalette = candidate ? hasInvalidPaletteColor(candidate) : false;

  useEffect(() => {
    if (!candidate || !detail) return;
    if (invalidPalette) {
      setPreviewing(false);
      setPreviewError(null);
      return;
    }
    const fingerprint = serializedCandidate;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setPreviewing(true);
      setPreviewError(null);
      void previewTeamChartStyle(styleId, candidate, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          setPreview(result);
          setPreviewFingerprint(fingerprint);
          if (result.candidateSnapshot) setLastValidPreview(result);
        })
        .catch((caught) => {
          if (controller.signal.aborted) return;
          setPreviewError(messageOf(caught, t("errors.preview"), t));
        })
        .finally(() => {
          if (!controller.signal.aborted) setPreviewing(false);
        });
    }, 150);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [candidate, detail?.draft, invalidPalette, serializedCandidate, styleId, t]);

  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  const beginOperation = () => {
    operationEpoch.current += 1;
    setRefreshNeeded(false);
    setRefreshBusy(false);
  };

  // Acknowledged writes stay acknowledged when a later history/theme read fails.
  const refreshConfirmedStatus = async (): Promise<boolean> => {
    const epoch = operationEpoch.current;
    setRefreshBusy(true);
    const [history, theme] = await Promise.allSettled([
      listTeamChartStyleRevisions(styleId),
      refreshEffectiveTheme(),
    ]);
    if (!mounted.current || epoch !== operationEpoch.current) return false;
    if (history.status === "fulfilled") setRevisions(history.value.items);
    const refreshed = history.status === "fulfilled" && theme.status === "fulfilled" && theme.value === true;
    setRefreshBusy(false);
    setRefreshNeeded(!refreshed);
    if (!refreshed) setError(t("errors.saved_refresh_failed"));
    else setError((current) => current === t("errors.saved_refresh_failed") ? null : current);
    return refreshed;
  };

  const save = async (): Promise<TeamChartStyleDetail | null> => {
    if (!candidate || !detail?.draftEtag || invalidPalette) return null;
    beginOperation();
    setStatus("saving");
    setError(null);
    try {
      const result = await saveTeamChartStyleDraft(styleId, detail.draftEtag, candidate);
      adopt(result.detail, { preserveWarningAcceptance: true });
      setStatus("saved");
      return result.detail;
    } catch (caught) {
      const conflict = caught instanceof ApiError && [409, 412].includes(caught.status);
      setStatus(conflict ? "conflict" : "error");
      setError(messageOf(caught, conflict ? t("errors.conflict") : t("errors.save"), t));
      return null;
    }
  };

  const startEditing = async () => {
    if (!detail?.capabilities.createDraft.allowed) return;
    beginOperation();
    setStatus("saving");
    try {
      const result = await createTeamChartStyleDraft(styleId, detail.headEtag);
      adopt(result.detail);
    } catch (caught) {
      setStatus(caught instanceof ApiError && [409, 412].includes(caught.status) ? "conflict" : "error");
      setError(messageOf(caught, t("errors.edit"), t));
    }
  };

  const publish = async () => {
    if (!detail?.draftEtag || !preview || previewFingerprint !== serializedCandidate) return;
    const firstPublish = detail.style.activeRevisionId === null;
    beginOperation();
    setStatus("publishing");
    setError(null);
    try {
      const result = await publishTeamChartStyle(
        styleId,
        detail.draftEtag,
        preview.validation.warnings.length ? preview.validationFingerprint : undefined,
      );
      adopt(result.detail);
      setStatus("saved");
      setPublishNextOpen(firstPublish);
      await refreshConfirmedStatus();
    } catch (caught) {
      setStatus(caught instanceof ApiError && [409, 412].includes(caught.status) ? "conflict" : "error");
      setError(messageOf(caught, t("errors.publish"), t));
    }
  };

  const addPublishedToPolicy = async (makeDefault: boolean) => {
    beginOperation();
    setStatus("saving");
    setError(null);
    try {
      const current = await getTeamChartStylePolicyV2();
      const enabledStyleIds = current.policy.enabledStyleIds.includes(styleId)
        ? current.policy.enabledStyleIds
        : [...current.policy.enabledStyleIds, styleId];
      await setTeamChartStylePolicyV2({
        ...current.policy,
        enabledStyleIds,
        defaultStyleId: makeDefault ? styleId : current.policy.defaultStyleId,
      }, current.etag);
      setPublishNextOpen(false);
      setStatus("saved");
      if (await refreshConfirmedStatus()) router.push("/team/settings/brand-appearance/");
    } catch (caught) {
      const conflict = caught instanceof ApiError && [409, 412].includes(caught.status);
      setStatus(conflict ? "conflict" : "error");
      setError(messageOf(caught, conflict ? t("errors.conflict") : t("errors.policy"), t));
    }
  };

  const discardDraft = async () => {
    if (!detail?.draftEtag) return;
    beginOperation();
    setStatus("saving");
    try {
      const result = await discardTeamChartStyleDraft(styleId, detail.draftEtag);
      adopt(result.detail);
      setDiscardOpen(false);
    } catch (caught) {
      setStatus(caught instanceof ApiError && [409, 412].includes(caught.status) ? "conflict" : "error");
      setError(messageOf(caught, t("errors.discard"), t));
    }
  };

  const deleteStyle = async () => {
    if (!detail) return;
    beginOperation();
    setStatus("saving");
    try {
      await deleteDraftOnlyTeamChartStyle(styleId, detail.resourceEtag);
      router.push("/team/settings/brand-appearance/");
    } catch (caught) {
      setStatus(caught instanceof ApiError && [409, 412].includes(caught.status) ? "conflict" : "error");
      setError(messageOf(caught, t("errors.delete"), t));
    }
  };

  const restore = async () => {
    if (!detail || !restoreTarget) return;
    beginOperation();
    setStatus("publishing");
    setError(null);
    try {
      const source: TeamChartStyleSource = {
        schemaVersion: 2,
        name: restoreTarget.name,
        description: restoreTarget.description,
        overrides: restoreTarget.overrides,
      };
      const validation = restoreValidation ?? await previewTeamChartStyle(styleId, source);
      if (validation.validation.blockers.length) {
        setRestoreTarget(null);
        setRestoreValidation(null);
        setStatus("error");
        setError(t("errors.restore"));
        return;
      }
      if (validation.validation.warnings.length && !restoreValidation) {
        setRestoreValidation(validation);
        setStatus("idle");
        return;
      }
      const result = await restoreTeamChartStyleRevision(
        styleId,
        restoreTarget.id,
        detail.headEtag,
        validation.validation.warnings.length ? validation.validationFingerprint : undefined,
      );
      adopt(result.detail);
      setRestoreTarget(null);
      setRestoreValidation(null);
      setStatus("saved");
      await refreshConfirmedStatus();
    } catch (caught) {
      setStatus(caught instanceof ApiError && [409, 412].includes(caught.status) ? "conflict" : "error");
      setError(messageOf(caught, t("errors.restore"), t));
    }
  };

  const back = () => {
    if (dirty) setLeaveOpen(true);
    else router.push("/team/settings/brand-appearance/");
  };

  if (unavailable && !detail) {
    return (
      <div className="cs-page cs-page--narrow">
        <Notice tone="info">{t(`unavailable.${unavailable}`)}</Notice>
        <Button onClick={() => router.push("/team/settings/brand-appearance/")}>
          <ArrowLeft size={14} aria-hidden /> {t("back")}
        </Button>
      </div>
    );
  }
  if (error && !detail) {
    return (
      <div className="cs-page cs-page--narrow">
        <Notice tone="error">{error}</Notice>
        <Button onClick={() => void load()}><RefreshCw size={14} aria-hidden /> {baseT("retry")}</Button>
      </div>
    );
  }
  if (!detail || !candidate) {
    return <div className="cs-editor-skeleton" aria-label={baseT("loading")} />;
  }

  const editable = Boolean(detail.draft);
  const previewCurrent = previewFingerprint === serializedCandidate;
  const blockers = previewCurrent ? preview?.validation.blockers ?? [] : [];
  const warnings = previewCurrent ? preview?.validation.warnings ?? [] : [];
  // Advisory only (e.g. two colors hard to tell apart): shown, never acknowledged or blocking.
  const notes = previewCurrent ? preview?.validation.info ?? [] : [];
  const canPublish = editable
    && !dirty
    && previewCurrent
    && !previewing
    && blockers.length === 0
    && (warnings.length === 0 || acceptedWarningFingerprint === preview?.validationFingerprint)
    && detail.capabilities.publish.allowed;
  // One derivation for "what is the preview actually showing", so a rejected
  // candidate cannot silently become a different style. Precedence: the current
  // candidate if it is publishable → the last publishable one → nothing.
  const previewView: PreviewView = (() => {
    if (previewing || !previewCurrent) return { kind: "loading" };
    if (preview?.candidateSnapshot) return { kind: "candidate", preview };
    if (lastValidPreview) return { kind: "lastValid", preview: lastValidPreview };
    return { kind: "blocked" };
  })();
  // What to *draw* is deliberately decoupled from what to *label*: while a new
  // preview is in flight we keep drawing the last publishable one, otherwise every
  // debounced keystroke would flash the product default palette on screen.
  const viewPreview = (preview?.candidateSnapshot ? preview : null) ?? lastValidPreview;
  // Colours the validation rows may draw swatches from. The explicit palette is
  // authoritative; a derived one is only trustworthy when it still has the same
  // number of entries as the candidate. Anything else stays undrawn — better no
  // swatch than a swatch showing a colour the issue is not about.
  const issuePalettes = (["light", "dark"] as const).reduce<{ light?: string[] | null; dark?: string[] | null }>(
    (acc, mode) => {
      const explicit = candidate.overrides.categoricalPalette?.[mode];
      const derived = viewPreview?.candidateSnapshot?.colors[mode].categorical;
      acc[mode] = explicit ?? derived ?? null;
      return acc;
    },
    {},
  );
  const publishBlockedReason = (() => {
    if (dirty) return t("publishBlockedUnsavedDraft");
    if (previewing || !previewCurrent) return t("previewUpdating");
    if (blockers.length) return t("publishBlockedBlockers", { count: blockers.length });
    if (warnings.length && acceptedWarningFingerprint !== preview?.validationFingerprint) {
      return t("publishBlockedWarnings");
    }
    if (!detail.capabilities.publish.allowed) return t("publishBlockedNoPermission");
    return null;
  })();
  const themeForMode = (mode: PaletteMode): ChartTheme => {
    // No `?? detail.activeRevision?.resolvedSnapshot` here on purpose: the server
    // withholds surfaceEvidence alongside candidateSnapshot, so that fallback could
    // never render anyway — it only ever misled the next reader.
    const snapshot = viewPreview?.candidateSnapshot;
    const projection = viewPreview?.surfaceEvidence?.modes[mode];
    if (!snapshot || !projection) return chartThemeBaseForMode(mode);
    return resolveChartThemeForV2Snapshot(
      snapshot,
      projection,
      { contrast: "standard", colorVision: "standard" },
      chartThemeBaseForMode(mode),
      mode,
    );
  };

  return (
    <div className="cs-page" data-testid="chart-style-editor">
      <header className="cs-page__header">
        <button type="button" className="cs-back" onClick={back}>
          <ArrowLeft size={15} aria-hidden /> {t("back")}
        </button>
        <div className="cs-page__heading-row">
          <div>
            <span className="cs-kicker">{t("editorKicker")}</span>
            <h1 className="ui-page-title">{candidate.name || t("untitled")}</h1>
            <p className="ui-page-desc">{t("editorDescription")}</p>
          </div>
          <div className="cs-page__actions">
            <SaveState status={status} t={t} />
            {!editable ? (
              <Button variant="primary" onClick={() => void startEditing()} disabled={!detail.capabilities.createDraft.allowed}>
                <Edit aria-hidden /> {t("edit")}
              </Button>
            ) : (
              <>
                <Button onClick={() => void save()} disabled={!dirty || invalidPalette || status === "saving" || status === "publishing"}>
                  {t("saveDraft")}
                </Button>
                <Button variant="primary" onClick={() => void publish()} disabled={!canPublish}>
                  <Check size={14} aria-hidden /> {t("saveAndApply")}
                </Button>
                {/* A greyed-out primary button with no stated reason reads as broken. */}
                {!canPublish && publishBlockedReason ? (
                  <span className="cs-publish-blocked" role="status">{publishBlockedReason}</span>
                ) : null}
              </>
            )}
          </div>
        </div>
      </header>

      {error ? (
        <Notice tone="error">
          <span>{error}</span>
          {status === "conflict" ? <Button size="sm" onClick={() => void load()}>{t("reload")}</Button> : null}
          {refreshNeeded ? <Button size="sm" disabled={refreshBusy} onClick={() => void refreshConfirmedStatus()}>{t("retryRefresh")}</Button> : null}
        </Notice>
      ) : null}

      <div className="cs-editor-layout">
        <main className="cs-editor-form" aria-disabled={!editable}>
          <EditorSection number="01" title={t("sections.identity")} description={t("sections.identityDesc")}>
            <div className="cs-form-grid">
              <Field id="chart-style-name" label={t("name")} hint={t("nameHint")}>
                <input
                  id="chart-style-name"
                  value={candidate.name}
                  maxLength={40}
                  disabled={!editable}
                  aria-describedby={fieldDescribedBy("chart-style-name", { hint: true })}
                  onChange={(event) => setCandidate({ ...candidate, name: event.target.value })}
                />
              </Field>
              <Field id="chart-style-base" label={t("basePreset")} hint={t("basePresetHint")}>
                <div id="chart-style-base" className="cs-readonly-field">
                  {baseT(BUILT_IN_LABELS[detail.draft?.basePreset.id ?? detail.activeRevision?.basePreset.id ?? "bowhead-editorial"] ?? "presets.bowheadEditorial")}
                </div>
              </Field>
            </div>
            <Field id="chart-style-description" label={t("description")} hint={t("descriptionHint")}>
              <textarea
                id="chart-style-description"
                rows={3}
                maxLength={120}
                value={candidate.description}
                disabled={!editable}
                aria-describedby={fieldDescribedBy("chart-style-description", { hint: true })}
                onChange={(event) => setCandidate({ ...candidate, description: event.target.value })}
              />
            </Field>
          </EditorSection>

          <EditorSection number="02" title={t("sections.palette")} description={t("sections.paletteDesc")}>
            <PaletteEditor
              mode={paletteMode}
              source={candidate}
              preview={viewPreview}
              disabled={!editable}
              dragIndex={dragIndex}
              t={t}
              onModeChange={setPaletteMode}
              onDragIndex={setDragIndex}
              onChange={setCandidate}
            />
          </EditorSection>

          <EditorSection number="03" title={t("sections.line")} description={t("sections.lineDesc")}>
            <ControlRow label={t("curve")} description={t("curveDesc")}>
              <ChoiceGroup
                label={t("curve")}
                disabled={!editable}
                value={selectedLineValue(candidate.overrides, preview, "curve")}
                options={[["straight", t("choices.straight")], ["monotone", t("choices.monotone")]]}
                onChange={(value) => setCandidate(withOverride(candidate, "line", "curve", value))}
              />
            </ControlRow>
            <ControlRow label={t("lineWidth")} description={t("lineWidthDesc")}>
              <ChoiceGroup
                label={t("lineWidth")}
                disabled={!editable}
                value={selectedLineValue(candidate.overrides, preview, "width")}
                options={[["thin", t("choices.thin")], ["standard", t("choices.standard")], ["strong", t("choices.strong")]]}
                onChange={(value) => setCandidate(withOverride(candidate, "line", "width", value))}
              />
            </ControlRow>
            <ControlRow label={t("points")} description={t("pointsDesc")}>
              <ChoiceGroup
                label={t("points")}
                disabled={!editable}
                value={selectedLineValue(candidate.overrides, preview, "points")}
                options={[["auto", t("choices.auto")], ["show", t("choices.show")], ["hide", t("choices.hide")]]}
                onChange={(value) => setCandidate(withOverride(candidate, "line", "points", value))}
              />
            </ControlRow>
          </EditorSection>

          <EditorSection number="04" title={t("sections.common")} description={t("sections.commonDesc")}>
            <ControlRow label={t("barRadius")} description={t("barRadiusDesc")}>
              <ChoiceGroup
                label={t("barRadius")}
                disabled={!editable}
                value={selectedBarRadius(candidate.overrides, preview)}
                options={[["square", t("choices.square")], ["soft", t("choices.soft")], ["round", t("choices.round")]]}
                onChange={(value) => setCandidate(withOverride(candidate, "bar", "radius", value))}
              />
            </ControlRow>
            <ControlRow label={t("grid")} description={t("gridDesc")}>
              <ChoiceGroup
                label={t("grid")}
                disabled={!editable}
                value={selectedComponent(candidate.overrides, preview, "grid")}
                options={[["hidden", t("choices.hidden")], ["quiet", t("choices.quiet")], ["standard", t("choices.standard")]]}
                onChange={(value) => setCandidate(withOverride(candidate, "components", "grid", value))}
              />
            </ControlRow>
            <ControlRow label={t("tooltip")} description={t("tooltipDesc")}>
              <ChoiceGroup
                label={t("tooltip")}
                disabled={!editable}
                value={selectedComponent(candidate.overrides, preview, "tooltip")}
                options={[["enabled", t("choices.enabled")], ["disabled", t("choices.disabled")]]}
                onChange={(value) => setCandidate(withOverride(candidate, "components", "tooltip", value))}
              />
            </ControlRow>
          </EditorSection>

          {editable ? (
            <EditorSection number="05" title={t("sections.validation")} description={t("sections.validationDesc")}>
              {previewError ? <Notice tone="error">{previewError}</Notice> : null}
              {previewing ? <div className="cs-validation__pending">{t("previewUpdating")}</div> : null}
              <IssueList issues={blockers} tone="error" t={t} palettes={issuePalettes} />
              <IssueList issues={warnings} tone="warning" t={t} palettes={issuePalettes} />
              <IssueList issues={notes} tone="info" t={t} palettes={issuePalettes} />
              {!previewing && previewCurrent && blockers.length === 0 && warnings.length === 0 ? (
                <div className="cs-validation__pass"><Check size={15} aria-hidden /> {t("validationPassed")}</div>
              ) : null}
              {warnings.length ? (
                <label className="cs-warning-ack">
                  <input
                    type="checkbox"
                    checked={acceptedWarningFingerprint === preview?.validationFingerprint}
                    onChange={(event) => setAcceptedWarningFingerprint(
                      event.target.checked && preview ? preview.validationFingerprint : null,
                    )}
                  />
                  <span>{t("acknowledgeWarnings")}</span>
                </label>
              ) : null}
            </EditorSection>
          ) : null}

          <EditorSection number="06" title={t("sections.history")} description={t("sections.historyDesc")}>
            <RevisionHistory
              revisions={revisions}
              activeId={detail.style.activeRevisionId}
              canRestore={detail.capabilities.restore.allowed}
              locale={locale}
              t={t}
              onRestore={(revision) => {
                setRestoreTarget(revision);
                setRestoreValidation(null);
              }}
            />
          </EditorSection>

          {editable ? (
            <div className="cs-danger-zone">
              {detail.state === "draft_only" ? (
                <Button variant="danger" onClick={() => setDeleteOpen(true)}>
                  <Trash size={14} aria-hidden /> {t("deleteDraftStyle")}
                </Button>
              ) : (
                <Button variant="danger" onClick={() => setDiscardOpen(true)}>
                  <Trash size={14} aria-hidden /> {t("discardDraft")}
                </Button>
              )}
            </div>
          ) : null}
        </main>

        <aside className="cs-editor-preview">
          <ChartStylePreview themeForMode={themeForMode} view={previewView.kind} />
          <div className="cs-scope-note">
            <Info size={15} aria-hidden />
            <span>{t("presentationOnly")}</span>
          </div>
        </aside>
      </div>

      {discardOpen ? (
        <ConfirmDialog
          title={t("discardTitle")}
          description={t("discardDescription")}
          confirmLabel={t("discardDraft")}
          cancelLabel={t("cancel")}
          busy={status === "saving"}
          onCancel={() => setDiscardOpen(false)}
          onConfirm={() => void discardDraft()}
        />
      ) : null}
      {deleteOpen ? (
        <ConfirmDialog
          title={t("deleteTitle")}
          description={t("deleteDescription")}
          confirmLabel={t("deleteDraftStyle")}
          cancelLabel={t("cancel")}
          busy={status === "saving"}
          onCancel={() => setDeleteOpen(false)}
          onConfirm={() => void deleteStyle()}
        />
      ) : null}
      {restoreTarget ? (
        <ConfirmDialog
          title={t("restoreTitle", { sequence: restoreTarget.sequence })}
          description={restoreValidation?.validation.warnings.length
            ? t("restoreWarningsDescription", { count: restoreValidation.validation.warnings.length })
            : t("restoreDescription")}
          confirmLabel={t("restore")}
          cancelLabel={t("cancel")}
          tone="primary"
          busy={status === "publishing"}
          onCancel={() => {
            setRestoreTarget(null);
            setRestoreValidation(null);
          }}
          onConfirm={() => void restore()}
        />
      ) : null}
      {leaveOpen ? (
        <Dialog
          title={t("leaveTitle")}
          onClose={() => setLeaveOpen(false)}
          footer={(
            <>
              <Button onClick={() => setLeaveOpen(false)}>{t("stay")}</Button>
              <Button onClick={() => router.push("/team/settings/brand-appearance/")}>{t("discardLocal")}</Button>
              <Button
                variant="primary"
                disabled={invalidPalette}
                onClick={() => void save().then((saved) => {
                  if (saved) router.push("/team/settings/brand-appearance/");
                })}
              >
                {t("saveAndLeave")}
              </Button>
            </>
          )}
        >
          <p className="settings-dialog__description">{t("leaveDescription")}</p>
        </Dialog>
      ) : null}
      {publishNextOpen ? (
        <Dialog
          title={t("publishedNextTitle")}
          onClose={() => setPublishNextOpen(false)}
          footer={(
            <>
              <Button onClick={() => setPublishNextOpen(false)}>{t("publishedLater")}</Button>
              <Button onClick={() => void addPublishedToPolicy(false)} disabled={status === "saving"}>
                {t("publishedAllow")}
              </Button>
              <Button variant="primary" onClick={() => void addPublishedToPolicy(true)} disabled={status === "saving"}>
                {t("publishedDefault")}
              </Button>
            </>
          )}
        >
          <p className="settings-dialog__description">{t("publishedNextDescription")}</p>
        </Dialog>
      ) : null}
    </div>
  );
}

function EditorSection({
  number,
  title,
  description,
  children,
}: {
  number: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="cs-editor-section">
      <header>
        <span>{number}</span>
        <div><h2>{title}</h2><p>{description}</p></div>
      </header>
      <div className="cs-editor-section__body">{children}</div>
    </section>
  );
}

function ControlRow({ label, description, children }: { label: string; description: string; children: React.ReactNode }) {
  return (
    <div className="cs-control-row">
      <div><strong>{label}</strong><span>{description}</span></div>
      {children}
    </div>
  );
}

function ChoiceGroup({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<[string, string]>;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <SegmentedControl className="cs-choice-group" aria-label={label} value={value} onValueChange={onChange}>
      {options.map(([id, text]) => (
        <Segment key={id} value={id} disabled={disabled}>
          {text}
        </Segment>
      ))}
    </SegmentedControl>
  );
}

function PaletteEditor({
  mode,
  source,
  preview,
  disabled,
  dragIndex,
  t,
  onModeChange,
  onDragIndex,
  onChange,
}: {
  mode: PaletteMode;
  source: TeamChartStyleSource;
  preview: ChartStylePreviewResult | null;
  disabled: boolean;
  dragIndex: number | null;
  t: ReturnType<typeof useTranslations>;
  onModeChange: (mode: PaletteMode) => void;
  onDragIndex: (index: number | null) => void;
  onChange: (source: TeamChartStyleSource) => void;
}) {
  const explicit = source.overrides.categoricalPalette?.[mode];
  // `preview` is already narrowed by the caller to a publishable result, so the
  // derived dark values shown here are always ones the server actually produced.
  const derived = preview?.candidateSnapshot?.colors[mode].categorical;
  const colors = explicit ?? derived ?? [];
  // Nothing explicit and nothing derived: the list contents are unknown, so
  // appending to it would fabricate a palette (and a 1-colour palette is itself a
  // `palette.count` blocker, which keeps the snapshot away — a self-sustaining loop).
  const unavailable = !explicit && !derived;
  const update = (next: string[]) => onChange(withPalette(source, mode, next));
  const move = (from: number, to: number) => {
    if (to < 0 || to >= colors.length || from === to) return;
    const next = [...colors];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    update(next);
  };
  const add = () => {
    const next = DEFAULT_NEW_COLORS.find((color) => !colors.includes(color)) ?? "#586F7C";
    update([...colors, next].slice(0, MAX_PALETTE_COLORS));
  };
  return (
    <div className="cs-palette-editor">
      <div className="cs-palette-editor__toolbar">
        <SegmentedControl className="cs-segment" aria-label={t("paletteMode")} value={mode} onValueChange={onModeChange}>
          {(["light", "dark"] as const).map((item) => (
            <Segment key={item} value={item}>
              {item === "light" ? t("lightMode") : t("darkMode")}
            </Segment>
          ))}
        </SegmentedControl>
        <span>{t("paletteCount", { count: colors.length })}</span>
      </div>
      {unavailable ? (
        <div className="cs-inline-note cs-inline-note--error" role="status">
          <AlertCircle size={14} aria-hidden /> {t("paletteUnavailable")}
        </div>
      ) : mode === "dark" && !source.overrides.categoricalPalette?.dark ? (
        <div className="cs-inline-note"><Info size={14} aria-hidden /> {t("darkGenerated")}</div>
      ) : null}
      {hasInvalidPaletteColor(source) ? (
        <div id="chart-style-palette-error" className="cs-inline-note cs-inline-note--error" role="alert">
          <AlertCircle size={14} aria-hidden /> {t("colorHexInvalid")}
        </div>
      ) : null}
      <div className="cs-palette-list">
        {colors.map((color, index) => {
          const valid = HEX.test(color);
          return (
            <div
              key={`${index}-${color}`}
              className="cs-palette-row"
              draggable={!disabled}
              onDragStart={() => onDragIndex(index)}
              onDragEnd={() => onDragIndex(null)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={() => {
                if (dragIndex !== null) move(dragIndex, index);
                onDragIndex(null);
              }}
            >
              <span className="cs-palette-row__index">{String(index + 1).padStart(2, "0")}</span>
              <input
                type="color"
                value={valid ? color : "#000000"}
                disabled={disabled}
                aria-label={t("colorPicker", { index: index + 1 })}
                onChange={(event) => {
                  const next = [...colors];
                  next[index] = event.target.value.toUpperCase();
                  update(next);
                }}
              />
              <input
                type="text"
                value={color}
                maxLength={7}
                disabled={disabled}
                aria-invalid={!valid}
                aria-describedby={!valid ? "chart-style-palette-error" : undefined}
                aria-label={t("colorHex", { index: index + 1 })}
                onChange={(event) => {
                  const next = [...colors];
                  next[index] = event.target.value.toUpperCase();
                  update(next);
                }}
              />
              <div className="cs-palette-row__actions">
                <button type="button" disabled={disabled || index === 0} aria-label={t("moveUp", { index: index + 1 })} onClick={() => move(index, index - 1)}><ArrowUp size={14} aria-hidden /></button>
                <button type="button" disabled={disabled || index === colors.length - 1} aria-label={t("moveDown", { index: index + 1 })} onClick={() => move(index, index + 1)}><ArrowDown size={14} aria-hidden /></button>
                <button type="button" disabled={disabled || colors.length <= MIN_PALETTE_COLORS} aria-label={t("removeColor", { index: index + 1 })} onClick={() => update(colors.filter((_, item) => item !== index))}><Trash size={14} aria-hidden /></button>
              </div>
            </div>
          );
        })}
      </div>
      <div className="cs-palette-editor__footer">
        <Button size="sm" onClick={add} disabled={disabled || unavailable || colors.length >= MAX_PALETTE_COLORS}><Plus size={14} aria-hidden /> {t("addColor")}</Button>
        {mode === "dark" && source.overrides.categoricalPalette?.dark ? (
          <Button size="sm" variant="ghost" onClick={() => onChange(withoutDarkPalette(source))} disabled={disabled}>{t("useGeneratedDark")}</Button>
        ) : null}
      </div>
    </div>
  );
}

function IssueList({
  issues,
  tone,
  t,
  palettes,
}: {
  issues: ChartStyleIssue[];
  tone: "error" | "warning" | "info";
  t: ReturnType<typeof useTranslations>;
  palettes: { light?: string[] | null; dark?: string[] | null };
}) {
  if (!issues.length) return null;
  return (
    <div className={`cs-issues cs-issues--${tone}`}>
      {issues.map((issue, index) => {
        const described = describeIssue(issue, t, palettes);
        return (
          <div key={`${issue.code}-${issue.path}-${index}`}>
            <AlertCircle size={15} aria-hidden />
            <span>
              <strong>{issueLabel(issue.code, t)}</strong>
              {described.location ? <small>{described.location}</small> : null}
              {described.metric || described.swatches.length ? (
                <small className="cs-issue-metric">
                  {described.swatches.map((color, swatchIndex) => (
                    <i
                      key={`${color}-${swatchIndex}`}
                      className="cs-issue-swatch"
                      style={{ background: color }}
                      aria-hidden
                    />
                  ))}
                  {described.metric}
                </small>
              ) : null}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function RevisionHistory({
  revisions,
  activeId,
  canRestore,
  locale,
  t,
  onRestore,
}: {
  revisions: ChartStyleRevision[];
  activeId: string | null;
  canRestore: boolean;
  locale: string;
  t: ReturnType<typeof useTranslations>;
  onRestore: (revision: ChartStyleRevision) => void;
}) {
  if (!revisions.length) return <div className="cs-empty-row">{t("noHistory")}</div>;
  return (
    <div className="cs-history">
      {revisions.map((revision) => {
        const active = revision.id === activeId;
        return (
          <div key={revision.id} className="cs-history__row">
            <Clock size={15} aria-hidden />
            <div>
              <strong>{t("revision", { sequence: revision.sequence })}</strong>
              <span>{revision.name} · {new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(revision.createdAt))}</span>
            </div>
            {active ? <span className="cs-status cs-status--active">{t("current")}</span> : (
              <Button size="sm" disabled={!canRestore} onClick={() => onRestore(revision)}>{t("restore")}</Button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function SaveState({ status, t }: { status: SaveStatus; t: ReturnType<typeof useTranslations> }) {
  if (status === "idle") return null;
  const key = status === "saving" ? "saving" : status === "publishing" ? "publishing" : status === "saved" ? "saved" : status === "conflict" ? "conflictShort" : "failedShort";
  return <span className={`cs-save-state cs-save-state--${status}`} role="status">{t(key)}</span>;
}

function withOverride(
  source: TeamChartStyleSource,
  group: "line" | "bar" | "components",
  field: string,
  value: string,
): TeamChartStyleSource {
  return {
    ...source,
    overrides: {
      ...source.overrides,
      [group]: { ...(source.overrides[group] ?? {}), [field]: value },
    },
  } as TeamChartStyleSource;
}

function withPalette(source: TeamChartStyleSource, mode: PaletteMode, colors: string[]): TeamChartStyleSource {
  return {
    ...source,
    overrides: {
      ...source.overrides,
      categoricalPalette: {
        ...source.overrides.categoricalPalette,
        [mode]: colors,
      },
    },
  };
}

function withoutDarkPalette(source: TeamChartStyleSource): TeamChartStyleSource {
  const palette = { ...source.overrides.categoricalPalette };
  delete palette.dark;
  return {
    ...source,
    overrides: { ...source.overrides, categoricalPalette: palette },
  };
}

function hasInvalidPaletteColor(source: TeamChartStyleSource): boolean {
  const palette = source.overrides.categoricalPalette;
  return [...(palette?.light ?? []), ...(palette?.dark ?? [])].some((color) => !HEX.test(color));
}

function selectedLineValue(overrides: TeamChartStyleOverrides, preview: ChartStylePreviewResult | null, field: "curve" | "width" | "points"): string {
  const explicit = overrides.line?.[field];
  if (explicit) return explicit;
  const kind = preview?.candidateSnapshot?.kindTokens.line as Record<string, unknown> | undefined;
  const line = kind?.line as Record<string, unknown> | undefined;
  if (field === "curve") return String(line?.curve ?? "monotone");
  if (field === "points") return String(line?.points ?? "auto");
  const width = Number(line?.widthPx ?? 2);
  return width <= 1.5 ? "thin" : width >= 2.5 ? "strong" : "standard";
}

function selectedBarRadius(overrides: TeamChartStyleOverrides, preview: ChartStylePreviewResult | null): string {
  if (overrides.bar?.radius) return overrides.bar.radius;
  const kind = preview?.candidateSnapshot?.kindTokens.bar as Record<string, unknown> | undefined;
  const bar = kind?.bar as Record<string, unknown> | undefined;
  const radius = Number(bar?.radiusPx ?? 3);
  return radius <= 0 ? "square" : radius >= 6 ? "round" : "soft";
}

function selectedComponent(overrides: TeamChartStyleOverrides, preview: ChartStylePreviewResult | null, field: "grid" | "tooltip"): string {
  const explicit = overrides.components?.[field];
  if (explicit) return explicit;
  const tokens = preview?.candidateSnapshot?.kindTokens.line as Record<string, unknown> | undefined;
  return String(tokens?.[field] ?? (field === "grid" ? "quiet" : "enabled"));
}

function issueLabel(code: string, t: ReturnType<typeof useTranslations>): string {
  const known: Record<string, string> = {
    "palette.count": "validation.paletteCount",
    "palette.duplicate_color": "validation.duplicate",
    "palette.surface_contrast": "validation.contrast",
    "palette.pairwise_delta": "validation.pairwise",
    "palette.adjacent_delta": "validation.adjacent",
    "palette.adjacent_grayscale": "validation.grayscale",
    "palette.cvd_separation": "validation.cvd",
    "palette.distinctness": "validation.distinct",
    "palette.mode_length_mismatch": "validation.modeLength",
  };
  return t(known[code] ?? "validation.unknown");
}

type UnavailableReason = "missingStyle" | "notFound" | "forbidden";

/** Failures that retrying cannot fix: the style is gone, or this account may not edit it. */
function unavailableReason(error: unknown): UnavailableReason | null {
  if (!(error instanceof ApiError) || error.nonApiResponse) return null;
  if (error.status === 404 && error.code === "chart_style_not_found") return "notFound";
  if (error.status === 403 && error.code === "insufficient_role") return "forbidden";
  return null;
}

function messageOf(error: unknown, fallback: string, t: ReturnType<typeof useTranslations>): string {
  if (error instanceof ApiError && error.code) {
    const known = [
      "chart_style_name_conflict",
      "chart_style_limit_reached",
      "chart_style_warning_ack_required",
      "chart_style_preview_rate_limited",
      "chart_style_active_revision_changed",
      "chart_style_draft_requires_resolution",
      "chart_style_precondition_failed",
    ];
    if (known.includes(error.code)) return t(`errors.${error.code}`);
  }
  return fallback;
}
