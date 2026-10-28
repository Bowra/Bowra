"use client";

import { useCallback, useEffect, useId, useRef, useState, type MouseEvent } from "react";
import { ChevronRight, Copy } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { agentManagementView, agentManagementReturnHref, withAgentReturnView } from "@/lib/agent-navigation";
import { agentChartsReturnHref } from "@/lib/agent-charts-navigation";
import { agentDashboardReturnHref } from "@/lib/agent-dashboard";
import { useLocale, useTranslations } from "next-intl";

import {
  getAgentCapabilityReport,
  getAgentDetail,
  hireAgent,
  setAgentPersonalStatus,
  type AgentAssignmentMutation,
  type AgentCapabilityEvidence,
  type AgentCapabilityReport,
  type AgentCatalogItem,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { useAccessRequestsEnabled } from "@/lib/use-access-requests-enabled";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import { agentArtworkStyle } from "./agent-artwork";
import { AgentForkDialog } from "./agent-fork-dialog";
import { useAgentListingText } from "./agent-listing-text";
import { AgentBreadcrumb } from "./agent-breadcrumb";
import { availabilityReason, limitedAvailability } from "@/lib/agent-availability";
import { ReasonFull, reasonText } from "./availability-reason";
import { useCapabilityLabel } from "./capability-label";
import { CharIcon } from "@/components/char-icon";
import { agentMark } from "./agent-mark";
import { AccessRequestPanel } from "@/components/access/access-request-panel";
import { AgentResourceList } from "./agent-resource-list";
import { LucideGlyph } from "@/components/icon-refresh";
import { toast } from "@/lib/use-toast";

const HIRED_STATUSES: ReadonlySet<string> = new Set(["active", "personal_disabled", "access_revoked"]);

const CAPABILITY_DESCRIPTION_KEYS: Record<string, string> = {
  cap_analysis: "capability_desc_analysis",
  cap_compute: "capability_desc_compute",
  cap_data_discovery: "capability_desc_data_discovery",
  cap_data_query: "capability_desc_data_query",
  cap_document: "capability_desc_document",
  cap_knowledge_search: "capability_desc_knowledge_search",
  cap_presentation: "capability_desc_presentation",
  cap_report: "capability_desc_report",
  cap_skill: "capability_desc_skill",
  cap_spreadsheet: "capability_desc_spreadsheet",
  cap_team_read: "capability_desc_team_read",
  cap_team_write: "capability_desc_team_write",
  cap_visualize: "capability_desc_visualize",
  cap_web_research: "capability_desc_web_research",
};

function trustContextKey(status: AgentCatalogItem["assignment"]["status"]): string {
  if (status === "system_required") return "trust_context_included";
  if (status === "active") return "trust_context_hired";
  if (status === "personal_disabled") return "trust_context_paused";
  if (status === "access_revoked") return "trust_context_access_changed";
  return "before_you_hire";
}

function activationCopyKeys(item: AgentCatalogItem): { title: string; body: string } | null {
  const status = item.assignment.status;
  if (status === "active") {
    return {
      title: "activation_active_title",
      body: item.availability.runnable ? "activation_active_ready" : "activation_active_not_ready",
    };
  }
  if (status === "personal_disabled") {
    return { title: "activation_paused_title", body: "activation_paused_body" };
  }
  if (status === "system_required") {
    return { title: "activation_included_title", body: "activation_included_body" };
  }
  if (status === "access_revoked") {
    return { title: "activation_access_changed_title", body: "activation_access_changed_body" };
  }
  return null;
}

function semanticSafetyKey(status: string): string {
  if (status === "not_run") return "listing_semantic_not_run";
  if (status === "pass") return "listing_semantic_checked";
  return "listing_semantic_needs_review";
}

function listingSafetyKeys(
  item: AgentCatalogItem,
): { primary: string; secondary: string | null; state: string; values?: { app: string } } {
  const listing = item.listing_safety;
  // Built-in Agents have no member-written listing to check: the gateway reports "managed", which used to fall
  // through to "Listing needs attention" (116 dogfood 1.5.25, Data & Compute Analyst).
  if (listing?.effective_status === "managed") {
    // The listing was written by the product itself: say so, naming the app as the gateway reports it (the edition's
    // product name), rather than reusing the runtime-safety line.
    const app = item.source.owner_label?.trim();
    return app
      ? { primary: "listing_managed", secondary: null, state: "managed", values: { app } }
      : { primary: "listing_managed_system", secondary: null, state: "managed" };
  }
  if (!listing) {
    return {
      primary: "listing_evidence_unavailable",
      secondary: "listing_semantic_not_run",
      state: "unknown",
    };
  }
  if (listing.effective_status === "blocked" || listing.deterministic_status === "blocked") {
    return {
      primary: "listing_needs_review",
      secondary: semanticSafetyKey(listing.semantic_status),
      state: "blocked",
    };
  }
  return {
    primary: listing.deterministic_status === "pass" ? "listing_rules_checked" : "listing_needs_review",
    secondary: semanticSafetyKey(listing.semantic_status),
    state: listing.effective_status,
  };
}

function runtimeSafety(item: AgentCatalogItem): { key: string; state: string } {
  if (item.safety.status === "managed") return { key: "runtime_safety_managed", state: "managed" };
  if (item.safety.checked_subject === "legacy_row") return { key: "runtime_safety_legacy", state: "unknown" };
  if (item.safety.status === "pass") return { key: "runtime_safety_checked", state: "pass" };
  return { key: "runtime_safety_needs_review", state: item.safety.status };
}

function riskEffectKey(item: AgentCatalogItem): string {
  if (item.risk.level === "managed") return "risk_effect_managed";
  if (item.risk.reasons.includes("destructive")) return "risk_effect_destructive";
  if (item.risk.reasons.includes("external_write")) return "risk_effect_external_write";
  if (item.risk.reasons.includes("artifact_write")) return "risk_effect_artifact_write";
  if (item.risk.level === "low") return "risk_effect_low";
  if (item.risk.level === "medium") return "risk_effect_medium";
  if (item.risk.level === "high") return "risk_effect_high";
  return "risk_effect_unknown";
}

/**
 * Contract identifiers, definition version and implementation reference of one capability, folded behind a
 * "Technical details" disclosure (Agents & Reports Polish Spec: reader-facing text first; identifiers only in a
 * details place, monospace 11 with a copy button). 116 dogfood 1.5.25: `Input: dataset.request.v1` and
 * `capi_data_query_native@1` were printed on the built-in Agent's page for every reader.
 */
function CapabilityTechnicalDetails({ evidence, locale }: { evidence: AgentCapabilityEvidence; locale: string }) {
  const t = useTranslations("agents_marketplace");
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast({ kind: "success", text: t("capability_identifier_copied") });
    } catch {
      // Clipboard can be unavailable; the identifier stays selectable.
    }
  };
  const identifier = (value: string) => (
    <span className="am-capability-identifier" key={value}>
      <code>{value}</code>
      <button type="button" aria-label={t("capability_copy_identifier", { identifier: value })} onClick={() => void copy(value)}>
        <LucideGlyph icon={Copy} size={12} aria-hidden="true" />
      </button>
    </span>
  );
  const implementation = evidence.implementation_evidence.status === "reviewed"
    ? evidence.implementation_evidence.reviewed_at
      ? t("capability_implementation_reviewed_at", { date: formatEvidenceDate(evidence.implementation_evidence.reviewed_at, locale) })
      : t("capability_implementation_reviewed")
    : t(`capability_implementation_${evidence.implementation_evidence.status}` as never);
  return (
    <div className="am-capability-details">
      <button type="button" className="am-capability-details-toggle" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen((value) => !value)}>
        <LucideGlyph icon={ChevronRight} size={14} aria-hidden="true" className="am-capability-details-chevron" />
        <span>{t("capability_details")}</span>
      </button>
      <dl id={panelId} className="am-capability-details-panel" hidden={!open}>
        <div><dt>{t("capability_definition_label")}</dt><dd>{t(CAPABILITY_DEFINITION_KEYS[evidence.definition.lifecycle], { version: evidence.definition.version ?? "—" })}</dd></div>
        <div><dt>{t("capability_implementation_label")}</dt><dd>{implementation}</dd></div>
        {evidence.inputs?.length ? <div><dt>{t("capability_input")}</dt><dd>{evidence.inputs.map((input) => identifier(input.contract))}</dd></div> : null}
        {evidence.outputs?.length ? <div><dt>{t("capability_output")}</dt><dd>{evidence.outputs.map((output) => identifier(output.contract))}</dd></div> : null}
        {evidence.management ? (
          <div>
            <dt>{t("capability_management_evidence")}</dt>
            <dd>{evidence.management.implementation_ref ? identifier(evidence.management.implementation_ref) : t("capability_management_no_implementation")}</dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}

function formatEvidenceDate(value: string, locale: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
  }).format(parsed);
}

