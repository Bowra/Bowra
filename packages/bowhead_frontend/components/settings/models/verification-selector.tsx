"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Check, Info, Search, XSm } from "@/components/icons";
import { DialogShell } from "@/components/ui/dialog";
import type { ModelCapability } from "@/lib/llm-config-v2-api";

export interface VerificationCandidate {
  providerId: string;
  providerName: string;
  modelId: string;
  capability: ModelCapability;
  factState: string;
  referenced: boolean;
  recommended: boolean;
}

export function verificationCandidateKey(candidate: Pick<
  VerificationCandidate,
  "providerId" | "modelId" | "capability"
>): string {
  return `${candidate.providerId}:${candidate.modelId}:${candidate.capability}`;
}

export function VerificationSelector({
  candidates,
  busy,
  onPlan,
  onClose,
  workflow,
  onBack,
  error,
}: {
  error?: string | null;
  workflow?: React.ReactNode;
  onBack?: () => void;
  candidates: VerificationCandidate[];
  busy: boolean;
  onPlan: (selected: VerificationCandidate[]) => void;
  onClose: () => void;
}) {
  const t = useTranslations("settings.models.v2");
  const overviewT = useTranslations("settings.models.overview");
  const closeRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [recommendedOnly, setRecommendedOnly] = useState(false);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(
    () => new Set(
      candidates
        .filter((candidate) => candidate.recommended)
        .map(verificationCandidateKey),
    ),
  );

  // DialogShell owns initial focus on opening (search has data-dialog-initial-focus); here handle only subsequent
  // selection/verification-plan switches: entering the plan focuses the panel's first button (Close); returning to selection focuses search.
  // Do not steal focus on mount, or DialogShell records the panel's search field as pre-open focus and cannot return to the trigger after closing.
  const hasWorkflow = Boolean(workflow);
  const previousHasWorkflowRef = useRef(hasWorkflow);
  useEffect(() => {
    if (previousHasWorkflowRef.current === hasWorkflow) return;
    previousHasWorkflowRef.current = hasWorkflow;
    if (hasWorkflow) closeRef.current?.focus();
    else searchRef.current?.focus();
  }, [hasWorkflow]);

  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return candidates.filter((candidate) => (
      (!recommendedOnly || candidate.recommended)
      && (!normalized || [
        candidate.providerName,
        candidate.modelId,
        candidate.capability,
      ].some((value) => value.toLowerCase().includes(normalized)))
    ));
  }, [candidates, query, recommendedOnly]);
  const selected = candidates.filter((candidate) =>
    selectedKeys.has(verificationCandidateKey(candidate)));

  return (
    <DialogShell
      onClose={onClose}
      busy={busy}
      mount="inline"
      labelledBy="mset-verification-selector-title"
      backdropClassName="mset-scrim verification-selector-scrim"
      panelClassName="verification-selector"
      panelProps={{ "aria-describedby": "mset-verification-selector-description" }}
    >
      <header className="verification-selector-head">
        <div>
          <span>{t("selection_kicker")}</span>
          <h2 id="mset-verification-selector-title">{t("selection_title")}</h2>
          <p id="mset-verification-selector-description">{t("selection_body")}</p>
        </div>
        <button ref={closeRef} type="button" className="iconbtn" onClick={onClose} aria-label={t("close")}>
          <XSm size={15} />
        </button>
      </header>

      {error ? <p className="test-result err" role="alert">{error}</p> : null}
      {workflow ? <>
        {onBack ? <button type="button" className="btn ghost sm" disabled={busy} onClick={onBack}>{t("selection_back")}</button> : null}
        {workflow}
      </> : <>
      <div className="verification-selector-tools">
        <label className="verification-selector-search">
          <Search size={14} aria-hidden />
          <input
            ref={searchRef}
            data-dialog-initial-focus
            type="search"
            name="model_verification_search"
            autoComplete="off"
            value={query}
            placeholder={t("selection_search")}
            aria-label={t("selection_search")}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <button
          type="button"
          className={`btn tiny ${recommendedOnly ? "sec" : "ghost"}`}
          aria-pressed={recommendedOnly}
          onClick={() => setRecommendedOnly((current) => !current)}
        >
          {t("selection_recommended")}
        </button>
      </div>

      <div className="verification-selector-list">
        {visible.length ? visible.map((candidate) => {
          const key = verificationCandidateKey(candidate);
          const checked = selectedKeys.has(key);
          return (
            <label className={`verification-selector-row${checked ? " is-selected" : ""}`} key={key}>
              <input
                type="checkbox"
                checked={checked}
                onChange={() => setSelectedKeys((current) => {
                  const next = new Set(current);
                  if (next.has(key)) next.delete(key);
                  else next.add(key);
                  return next;
                })}
              />
              <span className="verification-selector-check" aria-hidden>
                {checked ? <Check size={11} /> : null}
              </span>
              <span className="verification-selector-model">
                <b translate="no">{candidate.modelId}</b>
                <small>{candidate.providerName}</small>
              </span>
              <span className="verification-selector-purpose">
                <b>{overviewT(`capability_${candidate.capability}`)}</b>
                <small>{candidate.referenced
                  ? t("selection_current_assignment")
                  : t("selection_declared_only")}</small>
              </span>
              <span className={`overview-state fact-${candidate.factState}`}>
                {overviewT(`fact_${candidate.factState}`)}
              </span>
            </label>
          );
        }) : (
          <div className="verification-selector-empty">
            <Info size={16} /> {t("selection_empty")}
          </div>
        )}
      </div>

      <footer className="verification-selector-foot">
        <span>{t("selection_selected", { count: selected.length })}</span>
        <div>
          <button type="button" className="btn ghost sm" onClick={onClose}>{t("cancel")}</button>
          <button
            type="button"
            className="btn pri sm"
            disabled={busy || selected.length === 0}
            onClick={() => onPlan(selected)}
          >
            {t("selection_plan")}
          </button>
        </div>
      </footer>
      </>}
    </DialogShell>
  );
}