type CapabilityStateKey =
  | "capability_state_ready"
  | "capability_state_degraded"
  | "capability_state_unavailable"
  | "capability_state_shadow"
  | "capability_state_missing"
  | "capability_state_unknown";

const CAPABILITY_DEFINITION_KEYS = {
  active: "capability_definition_active",
  deprecated: "capability_definition_deprecated",
  retired: "capability_definition_retired",
  unknown: "capability_definition_unknown",
} as const;

function capabilityStateKey(capability: AgentCapabilityEvidence): CapabilityStateKey {
  if (capability.readiness.state === "ready") return "capability_state_ready";
  if (capability.readiness.state === "degraded") return "capability_state_degraded";
  if (capability.readiness.state === "unavailable") return "capability_state_unavailable";
  if (capability.implementation_evidence.status === "shadow") return "capability_state_shadow";
  if (capability.implementation_evidence.status === "missing") return "capability_state_missing";
  return "capability_state_unknown";
}

export function AgentDetail() {
  const params = useSearchParams();
  const search = params?.toString() ?? (typeof window === "undefined" ? "" : window.location.search);
  const query = new URLSearchParams(search);
  const returnView = agentManagementView(query.get("return_view"));
  const chartsReturnHref = agentChartsReturnHref(search);
  const dashboardReturnHref = agentDashboardReturnHref(search);
  const backHref = returnView ? agentManagementReturnHref(search) : chartsReturnHref ?? dashboardReturnHref ?? "/agents/marketplace/";
  const agentId = query.get("id");
  const router = useAppRouter();
  const t = useTranslations("agents_marketplace");
  const canDirectUse = useOptionalAuthSession()?.me?.capabilities?.composer?.agent_start_use === true;
  const accessRequestsEnabled = useAccessRequestsEnabled();
  const locale = useLocale();
  const capabilityLabel = useCapabilityLabel();
  const { displayName, summary } = useAgentListingText();
  const loadSequence = useRef(0);
  const activeAgentId = useRef(agentId);
  activeAgentId.current = agentId;
  const [item, setItem] = useState<AgentCatalogItem | null>(null);
  const [capabilityReport, setCapabilityReport] = useState<AgentCapabilityReport | null>(null);
  const [capabilityEvidenceState, setCapabilityEvidenceState] = useState<"loading" | "ready" | "unavailable" | "error">("loading");
  const [state, setState] = useState<"loading" | "ready" | "not_found" | "forbidden" | "error">("loading");
  const [pending, setPending] = useState(false);
  const [forkOpen, setForkOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (options: { preserve?: boolean; mutationSucceeded?: boolean } = {}) => {
    if (activeAgentId.current !== agentId) return;
    const sequence = ++loadSequence.current;
    if (!agentId) {
      setItem(null);
      setError(t("agent_not_found"));
      setState("not_found");
      return;
    }
    if (!options.preserve) {
      setItem(null);
      setForkOpen(false);
      setState("loading");
      setCapabilityReport(null);
      setCapabilityEvidenceState("loading");
    }
    const capabilityRequest = getAgentCapabilityReport(agentId, locale)
      .then((value) => ({ value, cause: null }))
      .catch((cause: unknown) => ({ value: null, cause }));
    try {
      const detail = await getAgentDetail(agentId);
      if (loadSequence.current !== sequence) return;
      setItem(detail);
      setError(null);
      setState("ready");
      const evidence = await capabilityRequest;
      if (loadSequence.current !== sequence) return;
      if (evidence.value) {
        setCapabilityReport(evidence.value);
        setCapabilityEvidenceState("ready");
      } else if (evidence.cause instanceof ApiError && evidence.cause.status === 404) {
        setCapabilityReport(null);
        setCapabilityEvidenceState("unavailable");
      } else {
        setCapabilityReport(null);
        setCapabilityEvidenceState("error");
      }
    } catch (cause) {
      if (loadSequence.current !== sequence) return;
      if (options.preserve) {
        setError(t(options.mutationSucceeded ? "saved_refresh_failed" : "load_failed"));
        setState("ready");
        return;
      }
      if (cause instanceof ApiError && cause.status === 404) {
        setError(t("agent_not_found"));
        setState("not_found");
      } else if (cause instanceof ApiError && cause.status === 403) {
        setError(t("agent_access_denied"));
        setState("forbidden");
      } else {
        setError(t("detail_load_failed"));
        setState("error");
      }
    }
  }, [agentId, locale, t]);

  useEffect(() => {
    void load();
    return () => { loadSequence.current += 1; };
  }, [load]);

  const navigate = useCallback((event: MouseEvent<HTMLAnchorElement>, path: string) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(path);
  }, [router]);

  const applyMutation = (mutation: AgentAssignmentMutation) => {
    setItem((current) => current?.agent_id === agentId ? {
      ...current,
      assignment: { ...current.assignment, ...mutation.assignment },
      availability: {
        ...current.availability,
        runnable: mutation.runnable,
        public_reason: mutation.public_reason,
      },
      actions: {
        ...current.actions,
        hire: false,
        enable: mutation.assignment.status === "personal_disabled",
        disable: mutation.assignment.status === "active",
        unhire: true,
      },
    } : current);
  };

  const mutate = async (action: "hire" | "active" | "disabled") => {
    if (!item) return;
    setPending(true);
    try {
      let mutation: AgentAssignmentMutation | null = null;
      if (action === "hire") mutation = await hireAgent(item.agent_id);
      else if (item.assignment.row_version) {
        mutation = await setAgentPersonalStatus(item.agent_id, action, item.assignment.row_version);
      }
      if (mutation) applyMutation(mutation);
      await load({ preserve: true, mutationSucceeded: true });
    } catch (cause) {
      setError(t("action_failed"));
    } finally {
      setPending(false);
    }
  };

  if (state === "loading" && !item) return <div className="am-page am-detail-page"><div className="am-detail-loading" /></div>;
  if (!item) {
    const requestId = agentId;
    if (accessRequestsEnabled && requestId && (state === "not_found" || state === "forbidden")) {
      // Missing, cross-team and forbidden Agents share one request panel, so the page never
      // tells them apart.
      return <div className="am-page am-detail-page"><AgentBreadcrumb href={backHref} label={t(returnView ? "back_to_manage" : "back_to_market")} /><AccessRequestPanel kind="team_subagent" resourceId={requestId} onBack={() => router.push(backHref)} onOpen={() => void load()} /></div>;
    }
    const title = state === "not_found"
      ? t("agent_not_found")
      : state === "forbidden"
        ? t("agent_access_denied")
        : t("detail_load_failed");
    return <div className="am-page am-detail-page"><AgentBreadcrumb href={backHref} label={t(returnView ? "back_to_manage" : "back_to_market")} /><div className="am-empty"><h1>{title}</h1><p>{error}</p>{state === "error" ? <button className="settings-button settings-button--secondary settings-button--md" onClick={() => void load()}>{t("retry")}</button> : null}</div></div>;
  }
  const name = displayName(item);
  const activationCopy = activationCopyKeys(item);
  // Why a hire cannot run for the viewer; not hired / turned off keep their own panels and buttons.
  const blockedReason = (() => {
    const view = availabilityReason(item, t("team_member"));
    return view && view.code !== "not_hired" && view.code !== "personally_disabled" ? view : null;
  })();
  const limited = limitedAvailability(item, locale);
  const listingSafety = listingSafetyKeys(item);
  const runtimeSafetyEvidence = runtimeSafety(item);
  const capabilityEvidenceById = new Map(
    capabilityReport?.capabilities.map((capability) => [capability.id, capability]) ?? [],
  );
  const capabilityEvidenceStatusKey = capabilityEvidenceState === "ready"
    ? capabilityReport?.drift.status === "review_required"
      ? "capability_evidence_review"
      : "capability_evidence_unverified"
    : capabilityEvidenceState === "loading"
      ? "capability_evidence_loading"
      : capabilityEvidenceState === "unavailable"
        ? "capability_evidence_unavailable"
        : "capability_evidence_error";

  return (
    <div className="am-page am-detail-page">
      <AgentBreadcrumb href={backHref} label={t(returnView ? "back_to_manage" : chartsReturnHref ? "charts_back" : dashboardReturnHref ? "dashboard_back" : "back_to_market")} />
      {error ? <div className="am-notice" role="alert"><span>{error}</span></div> : null}
      <header className="am-detail-hero">
        <div className="am-detail-mark" style={agentArtworkStyle(`${item.agent_id}:${name}`)} aria-hidden="true">{agentMark(name)}</div>
        <div className="am-detail-copy">
          <p className="am-detail-source">
            {item.source.kind === "system" ? t("by_bowhead") : t("by_creator", { name: item.source.owner_label ?? t("team_member") })}
            {item.source.kind === "team" ? <span> · {t("creator_listing_original")}</span> : null}
          </p>
          <h1>{name}</h1>
          <p>{summary(item)}</p>
          <div className="am-detail-actions">
            {canDirectUse && item.availability.runnable && item.assignment.status === "active" ? <button className="am-action-primary" onClick={() => handoffAgentToNewChat({ agentId: item.agent_id, label: name })}>{t("start_use")}</button> : null}
            {item.actions.hire ? <button className="am-action-primary" disabled={pending} onClick={() => void mutate("hire")}>{pending ? t("hiring") : t("hire_agent")}</button> : null}
            {item.actions.disable ? <button disabled={pending} onClick={() => void mutate("disabled")}>{t("pause_agent")}</button> : null}
            {item.actions.enable ? <button className="am-action-primary" disabled={pending} onClick={() => void mutate("active")}>{t("enable_agent")}</button> : null}
            {item.actions.fork ? <button disabled={pending} onClick={() => setForkOpen(true)}>{t("fork_agent")}</button> : null}
            {item.actions.manage && item.manager_url ? <a href={appPath(withAgentReturnView(item.manager_url, returnView ?? "created"))} onClick={(event) => navigate(event, withAgentReturnView(item.manager_url ?? "/agents/manage/", returnView ?? "created"))}>{t("manage_configuration")}</a> : null}
            {item.assignment.status === "system_required" ? <span className="am-included-note"><CharIcon ch="✓" /> {t("already_included")}</span> : null}
          </div>
          {blockedReason ? (
            <section className="am-activation-panel" data-state="unavailable" aria-label={t("activation_title")}>
              <span aria-hidden="true">i</span>
              <div>
                <ReasonFull view={blockedReason} />
              </div>
              {blockedReason.action === "retry" ? (
                <button type="button" disabled={pending} onClick={() => void load({ preserve: true })}>{t("reason_retry")}</button>
              ) : HIRED_STATUSES.has(item.assignment.status) ? (
                // Only a hire is on that list; with nothing to do the note line alone is the answer (brief §4.4).
                <a href={appPath("/agents/manage/")} onClick={(event) => navigate(event, "/agents/manage/")}>{t("manage_hired_agent")}</a>
              ) : null}
            </section>
          ) : activationCopy ? (
            <section className="am-activation-panel" aria-label={t("activation_title")}>
              <span aria-hidden="true"><CharIcon ch="✓" aria-hidden /></span>
              <div>
                <strong>{t(activationCopy.title as never)}</strong>
                <p>{t(activationCopy.body as never)}</p>
              </div>
              <a href={appPath("/agents/manage/")} onClick={(event) => navigate(event, "/agents/manage/")}>{t("manage_hired_agent")}</a>
            </section>
          ) : null}
        </div>
      </header>

      <div className="am-detail-layout">
        <section className="am-detail-main">
          <div className="am-detail-section-heading"><span>{t("what_it_can_do")}</span><h2>{t("capabilities_explained")}</h2></div>
          <section className="am-capability-model" aria-label={t("capability_model_label")}>
            {(["agent", "skill", "tool"] as const).map((layer) => (
              <article key={layer}>
                <div>
                  <h3>{t(`capability_model_${layer}_title`)}</h3>
                  <p>{t(`capability_model_${layer}_body`)}</p>
                </div>
              </article>
            ))}
          </section>
          <section
            className="am-capability-evidence"
            data-state={capabilityEvidenceState === "ready" ? capabilityReport?.drift.status : capabilityEvidenceState}
            aria-live="polite"
          >
            <div>
              <span>{t("capability_evidence_eyebrow")}</span>
              <strong>{t(`${capabilityEvidenceStatusKey}_title` as never)}</strong>
              <p>{t(`${capabilityEvidenceStatusKey}_body` as never)}</p>
            </div>
            {capabilityReport ? (
              <dl>
                <div><dt>{t("capability_evidence_tools")}</dt><dd>{capabilityReport.legacy_direct_dependencies.tool_count}</dd></div>
                <div><dt>{t("capability_evidence_skills")}</dt><dd>{capabilityReport.legacy_direct_dependencies.skill_count}</dd></div>
              </dl>
            ) : null}
          </section>
          <div className="am-capability-list">
            {item.capabilities.map((capability) => {
              const label = capabilityLabel(capability);
              const descriptionKey = CAPABILITY_DESCRIPTION_KEYS[capability.id];
              const evidence = capabilityEvidenceById.get(capability.id);
              return (
                <article key={capability.id}>
                  <div>
                    <h3>{evidence?.name ?? label}</h3>
                    <p>{evidence?.description ?? (descriptionKey ? t(descriptionKey as never) : t("capability_default", { capability: label }))}</p>
                    {evidence ? (
                      <>
                        <div className="am-capability-state" data-state={evidence.readiness.state}>
                          <strong>{t(`${capabilityStateKey(evidence)}_title` as never)}</strong>
                          <span>{t(`${capabilityStateKey(evidence)}_body` as never)}</span>
                        </div>
                        <CapabilityTechnicalDetails evidence={evidence} locale={locale} />
                        {evidence.management?.missing_dependencies.length || evidence.management?.unresolved_dependencies.length ? (
                          <div className="am-capability-management">
                            {evidence.management.missing_dependencies.length ? (
                              <div>
                                <span>{t("capability_management_missing", { count: evidence.management.missing_dependencies.length })}</span>
                                <div className="am-capability-repair-links">
                                  {evidence.management.missing_dependencies.map((dependency) => dependency.repair_href ? (
                                    <a
                                      key={`${dependency.kind}:${dependency.ref}`}
                                      href={appPath(dependency.repair_href)}
                                      onClick={(event) => navigate(event, dependency.repair_href ?? "/agents/marketplace/")}
                                    >
                                      {t(dependency.kind === "tool" ? "capability_repair_tool" : "capability_repair_skill", { ref: dependency.ref })}
                                    </a>
                                  ) : (
                                    <code key={`${dependency.kind}:${dependency.ref}`}>{dependency.ref}</code>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {evidence.management.unresolved_dependencies.length ? (
                              <small>{t("capability_management_unresolved", { count: evidence.management.unresolved_dependencies.length })}</small>
                            ) : null}
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
          {item.resources ? <AgentResourceList resources={item.resources} /> : null}
          {item.quality_evidence ? (
            <section className="am-quality-evidence" aria-labelledby="agent-quality-title">
              <div className="am-detail-section-heading"><span>{t("quality_evidence_eyebrow")}</span><h2 id="agent-quality-title">{t("quality_evidence_title")}</h2></div>
              <p>{t("quality_evidence_body")}</p>
              <div className="am-quality-evidence-grid">
                {item.quality_evidence.suites.length ? item.quality_evidence.suites.map((suite) => (
                  <article key={`${suite.suite_kind}:${suite.name}`} data-state={suite.state}>
                    <div><span>{suite.suite_kind === "platform_contract" ? t("eval_platform_suite") : t("eval_owner_suite")}</span><strong>{suite.suite_kind === "platform_contract" ? t("eval_platform_suite_name") : suite.name}</strong></div>
                    <em>{t(`eval_state_${suite.state}` as never)}</em>
                    <dl><div><dt>{t("eval_cases")}</dt><dd>{suite.sample_size}</dd></div><div><dt>{t("eval_passed")}</dt><dd>{suite.passed_cases}</dd></div><div><dt>{t("eval_flaky")}</dt><dd>{suite.flaky_cases}</dd></div></dl>
                    {suite.evaluated_at ? <small>{t("eval_last_run", { date: formatEvidenceDate(suite.evaluated_at, locale) })}</small> : null}
                  </article>
                )) : <div className="am-quality-empty"><strong>{t("quality_evidence_missing")}</strong><p>{t("quality_evidence_missing_body")}</p></div>}
              </div>
              <small>{t("quality_evidence_boundary")}</small>
            </section>
          ) : null}
        </section>
        <aside className="am-trust-panel">
          <span>{t(trustContextKey(item.assignment.status) as never)}</span>
          <h2>{t("trust_and_access")}</h2>
          <dl>
            <div>
              <dt>{t("listing_check")}</dt>
              <dd data-state={listingSafety.state}><strong>{t(listingSafety.primary as never, listingSafety.values as never)}</strong>{listingSafety.secondary ? <small>{t(listingSafety.secondary as never)}</small> : null}</dd>
            </div>
            <div>
              <dt>{t("runtime_safety")}</dt>
              <dd data-state={runtimeSafetyEvidence.state}><strong>{t(runtimeSafetyEvidence.key as never)}</strong></dd>
            </div>
            <div>
              <dt>{t("risk")}</dt>
              <dd><strong>{t(`risk_${item.risk.level}` as never)}</strong><small>{t(riskEffectKey(item) as never)}</small></dd>
            </div>
            {item.quality_evidence ? <div><dt>{t("quality")}</dt><dd data-state={item.quality_evidence.status}><strong>{t(`eval_state_${item.quality_evidence.status}` as never)}</strong></dd></div> : null}
            <div><dt>{t("access")}</dt><dd>{item.source.kind === "system" ? t("system_managed") : t("follows_your_access")}</dd></div>
            <div>
              <dt>{t("execution")}</dt>
              <dd>
                <strong>{limited ? `${t("limited_chip")} · ${reasonText(t, limited.shortKey, limited.values)}` : item.availability.runnable ? t("ready") : blockedReason ? reasonText(t, blockedReason.shortKey, blockedReason.values) : item.assignment.status === "not_hired" ? t("hire_to_use") : t("not_ready")}</strong>
                <small>{t("execution_evidence_note")}</small>
              </dd>
            </div>
            {item.assignment.source ? <div><dt>{t("assignment_source")}</dt><dd>{t((item.relationship === "owned" && item.assignment.source === "user_hired" ? "source_owner_added" : item.assignment.source === "system" ? "source_assignment_system" : `source_${item.assignment.source}`) as never)}</dd></div> : null}
          </dl>
          <p>{t("permission_note")}</p>
        </aside>
      </div>
      {forkOpen ? (
        <AgentForkDialog
          agentId={item.agent_id}
          onClose={() => setForkOpen(false)}
          onComplete={(result) => {
            setForkOpen(false);
            router.push(result.manager_url);
          }}
        />
      ) : null}
    </div>
  );
}
